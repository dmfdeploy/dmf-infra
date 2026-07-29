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
from urllib.parse import quote
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
# #295: once every launch-ready signal first goes green, take a SECOND
# COMPLETE snapshot — all four signals re-evaluated from scratch, nothing
# latched — after this short settle window before declaring ready. A single
# green snapshot can still be followed by another transient 500 (observed
# live: staggered per-worker readiness inside the single AWX web pod). Any
# signal regressing on the confirmation snapshot re-arms the whole gate.
STABILIZATION_DELAY = float(os.environ.get("AWX_AUTOSCALE_STABILIZATION_DELAY", "3"))
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
                         max_retries: int = 5, *,
                         allow_owner_shrink: bool = False) -> tuple[dict, bool]:
        """Create or update the Lease with a new min_awake_until.

        CAS retry loop on 409 (conflict). By default uses max(existing,
        requested) for min_awake_until so concurrent writers never shrink
        the window.

        allow_owner_shrink (keyword-only, default False — #312): when True
        AND this caller already holds the lease (existing_holder == holder),
        min_awake_until becomes a TRUE REWRITE — effective_min is set to
        exactly the requested value, which may shrink OR extend the current
        floor. Reserved for the do_ensure_awake post-ready branch, which
        knows readiness just arrived and wants to replace the acquire-time
        startup floor (now + MAX_STARTUP_WAIT) with the much nearer
        ready + GRACE_PERIOD deadline — the never-shrink max() default
        could only ever extend that startup floor, never replace it, which
        pinned every wake awake for ~MAX_STARTUP_WAIT regardless of how
        fast readiness actually arrived. NEVER honored for a different
        holder's active lease — the owner check below applies identically
        whether or not this flag is set.

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
            # Owner-only, REGARDLESS of allow_owner_shrink: a different
            # holder's active lease can never be stolen or shortened by
            # this flag (#312 invariant).
            if not lease_expired and existing_holder != holder:
                # Active lease held by someone else — don't steal.
                return (existing, False)

            if allow_owner_shrink and existing_holder == holder:
                # True rewrite (#312) — may shrink OR extend past the
                # startup floor, e.g. when readiness arrived late enough
                # that ready + GRACE_PERIOD exceeds it.
                effective_min = min_awake_until
            else:
                # Default: max(existing, requested) — never shrink the window.
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

    @staticmethod
    def _annotation_min_awake(lease: dict) -> float:
        """Parse min_awake_until off an already-fetched Lease dict (#312).

        Shared by get_min_awake_until (which does its own fresh GET) and
        do_ensure_awake's post-ready audit (which must log the value
        actually PERSISTED by create_or_update's own return — re-reading
        via a second GET would be both wasteful and not strictly the same
        moment in time).
        """
        annotations = lease.get("metadata", {}).get("annotations") or {}
        raw = annotations.get(LEASE_MIN_AWAKE_ANNOTATION, "0")
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    def get_min_awake_until(self) -> float:
        """Read min_awake_until from the Lease annotations. 0.0 if absent."""
        lease = self.get()
        if lease is None:
            return 0.0
        return self._annotation_min_awake(lease)

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


def _pod_is_nonterminal(pod: dict) -> bool:
    """True for a pod that has not reached a terminal phase.

    This is upstream ``controller.IsPodActive`` MINUS its deletionTimestamp
    clause, and the omission is deliberate — do not "restore" it. Upstream
    folds "terminating" into "not active" because it only wants a live count;
    this gate needs terminating pods to stay VISIBLE so
    ``_deployment_launch_ready`` can refuse on them. Termination is evaluated
    separately, by ``_pod_is_terminating``. Calling this a mirror of
    IsPodActive would be wrong, and a reader who assumed the equivalence
    would reintroduce #298.

    The phase filter itself matters on its own: an evicted pod left behind in
    phase Failed would otherwise count as a stuck member forever and hold
    /ensure-awake shut for the whole MAX_STARTUP_WAIT budget.
    """
    phase = (pod.get("status") or {}).get("phase")
    return phase not in ("Succeeded", "Failed")


def _pod_is_terminating(pod: dict) -> bool:
    """True once the pod has a deletionTimestamp — it is on its way out.

    Upstream drops such pods from readyReplicas the moment the timestamp is
    set, so their presence is invisible in Deployment status; the only way to
    know a drain is still in flight is to look at the pods themselves.
    """
    return bool((pod.get("metadata") or {}).get("deletionTimestamp"))


def _pod_is_ready(pod: dict) -> bool:
    """True only when the pod carries a Ready condition with status "True"."""
    conditions = (pod.get("status") or {}).get("conditions")
    if not isinstance(conditions, list):
        return False
    for cond in conditions:
        if isinstance(cond, dict) and cond.get("type") == "Ready":
            return cond.get("status") == "True"
    return False


def _list_deployment_pods(dep: dict) -> list | None:
    """List the Pods a Deployment selects, via the Deployment's OWN selector.

    Deriving the label selector from ``spec.selector.matchLabels`` rather than
    hardcoding AWX's labels keeps this correct across awx-operator versions.

    Returns None when the pod set cannot be determined (no usable selector,
    API error, malformed body). Callers MUST treat None as not-ready: failing
    open here would restore exactly the #298 behaviour.
    """
    selector = ((dep.get("spec") or {}).get("selector") or {}).get("matchLabels")
    if not isinstance(selector, dict) or not selector:
        return None
    label_selector = ",".join(f"{k}={v}" for k, v in sorted(selector.items()))
    try:
        resp = _k8s_request(
            "GET",
            f"/api/v1/namespaces/{NAMESPACE}/pods"
            f"?labelSelector={quote(label_selector, safe='=,')}",
        )
    except (HTTPError, URLError, OSError, ValueError) as e:
        # Most likely cause on first rollout: the namespaced Role is missing
        # the pods list/get rule this gate depends on. Loud, because the
        # symptom is an /ensure-awake that never returns ready. ValueError
        # covers a truncated/undecodable body — this runs on the /ensure-awake
        # request thread, so nothing may escape it.
        audit("pod_list_failed", selector=label_selector, error=str(e))
        return None
    if not isinstance(resp, dict):
        return None
    items = resp.get("items")
    if not isinstance(items, list):
        return None
    return [p for p in items if isinstance(p, dict)]


def _deployment_launch_ready(name: str) -> tuple:
    """Is this Deployment awake AND settled? Returns (ready, reason).

    ``status.readyReplicas`` is NOT the signal (#298). Every pre-#298 signal
    describes the REPLACEMENT generation or the AWX API; none of them can see
    that the PREVIOUS generation is still tearing down. AWX's web/task pods
    carry no readinessProbe, so the replacement pod's Ready condition flips the
    instant its container starts (measured ~18s post-wake), and the AWX API
    answers normally throughout — so the gate opens while the outgoing
    dispatcher/receptor is still cancelling its in-flight work units. Work
    admitted into that window is corrupted by the teardown, not by anything
    wrong with the pod serving it.

    What this predicate establishes is therefore narrow and specific: the
    previous generation is COMPLETELY GONE. Four conditions, all required:

      1. ``status.observedGeneration >= metadata.generation`` — otherwise the
         controller has not yet run a sync pass that saw our scale patch, and
         ``status`` describes the world before it. (The "no observedGeneration"
         note elsewhere in this file is about the AWX CR, which genuinely lacks
         it; Deployments publish it.) NOTE this is necessary, not sufficient:
         upstream sets observedGeneration unconditionally in the same struct
         literal as the replica counts, which are summed from whatever
         ReplicaSet status happened to be in the informer cache. It rules out
         obviously pre-patch status; it does not prove convergence. Conditions
         3 and 4 are what actually establish that.
      2. ``spec.replicas >= 1`` — defensive. The awx-operator reconciles
         asynchronously, so in principle a pass that began while the CR still
         read asleep could land after our wake patch and put replicas back to
         0; a pod that is Ready right now and already scheduled for deletion is
         not launch-ready. (This was NOT the #298 mechanism — the live data
         shows no such lost update — but the check is free and the state is
         unambiguously not-launch-ready.)
      3. NO active pod carries a deletionTimestamp — the previous generation's
         drain must have COMPLETED, not merely been started. There is no
         status-level signal for this: upstream excludes terminating pods from
         readyReplicas entirely (controller.IsPodActive), and the field that
         would report them, status.terminatingReplicas, is KEP-3973 — alpha
         and off by default until k8s 1.35. Listing the pods is the only way.
      4. at least one active, non-terminating pod reports Ready.

    Condition 3 is the load-bearing one: 4 alone is satisfied by the fresh pod
    while the old one is still dying.
    """
    path = f"/apis/apps/v1/namespaces/{NAMESPACE}/deployments/{name}"
    try:
        dep = _k8s_request("GET", path)
    except (HTTPError, URLError, OSError, ValueError) as e:
        return False, f"deployment_unreadable:{e}"
    if not isinstance(dep, dict):
        return False, "deployment_malformed"

    generation = (dep.get("metadata") or {}).get("generation")
    observed = (dep.get("status") or {}).get("observedGeneration")
    if isinstance(generation, bool) or not isinstance(generation, int):
        return False, "generation_missing"
    if isinstance(observed, bool) or not isinstance(observed, int):
        return False, "observed_generation_missing"
    if observed < generation:
        return False, f"status_stale:observed={observed}<generation={generation}"

    replicas = (dep.get("spec") or {}).get("replicas")
    if isinstance(replicas, bool) or not isinstance(replicas, int) or replicas < 1:
        return False, f"spec_replicas={replicas}"

    pods = _list_deployment_pods(dep)
    if pods is None:
        return False, "pods_unreadable"

    active = [p for p in pods if _pod_is_nonterminal(p)]
    terminating = [p for p in active if _pod_is_terminating(p)]
    if terminating:
        return False, f"draining:{len(terminating)}_terminating"
    ready = [p for p in active if _pod_is_ready(p)]
    if not ready:
        return False, f"ready_pods=0/active={len(active)}"
    return True, "ready"


def check_launch_ready_snapshot() -> dict:
    """Evaluate all four readiness signals at this moment (not latched).

    Returns dict with web_ready, task_ready, api_ok, auth_ok, and all_ready keys
    plus a web_reason/task_reason pair for diagnosing a wake that never opens.
    Each signal is re-evaluated fresh on every call — no persistence across snapshots.
    """
    web_ready, web_reason = _deployment_launch_ready(f"{AWX_CR_NAME}-web")
    task_ready, task_reason = _deployment_launch_ready(f"{AWX_CR_NAME}-task")
    api_ok = False
    auth_ok = False

    # Check AWX API ping + capacity (only after both Deployments are ready)
    if web_ready and task_ready:
        api_ok = _check_awx_api_ping()

    # Authenticated read (only after all prior signals green)
    if web_ready and task_ready and api_ok:
        auth_ok = _check_awx_authenticated_ready()

    return {
        "web_ready": web_ready,
        "task_ready": task_ready,
        "web_reason": web_reason,
        "task_reason": task_reason,
        "api_ok": api_ok,
        "auth_ok": auth_ok,
        "all_ready": web_ready and task_ready and api_ok and auth_ok,
    }


def wait_awx_ready(timeout: float) -> bool:
    """Poll until AWX is launch-ready via two complete green snapshots (#295).

    Reproduced live against AWX 24.6.1: readyReplicas >= 1 on both
    Deployments plus an unauthenticated /api/v2/ping/ 200 is NOT sufficient.
    A real deploy dispatched the instant that gate went green hit a bare
    Django ``Server Error (500)`` from AWX's OWN backend on the very next
    authenticated call (job template lookup) — not a network-level
    reset/timeout. ping's lightweight view can succeed for several seconds
    while an authenticated, ORM/RBAC-backed read still 500s.

    All four signals must be GREEN TOGETHER on TWO separate snapshots
    separated by STABILIZATION_DELAY before returning True:
      1. web Deployment settled + at least one Ready, non-terminating pod
      2. task Deployment settled + at least one Ready, non-terminating pod
      3. /api/v2/ping/ 200 + registered 'controlplane' instance_group capacity > 0
      4. an authenticated read succeeds

    Signals 1 and 2 are Deployment-SETTLED checks, NOT readyReplicas (#298):
    see _deployment_launch_ready. A wake arriving mid-drain used to go green
    off the replacement generation alone — readyReplicas from the fresh pod
    (no readinessProbe, so Ready at container start) and a perfectly healthy
    AWX API — while the outgoing generation was still tearing down.

    This is a state predicate, NOT a delay. A wake from a settled asleep state
    still opens the gate as fast as it did before (live: 16-25s, unchanged);
    only a wake that overlaps an in-flight drain is held back.

    Each snapshot re-evaluates all signals from scratch — no latching.

    The stabilization check happens immediately: as soon as a snapshot comes
    back all-ready, sleep exactly STABILIZATION_DELAY and take the second
    confirmation snapshot right away — NOT via the normal WAKE_POLL_INTERVAL
    poll cadence, which would (a) let extra, unaccounted-for snapshots run
    in between and (b) delay confirmation by a full poll interval instead of
    the short stabilization window. If the confirmation snapshot is not
    all-ready, the stabilization window resets — the outer loop requires a
    brand-new all-ready snapshot (at the normal WAKE_POLL_INTERVAL cadence)
    before attempting stabilization again.

    Keyed on Deployment-settled + pods + API. observedGeneration IS consulted
    for the Deployments (they publish it); the AWX CR, which does not, is
    still never keyed on.

    Returns True if two complete green snapshots are achieved within timeout,
    False otherwise.
    """
    deadline = time.monotonic() + timeout
    snapshot = None

    while time.monotonic() < deadline:
        snapshot = check_launch_ready_snapshot()

        if snapshot["all_ready"]:  # All four signals green
            time.sleep(STABILIZATION_DELAY)
            confirmation = check_launch_ready_snapshot()
            if confirmation["all_ready"]:
                return True
            # Confirmation snapshot regressed — stabilization window reset.
            # Fall through to the normal poll cadence below; the next
            # iteration requires a fresh all-ready snapshot before
            # stabilization is retried.
            snapshot = confirmation

        time.sleep(WAKE_POLL_INTERVAL)

    # Timed out. Log WHY the last snapshot fell short — with the gate now
    # keyed on pods, "the wake never opened" has several distinct causes
    # (still draining, missing pods RBAC, operator wrote replicas back to 0)
    # and they are indistinguishable without this.
    if snapshot is not None:
        audit("wake_gate_timeout", **snapshot)
    return False


def _positive_capacity(value) -> bool:
    """True only for a real, positive numeric capacity.

    JSON booleans must be rejected explicitly: ``isinstance(True, int)`` is
    true in Python, so a body carrying ``"capacity": true`` would otherwise
    read as capacity 1 and pass the gate (#295).
    """
    if isinstance(value, bool):
        return False
    return isinstance(value, (int, float)) and value > 0


def _check_awx_api_ping() -> bool:
    """Check /api/v2/ping/ returns 200 AND the named 'controlplane'
    instance_group reports registered capacity > 0.

    Status-200 alone is insufficient (#295): right after wake, ping can
    return 200 while every instance_group still shows capacity 0 — the
    execution/control instance hasn't sent its first heartbeat yet.
    Reproduced live against AWX 24.6.1.

    Must check the named 'controlplane' group specifically rather than
    any() over all groups — an unrelated instance_group (e.g. an
    execution-node pool) reporting capacity > 0 must not be mistaken for
    the control plane being ready.

    Every decoded layer is validated: a non-object body, a non-list
    ``instance_groups``, or a non-object group entry yields a plain False
    (a normal not-ready snapshot) rather than an escaping AttributeError.
    """
    try:
        req = Request(f"{AWX_API_URL}/api/v2/ping/", method="GET")
        with urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                return False
            body = json.loads(resp.read())
            if not isinstance(body, dict):
                return False
            groups = body.get("instance_groups")
            if not isinstance(groups, list):
                return False
            for group in groups:
                if isinstance(group, dict) and group.get("name") == "controlplane":
                    return _positive_capacity(group.get("capacity"))
            return False  # controlplane group not found
    except (HTTPError, URLError, OSError, ValueError, TypeError, AttributeError):
        return False


def _is_awx_collection(body) -> bool:
    """Validate an AWX paginated-collection response body (#295).

    Key presence is NOT enough. A proxy error page, a partially-initialized
    backend, or a truncated response can decode to JSON whose ``count`` /
    ``results`` are null or the wrong type; such a body cannot support the
    console's lookup semantics and must not count as authenticated-ready.
    Direct probes against the previous key-presence predicate accepted both
    ``{"count": null, "results": null}`` and ``{"count": "five",
    "results": {}}``.

    Requires the real AWX shape: ``count`` a non-boolean int >= 0,
    ``results`` a list, and every element of ``results`` an object.
    """
    if not isinstance(body, dict):
        return False
    count = body.get("count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 0:
        return False
    results = body.get("results")
    if not isinstance(results, list):
        return False
    return all(isinstance(item, dict) for item in results)


def _check_awx_authenticated_ready() -> bool:
    """Best-effort authenticated API read using awx-svc token.

    ping-200 alone proved insufficient (#295, reproduced live against AWX
    24.6.1): the Django backend can still return a bare 500 on endpoints
    that exercise the ORM/RBAC stack for several seconds after ping already
    succeeds and both Deployments report readyReplicas >= 1.

    NOTE: This helper uses awx-svc token, NOT the console's dmf-cms-svc
    identity, and does NOT use the console's exact lookup/launch paths.
    It is a strengthened best-effort gate only. Console-side bounded
    transient retry is the required companion fix (#295).

    The body is fully read, decoded ONCE, and validated against the AWX
    paginated-collection shape (see _is_awx_collection) — status 200 plus
    key presence is not enough.
    """
    try:
        token = _read_secret(AWX_TOKEN_PATH)
        req = Request(
            f"{AWX_API_URL}/api/v2/job_templates/?page_size=1",
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            method="GET",
        )
        with urlopen(req, timeout=5) as resp:
            if resp.status != 200:
                return False
            return _is_awx_collection(json.loads(resp.read()))
    except (HTTPError, URLError, OSError, ValueError, KeyError, TypeError, AttributeError):
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
# AWX API-activity query (idle-reaper, #298)
# ---------------------------------------------------------------------------

# SUPPLEMENTARY, BEST-EFFORT ONLY. This is NOT a fix for the 693 guillotine
# and must not be described as one.
#
# Jobs are not the only way AWX is used: 693-awx-integration.yml drives ~152
# tasks entirely through the REST API and launches nothing, so a job-only
# reaper cannot distinguish it from an empty cluster. The activity stream is
# the nearest available evidence that SOMETHING is driving AWX, so consulting
# it delays a sleep more often than not consulting it. What it cannot do is
# establish that 693 is running:
#
#   * it records MUTATIONS only — a read-only or idempotent stretch of a play
#     produces no entries at all, however long it runs;
#   * this helper authenticates as awx-svc, deliberately NOT a superuser and
#     NOT a system auditor. AWX 24.6.1 filters the activity stream for normal
#     users down to objects they can read (awx/main/access.py), and 693's
#     mutations are made under ADMIN credentials — so most of them are
#     invisible here. Broadening awx-svc to system auditor to "fix" this is
#     explicitly rejected: it trades a scheduling nicety for a real
#     privilege escalation.
#
# So a long play CAN still age past the grace period and be slept mid-run —
# whether it does depends on whether awx-svc happens to keep seeing fresh
# entries, which nothing guarantees.
# Pausing the autoscaler remains the operator workaround. The real fix is an
# explicit, renewable hold on the wake Lease taken by the AWX-consumer phase
# itself (patching the Lease directly, so no helper endpoint and no
# NetworkPolicy widening) — tracked separately, out of scope here.
#
# Direction of failure: this predicate can only ever EXTEND the wake window,
# never shorten it. A degraded outcome either falls back to the job-only
# behaviour that shipped before it (UNAVAILABLE, QUIET) or fails open by
# holding the window open (UNKNOWN, which deliberately does NOT fall back —
# it resets the idle timer and keeps AWX awake). Neither can produce an
# EARLIER sleep than the pre-#298 reaper would have taken.
ACTIVITY_RECENT = "recent"            # a mutation younger than the grace period
ACTIVITY_QUIET = "quiet"              # reachable, newest mutation is older
ACTIVITY_UNAVAILABLE = "unavailable"  # structurally absent (401/403/404)
ACTIVITY_UNKNOWN = "unknown"          # transient failure / unparseable


def _parse_awx_timestamp(raw) -> float | None:
    """Parse an AWX ISO-8601 timestamp to a POSIX float. None if unparseable.

    AWX emits e.g. "2026-07-29T06:54:01.123456Z"; datetime.fromisoformat does
    not accept a bare trailing Z before 3.11, and the container base image is
    pinned to python:3.12-slim, so normalise it rather than depend on that.
    A naive timestamp is read as UTC (AWX stores UTC).
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def query_awx_activity(max_age: float) -> str:
    """Is there a mutation VISIBLE TO awx-svc newer than ``max_age`` seconds?

    Supplementary evidence only — see this section's header comment for what
    this can and cannot establish. A QUIET answer does NOT mean nobody is
    using AWX.

    Returns ACTIVITY_RECENT / ACTIVITY_QUIET / ACTIVITY_UNAVAILABLE /
    ACTIVITY_UNKNOWN.

    Degradation contract (#298):
      - 401/403/404 -> ACTIVITY_UNAVAILABLE. The token cannot read the stream,
        or the endpoint is not there. Callers fall back to TODAY's job-only
        behaviour — never to anything sleepier than today.
      - activity stream administratively disabled -> AWX keeps serving the
        endpoint and simply stops appending, so the newest entry ages out and
        this returns ACTIVITY_QUIET. Same fallback, no special case.
      - transient failure / malformed body -> ACTIVITY_UNKNOWN, which callers
        treat as "not idle".

    ACTIVITY_UNKNOWN is a deliberate fail-open with a REAL COST, not a
    can't-happen case. An endpoint-specific persistent fault — the activity
    stream 500ing or returning a malformed body while /api/v2/unified_jobs/
    stays perfectly healthy — yields UNKNOWN on every poll and keeps AWX
    awake indefinitely. query_awx_work() does NOT rescue this: it queries a
    different endpoint and would keep answering WORK_IDLE throughout. We take
    that cost because the alternative (treat "cannot tell" as "idle") sleeps
    AWX out from under work we simply failed to observe. The audit trail is
    activity_stream_error / activity_stream_malformed on every poll, so a
    wedged reaper is diagnosable from the log rather than silent.

    A clock skew that puts the newest entry in the future yields a negative
    age, which reads as RECENT — the safe direction (stay awake).

    This helper's own polling cannot self-perpetuate: the activity stream
    records create/update/delete, and every call the helper makes is a GET.
    """
    url = (f"{AWX_API_URL}/api/v2/activity_stream/"
           "?order_by=-timestamp&page_size=1")
    try:
        token = _read_secret(AWX_TOKEN_PATH)
        req = Request(
            url,
            headers={"Authorization": f"Bearer {token}",
                     "Accept": "application/json"},
            method="GET",
        )
        with urlopen(req, timeout=10) as resp:
            if resp.status != 200:
                audit("activity_stream_unexpected_status", status=resp.status)
                return ACTIVITY_UNKNOWN
            body = json.loads(resp.read())
    except HTTPError as e:
        if e.code in (401, 403, 404):
            audit("activity_stream_unavailable", status=e.code,
                  action="fall_back_to_job_only")
            return ACTIVITY_UNAVAILABLE
        audit("activity_stream_error", status=e.code, error=str(e))
        return ACTIVITY_UNKNOWN
    except (URLError, OSError, ValueError, TypeError) as e:
        audit("activity_stream_error", error=str(e))
        return ACTIVITY_UNKNOWN

    if not _is_awx_collection(body):
        audit("activity_stream_malformed")
        return ACTIVITY_UNKNOWN

    results = body["results"]
    if not results:
        return ACTIVITY_QUIET

    stamp = _parse_awx_timestamp(results[0].get("timestamp"))
    if stamp is None:
        audit("activity_stream_unparseable_timestamp",
              raw=results[0].get("timestamp"))
        return ACTIVITY_UNKNOWN

    age = time.time() - stamp
    if age < max_age:
        audit("api_activity_found", age_seconds=round(age, 1))
        return ACTIVITY_RECENT
    return ACTIVITY_QUIET


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
            # Rewrite (not merely extend) the lease floor to
            # ready + GRACE_PERIOD now that readiness is known — replaces
            # the acquire-time startup floor rather than only ever
            # extending past it (#312: the old max()-based write pinned
            # every wake awake for ~MAX_STARTUP_WAIT regardless of how
            # fast readiness actually arrived). allow_owner_shrink is safe
            # here specifically: this call chain is provably still the
            # holder from the acquire above — an unexpired lease can only
            # be rewritten by its own holder (create_or_update's owner-only
            # guard applies identically with the flag set).
            requested_min_awake = time.time() + GRACE_PERIOD
            persisted_lease, _ = _lease.create_or_update(
                requested_min_awake, holder_id, allow_owner_shrink=True)
            persisted_min_awake = LeaseManager._annotation_min_awake(persisted_lease)
            audit("ensure_awake_ready",
                  min_awake_until=persisted_min_awake,
                  requested_min_awake_until=requested_min_awake,
                  holder=holder_id)
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
      2. No awx-svc-VISIBLE AWX API mutation in the last grace period either
         (activity stream, #298) — supplementary evidence that delays a sleep,
         NOT a guarantee that nothing is driving AWX. See the ACTIVITY_*
         section header: a REST-only play can still be slept mid-run, and
         pausing the autoscaler remains the operator workaround for 693.
      3. now > min_awake_until (the wake lease has expired).
      4. Idle time exceeds the grace period.

    If the AWX API cannot be queried, DO NOT sleep (fail-open).
    """
    audit("reaper_started", interval=REAPER_POLL_INTERVAL, grace_period=GRACE_PERIOD)
    idle_since: float | None = None
    # #312: the min_awake_until floor last audited as "parked" — None
    # when not currently parked. Lets the parked branch below emit exactly
    # one reaper_window_parked event per park (on the transition in, or
    # when the floor itself changes) instead of one every poll.
    parked_until: float | None = None

    while True:
        time.sleep(REAPER_POLL_INTERVAL)
        try:
            now = time.time()
            min_awake = _lease.get_min_awake_until()

            # Within the wake lease window — don't even check.
            if now < min_awake:
                if parked_until is None or parked_until != min_awake:
                    audit("reaper_window_parked", min_awake_until=min_awake,
                          remaining=round(min_awake - now, 1))
                    parked_until = min_awake
                idle_since = None
                continue

            # Floor no longer holds us back — clear the sentinel BEFORE
            # querying AWX (#312), so a query made this iteration is never
            # attributed to a stale parked state.
            if parked_until is not None:
                audit("reaper_window_expired", min_awake_until=parked_until)
                parked_until = None

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

            # WORK_IDLE: AWX reachable, no active JOBS, past the lease window.
            # Before acting on that, look for supplementary evidence that
            # something is driving the AWX API (#298). This DELAYS a sleep
            # when a mutation happens to be visible to awx-svc; it does not
            # establish that nothing is running when it comes back QUIET.
            activity = query_awx_activity(GRACE_PERIOD)

            if activity == ACTIVITY_RECENT:
                # Identical treatment to WORK_ACTIVE: AWX is in use, keep it
                # awake and hold the wake window open. Extending the Lease is
                # safe here (unlike the UNREACHABLE path, issue #103) — we only
                # reach this branch because the AWX API answered, so AWX is
                # awake and an /ensure-awake caller that waits on this Lease
                # will be satisfied rather than starved.
                new_min_awake = now + GRACE_PERIOD
                _lease.create_or_update(
                    new_min_awake, f"reaper-{os.getpid()}")
                idle_since = None
                continue

            if activity == ACTIVITY_UNKNOWN:
                # Could not tell. Do NOT sleep, and do NOT extend the Lease —
                # the same #103-safe shape as WORK_UNREACHABLE.
                #
                # This is a deliberate fail-open with a real cost: an
                # activity-stream-specific persistent fault (500s, or a
                # malformed body) alongside a perfectly healthy
                # /api/v2/unified_jobs/ yields UNKNOWN every poll and keeps
                # AWX awake indefinitely. query_awx_work() does not rescue it
                # — different endpoint, still answering IDLE. Accepted over
                # the alternative, which sleeps AWX out from under work we
                # merely failed to observe. Diagnosable from the repeated
                # activity_stream_error / activity_stream_malformed audit
                # lines rather than silent.
                idle_since = None
                continue

            # ACTIVITY_QUIET or ACTIVITY_UNAVAILABLE: proceed on the job-only
            # evidence, exactly as before this change. QUIET is NOT proof of
            # an idle AWX — only that nothing awx-svc can see has changed
            # recently.
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
