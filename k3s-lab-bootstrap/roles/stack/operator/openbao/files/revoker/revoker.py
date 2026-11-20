#!/usr/bin/env python3
"""The standing token revoker for OpenBao.

Every scan reads the executor's audit record from a read-only mount, lists the
tokens of the server by accessor, and revokes the executor tokens that have
outlived their change. The script uses the standard library only.

The record format and the chain check come from dmf-init at revision ade3967
(src/dmf_init/audit/segment.py and verify.py). They are ported without a change of
meaning, and `read_record` reads the segments once: it checks the chain and collects
what a scan needs (open intents, recorded accessors with their requests, closed
requests) from the same lines, so a scan never mixes two views of a record that the
executor appends to. The restore rule (a `restore` entry links to the entry before it),
the anchor rules and the segment order are all part of the chain check.

Rules of a scan:

- A token is a root token when its policies include `root`. It is an executor
  token when its policies include `root` or any policy named in the catalogue
  file.
- A recorded accessor (named by a `minted` entry) is revoked when its request has
  a `closed` entry. A recorded token that holds `root` is also revoked when it is
  older than the grace period: the executor revokes the generated root token before
  its first write, so one that is still live was left by a crash. A recorded scoped
  token whose request is open is left alone: its use limit and its lifetime of 15
  minutes at most bound it.
- An unrecorded executor token is revoked when no intent is open, or when it is
  older than the grace period.
- When the record cannot be read, or its chain check fails, no accessor counts as
  recorded. The scan revokes only executor tokens older than the grace period,
  whether an intent is open or not, and raises the alert.

The age of a token is the server's `creation_time` against the server's own time. The
script reads that time in the `Date` header of the answer to the listing, never on this
pod's clock. A scan whose listing carries no usable `Date` header is incomplete and revokes
nothing. An executor token whose `creation_time` is not a number counts as old, and the
scan logs it.

A scan is complete only when every step of it succeeded: the login, the listing, every
lookup and every revoke, each with a well-formed answer. A failed or malformed step is
logged and the scan goes on to the next token, but the scan then counts as incomplete:
it does not move the last-scan gauge, and the failures gauge shows how many steps failed.
One answer is not a failure: HTTP 400 whose error list is exactly one message, "invalid
accessor" on a lookup or "token not found" on a revoke. A token that expires between the
listing and the lookup is gone in this way. OpenBao answers 400 to any invalid request, so
every other 400 is a failure. A log line never
holds a token. The script logs in with the projected ServiceAccount token on every scan
and keeps the OpenBao token in memory only.
"""

from __future__ import annotations

import email.utils
import hashlib
import http.server
import json
import os
import re
import ssl
import sys
import threading
import time
import urllib.error
import urllib.request
from enum import Enum
from pathlib import Path

GENESIS_PREV = "0" * 64
ANCHOR_NAME = "anchor.json"
SEGMENT_RE = re.compile(r"segment-(\d{8})\.jsonl")
ROOT_POLICY = "root"


# --- the record: ported from dmf-init ade3967, src/dmf_init/audit/segment.py -------


def segments(directory: Path) -> list[tuple[int, Path]]:
    """Return (number, path) for every segment file, oldest first."""
    found = []
    if directory.is_dir():
        for path in directory.iterdir():
            match = SEGMENT_RE.fullmatch(path.name)
            if match:
                found.append((int(match.group(1)), path))
    return sorted(found)


def canonical(obj: object) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def entry_hash(entry: dict) -> str:
    """SHA-256 over the canonical JSON of the entry without its `hash` field."""
    body = {key: value for key, value in entry.items() if key != "hash"}
    return hashlib.sha256(canonical(body)).hexdigest()


def read_lines(path: Path) -> list[str]:
    return path.read_bytes().decode("utf-8", errors="replace").splitlines()


def read_anchor(directory: Path) -> dict | None:
    try:
        anchor = json.loads((directory / ANCHOR_NAME).read_text())
    except (OSError, ValueError):
        return None
    if isinstance(anchor, dict) and isinstance(anchor.get("seq"), int):
        return anchor
    return None


# --- the chain check: ported from dmf-init ade3967, src/dmf_init/audit/verify.py ---


class Status(Enum):
    OK = "ok"
    TRIMMED = "trimmed"  # old entries were removed by retention; the rest is intact
    GAP = "gap"  # entries or whole segments are missing
    TAMPER = "tamper"  # an entry changed, or the chain links are broken


class Record:
    """What a scan knows about the record, or that it knows nothing."""

    def __init__(self, status: Status | None, reason: str = ""):
        self.status = status
        self.readable = status in (Status.OK, Status.TRIMMED)
        self.reason = reason if self.readable or status is None else f"chain check {status.value}: {reason}"
        self.intents: set = set()  # request ids with an `intent` entry
        self.closed: set = set()  # request ids with a `closed` entry
        self.accessors: dict = {}  # accessor of a `minted` entry -> its request id

    @property
    def open_count(self) -> int:
        return len(self.intents - self.closed)

    def note(self, entry: dict) -> None:
        event, request = entry.get("event"), entry.get("request_id")
        if event == "intent":
            self.intents.add(request)
        elif event == "closed":
            self.closed.add(request)
        elif event == "minted":
            for accessor in entry.get("accessors", []):
                self.accessors[accessor] = request


def read_record(directory: Path) -> Record:
    """Read the record read-only, once. Any failure makes it unreadable."""
    try:
        return _read_record(directory)
    except (OSError, ValueError, TypeError) as error:
        return Record(None, f"the record cannot be read: {type(error).__name__}")


def _read_record(directory: Path) -> Record:
    if not directory.is_dir():
        return Record(None, "the record directory is missing")
    found = segments(directory)
    anchor = read_anchor(directory)
    if not found and (directory / ANCHOR_NAME).exists():
        # Trimming never removes the newest segment, so this means the record was deleted.
        return Record(Status.GAP, "every segment is missing")
    status = Status.OK
    expected: int | None = None
    prev = GENESIS_PREV
    overlap = False
    anchor_found = False
    record = Record(Status.OK)

    for _, path in found:
        for line in read_lines(path):
            try:
                entry = json.loads(line)
                seq, claimed_hash, claimed_prev = entry["seq"], entry["hash"], entry["prev"]
            except (ValueError, KeyError, TypeError):
                return Record(Status.TAMPER, f"{path.name}: unreadable")
            if entry_hash(entry) != claimed_hash:
                return Record(Status.TAMPER, f"seq {seq}: hash does not match the entry")
            if anchor and seq == anchor["seq"] and claimed_hash != anchor.get("hash"):
                return Record(Status.TAMPER, f"seq {seq}: entry does not match the anchor")
            if anchor and seq == anchor["seq"]:
                anchor_found = True
            if expected is None:
                overlap = bool(anchor) and seq <= anchor["seq"]
                if seq == 1:
                    pass
                elif overlap:
                    # A trim was interrupted after it wrote the anchor.
                    status = Status.TRIMMED
                    prev = claimed_prev
                elif anchor and anchor["seq"] == seq - 1 and anchor.get("hash") == claimed_prev:
                    status = Status.TRIMMED
                    prev = claimed_prev
                else:
                    return Record(Status.GAP, "record does not start at seq 1")
            elif seq != expected:
                return Record(Status.GAP, f"expected seq {expected}, found {seq}")
            if claimed_prev != prev:
                return Record(Status.TAMPER, f"seq {seq}: prev does not match entry before")
            prev = claimed_hash
            expected = seq + 1
            record.note(entry)
    if overlap and not anchor_found:
        return Record(Status.TAMPER, "the anchor entry is missing")
    record.status = status
    return record


# --- OpenBao over HTTP --------------------------------------------------------------


class BaoError(Exception):
    """A request that OpenBao refused or that failed on the way. Never holds a token."""

    def __init__(self, message: str, status: int | None = None, errors: list | None = None):
        super().__init__(message)
        self.status = status
        self.errors = errors


# The one message OpenBao puts in the 400 answer that means "no such token", per endpoint:
# lookup-accessor says "invalid accessor" for an accessor that no token has, and
# revoke-accessor says "token not found" when the accessor is known but its token is gone.
GONE_LOOKUP = "invalid accessor"
GONE_REVOKE = "token not found"


def token_is_gone(error: BaoError, message: str) -> bool:
    """True only for a 400 whose error list is exactly the one message `message`.

    OpenBao uses 400 for any invalid request, so the status alone proves nothing. A 400 with
    another message, with more than one message, or with a body that is not an OpenBao error
    list is a failure.
    """
    return error.status == 400 and error.errors == [message]


def http_date(value: str | None) -> float | None:
    """The time in a `Date` header as epoch seconds, or None when it is missing or unreadable."""
    try:
        return email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError, IndexError):
        return None


class Bao:
    def __init__(self, address: str, ca_file: str | None, timeout: float = 10.0):
        self.address = address.rstrip("/")
        self.timeout = timeout
        self.context = ssl.create_default_context(cafile=ca_file) if self.address.startswith("https") else None
        self.token = ""
        self.server_time = None

    def request(self, method: str, path: str, body: dict | None = None, token: bool = True) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(f"{self.address}/v1/{path}", data=data, method=method)
        request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("X-Vault-Token", self.token)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout, context=self.context) as response:
                text = response.read().decode() or "{}"
                self.server_time = http_date(response.headers.get("Date"))
        except urllib.error.HTTPError as error:
            errors = None
            try:
                body = json.loads(error.read().decode())
                if isinstance(body, dict) and isinstance(body.get("errors"), list):
                    errors = body["errors"]
            except (OSError, ValueError):
                pass
            error.close()
            raise BaoError(f"{method} {path}: HTTP {error.code}", error.code, errors) from None
        except (urllib.error.URLError, OSError, ValueError) as error:
            raise BaoError(f"{method} {path}: {type(error).__name__}") from None
        try:
            parsed = json.loads(text)
        except ValueError:
            raise BaoError(f"{method} {path}: the answer is not JSON") from None
        if not isinstance(parsed, dict):
            raise BaoError(f"{method} {path}: the answer is not an object")
        return parsed

    def login(self, role: str, jwt: str) -> None:
        answer = self.request("POST", "auth/kubernetes/login", {"role": role, "jwt": jwt}, token=False)
        token = (answer.get("auth") or {}).get("client_token")
        if not isinstance(token, str) or not token:
            raise BaoError("login: the answer has no token")
        self.token = token

    def accessors(self) -> list[str]:
        answer = self.request("LIST", "auth/token/accessors")
        keys = (answer.get("data") or {}).get("keys")
        if not isinstance(keys, list) or not all(isinstance(key, str) and key for key in keys):
            raise BaoError("list accessors: the answer is not a list of accessors")
        return keys

    def lookup(self, accessor: str) -> dict:
        data = self.request("POST", "auth/token/lookup-accessor", {"accessor": accessor}).get("data")
        if not isinstance(data, dict):
            raise BaoError("lookup: the answer has no data")
        return data

    def revoke(self, accessor: str) -> None:
        self.request("POST", "auth/token/revoke-accessor", {"accessor": accessor})


# --- one scan -----------------------------------------------------------------------


class Config:
    def __init__(self, env: dict | None = None):
        env = os.environ if env is None else env
        self.address = env.get("REVOKER_BAO_ADDR", "https://openbao.openbao.svc.cluster.local:8200")
        self.ca_file = env.get("REVOKER_CA_FILE") or None
        self.role = env.get("REVOKER_ROLE", "token-revoker")
        self.jwt_file = env.get("REVOKER_JWT_FILE", "/var/run/secrets/openbao/token")
        self.record_dir = Path(env.get("REVOKER_RECORD_DIR", "/record"))
        self.catalogue_file = Path(env.get("REVOKER_CATALOGUE_FILE", "/config/catalogue-policies.json"))
        self.grace = int(env.get("REVOKER_GRACE_SECONDS", "300"))
        self.interval = int(env.get("REVOKER_SCAN_INTERVAL_SECONDS", "60"))
        self.metrics_port = int(env.get("REVOKER_METRICS_PORT", "9102"))


def log(message: str) -> None:
    print(message, flush=True)


def catalogue_policies(path: Path) -> set:
    names = json.loads(path.read_text())
    if not isinstance(names, list) or not all(isinstance(n, str) and n for n in names):
        raise ValueError("the catalogue policy file is not a list of names")
    return set(names)


def scan(bao: Bao, config: Config, policies: set) -> dict:
    """Run one scan with a logged-in client. Return counters for the metrics."""
    record = read_record(config.record_dir)
    result = {"readable": record.readable, "revoked": 0, "failed": 0}
    if not record.readable:
        log(f"ALERT revoker-record-unreadable: {record.reason}; no accessor counts as recorded")
    try:
        accessors = bao.accessors()
    except BaoError as error:
        log(f"list accessors failed: {error}")
        result["failed"] += 1
        return result
    now = bao.server_time
    if now is None:
        log("list accessors: the answer has no usable Date header, so the age of a token is unknown")
        result["failed"] += 1
        return result
    for accessor in accessors:
        try:
            data = bao.lookup(accessor)
        except BaoError as error:
            if token_is_gone(error, GONE_LOOKUP):
                log(f"lookup {accessor}: the token is gone")
            else:
                log(f"lookup {accessor} failed: {error}")
                result["failed"] += 1
            continue
        held = data.get("policies")
        created = data.get("creation_time")
        if not isinstance(held, list) or not all(isinstance(p, str) for p in held):
            log(f"lookup {accessor} has no policies list")
            result["failed"] += 1
            continue
        executor = ROOT_POLICY in held or any(p in policies for p in held)
        recorded = record.readable and accessor in record.accessors
        if recorded and record.accessors[accessor] in record.closed:
            reason = "its request is closed"
        elif not executor:
            continue
        else:
            number = isinstance(created, (int, float)) and not isinstance(created, bool)
            if not number:
                log(f"lookup {accessor} has no readable creation_time, counts as old")
            old = not number or now - created > config.grace
            if recorded:
                if not (ROOT_POLICY in held and old):
                    continue
                reason = "it is a recorded root token older than the grace period"
            elif not record.readable:
                if not old:
                    continue
                reason = "it is older than the grace period and the record is unreadable"
            elif record.open_count == 0:
                reason = "it is unrecorded and no intent is open"
            elif old:
                reason = "it is unrecorded and older than the grace period"
            else:
                continue
        try:
            bao.revoke(accessor)
        except BaoError as error:
            if token_is_gone(error, GONE_REVOKE):
                log(f"revoke {accessor}: the token is gone")
            else:
                log(f"revoke {accessor} failed: {error}")
                result["failed"] += 1
            continue
        log(f"revoked {accessor}: {reason}")
        result["revoked"] += 1
    return result


def run_once(config: Config) -> dict:
    """Log in, scan, and report. The OpenBao token stays in this function's memory."""
    bao = Bao(config.address, config.ca_file)
    try:
        policies = catalogue_policies(config.catalogue_file)
        bao.login(config.role, Path(config.jwt_file).read_text().strip())
    except BaoError as error:
        log(f"scan skipped: {error}")
        return {"revoked": 0, "failed": 1, "ran": False}
    except (OSError, ValueError) as error:
        log(f"scan skipped: {type(error).__name__}")
        return {"revoked": 0, "failed": 1, "ran": False}
    result = scan(bao, config, policies)
    result["ran"] = result["failed"] == 0
    return result


# --- metrics ------------------------------------------------------------------------


class Metrics:
    def __init__(self):
        self.lock = threading.Lock()
        self.complete_at = None
        self.readable = None
        self.failures = None
        self.revoked_total = 0

    def update(self, result: dict, now: float) -> None:
        """The failures of the latest attempt show at once. Only a complete scan moves the
        last-scan time, and only an attempt that reached the record sets its gauge."""
        with self.lock:
            self.failures = result["failed"]
            if "readable" in result:
                self.readable = result["readable"]
            if result["ran"]:
                self.complete_at = now
            self.revoked_total += result["revoked"]

    def render(self) -> str:
        with self.lock:
            lines = [
                "# TYPE dmf_openbao_revoker_revoked_total counter",
                f"dmf_openbao_revoker_revoked_total {self.revoked_total}",
            ]
            if self.readable is not None:
                lines += [
                    "# TYPE dmf_openbao_revoker_record_readable gauge",
                    f"dmf_openbao_revoker_record_readable {1 if self.readable else 0}",
                ]
            if self.complete_at is not None:
                lines += [
                    "# TYPE dmf_openbao_revoker_last_scan_timestamp_seconds gauge",
                    f"dmf_openbao_revoker_last_scan_timestamp_seconds {int(self.complete_at)}",
                ]
            if self.failures is not None:
                lines += [
                    "# TYPE dmf_openbao_revoker_failures gauge",
                    f"dmf_openbao_revoker_failures {self.failures}",
                ]
            return "\n".join(lines) + "\n"


def serve_metrics(metrics: Metrics, port: int) -> http.server.HTTPServer:
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = metrics.render().encode()
            self.send_response(200 if self.path == "/metrics" else 404)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.send_header("Content-Length", str(len(body) if self.path == "/metrics" else 0))
            self.end_headers()
            if self.path == "/metrics":
                self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> int:
    config = Config()
    metrics = Metrics()
    serve_metrics(metrics, config.metrics_port)
    while True:
        started = time.time()
        metrics.update(run_once(config), started)
        time.sleep(max(config.interval - (time.time() - started), 1))


if __name__ == "__main__":
    sys.exit(main())
