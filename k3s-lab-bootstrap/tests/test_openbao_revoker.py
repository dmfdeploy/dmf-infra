"""The standing token revoker, run against the fake bao.

The script talks to OpenBao over HTTP, as it does in the cluster. A small HTTP server in this
file turns each request into one call of the fake bao (tests/fixtures/fake-bao/bao) and puts the
answer in the shape of the HTTP API. The fake keeps the tokens, their creation times and their
policies; FAKE_NOW moves its clock and the script's clock together.

The record fixtures use the field names of dmf-init's audit schema (revision ade3967):
`intent` and `closed` entries carry request_id, `minted` carries request_id and two accessors.

Set REVOKER_SCRIPT to run the tests against another copy of the script, which is how the
mutation runs in the work report work.
"""
import contextlib
import email.utils
import hashlib
import http.server
import importlib.util
import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OPENBAO = ROOT / "roles/stack/operator/openbao"
SCRIPT = Path(os.environ.get("REVOKER_SCRIPT", OPENBAO / "files/revoker/revoker.py"))
FAKE = ROOT / "tests/fixtures/fake-bao/bao"
ADMIN = "fake-root-genroot"
NOW = 1800000000
GRACE = 300
DEFAULTS = yaml.safe_load((OPENBAO / "defaults/main.yml").read_text())
CATALOGUE = yaml.safe_load((OPENBAO / "files/catalogue.yml").read_text())
CATALOGUE_POLICY = CATALOGUE["entries"][0]["policy_name"]
REVOKER_POLICY = DEFAULTS["openbao_revoker_policy_name"]
ROLE = DEFAULTS["openbao_revoker_k8s_role"]


def load_script():
    spec = importlib.util.spec_from_file_location("revoker_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


revoker = load_script()


def http_date(epoch):
    return email.utils.formatdate(epoch, usegmt=True)


def canonical(obj):
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def seal(entry, seq, prev):
    """The entry as dmf-init stores it: seq, time, prev, then the hash over the rest."""
    body = {**entry, "seq": seq, "time": "2026-11-19T10:00:00Z", "prev": prev}
    return {**body, "hash": hashlib.sha256(canonical(body)).hexdigest()}


def write_chain(directory, events, segment="segment-00000001.jsonl"):
    """Write a record of sealed entries, starting with the genesis entry."""
    directory.mkdir(parents=True, exist_ok=True)
    entries, prev = [], "0" * 64
    for seq, event in enumerate([{"event": "genesis"}, *events], start=1):
        entries.append(seal(event, seq, prev))
        prev = entries[-1]["hash"]
    (directory / segment).write_bytes(b"".join(canonical(e) + b"\n" for e in entries))
    return entries


def intent(request):
    return {"event": "intent", "request_id": request, "actor": "approver", "role": "operator",
            "session": "s1", "catalogue_entry": "ops-admin-password-rotation"}


def minted(request, *accessors):
    return {"event": "minted", "request_id": request, "session": "s1", "accessors": list(accessors)}


def closed(request, outcome="ok"):
    return {"event": "closed", "request_id": request, "outcome": outcome}


class Bridge(http.server.ThreadingHTTPServer):
    """HTTP in front of the fake bao. A test sets the failures it wants."""

    daemon_threads = True

    def __init__(self, state):
        super().__init__(("127.0.0.1", 0), self.Handler)
        self.state = state
        self.fail_lookup, self.fail_revoke, self.hide_policies = set(), set(), set()
        self.gone, self.data_list = set(), set()
        # accessor -> the raw body of a 400 answer; the key is ("lookup" | "revoke", accessor)
        self.bad_request = {}
        self.fail_list = False
        self.list_keys = None
        self.date_header = http_date(NOW)  # the Date header of every answer; None sends none
        self.calls = []

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server_address[1]}"

    def bao(self, args, token, stdin=""):
        env = {**os.environ, "FAKE_STATE": str(self.state), "FAKE_POD": "openbao-0", "FAKE_NOW": str(NOW)}
        env.pop("BAO_TOKEN", None)
        if token:
            env["BAO_TOKEN"] = token
        return subprocess.run([str(FAKE), *args], input=stdin, capture_output=True, text=True, env=env)

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def send_response(self, code, message=None):
            self.send_response_only(code, message)
            if self.server.date_header is not None:
                self.send_header("Date", self.server.date_header)

        def reply(self, code, body=None):
            data = json.dumps(body if body is not None else {"errors": ["failed"]}).encode()
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def reply_raw(self, code, data):
            self.send_response(code)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def handle_any(self):
            server = self.server
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            token = self.headers.get("X-Vault-Token")
            path = self.path.removeprefix("/v1/")
            server.calls.append((self.command, path, body))
            if path == "auth/kubernetes/login":
                result = server.bao(["write", "-format=json", path, f"role={body['role']}", "jwt=-"], None, body["jwt"])
                return self.reply(200, json.loads(result.stdout)) if result.returncode == 0 else self.reply(403)
            if path == "auth/token/accessors" and self.command == "LIST":
                if server.fail_list:
                    return self.reply(500)
                result = server.bao(["list", "-format=json", path], token)
                if result.returncode != 0:
                    return self.reply(403)
                keys = json.loads(result.stdout)
                if server.list_keys is not None:
                    keys = server.list_keys(keys)
                return self.reply(200, {"data": {"keys": keys}}) if keys else self.reply(404)
            accessor = body.get("accessor", "")
            if path == "auth/token/lookup-accessor":
                if accessor in server.fail_lookup:
                    return self.reply(500)
                if ("lookup", accessor) in server.bad_request:
                    return self.reply_raw(400, server.bad_request[("lookup", accessor)])
                if accessor in server.gone:
                    return self.reply(400, {"errors": ["1 error occurred:\n\t* invalid accessor\n\n"]})
                if accessor in server.data_list:
                    return self.reply(200, {"data": []})
                result = server.bao(["write", "-format=json", path, f"accessor={accessor}"], token)
                if result.returncode != 0:
                    return self.reply(400, {"errors": ["1 error occurred:\n\t* invalid accessor\n\n"]})
                answer = json.loads(result.stdout)
                if accessor in server.hide_policies:
                    answer["data"].pop("policies")
                return self.reply(200, answer)
            if path == "auth/token/revoke-accessor":
                if accessor in server.fail_revoke:
                    return self.reply(500)
                if ("revoke", accessor) in server.bad_request:
                    return self.reply_raw(400, server.bad_request[("revoke", accessor)])
                result = server.bao(["write", path, f"accessor={accessor}"], token)
                return self.reply(204 if result.returncode == 0 else 403, {})
            return self.reply(404)

        do_POST = do_LIST = do_GET = handle_any


class RevokerCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="revoker-test-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.dir)]))
        self.state = self.dir / "state"
        self.record = self.dir / "record"
        self.bridge = Bridge(self.state)
        threading.Thread(target=self.bridge.serve_forever, daemon=True).start()
        self.addCleanup(self.bridge.server_close)
        self.addCleanup(self.bridge.shutdown)
        (self.dir / "jwt").write_text("fake-sa:openbao:" + DEFAULTS["openbao_revoker_service_account"])
        (self.dir / "catalogue.json").write_text(json.dumps([e["policy_name"] for e in CATALOGUE["entries"]]))
        result = self.bridge.bao(
            ["write", f"auth/kubernetes/role/{ROLE}",
             f"bound_service_account_names={DEFAULTS['openbao_revoker_service_account']}",
             f"bound_service_account_namespaces={DEFAULTS['openbao_namespace']}",
             f"token_policies={REVOKER_POLICY}", "token_ttl=2m"], ADMIN)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.tokens = {}

    def token(self, accessor, policies, age):
        """Seed a live token of the given age in seconds on the fake's clock."""
        path = self.state / "tokens"
        path.mkdir(parents=True, exist_ok=True)
        record = {"parent": "", "orphan": True, "policies": policies, "ttl": 0, "explicit_max_ttl": 0,
                  "renewable": False, "created": NOW - age, "uses_left": None, "use_limit": 0,
                  "accessor": accessor}
        (path / f"{accessor}-token.json").write_text(json.dumps(record))
        self.tokens[accessor] = policies
        return accessor

    def config(self, **overrides):
        env = {"REVOKER_BAO_ADDR": self.bridge.url, "REVOKER_ROLE": ROLE, "REVOKER_JWT_FILE": str(self.dir / "jwt"),
               "REVOKER_RECORD_DIR": str(self.record), "REVOKER_CATALOGUE_FILE": str(self.dir / "catalogue.json"),
               "REVOKER_GRACE_SECONDS": str(GRACE), **overrides}
        return revoker.Config(env)

    def scan(self):
        """Run one scan. Return its log text."""
        lines = []
        original = revoker.log
        revoker.log = lines.append
        try:
            self.result = revoker.run_once(self.config())
        finally:
            revoker.log = original
        self.log = "\n".join(lines)
        return self.log

    def live(self):
        result = self.bridge.bao(["list", "-format=json", "auth/token/accessors"], ADMIN)
        return set(json.loads(result.stdout))

    def assertRevoked(self, *accessors):
        for accessor in accessors:
            self.assertNotIn(accessor, self.live(), f"{accessor} should be revoked")

    def assertKept(self, *accessors):
        for accessor in accessors:
            self.assertIn(accessor, self.live(), f"{accessor} should be kept")


class RecordedTokens(RevokerCase):
    def test_a_token_of_a_closed_request_is_revoked_at_once(self):
        # Catches a revoker that waits out the grace period for a closed request, or one that
        # reads only `intent` entries and so never learns that a request closed.
        write_chain(self.record, [intent("r1"), minted("r1", "a-root", "a-scoped"), closed("r1")])
        self.token("a-root", ["root"], 10)
        self.token("a-scoped", [CATALOGUE_POLICY], 10)
        self.scan()
        self.assertRevoked("a-root", "a-scoped")

    def test_the_closed_request_revokes_only_its_own_tokens(self):
        # Catches a revoker that treats any `closed` entry as closing every request.
        write_chain(self.record, [intent("r1"), minted("r1", "a-root", "a-scoped"), closed("r1"),
                                  intent("r2"), minted("r2", "b-root", "b-scoped")])
        for accessor in ("a-root", "b-root"):
            self.token(accessor, ["root"], 10)
        for accessor in ("a-scoped", "b-scoped"):
            self.token(accessor, [CATALOGUE_POLICY], 10)
        self.scan()
        self.assertRevoked("a-root", "a-scoped")
        self.assertKept("b-root", "b-scoped")

    def test_a_recorded_scoped_token_of_an_open_request_is_kept_past_the_grace_period(self):
        # Catches a revoker that applies the grace period to a recorded scoped token: a valid
        # approval that runs longer than 5 minutes would lose its token in the middle of the
        # change. The use limit and the lifetime of 15 minutes at most bound that token.
        write_chain(self.record, [intent("r1"), minted("r1", "a-root", "a-scoped")])
        for age in (GRACE + 1, 600):
            self.token("a-scoped", [CATALOGUE_POLICY], age)
            self.scan()
            self.assertKept("a-scoped")
            self.assertNotIn("revoked a-scoped", self.log)

    def test_a_recorded_root_token_left_by_a_failed_cleanup_is_revoked_after_the_grace_period(self):
        # Catches a revoker that revokes a recorded token only when its request closes: the
        # executor's cleanup failed, so the request never closed and the root token stays live.
        # The age must be MORE than 5 minutes.
        write_chain(self.record, [intent("r1"), minted("r1", "a-root", "a-scoped")])
        self.token("a-root", ["root"], GRACE)
        self.scan()
        self.assertKept("a-root")
        self.token("a-root", ["root"], GRACE + 1)
        self.scan()
        self.assertRevoked("a-root")

    def test_a_recorded_token_of_an_open_request_is_kept_whatever_the_open_count(self):
        # Catches a revoker that revokes every executor token when it finds a record, ignoring
        # which accessors the record holds.
        write_chain(self.record, [intent("r1"), minted("r1", "a-root", "a-scoped")])
        self.token("a-root", ["root"], 30)
        self.token("a-scoped", [CATALOGUE_POLICY], 30)
        self.scan()
        self.assertKept("a-root", "a-scoped")

    def test_both_accessors_of_a_minted_entry_count_as_recorded(self):
        # Catches a port that reads only the first accessor of a `minted` entry: the scoped
        # token would then count as unrecorded and be revoked with an intent open and the
        # token young.
        write_chain(self.record, [intent("r1"), minted("r1", "a-root", "a-scoped")])
        self.token("a-scoped", [CATALOGUE_POLICY], 30)
        self.scan()
        self.assertKept("a-scoped")


class UnrecordedTokens(RevokerCase):
    def test_an_unrecorded_root_token_is_kept_while_young_and_an_intent_is_open_then_revoked(self):
        # Catches a revoker that revokes the young root token of a request in progress (a mint
        # in flight), and one that never revokes an unrecorded token while an intent is open.
        write_chain(self.record, [intent("r1")])
        self.token("u-root", ["root"], 60)
        self.scan()
        self.assertKept("u-root")
        self.token("u-root", ["root"], GRACE + 1)
        self.scan()
        self.assertRevoked("u-root")

    def test_an_unrecorded_root_token_is_revoked_at_once_when_no_intent_is_open(self):
        # Catches a revoker that applies the grace period when nothing is in flight.
        write_chain(self.record, [intent("r1"), minted("r1", "a-root", "a-scoped"), closed("r1")])
        self.token("u-root", ["root"], 5)
        self.scan()
        self.assertRevoked("u-root")

    def test_an_empty_record_has_no_open_intent(self):
        # Catches a revoker that reads a record directory with no segments as unreadable and so
        # waits out the grace period: a fresh install has an empty record.
        self.record.mkdir()
        self.token("u-root", ["root"], 5)
        self.scan()
        self.assertRevoked("u-root")
        self.assertNotIn("ALERT", self.log)

    def test_a_crash_between_the_scoped_token_and_its_minted_entry_leaves_a_catalogue_token(self):
        # The executor made the scoped token, then crashed before it wrote `minted`. Catches a
        # revoker that looks only at root tokens and so never revokes the catalogue-policy token.
        write_chain(self.record, [intent("r1")])
        self.token("u-scoped", [CATALOGUE_POLICY], 60)
        self.scan()
        self.assertKept("u-scoped")
        self.token("u-scoped", [CATALOGUE_POLICY], GRACE + 1)
        self.scan()
        self.assertRevoked("u-scoped")


class WhichTokensCount(RevokerCase):
    def setUp(self):
        super().setUp()
        write_chain(self.record, [intent("r1"), minted("r1", "a-root", "a-scoped"), closed("r1")])

    def test_root_among_other_policies_counts_as_an_executor_token(self):
        # Catches a check for a policy list equal to ["root"]: a token can hold root and more.
        self.token("m-root", ["default", "root"], 10)
        self.scan()
        self.assertRevoked("m-root")

    def test_a_catalogue_policy_counts_as_an_executor_token(self):
        # Catches a check that looks for root only, or for a hard-coded policy name.
        self.token("m-cat", ["default", CATALOGUE_POLICY], 10)
        self.scan()
        self.assertRevoked("m-cat")

    def test_every_catalogue_policy_name_counts(self):
        # Catches a check that reads only the first name of the catalogue file.
        (self.dir / "catalogue.json").write_text(json.dumps(["catalogue-one", "catalogue-two"]))
        self.token("m-two", ["catalogue-two"], 10)
        self.scan()
        self.assertRevoked("m-two")

    def test_a_token_with_neither_never_counts(self):
        # The executor's own login token holds only generate-root, ESO's holds its reader
        # policy, and the revoker's own holds its policy. Catches a revoker that revokes every
        # token it finds, which would revoke itself and the services it protects.
        self.token("m-genroot", ["default", "generate-root"], 10 * GRACE)
        self.token("m-eso", ["default", "eso-reader"], 10 * GRACE)
        self.token("m-plain", ["default"], 10 * GRACE)
        self.scan()
        self.assertKept("m-genroot", "m-eso", "m-plain")
        own = [a for a in self.live() if a.startswith("fake-k8s-")]
        self.assertEqual(len(own), 1, "the revoker's own login token stays live")

    def test_a_lookup_without_policies_leaves_the_token_and_makes_the_scan_incomplete(self):
        # Catches a revoker that treats a missing policies list as an empty one, as root, or as a
        # harmless skip: a root token that cannot be read stays live, so the scan must not count.
        self.token("m-hidden", ["root"], 10 * GRACE)
        self.bridge.hide_policies.add("m-hidden")
        self.scan()
        self.assertKept("m-hidden")
        self.assertIn("m-hidden", self.log)
        self.assertIn("no policies list", self.log)
        self.assertFalse(self.result["ran"])
        self.assertEqual(self.result["failed"], 1)


class UnreadableRecord(RevokerCase):
    GOOD = None

    def good_record(self):
        return write_chain(self.record, [intent("r1"), minted("r1", "a-root", "a-scoped"), closed("r1"),
                                         intent("r2")])

    def break_hash(self):
        path = self.record / "segment-00000001.jsonl"
        lines = path.read_text().splitlines()
        lines[1] = lines[1].replace('"event":"intent"', '"event":"inten7"')
        path.write_text("\n".join(lines) + "\n")

    def make_gap(self):
        path = self.record / "segment-00000001.jsonl"
        lines = path.read_text().splitlines()
        del lines[2]
        path.write_text("\n".join(lines) + "\n")

    def truncate_newest(self):
        path = self.record / "segment-00000001.jsonl"
        data = path.read_bytes().rstrip(b"\n")
        path.write_bytes(data[:-25])

    def delete_directory(self):
        subprocess.run(["rm", "-rf", str(self.record)])

    def make_unreadable_file(self):
        if os.geteuid() == 0:
            self.skipTest("root reads every file")
        path = self.record / "segment-00000001.jsonl"
        path.chmod(0)
        self.addCleanup(lambda: path.chmod(0o644))

    CASES = ["break_hash", "make_gap", "truncate_newest", "delete_directory", "make_unreadable_file"]

    def test_each_failure_revokes_old_root_tokens_keeps_young_ones_and_raises_the_alert(self):
        # Catches a revoker that fails open (revokes nothing) or fails closed too hard (revokes
        # a young token whose request may be in flight), and one that raises no alert. A hash
        # mismatch, a sequence gap, a truncated newest entry, a missing directory and an
        # unreadable file each count as unreadable.
        for name in self.CASES:
            with self.subTest(case=name):
                self.setUp()
                self.good_record()
                getattr(self, name)()
                self.token("old-root", ["root"], GRACE + 1)
                self.token("young-root", ["root"], 60)
                self.token("old-cat", [CATALOGUE_POLICY], GRACE + 1)
                self.token("a-root", ["root"], 60)  # named by the record, which no longer counts
                self.scan()
                self.assertRevoked("old-root", "old-cat")
                self.assertKept("young-root", "a-root")
                self.assertIn("ALERT revoker-record-unreadable", self.log)
                self.assertFalse(self.result["readable"])

    def test_an_active_mint_survives_a_read_failure(self):
        # The intent is written, the root token is minted, `minted` is not yet written, and the
        # record cannot be read. Catches a revoker that revokes the young root token on a read
        # failure, which would abort a change that is going well.
        self.good_record()
        self.break_hash()
        self.token("mint-root", ["root"], 20)
        self.scan()
        self.assertKept("mint-root")

    def test_an_unreadable_record_does_not_make_recorded_tokens_count(self):
        # Catches a revoker that falls back to the accessors it read before the failure.
        self.good_record()
        self.break_hash()
        self.token("a-scoped", [CATALOGUE_POLICY], 60)
        self.scan()
        self.assertKept("a-scoped")
        self.assertNotIn("revoked a-scoped", self.log)

    def test_a_readable_record_raises_no_alert(self):
        # Catches an alert that fires on every scan.
        self.good_record()
        self.scan()
        self.assertNotIn("ALERT", self.log)
        self.assertTrue(self.result["readable"])


class StubBao:
    """A client whose lookups answer exactly what a test says, for answers the fake never gives."""

    def __init__(self, tokens):
        self.tokens = tokens  # accessor -> lookup data
        self.revoked = []
        self.server_time = NOW

    def accessors(self):
        return list(self.tokens)

    def lookup(self, accessor):
        return self.tokens[accessor]

    def revoke(self, accessor):
        self.revoked.append(accessor)


class UnreadableCreationTime(RevokerCase):
    BAD = [{}, {"creation_time": None}, {"creation_time": "soon"}, {"creation_time": True}]

    def scan_stub(self, tokens):
        bao, lines = StubBao(tokens), []
        original = revoker.log
        revoker.log = lines.append
        try:
            revoker.scan(bao, self.config(), {CATALOGUE_POLICY})
        finally:
            revoker.log = original
        self.log = "\n".join(lines)
        return bao.revoked

    def test_an_executor_token_without_a_readable_creation_time_counts_as_old(self):
        # Catches a revoker that treats a lookup it cannot date as young, so that a root token
        # whose lookup omits or garbles the time is never cleaned up. Covers an unreadable
        # record, an open intent, and no open intent.
        for name in ("unreadable", "open-intent", "no-intent"):
            for bad in self.BAD:
                with self.subTest(record=name, answer=bad):
                    self.setUp()
                    if name == "open-intent":
                        write_chain(self.record, [intent("r1")])
                    elif name == "no-intent":
                        write_chain(self.record, [intent("r1"), closed("r1")])
                    revoked = self.scan_stub({
                        "bad-root": {"policies": ["root"], **bad},
                        "bad-cat": {"policies": [CATALOGUE_POLICY], **bad},
                        "other": {"policies": ["default"], **bad},
                    })
                    self.assertEqual(sorted(revoked), ["bad-cat", "bad-root"])
                    self.assertIn("bad-root has no readable creation_time", self.log)
                    self.assertNotIn("other has no readable", self.log)

    def test_a_recorded_scoped_token_without_a_creation_time_is_kept_while_its_request_is_open(self):
        write_chain(self.record, [intent("r1"), minted("r1", "a-root", "a-scoped")])
        revoked = self.scan_stub({"a-scoped": {"policies": [CATALOGUE_POLICY]}, "a-root": {"policies": ["root"]}})
        self.assertEqual(revoked, ["a-root"])


class TrustBoundary(RevokerCase):
    def test_a_rehashed_forged_record_is_accepted(self):
        # Documents the trust boundary in the README: the chain is unkeyed. Whoever can write
        # the volume can drop the `closed` entry and rehash the chain, and the revoker then
        # reads a well-formed record. The read-only mount stops the revoker from changing the
        # record. It does not make the record authentic.
        write_chain(self.record, [intent("r1"), minted("r1", "a-root", "a-scoped"), closed("r1")])
        self.token("a-root", ["root"], 10)
        self.scan()
        self.assertRevoked("a-root")
        # The same request, with its `closed` entry dropped and the chain rehashed.
        self.token("b-root", ["root"], 10)
        write_chain(self.record, [intent("r1"), minted("r1", "b-root", "b-scoped")])
        self.scan()
        self.assertTrue(self.result["readable"])
        self.assertNotIn("ALERT", self.log)
        self.assertKept("b-root")


class FailuresDoNotStopTheScan(RevokerCase):
    def test_a_failed_lookup_leaves_that_token_and_the_scan_goes_on(self):
        # Catches a scan that stops at the first error.
        write_chain(self.record, [intent("r1"), closed("r1")])
        self.token("b-first", ["root"], 10)
        self.token("b-second", ["root"], 10)
        self.token("b-third", ["root"], 10)
        self.bridge.fail_lookup.add("b-second")
        self.scan()
        self.assertKept("b-second")
        self.assertRevoked("b-first", "b-third")
        self.assertIn("lookup b-second failed", self.log)
        self.assertFalse(self.result["ran"])
        self.assertEqual(self.result["failed"], 1)

    def test_a_failed_revoke_is_logged_and_the_scan_goes_on(self):
        # Catches a scan that stops, or reports a revoke that did not happen.
        write_chain(self.record, [intent("r1"), closed("r1")])
        self.token("c-first", ["root"], 10)
        self.token("c-second", ["root"], 10)
        self.bridge.fail_revoke.add("c-first")
        self.scan()
        self.assertKept("c-first")
        self.assertRevoked("c-second")
        self.assertIn("revoke c-first failed", self.log)
        self.assertNotIn("revoked c-first", self.log)
        self.assertFalse(self.result["ran"])
        self.assertEqual(self.result["failed"], 1)

    def test_a_failed_listing_ends_the_scan_without_a_crash(self):
        write_chain(self.record, [intent("r1"), closed("r1")])
        self.token("d-root", ["root"], 10)
        self.bridge.fail_list = True
        self.scan()
        self.assertKept("d-root")
        self.assertIn("list accessors failed", self.log)
        self.assertFalse(self.result["ran"])

    def test_a_listing_with_an_entry_that_is_not_an_accessor_is_rejected_whole(self):
        # Catches a client that filters the bad entries and scans the rest: a malformed answer
        # could hide the accessor of a root token.
        write_chain(self.record, [intent("r1"), closed("r1")])
        self.token("g-root", ["root"], 10)
        self.bridge.list_keys = lambda keys: [*keys, 7, None]
        self.scan()
        self.assertKept("g-root")
        self.assertIn("not a list of accessors", self.log)
        self.assertFalse(self.result["ran"])

    def test_a_lookup_whose_data_is_not_an_object_makes_the_scan_incomplete(self):
        # Catches a crash on the malformed answer, and a scan that counts as complete.
        write_chain(self.record, [intent("r1"), closed("r1")])
        self.token("h-root", ["root"], 10)
        self.token("h-other", ["root"], 10)
        self.bridge.data_list.add("h-root")
        self.scan()
        self.assertKept("h-root")
        self.assertRevoked("h-other")
        self.assertFalse(self.result["ran"])
        self.assertEqual(self.result["failed"], 1)

    def test_a_token_that_expired_between_the_listing_and_the_lookup_is_not_a_failure(self):
        # HTTP 400 with `invalid accessor` means that the token is gone. Short-lived tokens make that routine.
        write_chain(self.record, [intent("r1"), closed("r1")])
        self.token("i-gone", ["root"], 10)
        self.bridge.gone.add("i-gone")
        self.scan()
        self.assertIn("i-gone: the token is gone", self.log)
        self.assertTrue(self.result["ran"])
        self.assertEqual(self.result["failed"], 0)

    def errors(self, *messages):
        return json.dumps({"errors": list(messages)}).encode()

    def refuse(self, step, accessor, body):
        self.bridge.bad_request[(step, accessor)] = body

    def test_a_lookup_that_says_invalid_accessor_counts_as_a_gone_token(self):
        write_chain(self.record, [intent("r1"), closed("r1")])
        self.token("j-root", ["root"], 10)
        self.refuse("lookup", "j-root", self.errors("invalid accessor"))
        self.scan()
        self.assertIn("j-root: the token is gone", self.log)
        self.assertTrue(self.result["ran"])
        self.assertEqual(self.result["failed"], 0)

    # OpenBao's own answer for an unknown accessor, byte for byte from a live server.
    LIVE_INVALID_ACCESSOR = b'{"errors": ["1 error occurred:\\n\\t* invalid accessor\\n\\n"]}'

    def wrapped(self, *messages):
        count = len(messages)
        noun = "error" if count == 1 else "errors"
        body = f"{count} {noun} occurred:\n" + "".join(f"\t* {m}\n" for m in messages) + "\n"
        return json.dumps({"errors": [body]}).encode()

    def test_the_live_wrapped_answer_for_an_unknown_accessor_counts_as_a_gone_token(self):
        write_chain(self.record, [intent("r1"), closed("r1")])
        self.token("n-live", ["root"], 10)
        self.refuse("lookup", "n-live", self.LIVE_INVALID_ACCESSOR)
        self.scan()
        self.assertIn("n-live: the token is gone", self.log)
        self.assertTrue(self.result["ran"])
        self.assertEqual(self.result["failed"], 0)

    def test_the_wrapped_answer_for_a_gone_token_counts_on_a_revoke_too(self):
        write_chain(self.record, [intent("r1"), closed("r1")])
        self.token("o-wrapped", ["root"], 10)
        self.refuse("revoke", "o-wrapped", self.wrapped("token not found"))
        self.scan()
        self.assertIn("revoke o-wrapped: the token is gone", self.log)
        self.assertTrue(self.result["ran"])
        self.assertEqual(self.result["failed"], 0)

    def test_the_wrapper_is_read_into_its_messages(self):
        gone = revoker.token_is_gone
        def refused(errors):
            return revoker.BaoError("x", 400, errors)
        cases = {
            "live body": (["1 error occurred:\n\t* invalid accessor\n\n"], True),
            "plain form": (["invalid accessor"], True),
            "two messages": (["2 errors occurred:\n\t* invalid accessor\n\t* permission denied\n\n"], False),
            "plain next to a wrapper": (["invalid accessor", "1 error occurred:\n\t* invalid accessor\n\n"], False),
            "other message in the wrapper": (["1 error occurred:\n\t* permission denied\n\n"], False),
            "count too low": (["1 error occurred:\n\t* invalid accessor\n\t* permission denied\n\n"], False),
            "count too high": (["2 errors occurred:\n\t* invalid accessor\n\n"], False),
            "no message line": (["1 error occurred:\n\n"], False),
            "no bullet": (["1 error occurred:\ninvalid accessor\n\n"], False),
            "no closing blank line": (["1 error occurred:\n\t* invalid accessor"], False),
            "trailing text": (["1 error occurred:\n\t* invalid accessor\n\nmore"], False),
            "text before the wrapper": (["oops 1 error occurred:\n\t* invalid accessor\n\n"], False),
            "not a string": ([{"error": "invalid accessor"}], False),
            "empty list": ([], False),
            "no list": (None, False),
        }
        for name, (errors, expected) in cases.items():
            with self.subTest(body=name):
                self.assertIs(gone(refused(errors), "invalid accessor"), expected)
        self.assertFalse(gone(revoker.BaoError("x", 500, ["invalid accessor"]), "invalid accessor"))
        self.assertFalse(gone(revoker.BaoError("x", None, ["invalid accessor"]), "invalid accessor"))

    def test_a_wrapped_answer_that_is_not_exactly_one_gone_message_makes_the_scan_incomplete(self):
        write_chain(self.record, [intent("r1"), closed("r1")])
        for step, message in (("lookup", "invalid accessor"), ("revoke", "token not found")):
            bodies = {
                "wrapped two messages": self.wrapped(message, "permission denied"),
                "wrapped other message": self.wrapped("permission denied"),
                "wrapped other endpoint's message": self.wrapped("token not found" if step == "lookup" else "invalid accessor"),
                "wrapped malformed": json.dumps({"errors": [f"1 error occurred:\n{message}\n\n"]}).encode(),
            }
            for name, body in bodies.items():
                with self.subTest(step=step, body=name):
                    accessor = "q-" + step + "-" + name.replace(" ", "-")
                    self.token(accessor, ["root"], 10)
                    self.refuse(step, accessor, body)
                    self.scan()
                    self.assertKept(accessor)
                    self.assertIn(f"{step} {accessor} failed", self.log)
                    self.assertFalse(self.result["ran"])
                    self.bridge.bad_request.clear()
                    self.tokens.pop(accessor)
                    (self.state / "tokens" / f"{accessor}-token.json").unlink()

    def test_a_revoke_that_says_token_not_found_counts_as_a_gone_token(self):
        write_chain(self.record, [intent("r1"), closed("r1")])
        self.token("k-notfound", ["root"], 10)
        self.refuse("revoke", "k-notfound", self.errors("token not found"))
        self.scan()
        self.assertIn("revoke k-notfound: the token is gone", self.log)
        self.assertTrue(self.result["ran"])
        self.assertEqual(self.result["failed"], 0)

    def test_any_other_400_makes_the_scan_incomplete(self):
        # Catches a scan that reports complete while a root token stays live behind a refused request.
        write_chain(self.record, [intent("r1"), closed("r1")])
        def make_bodies(step):
            return {
                "other message": self.errors("missing client token"),
                "invalid request": self.errors("invalid request"),
                "no errors list": b'{"errors": "invalid accessor"}',
                "not json": b"Bad Request",
                "empty": b"",
                "mixed list": self.errors("invalid accessor", "permission denied"),
                "mixed list, revoke message": self.errors("token not found", "permission denied"),
                "other endpoint's message": self.errors("token not found" if step == "lookup" else "invalid accessor"),
            }
        for step in ("lookup", "revoke"):
            bodies = make_bodies(step)
            for name, body in bodies.items():
                with self.subTest(step=step, body=name):
                    accessor = "m-" + step + "-" + name.replace(" ", "-")
                    self.token(accessor, ["root"], 10)
                    self.refuse(step, accessor, body)
                    self.scan()
                    self.assertKept(accessor)
                    self.assertIn(f"{step} {accessor} failed", self.log)
                    self.assertFalse(self.result["ran"])
                    self.assertGreaterEqual(self.result["failed"], 1)
                    self.bridge.bad_request.clear()
                    self.tokens.pop(accessor)
                    (self.state / "tokens" / f"{accessor}-token.json").unlink()

    def test_a_failed_login_skips_the_scan(self):
        # The JWT binds another ServiceAccount, so the fake refuses the login.
        (self.dir / "jwt").write_text("fake-sa:openbao:somebody-else")
        write_chain(self.record, [intent("r1"), closed("r1")])
        self.token("e-root", ["root"], 10)
        self.scan()
        self.assertKept("e-root")
        self.assertIn("scan skipped", self.log)
        self.assertFalse(self.result["ran"])


class ServerTime(RevokerCase):
    def test_the_age_of_a_token_follows_the_server_clock_and_not_the_pod_clock(self):
        # Catches an age taken from the pod: with the pod far ahead, a young token that an open
        # request still needs would be revoked, and with the pod far behind an old one would stay.
        write_chain(self.record, [intent("r1")])
        self.token("k-young", ["root"], 10)
        self.token("k-old", ["root"], 10 * GRACE)
        original = revoker.time.time
        for pod_clock in (NOW + 100 * GRACE, NOW - 100 * GRACE):
            revoker.time.time = lambda pod_clock=pod_clock: pod_clock
            try:
                self.scan()
            finally:
                revoker.time.time = original
            self.assertTrue(self.result["ran"])
        self.assertKept("k-young")
        self.assertRevoked("k-old")

    def test_a_server_clock_that_runs_ahead_makes_a_token_old(self):
        write_chain(self.record, [intent("r1")])
        self.token("l-young", ["root"], 10)
        self.bridge.date_header = http_date(NOW + 10 * GRACE)
        self.scan()
        self.assertRevoked("l-young")

    def test_an_answer_without_a_usable_date_makes_the_scan_incomplete_and_revokes_nothing(self):
        # With no server time the age is unknown. Not even a closed request's token is touched.
        write_chain(self.record, [intent("r1"), closed("r1")])
        self.token("m-old", ["root"], 10 * GRACE)
        for date in (None, "yesterday-ish"):
            with self.subTest(date=date):
                self.bridge.date_header = date
                self.scan()
                self.assertFalse(self.result["ran"])
                self.assertEqual(self.result["failed"], 1)
                self.assertIn("no usable Date header", self.log)
                self.assertKept("m-old")


class LogsAndLogin(RevokerCase):
    def test_no_log_line_holds_a_token_and_the_script_stores_none(self):
        # Catches a log of the login answer or the request headers, and a token written to disk.
        write_chain(self.record, [intent("r1"), closed("r1")])
        self.token("f-root", ["root"], 10)
        self.scan()
        login_token = [t.stem for t in (self.state / "tokens").glob("fake-k8s-*.json")][0]
        self.assertNotIn(login_token, self.log)
        self.assertNotIn("fake-sa:", self.log)
        for path in list(self.record.rglob("*")) + [self.dir / "jwt"]:
            if path.is_file() and path.stat().st_mode & 0o444:
                self.assertNotIn(login_token, path.read_text())

    def test_each_scan_logs_in_again(self):
        # The script logs in on every scan and keeps no token between scans.
        write_chain(self.record, [intent("r1"), closed("r1")])
        self.scan()
        self.scan()
        logins = [c for c in self.bridge.calls if c[1] == "auth/kubernetes/login"]
        self.assertEqual(len(logins), 2)

    def test_an_incomplete_scan_shows_its_failures_and_does_not_move_the_last_scan_time(self):
        # Catches a heartbeat that advances while steps fail, which keeps the not-scanning alert quiet.
        metrics = revoker.Metrics()
        write_chain(self.record, [intent("r1"), closed("r1")])
        self.scan()
        metrics.update(self.result, NOW)
        self.token("j-root", ["root"], 10)
        self.bridge.fail_lookup.add("j-root")
        self.scan()
        metrics.update(self.result, NOW + 60)
        page = metrics.render()
        self.assertIn(f"dmf_openbao_revoker_last_scan_timestamp_seconds {NOW}\n", page)
        self.assertIn("dmf_openbao_revoker_failures 1\n", page)

    def test_a_failed_login_shows_a_failure_and_leaves_the_record_gauge_alone(self):
        metrics = revoker.Metrics()
        (self.dir / "jwt").write_text("fake-sa:openbao:somebody-else")
        self.scan()
        metrics.update(self.result, NOW)
        page = metrics.render()
        self.assertIn("dmf_openbao_revoker_failures 1\n", page)
        self.assertNotIn("record_readable", page)
        self.assertNotIn("last_scan_timestamp", page)

    def test_the_metrics_follow_the_scan(self):
        # Catches a metrics page that does not show an unreadable record.
        metrics = revoker.Metrics()
        write_chain(self.record, [intent("r1")])
        (self.record / "segment-00000001.jsonl").write_text("not json\n")
        self.scan()
        metrics.update(self.result, NOW)
        self.assertIn("dmf_openbao_revoker_record_readable 0", metrics.render())
        self.assertIn(f"dmf_openbao_revoker_last_scan_timestamp_seconds {NOW}", metrics.render())


class RecordsFromTheWriter(unittest.TestCase):
    """Records that dmf-init's writer produced, parsed by the revoker's copy of the parser.

    The writer is AuditLog in dmf-init, src/dmf_init/audit/chain.py. The two directories under
    tests/fixtures/audit-record/ are its output. dmf-init's tests/test_audit_revoker_fixture.py
    writes the same records again and compares them byte for byte, so the fixtures change only
    together with the writer, and these tests then show whether the parser follows.
    """

    FIXTURES = ROOT / "tests/fixtures/audit-record"
    ACCESSORS = {"acc-closed-1": "r-closed", "acc-closed-2": "r-closed",
                 "acc-open-1": "r-open", "acc-open-2": "r-open"}

    def test_a_record_over_two_segments_is_readable_and_holds_the_facts(self):
        record = revoker.read_record(self.FIXTURES / "rolled")

        self.assertTrue(record.readable, record.reason)
        self.assertEqual(record.accessors, self.ACCESSORS)
        self.assertEqual(record.closed, {"r-closed"})
        self.assertEqual(record.open_count, 1)

    def test_a_trimmed_record_that_starts_at_an_anchor_is_readable_and_holds_the_facts(self):
        record = revoker.read_record(self.FIXTURES / "trimmed")

        self.assertTrue(record.readable, record.reason)
        self.assertEqual(record.accessors, self.ACCESSORS)
        self.assertEqual(record.closed, {"r-closed"})
        self.assertEqual(record.open_count, 1)

    def test_an_edited_byte_in_a_fixture_makes_the_record_unreadable(self):
        # Shows that the fixtures are checked by the hash chain and not only parsed.
        copy = Path(tempfile.mkdtemp(prefix="record-fixture-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(copy)]))
        for path in (self.FIXTURES / "rolled").iterdir():
            (copy / path.name).write_bytes(path.read_bytes())
        first = copy / "segment-00000001.jsonl"
        first.write_text(first.read_text().replace("r-closed", "r-closer", 1))

        self.assertFalse(revoker.read_record(copy).readable)


class PortedRecord(unittest.TestCase):
    """The snapshot of a record, on chains that have a restore and a trim anchor."""

    def test_the_snapshot_holds_open_requests_accessors_and_closed_requests(self):
        # Catches a port that changes what the scan learns: an intent whose request has no
        # `closed` entry is open, and every accessor of every `minted` entry is recorded with
        # its request.
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            write_chain(directory, [intent("r1"), minted("r1", "x1", "x2"), closed("r1"), intent("r2"),
                                    minted("r2", "y1", "y2")])
            record = revoker.read_record(directory)
            self.assertEqual(record.status, revoker.Status.OK)
            self.assertEqual(record.open_count, 1)
            self.assertEqual(record.closed, {"r1"})
            self.assertEqual(record.accessors, {"x1": "r1", "x2": "r1", "y1": "r2", "y2": "r2"})

    def test_a_record_that_does_not_start_at_one_is_a_gap_unless_an_anchor_explains_it(self):
        # Catches a port of the chain check without its anchor rule.
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            entries = write_chain(directory, [intent("r1"), minted("r1", "x1", "x2"), closed("r1")])
            path = directory / "segment-00000001.jsonl"
            tail = path.read_text().splitlines()[2:]
            path.write_text("\n".join(tail) + "\n")
            self.assertEqual(revoker.read_record(directory).status, revoker.Status.GAP)
            (directory / "anchor.json").write_text(json.dumps({"seq": 2, "hash": entries[1]["hash"]}))
            record = revoker.read_record(directory)
            self.assertEqual(record.status, revoker.Status.TRIMMED)
            self.assertTrue(record.readable)

    def test_a_deleted_record_with_an_anchor_is_a_gap(self):
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            (directory / "anchor.json").write_text(json.dumps({"seq": 3, "hash": "0" * 64}))
            self.assertEqual(revoker.read_record(directory).status, revoker.Status.GAP)

    def test_segments_are_read_in_order(self):
        # Catches a port that sorts segment names as text when the numbers differ in width.
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            write_chain(directory, [intent("r1")], segment="segment-00000002.jsonl")
            self.assertEqual([n for n, _ in revoker.segments(directory)], [2])

    def test_an_unreadable_snapshot_holds_no_facts_a_scan_could_use(self):
        # Catches a snapshot that keeps the facts it read before the chain check failed.
        with tempfile.TemporaryDirectory() as raw:
            directory = Path(raw)
            write_chain(directory, [intent("r1"), minted("r1", "x1", "x2")])
            path = directory / "segment-00000001.jsonl"
            lines = path.read_text().splitlines()
            lines[2] = lines[2].replace('"event":"minted"', '"event":"minte7"')
            path.write_text("\n".join(lines) + "\n")
            record = revoker.read_record(directory)
            self.assertFalse(record.readable)
            self.assertEqual(record.status, revoker.Status.TAMPER)
            self.assertEqual((record.accessors, record.open_count), ({}, 0))


if __name__ == "__main__":
    unittest.main()
