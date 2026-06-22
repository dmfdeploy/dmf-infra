#!/usr/bin/env python3
"""awx-autoscale helper — stdlib-only on-demand scale-to-zero for AWX.

Deployed as a ConfigMap-mounted script in a python:3-slim container.
Exposes POST /ensure-awake (bearer-token auth) and a background idle-reaper.

Design authority: umbrella docs/plans/DMF AWX On-Demand Scale-to-Zero Plan 2026-06-18.md §B.

All k8s API calls use the mounted ServiceAccount token + ca.crt via urllib.
All AWX API calls use a bearer token from a mounted Secret via urllib.
No pip dependencies — stdlib only.
"""

from __future__ import annotations

import hmac
import json
import logging
import os
import ssl
import sys
import threading
import time
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

# ---------------------------------------------------------------------------
# Configuration from environment (set by the Ansible role's Deployment spec)
# ---------------------------------------------------------------------------

NAMESPACE = os.environ["AWX_AUTOSCALE_NAMESPACE"]
AWX_CR_NAME = os.environ.get("AWX_AUTOSCALE_CR_NAME", "awx")
AWX_API_URL = os.environ["AWX_AUTOSCALE_AWX_API_URL"].rstrip("/")
LISTEN_PORT = int(os.environ.get("AWX_AUTOSCALE_LISTEN_PORT", "8080"))
GRACE_PERIOD = int(os.environ.get("AWX_AUTOSCALE_GRACE_PERIOD", "300"))
MAX_STARTUP_WAIT = int(os.environ.get("AWX_AUTOSCALE_MAX_STARTUP_WAIT", "1200"))
WAKE_POLL_INTERVAL = int(os.environ.get("AWX_AUTOSCALE_WAKE_POLL_INTERVAL", "10"))
REAPER_POLL_INTERVAL = int(os.environ.get("AWX_AUTOSCALE_REAPER_POLL_INTERVAL", "60"))
WEB_REPLICAS = int(os.environ.get("AWX_AUTOSCALE_WEB_REPLICAS", "1"))
TASK_REPLICAS = int(os.environ.get("AWX_AUTOSCALE_TASK_REPLICAS", "1"))
LEASE_NAME = os.environ.get("AWX_AUTOSCALE_LEASE_NAME", "awx-autoscale-wake")

# Paths — mounted volumes
SA_TOKEN_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/token"
SA_CA_PATH = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
AWX_TOKEN_PATH = "/etc/awx-autoscale/secrets/awx-svc-token"
BEARER_TOKEN_PATH = "/etc/awx-autoscale/secrets/bearerToken"

# Kubernetes API base (in-cluster)
K8S_API = "https://kubernetes.default.svc"

# Annotation key for min_awake_until on the Lease object
LEASE_MIN_AWAKE_ANNOTATION = "awx-autoscale.dmf/min-awake-until"
LEASE_LAST_SLEEP_ANNOTATION = "awx-autoscale.dmf/last-sleep-at"

# ---------------------------------------------------------------------------
# Logging — JSON audit log to stdout
# ---------------------------------------------------------------------------

class JSONFormatter(logging.Formatter):
    """Emit structured JSON log lines for audit trail."""

    def format(self, record: logging.LogRecord) -> str:
        entry: dict[str, Any] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "msg": record.getMessage(),
        }
        if hasattr(record, "event"):
            entry["event"] = record.event  # type: ignore[attr-defined]
        if record.exc_info and record.exc_info[1]:
            entry["error"] = str(record.exc_info[1])
        return json.dumps(entry, separators=(",", ":"))


logger = logging.getLogger("awx-autoscale")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler(sys.stdout)
handler.setFormatter(JSONFormatter())
logger.addHandler(handler)


def audit(event: str, **fields: Any) -> None:
    """Emit a structured audit-log entry."""
    extra = {"event": event}
    msg_parts = [f"{k}={v}" for k, v in fields.items()]
    logger.info(f"{event} {' '.join(msg_parts)}", extra=extra)


# ---------------------------------------------------------------------------
# Kubernetes REST helpers (stdlib urllib, mounted SA token)
# ---------------------------------------------------------------------------

_sa_token_cache: str | None = None
_sa_token_read_at: float = 0.0
_ssl_ctx: ssl.SSLContext | None = None
# Re-read SA token every 60s (forward-compat for projected/rotated tokens).
_SA_TOKEN_MAX_AGE = 60.0


def _read_sa_token() -> str:
    global _sa_token_cache, _sa_token_read_at
    now = time.monotonic()
    if _sa_token_cache is None or (now - _sa_token_read_at) > _SA_TOKEN_MAX_AGE:
        with open(SA_TOKEN_PATH) as f:
            _sa_token_cache = f.read().strip()
        _sa_token_read_at = now
    return _sa_token_cache


def _get_ssl_ctx() -> ssl.SSLContext:
    global _ssl_ctx
    if _ssl_ctx is None:
        _ssl_ctx = ssl.create_default_context(cafile=SA_CA_PATH)
    return _ssl_ctx


def _k8s_request(
    method: str,
    path: str,
    body: dict | None = None,
    *,
    timeout: float = 10.0,
) -> dict:
    """Issue a request to the in-cluster Kubernetes API.

    Returns the parsed JSON response body.
    Raises urllib.error.HTTPError on non-2xx.
    """
    url = f"{K8S_API}{path}"
    token = _read_sa_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/merge-patch+json" if method == "PATCH" else "application/json"
    req = Request(url, data=data, headers=headers, method=method)
    with urlopen(req, context=_get_ssl_ctx(), timeout=timeout) as resp:
        return json.loads(resp.read())


def _read_secret(path_key: str) -> str:
    """Read a token from a mounted Secret volume."""
    with open(path_key) as f:
        return f.read().strip()


# ---------------------------------------------------------------------------
# Lease management — durable single-flight via coordination.k8s.io/Lease
# ---------------------------------------------------------------------------

class LeaseManager:
    """Manages the wake Lease object for single-flight + min_awake_until.

    The Lease stores min_awake_until as an annotation. On helper restart the
    Lease persists in etcd, so the helper can observe an active wake window
    and fail-open (keep AWX awake) rather than racing to sleep.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()

    def _lease_path(self) -> str:
        return f"/apis/coordination.k8s.io/v1/namespaces/{NAMESPACE}/leases/{LEASE_NAME}"

    def get(self) -> dict | None:
        """Read the Lease, returning None if it doesn't exist yet."""
        try:
            return _k8s_request("GET", self._lease_path())
        except HTTPError as e:
            if e.code == 404:
                return None
            raise

    def create_or_update(self, min_awake_until: float, holder: str,
                         max_retries: int = 5) -> tuple[dict, bool]:
        """Create or update the Lease with a new min_awake_until.

        CAS retry loop on 409 (conflict). Uses max(existing, requested) for
        min_awake_until so concurrent writers never shrink the window.

        Returns (lease_dict, acquired) where acquired is True ONLY if this
        caller won ownership (our holderIdentity written to the Lease).
        - create-success -> acquired True
        - create-409 -> re-read, acquired False (don't steal their holder)
        - update path -> acquire (write our holder) ONLY if lease expired
          or already ours; else acquired False and return their lease
          unchanged.
        """
        for attempt in range(max_retries):
            existing = self.get()
            if existing is None:
                # Try create — if another process creates first, 409 falls
                # through to the update path on the next retry.
                try:
                    now_iso = datetime.now(timezone.utc).isoformat()
                    body = {
                        "apiVersion": "coordination.k8s.io/v1",
                        "kind": "Lease",
                        "metadata": {
                            "name": LEASE_NAME,
                            "namespace": NAMESPACE,
                            "annotations": {
                                LEASE_MIN_AWAKE_ANNOTATION: str(min_awake_until),
                            },
                        },
                        "spec": {
                            "holderIdentity": holder,
                            "leaseDurationSeconds": MAX_STARTUP_WAIT + GRACE_PERIOD,
                            "acquireTime": now_iso,
                            "renewTime": now_iso,
                        },
                    }
                    created = _k8s_request(
                        "POST",
                        f"/apis/coordination.k8s.io/v1/namespaces/{NAMESPACE}/leases",
                        body=body,
                    )
                    return (created, True)
                except HTTPError as e:
                    if e.code == 409:
                        # Someone else created — re-read, we are NOT the holder.
                        audit("lease_cas_conflict", attempt=attempt,
                              action="not_acquired")
                        re_read = self.get()
                        if re_read is not None:
                            return (re_read, False)
                        continue  # race: created then deleted; retry
                    raise

            # Update path — determine if we should acquire.
            annotations = existing.get("metadata", {}).get("annotations") or {}
            existing_min = 0.0
            try:
                existing_min = float(annotations.get(
                    LEASE_MIN_AWAKE_ANNOTATION, "0"))
            except ValueError:
                pass
            existing_holder = existing.get(
                "spec", {}).get("holderIdentity", "")
            now = time.time()
            lease_expired = existing_min <= now

            # Acquire only if the lease is expired or we already hold it.
            if not lease_expired and existing_holder != holder:
                # Active lease held by someone else — don't steal.
                return (existing, False)

            # max(existing, requested) — never shrink the window.
            effective_min = max(existing_min, min_awake_until)

            annotations[LEASE_MIN_AWAKE_ANNOTATION] = str(effective_min)
            existing["metadata"]["annotations"] = annotations
            existing["spec"]["holderIdentity"] = holder
            existing["spec"]["renewTime"] = datetime.now(
                timezone.utc).isoformat()
            existing["spec"]["leaseDurationSeconds"] = (
                MAX_STARTUP_WAIT + GRACE_PERIOD)
            try:
                updated = _k8s_request(
                    "PUT", self._lease_path(), body=existing)
                return (updated, True)
            except HTTPError as e:
                if e.code == 409:
                    audit("lease_cas_conflict", attempt=attempt,
                          action="retry")
                    continue
                raise

        # Exhausted retries — return best-effort read, not acquired.
        audit("lease_cas_exhausted", retries=max_retries)
        final = self.get()
        return (final or {}, False)

    def get_min_awake_until(self) -> float:
        """Read min_awake_until from the Lease annotations. 0.0 if absent."""
        lease = self.get()
        if lease is None:
            return 0.0
        annotations = lease.get("metadata", {}).get("annotations") or {}
        raw = annotations.get(LEASE_MIN_AWAKE_ANNOTATION, "0")
        try:
            return float(raw)
        except ValueError:
            return 0.0

    def record_sleep(self) -> None:
        """Annotate the Lease with sleep time + grace period."""
        lease = self.get()
        if lease is None:
            return
        annotations = lease.get("metadata", {}).get("annotations") or {}
        annotations[LEASE_LAST_SLEEP_ANNOTATION] = datetime.now(timezone.utc).isoformat()
        annotations["awx-autoscale.dmf/grace-period"] = str(GRACE_PERIOD)
        lease["metadata"]["annotations"] = annotations
        try:
            _k8s_request("PUT", self._lease_path(), body=lease)
        except HTTPError:
            pass  # best-effort annotation


_lease = LeaseManager()


# ---------------------------------------------------------------------------
# AWX CR patch + readiness wait
# ---------------------------------------------------------------------------

def _scale_awx_deployments(web_replicas: int, task_replicas: int) -> None:
    """Directly patch awx-web + awx-task Deployment replicas (#110).

    The AWX CR patch (with manage_replicas=true) is authoritative, but the AWX
    operator's reconcile is async and slow on Pi-class nodes (~6-10 min) — so a
    CR-only change leaves AWX in the old state for minutes. Scaling the
    Deployments directly takes effect in seconds; because the CR is patched to
    the SAME target alongside this, the operator's next reconcile is a no-op
    (no fight). Best-effort: a failure here is non-fatal — the operator still
    converges the Deployments to the CR value eventually.
    """
    for name, replicas in (
        (f"{AWX_CR_NAME}-web", web_replicas),
        (f"{AWX_CR_NAME}-task", task_replicas),
    ):
        try:
            _k8s_request(
                "PATCH",
                f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{name}",
                body={"spec": {"replicas": replicas}},
            )
        except (HTTPError, URLError) as e:
            audit("awx_deploy_scale_failed", deployment=name,
                  replicas=replicas, error=str(e))


def patch_awx_awake() -> None:
    """Merge-patch the AWX CR to awake replica counts + manage_replicas, then
    scale the Deployments directly so the wake is seconds, not an operator
    reconcile cycle (#110)."""
    body = {
        "spec": {
            "web_replicas": WEB_REPLICAS,
            "task_replicas": TASK_REPLICAS,
            "web_manage_replicas": True,
            "task_manage_replicas": True,
        }
    }
    _k8s_request(
        "PATCH",
        f"/apis/awx.ansible.com/v1beta1/namespaces/{NAMESPACE}/awxs/{AWX_CR_NAME}",
        body=body,
    )
    _scale_awx_deployments(WEB_REPLICAS, TASK_REPLICAS)
    audit("awx_cr_patched", state="awake",
          web_replicas=WEB_REPLICAS, task_replicas=TASK_REPLICAS)


def patch_awx_asleep() -> None:
    """Merge-patch the AWX CR to zero replicas + manage_replicas, then scale the
    Deployments directly to 0 so the sleep is immediate, not an operator
    reconcile cycle (#110)."""
    body = {
        "spec": {
            "web_replicas": 0,
            "task_replicas": 0,
            "web_manage_replicas": True,
            "task_manage_replicas": True,
        }
    }
    _k8s_request(
        "PATCH",
        f"/apis/awx.ansible.com/v1beta1/namespaces/{NAMESPACE}/awxs/{AWX_CR_NAME}",
        body=body,
    )
    _scale_awx_deployments(0, 0)
    _lease.record_sleep()
    audit("awx_cr_patched", state="asleep")


def wait_awx_ready(timeout: float) -> bool:
    """Poll until web + task Deployments have readyReplicas >= 1 AND
    the AWX API /api/v2/ping/ returns 200.

    Keyed on readyReplicas + API (never observedGeneration — absent on this CR).
    Returns True if ready within timeout, False otherwise.
    """
    deadline = time.monotonic() + timeout
    web_ready = False
    task_ready = False
    api_ok = False

    while time.monotonic() < deadline:
        # Check web Deployment
        if not web_ready:
            try:
                dep = _k8s_request(
                    "GET",
                    f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{AWX_CR_NAME}-web",
                )
                web_ready = (dep.get("status", {}).get("readyReplicas") or 0) >= 1
            except (HTTPError, URLError):
                pass

        # Check task Deployment
        if not task_ready:
            try:
                dep = _k8s_request(
                    "GET",
                    f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{AWX_CR_NAME}-task",
                )
                task_ready = (dep.get("status", {}).get("readyReplicas") or 0) >= 1
            except (HTTPError, URLError):
                pass

        # Check AWX API ping (only after both Deployments are ready)
        if web_ready and task_ready and not api_ok:
            api_ok = _check_awx_api_ping()

        if web_ready and task_ready and api_ok:
            return True

        time.sleep(WAKE_POLL_INTERVAL)

    return False


def _check_awx_api_ping() -> bool:
    """Check if AWX API /api/v2/ping/ returns 200."""
    try:
        req = Request(f"{AWX_API_URL}/api/v2/ping/", method="GET")
        with urlopen(req, timeout=5) as resp:
            return resp.status == 200
    except (HTTPError, URLError, OSError):
        return False


# ---------------------------------------------------------------------------
# AWX active-work query (idle-reaper)
# ---------------------------------------------------------------------------

# Tri-state results for the reaper's AWX work query. The reaper MUST treat
# "AWX unreachable" (typically asleep) differently from "active work": an
# unreachable AWX must NOT cause the reaper to claim the wake Lease, or it
# starves /ensure-awake and AWX can never wake (issue #103).
WORK_ACTIVE = "active"            # AWX reachable, active jobs present
WORK_IDLE = "idle"               # AWX reachable, no active jobs
WORK_UNREACHABLE = "unreachable"  # AWX API not reachable (e.g. AWX asleep)


def query_awx_work() -> str:
    """Tri-state AWX active-work query for the idle reaper.

    Returns WORK_ACTIVE / WORK_IDLE / WORK_UNREACHABLE.

    status__in=new,pending,waiting,running (NOT status=running,pending,waiting).
    Covers jobs, workflow jobs, project updates, inventory updates via
    /api/v2/unified_jobs/.

    On an unreachable API this returns WORK_UNREACHABLE (NOT WORK_ACTIVE): the
    reaper still fail-opens by NOT sleeping, but it must also NOT claim the wake
    Lease for an unreachable (asleep) AWX — see idle_reaper_loop and issue #103.
    """
    token = _read_secret(AWX_TOKEN_PATH)
    # status__in=new,pending,waiting,running — the proven DMF pattern.
    # NOT status=running,pending,waiting (drops "new", wrong separator).
    url = f"{AWX_API_URL}/api/v2/unified_jobs/?status__in=new,pending,waiting,running&page_size=1"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    try:
        req = Request(url, headers=headers, method="GET")
        # AWX_API_URL is plain HTTP (in-cluster svc DNS, ADR-0023).
        with urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
            count = data.get("count", 0)
            if count > 0:
                audit("active_work_found", count=count)
                return WORK_ACTIVE
            return WORK_IDLE
    except (HTTPError, URLError, OSError) as e:
        # API unreachable — typically AWX asleep. Fail-open for the SLEEP
        # decision (the reaper won't sleep), but it must NOT extend the wake
        # Lease for this state (issue #103).
        audit("awx_api_unreachable", error=str(e), action="reaper_noop")
        return WORK_UNREACHABLE


def has_active_work() -> bool:
    """Back-compat bool wrapper around query_awx_work().

    Preserves the original fail-open contract (True unless AWX is
    reachable-and-idle): WORK_UNREACHABLE -> True. The reaper uses
    query_awx_work() directly because it must act differently on UNREACHABLE.
    """
    return query_awx_work() != WORK_IDLE


# ---------------------------------------------------------------------------
# /ensure-awake handler
# ---------------------------------------------------------------------------

# In-process guard: collapses concurrent requests within the same process
# into a single wake operation. The Lease provides durability across restarts.
_wake_lock = threading.Lock()


def do_ensure_awake() -> dict[str, Any]:
    """Execute the wake flow with CAS single-flight.

    Ownership semantics (acquired flag from Lease CAS):
      - acquired=True: this caller owns the Lease and patches the AWX CR.
      - acquired=False: another holder owns an unexpired Lease; this caller
        calls wait_awx_ready() only — it NEVER patches the CR, regardless
        of whether AWX is currently pinging (cold-wake guard).
    """
    holder_id = f"awx-autoscale-{os.getpid()}-{threading.current_thread().name}"
    new_min_awake = time.time() + MAX_STARTUP_WAIT

    # Acquire in-process lock for the CAS attempt.
    with _wake_lock:
        _lease_data, acquired = _lease.create_or_update(
            new_min_awake, holder_id)

    if acquired:
        # We are the holder — patch CR + wait.
        audit("ensure_awake_holder_start", holder=holder_id,
              max_startup_wait=MAX_STARTUP_WAIT)
        try:
            patch_awx_awake()
        except HTTPError as e:
            audit("awx_cr_patch_failed", status=e.code, error=str(e))
            return {"ok": False, "detail": f"CR patch failed: {e.code}"}

        ready = wait_awx_ready(MAX_STARTUP_WAIT)
        if ready:
            # Extend lease from now — wake took some time.
            final_min_awake = time.time() + GRACE_PERIOD
            _lease.create_or_update(final_min_awake, holder_id)
            audit("ensure_awake_ready",
                  min_awake_until=final_min_awake, holder=holder_id)
            return {"ok": True, "detail": "awake and ready"}

        audit("ensure_awake_timeout", max_startup_wait=MAX_STARTUP_WAIT)
        return {"ok": False,
                "detail": f"timeout after {MAX_STARTUP_WAIT}s"}

    # Not acquired — another holder is waking AWX. Just wait.
    # NO _check_awx_api_ping() precondition: an unexpired different-holder
    # lease ALWAYS means wait, even during a cold wake when AWX isn't up yet.
    lease_holder = _lease_data.get(
        "spec", {}).get("holderIdentity", "unknown")
    audit("ensure_awake_non_holder", holder=lease_holder)
    ready = wait_awx_ready(MAX_STARTUP_WAIT)
    if ready:
        return {"ok": True, "detail": f"awake (holder {lease_holder} woke)"}
    return {"ok": False,
            "detail": f"timeout waiting for holder {lease_holder}"}


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------

class HelperHandler(BaseHTTPRequestHandler):
    """Minimal HTTP handler for /ensure-awake, /healthz, /readyz, /awx-status.

    Probe semantics (CRITICAL — probes NEVER depend on AWX state):
      /healthz  — liveness: process-local only (no k8s API, no AWX API).
      /readyz   — readiness: helper can serve (bearer secret readable).
                  Returns 200 even when AWX is ASLEEP — otherwise the pod
                  loses its Service endpoint and dmf-cms cannot reach
                  /ensure-awake to wake AWX (deadlock).
      /awx-status — observability only (not a probe): reports AWX state.
    """

    def _check_bearer(self) -> bool:
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        token = auth[7:]
        try:
            expected = _read_secret(BEARER_TOKEN_PATH)
        except OSError:
            logger.error("bearer token file unreadable")
            return False
        return hmac.compare_digest(token, expected)

    def _send_json(self, code: int, body: dict) -> None:
        payload = json.dumps(body, separators=(",", ":")).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/healthz":
            # LOCAL-ONLY — no k8s or AWX API calls. An apiserver hiccup
            # must never cause a liveness kill.
            self._send_json(200, {"status": "ok"})
        elif self.path == "/readyz":
            # Helper readiness — bearer secret must be readable.
            # NOT AWX state: returning 503 when AWX is asleep would remove
            # this pod from the Service, making /ensure-awake unreachable.
            try:
                _read_secret(BEARER_TOKEN_PATH)
                self._send_json(200, {"helper": "ready"})
            except OSError:
                self._send_json(503, {"helper": "secrets_unavailable"})
        elif self.path == "/awx-status":
            # Observability only — not used by any probe.
            awx_ok = _check_awx_api_ping()
            self._send_json(
                200,
                {"awx_api": "ok" if awx_ok else "unreachable"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/ensure-awake":
            self._send_json(404, {"error": "not found"})
            return

        if not self._check_bearer():
            audit("ensure_awake_auth_failed", remote=self.client_address[0])
            self._send_json(401, {"error": "unauthorized"})
            return

        result = do_ensure_awake()
        code = 200 if result["ok"] else 503
        self._send_json(code, result)

    def log_message(self, fmt: str, *args: Any) -> None:
        # Suppress default stderr logging — we use structured audit logs.
        pass


# ---------------------------------------------------------------------------
# Idle-reaper background thread
# ---------------------------------------------------------------------------

def idle_reaper_loop() -> None:
    """Background loop: sleep AWX when idle past grace period.

    Sleep ONLY when:
      1. No active work (unified_jobs query returns 0).
      2. now > min_awake_until (the wake lease has expired).
      3. Idle time exceeds the grace period.

    If the AWX API cannot be queried, DO NOT sleep (fail-open).
    """
    audit("reaper_started", interval=REAPER_POLL_INTERVAL, grace_period=GRACE_PERIOD)
    idle_since: float | None = None

    while True:
        time.sleep(REAPER_POLL_INTERVAL)
        try:
            now = time.time()
            min_awake = _lease.get_min_awake_until()

            # Within the wake lease window — don't even check.
            if now < min_awake:
                idle_since = None
                continue

            # Query active work (tri-state — UNREACHABLE is NOT ACTIVE).
            state = query_awx_work()
            if state == WORK_UNREACHABLE:
                # AWX API unreachable — typically asleep. Fail-open: do NOT
                # sleep. CRUCIALLY do NOT extend the wake Lease — claiming it as
                # the reaper would starve an incoming /ensure-awake so AWX could
                # never wake (issue #103). Leave the Lease free for the wake.
                idle_since = None
                continue

            if state == WORK_ACTIVE:
                # Confirmed active work — extend the lease and reset idle timer.
                new_min_awake = now + GRACE_PERIOD
                _lease.create_or_update(
                    new_min_awake, f"reaper-{os.getpid()}")
                idle_since = None
                continue

            # WORK_IDLE: AWX reachable, no active work, past the lease window.
            if idle_since is None:
                idle_since = now
                audit("reaper_idle_start", idle_since=now)
                continue

            idle_duration = now - idle_since
            if idle_duration >= GRACE_PERIOD:
                audit("reaper_sleep_trigger",
                      idle_seconds=round(idle_duration, 1),
                      grace_period=GRACE_PERIOD)
                patch_awx_asleep()
                idle_since = None
            else:
                audit("reaper_idle_waiting",
                      idle_seconds=round(idle_duration, 1),
                      remaining=round(GRACE_PERIOD - idle_duration, 1))

        except Exception:
            # Any unhandled error in the reaper — log and continue.
            # Never crash the reaper; never sleep on error.
            logger.exception("reaper_error")
            idle_since = None


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    audit("helper_starting",
          namespace=NAMESPACE, cr=AWX_CR_NAME,
          listen_port=LISTEN_PORT,
          grace_period=GRACE_PERIOD,
          max_startup_wait=MAX_STARTUP_WAIT)

    # Start the idle-reaper in a daemon thread.
    reaper = threading.Thread(target=idle_reaper_loop, daemon=True, name="idle-reaper")
    reaper.start()

    # Start the HTTP server (threaded — wake blocks /ensure-awake but must
    # not block /healthz or the kubelet kills us mid-wake).
    server = ThreadingHTTPServer(("0.0.0.0", LISTEN_PORT), HelperHandler)
    audit("helper_listening", port=LISTEN_PORT)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        audit("helper_stopping")
        server.shutdown()


if __name__ == "__main__":
    main()
