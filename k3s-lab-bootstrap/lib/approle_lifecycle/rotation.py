"""Registry-driven rotation transaction for one runtime AppRole target, and a
sweep over every runtime target.

One run follows the six steps of the "Safe rotation transaction" section of
`docs/plans/openbao-approle-credential-lifecycle-plan.md` in the dmfdeploy
umbrella repository: read, stage a candidate, issue it, deliver it, wait for
readiness, promote. A run takes a registry target, the audit context (actor,
role, request id, reason) and clients. The request id is the run id. All I/O
goes through the clients listed on `RotationClients`, so the same code runs in
a playbook, a job or a test. This module has no transport.

Concurrency protocol: a Lease with a lock delay. What it guarantees, and what
it assumes.

A request that is already in flight cannot be fenced at the server, and a run
can stall between two steps. So the engine does not rely on one more check
before each request. It relies on time bounds that it states and checks:

- Assumption 1, bounded requests. Every client call fails on the client side
  after `request_timeout` seconds (`MAX_REQUEST_SECONDS` by default). Each
  client declares that bound as its `request_timeout` attribute, and a run
  refuses a client whose bound is larger than the config's. The protocol also
  counts on the servers bounding a request (OpenBao `max_request_duration`,
  the Kubernetes API server's request timeout). The caller that wires the
  real clients validates both bounds on the sandbox cluster.
- Assumption 2, bounded clock skew and pauses. The clocks of the runs differ by
  at most `clock_skew_margin` seconds (`CLOCK_SKEW_MARGIN_SECONDS` by default),
  and a run does not pause for longer than that between a check and the
  request it guards.
- Unanswered calls. A client call that ends without a server response (a
  timeout or a connection error, as opposed to an `ApiError` or another
  answer) can still run on the server. A run that had one does not delete its
  Lease at release, on any exit path. The Lease's own calls count too,
  including a renewal. The Lease stays until it expires, so a
  successor acts only after `renew_time + duration + lock_delay`, the same as
  after a lost process. Nobody deletes the Lease by hand while a run is in
  flight: that is the one more assumption here.
- Self-stop. After every client call returns, the run checks that its Lease is
  still valid with the margin: `now < renew_time + duration - clock_skew_margin`.
  If not, it makes no further write and fails with `lease_conflict`. It also
  records nothing about that failure, because the successor owns the state.
- Lock delay. An expired Lease can be taken over only when
  `now >= renew_time + duration + lock_delay`, and `lock_delay` must be at
  least `request_timeout + clock_skew_margin`. A config that breaks this is
  refused before any client call.

Guarantee: under assumptions 1 and 2, no run acts after a successor has
started. A request the old holder sent while its Lease was valid has finished
or timed out before a successor may take over, and a call that returns late
leads to no further write. The in-flight interleavings of a stalled
read-then-write and of a create delayed across a takeover therefore cannot
occur. Nothing more is claimed. A server that does not bound a request, a
clock that drifts by more than the margin, or a pause longer than the margin
is outside the guarantee. The conditional destination writes and the abort that
resolves by lookup limit the damage there, and do not remove it.

States, persisted in the transaction record (`transaction_path`):

    pending-create               intent and staging written; no confirmed SecretID yet
    issued-pending-delivery      OpenBao holds the candidate, its accessor is recorded,
                                 and no destination carries the transaction marker
    delivered-pending-readiness  at least one destination carries the marker
    active                       every destination is marked and every gate passed
    aborted                      safe abort: no destination ever carried the marker

The rules that keep a consumer from being stranded:

- The candidate comes from a CSPRNG and is staged before `custom-secret-id`.
  One candidate exists per pending transaction. A lost or duplicate create
  response resolves by looking up that same candidate.
- No destination is written before the state is `issued-pending-delivery` with
  the accessor recorded.
- Once any destination carries the marker, the replacement is never destroyed
  automatically. Only an abort that finds every destination unmarked destroys it.
- The previous accessor is destroyed and the staging copy deleted only after
  every destination is marked and every gate passed.
- A Lease cannot stop a write that is already in flight, so the engine fences.
  Before each mutating step it confirms and renews the Lease. Each delivery is
  conditional on the destination's version, read just before the write. A safe
  abort first writes to every destination to change its version, then reads
  the markers again, and only then destroys the replacement: a stale delivery
  fails instead of installing a destroyed SecretID.
- The recorded issue time is never later than the real one: it comes from
  `creation_time` and `expiration_time` of `secret-id/lookup` when both are
  well formed, and otherwise from a time persisted before the first create.
  A staged candidate with less than the critical share of its TTL left is not
  delivered: it is aborted when no destination is marked, and otherwise fails
  with reason code `delivery_failed` and the cause `credential_expiring`, which
  only the transaction record names.
- The success heartbeat (`last_successful_rotation_at` and, for a scheduled run,
  `last_scheduled_success_at`) moves only after the previous accessor is
  destroyed and staging is deleted. A later run that finishes a failed cleanup
  stamps it then.
- No SecretID, accessor, token or staging value appears in an exception message,
  a log record, a result or a reason code. The random `secret-id` endpoint and
  accessor listing are not part of the client interfaces.

Readiness uses `adapters.evaluate_target` unless the config supplies another
evaluator. That function never accepts a target yet (the registry
status `missing` for `fresh_authentication_proof`), so with the default a
transaction stops at `delivered-pending-readiness` and keeps its replacement.
A later change that defines the proof supplies it.

Metadata v1 has no field for the transaction id, state, staging path or the
pending accessor. The engine keeps them in its own record at
`transaction_path` and writes the v1 record in its documented shape.
"""

import logging
import re
import secrets
from datetime import datetime, timezone
from dataclasses import dataclass, field
from typing import Callable, Optional, Protocol

from . import adapters, registry

LOG = logging.getLogger("approle_lifecycle.rotation")

TRANSACTION_SCHEMA_VERSION = 1
TRANSACTION_PREFIX = "secret/data/platform/approle-rotation-transactions/"
STAGING_PREFIX = "secret/data/platform/approle-rotation-staging/"
MAX_REQUEST_SECONDS = 60
CLOCK_SKEW_MARGIN_SECONDS = 30
SCHEDULER_ROLE = "approle-rotation-scheduler"
LEASE_NAME = "approle-rotation"
# A continuously used consumer logs in many times, so the SecretID has no use
# limit. The TTL comes from the registry.
SECRET_ID_NUM_USES = 0
# An abort destroys what a lookup shows, then looks again; a candidate that
# keeps coming back after this many destroys is reported, not chased.
ABORT_RESOLVE_ATTEMPTS = 3

PENDING_CREATE = "pending-create"
ISSUED_PENDING_DELIVERY = "issued-pending-delivery"
DELIVERED_PENDING_READINESS = "delivered-pending-readiness"
ACTIVE = "active"
ABORTED = "aborted"
PENDING_STATES = (PENDING_CREATE, ISSUED_PENDING_DELIVERY, DELIVERED_PENDING_READINESS)
STATES = PENDING_STATES + (ACTIVE, ABORTED)

REASON_CODES = {
    "create_failed", "lookup_failed", "delivery_failed", "readiness_failed",
    "cleanup_failed", "metadata_failed", "lease_conflict", "invalid_state",
}
# Precise causes that the v1 record cannot name. They go in the transaction
# record only, beside the v1 reason code that stands for them.
CAUSES = {"credential_expiring"}

_IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_FIELD_LIMIT = 256


class RotationError(RuntimeError):
    """A run failed closed. `reason_code` is one of REASON_CODES, `cause` is
    empty or one of CAUSES, and the message is fixed text that never carries a
    credential."""

    def __init__(self, reason_code, message, cause=""):
        super().__init__(message)
        self.reason_code = reason_code
        self.cause = cause


class AlreadyRegistered(Exception):
    """`custom-secret-id` reports that the candidate is already registered."""


class StagingExists(Exception):
    """The staging record already exists."""


class LeaseExists(Exception):
    """`create_lease` found a Lease with that name."""


class LeaseConflict(Exception):
    """`update_lease` or `delete_lease` saw another resource version."""


class ApiError(Exception):
    """A client call got an HTTP or API error response from the server, so the
    server answered. Any other exception from a client call (a timeout or a
    connection error) leaves the outcome unknown: the request can still run
    on the server."""


# Exceptions that show the server answered. Every other exception, including
# a BaseException, marks the call as unanswered.
ANSWERED = (ApiError, AlreadyRegistered, StagingExists, LeaseExists, LeaseConflict)


class OpenBaoClient(Protocol):
    """Methods the engine calls on `RotationClients.openbao`. Destination
    adapters also call `read_kv2` and `write_kv2` on this client.

    read_role(approle_name) -> None or a dict with `secret_id_ttl` (seconds)
    lookup_secret_id(approle_name, secret_id) -> None when OpenBao does not
        know the SecretID, else a dict with `secret_id_accessor`,
        `secret_id_ttl` and `secret_id_num_uses`
        (`secret-id/lookup`; never the random `secret-id` endpoint)
    create_custom_secret_id(approle_name, secret_id, params) -> a dict with
        the same three keys; raises AlreadyRegistered when the candidate exists
        (`custom-secret-id`; `params` holds `ttl` and `num_uses`)
    destroy_secret_id_accessor(approle_name, accessor) -> None; returns
        normally when the accessor is already gone
    read_kv2(path) -> None or (data, version)
    write_kv2(path, data, cas) -> None; fails unless the version equals `cas`
    create_staging(path, data) -> None; raises StagingExists
    read_staging(path) -> None or the dict that was staged
    delete_staging(path) -> None; returns normally when the record is gone
    """


class KubernetesClient(Protocol):
    """Methods the engine calls on `RotationClients.kubernetes`. Destination
    and gate adapters call the methods that `adapters.py` lists.

    get_lease(namespace, name) -> None or a dict with `holder`, `renew_time`
        (Unix seconds), `duration_seconds` and `resource_version`
    create_lease(namespace, name, holder, renew_time, duration_seconds) ->
        None; raises LeaseExists
    update_lease(namespace, name, holder, renew_time, duration_seconds,
        resource_version) -> None; raises LeaseConflict when the resource
        version changed
    delete_lease(namespace, name, resource_version) -> None; raises
        LeaseConflict when the resource version changed
    """


@dataclass
class RotationClients:
    kubernetes: object
    openbao: object


@dataclass
class RotationConfig:
    """`abort_after` is the age in seconds after which a transaction that no
    destination carries is aborted safely. `lease_duration` is how long one
    holder's Lease stays live without a renewal. `request_timeout`,
    `clock_skew_margin` and `lock_delay` are the protocol's bounds (see the
    module docstring); `lock_delay` defaults to the least allowed value."""

    lease_namespace: str
    lease_duration: int = 900
    abort_after: int = 6 * 3600
    request_timeout: int = MAX_REQUEST_SECONDS
    clock_skew_margin: int = CLOCK_SKEW_MARGIN_SECONDS
    lock_delay: Optional[int] = None
    inventory: dict = field(default_factory=dict)
    evaluate: Callable = adapters.evaluate_target
    token_source: Callable = lambda: secrets.token_urlsafe(32)
    holder_source: Callable = lambda: secrets.token_hex(8)
    reconcile: Optional[Callable] = None

    def __post_init__(self):
        if self.lock_delay is None:
            self.lock_delay = self.request_timeout + self.clock_skew_margin


@dataclass(frozen=True)
class RunContext:
    """The audit context of a run. `request_id` is the run id."""

    actor: str
    role: str
    request_id: str
    reason: str

    @property
    def kind(self):
        return "scheduled" if self.role == SCHEDULER_ROLE else "manual"


@dataclass(frozen=True)
class TargetResult:
    """Non-secret outcome of one target. `outcome` is `rotated`, `not_due` or
    `failed`; `reason_code` is empty unless the target failed."""

    target: str
    outcome: str
    state: str
    reason_code: str
    transaction_id: str
    cause: str = ""


@dataclass(frozen=True)
class SweepResult:
    results: tuple

    @property
    def failed(self):
        return tuple(r for r in self.results if r.outcome == "failed")


def validate_context(context):
    """Raise RotationError unless the audit context is complete and well formed.
    Nothing has been read or written yet."""
    if not isinstance(context, RunContext):
        raise RotationError("invalid_state", "the run context is missing")
    if not isinstance(context.request_id, str) or not _IDENTIFIER.fullmatch(context.request_id):
        raise RotationError("invalid_state", "the request id is missing or malformed")
    for name in ("actor", "role", "reason"):
        value = getattr(context, name)
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value) > _FIELD_LIMIT
            or any(ord(c) < 32 or ord(c) == 127 for c in value)
        ):
            raise RotationError("invalid_state", f"the run context {name} is missing or malformed")


_EXPIRING = "the staged SecretID is expired or about to expire"


def _positive(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def validate_config(config, clients):
    """Raise RotationError unless the protocol's bounds hold. Nothing has been
    read or written yet. Reads only attributes, never calls a client."""
    numbers = (config.request_timeout, config.clock_skew_margin, config.lock_delay, config.lease_duration, config.abort_after)
    if not all(_positive(n) for n in numbers):
        raise RotationError("invalid_state", "the timing config must hold positive whole seconds")
    if config.lock_delay < config.request_timeout + config.clock_skew_margin:
        raise RotationError("invalid_state", "lock_delay is shorter than request_timeout plus clock_skew_margin")
    if config.lease_duration <= config.clock_skew_margin:
        raise RotationError("invalid_state", "lease_duration is not longer than clock_skew_margin")
    for client in (clients.kubernetes, clients.openbao):
        declared = getattr(client, "request_timeout", None)
        if not _positive(declared) or declared > config.request_timeout:
            raise RotationError("invalid_state", "a client does not declare a request timeout within the config's")


def transaction_path(entry):
    return TRANSACTION_PREFIX + entry["approle_name"]


def staging_path(entry):
    return STAGING_PREFIX + entry["approle_name"]


def _call(code, message, function, *args, expect=()):
    """Call a client method so that nothing it raises carries a credential.

    An `Exception` becomes a RotationError with a fixed message. An exception
    class in `expect` becomes a fresh instance with no arguments. A
    `BaseException` such as KeyboardInterrupt becomes a fresh instance of its
    class. Each is raised after the handler, so none has a cause or context."""
    outcome = None
    passed_on = None
    try:
        return function(*args)
    except expect as caught:
        outcome = type(caught)
    except RotationError as caught:
        passed_on = caught  # a fixed-text error from the engine itself, such as the self-stop
    except Exception:
        outcome = RotationError
    except BaseException as caught:
        outcome = type(caught)
    if passed_on is not None:
        raise passed_on
    if outcome is RotationError:
        raise RotationError(code, message)
    raise outcome()


def _epoch(text):
    """Unix seconds from an RFC 3339 timestamp, or None when it does not parse."""
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp())
    except (AttributeError, ValueError):
        return None


def _exact(value, expected):
    if isinstance(value, bool):
        return False
    return value == expected if isinstance(value, int) else value == str(expected)


# ---- Lease -----------------------------------------------------------------

def _may_delete_lease(lease):
    """The release rule. A request that ended without a server response can
    still run on the server within `request_timeout`. The Lease and its lock
    delay are what hold a successor back for that time, so a run that had an
    unanswered call leaves the Lease in place and a successor waits for expiry
    plus the lock delay."""
    return not lease.unanswered_call


class _Lease:
    """One Kubernetes Lease for the whole lifecycle.

    Rules. A run creates the Lease when none exists. It takes over another
    holder's Lease only when `now >= renew_time + duration_seconds + lock_delay`,
    with a conditional update on the resource version, so two takers cannot
    both win. Between expiry and expiry plus the lock delay a new run refuses
    with `lease_conflict`: that gives every request the old holder sent time to
    finish or time out. Holders are unique per process (run id plus a nonce),
    so a retry of a crashed run waits like any other run.

    The holder tracks `valid_until`, the end of the last Lease period measured
    from the time before it asked for it. `stop_if_stale` is the self-stop:
    the run calls it after every client call. `renew` runs before every
    mutating step and refuses when the Lease is another holder's or has run
    out. Release is a conditional delete, and only while the Lease is still
    this holder's."""

    def __init__(self, kube, config, context, now):
        self.kube = _Checked(kube, self, self_stop=False)
        self.namespace = config.lease_namespace
        self.duration = config.lease_duration
        self.lock_delay = config.lock_delay
        self.skew = config.clock_skew_margin
        self.holder = f"{context.request_id}:{config.holder_source()}"
        self.now = now
        self.valid_until = None
        self.unanswered_call = False

    def acquire(self):
        current = _call("lease_conflict", "the Lease could not be read", self.kube.get_lease, self.namespace, LEASE_NAME)
        now = self.now()
        if current is None:
            try:
                _call(
                    "lease_conflict", "the Lease could not be created", self.kube.create_lease,
                    self.namespace, LEASE_NAME, self.holder, now, self.duration, expect=(LeaseExists,),
                )
            except LeaseExists:
                raise RotationError("lease_conflict", "another run holds the Lease") from None
            self.valid_until = now + self.duration
        elif current["renew_time"] + current["duration_seconds"] + self.lock_delay > now:
            raise RotationError("lease_conflict", "another run holds the Lease or its lock delay has not ended")
        else:
            self._update(now, current["resource_version"])

    def stop_if_stale(self):
        """The self-stop: raise `lease_conflict` unless the Lease is still
        valid with the clock skew margin."""
        if self.valid_until is None or self.now() >= self.valid_until - self.skew:
            raise RotationError("lease_conflict", "the Lease is no longer valid with margin")

    def renew(self):
        current = _call("lease_conflict", "the Lease could not be read", self.kube.get_lease, self.namespace, LEASE_NAME)
        if current is None or current["holder"] != self.holder:
            raise RotationError("lease_conflict", "the run lost the Lease")
        now = self.now()
        if current["renew_time"] + current["duration_seconds"] - self.skew <= now:
            raise RotationError("lease_conflict", "the Lease expired while the run was working")
        self._update(now, current["resource_version"])

    def _update(self, now, resource_version):
        try:
            _call(
                "lease_conflict", "the Lease could not be updated", self.kube.update_lease,
                self.namespace, LEASE_NAME, self.holder, now, self.duration, resource_version,
                expect=(LeaseConflict,),
            )
        except LeaseConflict:
            raise RotationError("lease_conflict", "another run took the Lease") from None
        self.valid_until = now + self.duration

    def release(self):
        """Delete the Lease while it still names this holder, unless a call in
        the run went unanswered. A failure is logged, and the Lease then
        expires on its own."""
        if not _may_delete_lease(self):
            LOG.warning("lease kept after an unanswered call; it expires on its own")
            return
        try:
            current = _call("lease_conflict", "the Lease could not be read", self.kube.get_lease, self.namespace, LEASE_NAME)
            if current is None or current["holder"] != self.holder:
                return
            _call(
                "lease_conflict", "the Lease could not be released", self.kube.delete_lease,
                self.namespace, LEASE_NAME, current["resource_version"], expect=(LeaseConflict,),
            )
        except (RotationError, LeaseConflict):
            LOG.warning("lease not released; it expires on its own")


# ---- Transaction record -----------------------------------------------------

class _Transaction:
    """The persisted, non-secret transaction record of one target."""

    _KEYS = {
        "schema_version", "target", "transaction_id", "state", "target_version",
        "staging_path", "new_accessor", "previous_accessor", "started_at",
        "create_attempted_at", "issued_at",
        "delivered_at", "cleanup_done", "request_id",
        "last_failure_at", "last_failure_reason_code", "last_failure_cause",
    }

    def __init__(self, bao, path, data, version):
        self.bao = bao
        self.path = path
        self.data = data
        self.version = version

    @classmethod
    def load(cls, bao, entry):
        path = transaction_path(entry)
        current = _call("lookup_failed", "the transaction record could not be read", bao.read_kv2, path)
        if current is None:
            return cls(bao, path, None, 0)
        data, version = current
        if (
            not isinstance(data, dict)
            or set(data) != cls._KEYS
            or data["schema_version"] != TRANSACTION_SCHEMA_VERSION
            or data["state"] not in STATES
            or data["target"] != entry["approle_name"]
            or not isinstance(data["transaction_id"], str)
        ):
            raise RotationError("invalid_state", "the transaction record is malformed")
        return cls(bao, path, data, version)

    def pending(self):
        return self.data is not None and self.data["state"] in PENDING_STATES

    def write(self, data):
        _call("metadata_failed", "the transaction record could not be written", self.bao.write_kv2, self.path, data, self.version)
        self.data = data
        self.version += 1

    def update(self, **changes):
        self.write({**self.data, **changes})


def _new_transaction(entry, context, target_version, now):
    return {
        "schema_version": TRANSACTION_SCHEMA_VERSION,
        "target": entry["approle_name"],
        "transaction_id": f"{context.request_id}-v{target_version}",
        "state": PENDING_CREATE,
        "target_version": target_version,
        "staging_path": staging_path(entry),
        "new_accessor": None,
        "previous_accessor": None,
        "started_at": now,
        "create_attempted_at": None,
        "issued_at": None,
        "delivered_at": None,
        "cleanup_done": False,
        "request_id": context.request_id,
        "last_failure_at": None,
        "last_failure_reason_code": None,
        "last_failure_cause": None,
    }


# ---- One target -------------------------------------------------------------

class _Checked:
    """Wrap a client so that every call is followed by the self-stop: a call
    that returns after the Lease lapsed (with margin) leads to no further
    write. A call that raises something other than a server answer marks the
    Lease `unanswered_call`. The Lease reaches its own client through this
    class with `self_stop=False`: its calls are marked in the same way, and
    they must not run the self-stop, because `acquire` runs before the Lease
    is valid. Only methods are reached through it."""

    def __init__(self, client, lease, self_stop=True):
        self._client = client
        self._lease = lease
        self._self_stop = self_stop

    def __getattr__(self, name):
        attribute = getattr(self._client, name)

        def call(*args, **kwargs):
            try:
                result = attribute(*args, **kwargs)
            except ANSWERED:
                raise
            except BaseException:
                self._lease.unanswered_call = True
                raise
            if self._self_stop:
                self._lease.stop_if_stale()
            return result

        return call


class _TargetRun:
    def __init__(self, target, entry, context, clients, config, now, lease):
        self.target = target
        self.lease = lease
        self.entry = entry
        self.context = context
        self.bao = _Checked(clients.openbao, lease)
        self.config = config
        self.now = now
        self.approle = entry["approle_name"]
        self.limits = registry.thresholds(entry)
        self.destinations, self.gates = adapters.adapters_for(
            entry, {"kubernetes": _Checked(clients.kubernetes, lease), "openbao": self.bao}
        )
        self.main_path = entry["metadata_path"]
        self.tx = None
        self.main = None
        self.main_version = 0

    def _fence(self):
        """Confirm and renew the Lease before a mutating step. Raises
        `lease_conflict` when the run no longer holds it."""
        self.lease.renew()

    # -- entry point
    def run(self):
        try:
            return self._run()
        except RotationError as error:
            # A run that lost its Lease records nothing: the successor owns the state.
            if error.reason_code != "lease_conflict":
                self._record_failure(error.reason_code, error.cause)
            return self._result("failed", error.reason_code, error.cause)

    def _result(self, outcome, reason="", cause=""):
        data = self.tx.data if self.tx is not None and self.tx.data else None
        return TargetResult(
            self.target, outcome,
            data["state"] if data else "",
            reason,
            data["transaction_id"] if data else "",
            cause,
        )

    def _run(self):
        self._read_role()
        self._load_main()
        self.tx = _Transaction.load(self.bao, self.entry)
        self._finish_leftovers()
        if not self.tx.pending():
            if not self._due():
                return self._result("not_due")
            self._begin()
        state = self.tx.data["state"]
        LOG.info("target %s transaction %s resumes at %s", self.target, self.tx.data["transaction_id"], state)
        if state == PENDING_CREATE:
            self._issue()
            state = ISSUED_PENDING_DELIVERY
        if state == ISSUED_PENDING_DELIVERY:
            self._abort_if_safe()
            self._classify()
        self._check_remaining()
        self._reconcile_and_accept()
        self._promote()
        return self._result("rotated")

    # -- read
    def _read_role(self):
        role = _call("lookup_failed", "the AppRole could not be read", self.bao.read_role, self.approle)
        if not isinstance(role, dict):
            raise RotationError("invalid_state", "the AppRole is unknown")
        if not _exact(role.get("secret_id_ttl"), self.limits.ttl):
            raise RotationError("invalid_state", "the AppRole TTL differs from the registry")

    def _load_main(self):
        current = _call("lookup_failed", "the metadata could not be read", self.bao.read_kv2, self.main_path)
        if current is not None:
            self.main, self.main_version = current

    def _due(self):
        main = self.main
        if main is None or not main.get("secret_id_accessor"):
            return True  # a target with no prior accessor
        issued, expires = main.get("issued_at"), main.get("expires_at")
        if not isinstance(issued, int) or not isinstance(expires, int):
            raise RotationError("invalid_state", "the metadata has no usable issue or expiry time")
        now = self.now()
        if expires <= now:
            raise RotationError("invalid_state", "the SecretID expired and no recovery state exists")
        return now - issued >= self.limits.rotation_due_after

    def _finish_leftovers(self):
        """Complete the cleanup that an earlier run left behind."""
        data = self.tx.data
        if data is None:
            return
        if data["state"] == ACTIVE and not data["cleanup_done"]:
            self._cleanup(data["previous_accessor"])
        elif data["state"] == ABORTED:
            self._finish_abort()

    # -- intent and staging
    def _begin(self):
        version = (self.main or {}).get("target_version") or 0
        data = _new_transaction(self.entry, self.context, version + 1, self.now())
        data["previous_accessor"] = (self.main or {}).get("secret_id_accessor") or None
        self.tx.write(data)

    def _staged(self):
        staged = _call("lookup_failed", "the staging record could not be read", self.bao.read_staging, staging_path(self.entry))
        if staged is None:
            return None
        if (
            not isinstance(staged, dict)
            or staged.get("transaction_id") != self.tx.data["transaction_id"]
            or not isinstance(staged.get("secret_id"), str)
            or not staged["secret_id"]
        ):
            raise RotationError("invalid_state", "the staging record does not belong to this transaction")
        return staged["secret_id"]

    def _stage_candidate(self):
        candidate = self._staged()
        if candidate is not None:
            return candidate
        candidate = self.config.token_source()
        self._fence()
        try:
            _call(
                "create_failed", "the candidate could not be staged", self.bao.create_staging,
                staging_path(self.entry),
                {"transaction_id": self.tx.data["transaction_id"], "secret_id": candidate},
                expect=(StagingExists,),
            )
        except StagingExists:
            raise RotationError("invalid_state", "a staging record already exists") from None
        return candidate

    # -- look up or create the same candidate
    def _bounds_ok(self, found):
        return (
            isinstance(found, dict)
            and found.get("secret_id_accessor")
            and _exact(found.get("secret_id_ttl"), self.limits.ttl)
            and _exact(found.get("secret_id_num_uses"), SECRET_ID_NUM_USES)
        )

    def _lookup(self, candidate):
        return _call(
            "lookup_failed", "the candidate could not be looked up",
            self.bao.lookup_secret_id, self.approle, candidate,
        )

    def _issue(self):
        candidate = self._stage_candidate()
        found = self._lookup(candidate)
        if found is None:
            if self.tx.data["create_attempted_at"] is None:
                # Earlier than the real creation, so a recovered expiry errs early.
                self.tx.update(create_attempted_at=self.now())
            self._fence()
            params = {"ttl": f"{self.limits.ttl}s", "num_uses": SECRET_ID_NUM_USES}
            try:
                found = _call(
                    "create_failed", "the create call failed", self.bao.create_custom_secret_id,
                    self.approle, candidate, params, expect=(AlreadyRegistered,),
                )
            except (RotationError, AlreadyRegistered) as error:
                if isinstance(error, RotationError) and error.reason_code == "lease_conflict":
                    raise  # the Lease lapsed: no further call, not even the lookup
                # A lost response or a duplicate create: resolve by looking up
                # the same candidate. Never create a second one.
                found = self._lookup(candidate)
                if found is None:
                    raise RotationError("create_failed", "the create call failed and the candidate is not registered") from None
        if not self._bounds_ok(found):
            raise RotationError("invalid_state", "OpenBao did not confirm the candidate's accessor and bounds")
        self.tx.update(
            state=ISSUED_PENDING_DELIVERY,
            new_accessor=found["secret_id_accessor"],
            issued_at=self._issued_at(found),
        )

    def _issued_at(self, found):
        """The issuance time to record, never later than the real one.

        `secret-id/lookup` returns `creation_time` and `expiration_time`. When
        both parse, are not in the future and are in order, the expiry is the
        earlier of the reported one and creation plus the TTL, and the issue
        time is that expiry minus the TTL. Otherwise the time persisted before
        the first create call is used, which is earlier than the creation."""
        created, expires = _epoch(found.get("creation_time")), _epoch(found.get("expiration_time"))
        if created is not None and expires is not None and created <= self.now() and created < expires:
            return min(expires, created + self.limits.ttl) - self.limits.ttl
        return self.tx.data["create_attempted_at"] or self.tx.data["started_at"]

    # -- steps 4 and 5: deliver and classify
    def _markers(self):
        return [self._guard_adapter(d.has_marker, self.tx.data["transaction_id"]) for d in self.destinations]

    def _guard_adapter(self, function, *args):
        try:
            return function(*args)
        except adapters.AdapterError as error:
            self.lease.stop_if_stale()  # a lapsed Lease outranks the adapter's own reason
            raise RotationError(error.reason_code, "a destination call failed") from None

    def _abort_if_safe(self):
        """Abort when the transaction is too old and no destination carries it.
        A destination that carries it makes the transaction delivered instead.
        An abort raises, so it ends the run."""
        if self.now() - self.tx.data["started_at"] < self.config.abort_after:
            return
        self._abort("delivery_failed", "the transaction was aborted; no destination received it")

    def _check_remaining(self):
        """Do not deliver a candidate that is expired or too close to expiry.

        The threshold is the critical alert level of the registry, a tenth of
        the TTL by default: a SecretID with less left would alert at once.
        With no destination marked the candidate is aborted safely and a later
        run starts a new transaction. With a marked destination the run fails
        with the v1 reason code `delivery_failed`, the cause
        `credential_expiring` and keeps the replacement."""
        remaining = self.tx.data["issued_at"] + self.limits.ttl - self.now()
        if remaining >= self.limits.critical_remaining:
            return
        if self.tx.data["state"] == ISSUED_PENDING_DELIVERY:
            self._abort("delivery_failed", _EXPIRING, "credential_expiring")
        raise RotationError("delivery_failed", _EXPIRING, "credential_expiring")

    def _mark_delivered(self):
        self.tx.update(state=DELIVERED_PENDING_READINESS, delivered_at=self.now())

    def _classify(self):
        """Any destination with the marker makes the transaction delivered."""
        if any(self._markers()):
            self._mark_delivered()

    def _deliver_missing(self):
        marks = self._markers()
        if all(marks):
            return
        secret_id = self._staged()
        if secret_id is None:
            raise RotationError("invalid_state", "the staged SecretID is missing")
        data = self.tx.data
        for destination, marked in zip(self.destinations, marks):
            if marked:
                continue
            self._fence()
            self._guard_adapter(destination.deliver, secret_id, data["transaction_id"], data["target_version"])
            self._mark_delivered()
        if not all(self._markers()):
            raise RotationError("delivery_failed", "a destination does not carry the transaction marker")

    def _reconcile_and_accept(self):
        self._deliver_missing()
        if self.config.reconcile is not None:
            _call("readiness_failed", "the consumer could not be reconciled", self.config.reconcile, self.entry, self.tx.data["transaction_id"])
            self.lease.stop_if_stale()
        verdict = _call(
            "readiness_failed", "the readiness check failed", self.config.evaluate,
            self.entry, self.gates, self.tx.data["delivered_at"], self.config.inventory,
        )
        self.lease.stop_if_stale()
        if not verdict.ok:
            raise RotationError("readiness_failed", "a readiness gate did not pass")

    # -- promote, then clean up
    def _promote(self):
        """Switch the v1 record to the new accessor. The success heartbeat
        stays as it was: it moves only after cleanup succeeds."""
        data, now = self.tx.data, self.now()
        main = dict(self.main or {})
        main.update({
            "schema_version": registry.SCHEMA_VERSION,
            "target": self.target,
            "approle_name": self.approle,
            "target_version": data["target_version"],
            "secret_id_accessor": data["new_accessor"],
            "issued_at": data["issued_at"],
            "expires_at": data["issued_at"] + self.limits.ttl,
            "ttl_seconds": self.limits.ttl,
            "last_successful_rotation_at": main.get("last_successful_rotation_at"),
            "last_scheduled_success_at": main.get("last_scheduled_success_at"),
            "last_failure_at": main.get("last_failure_at"),
            "last_failure_reason_code": main.get("last_failure_reason_code"),
        })
        main.update(self._attribution("success", now))
        self._fence()
        self._write_main(main)
        self.tx.update(state=ACTIVE)
        self._cleanup(data["previous_accessor"])

    def _write_main(self, main):
        _call("metadata_failed", "the metadata could not be written", self.bao.write_kv2, self.main_path, main, self.main_version)
        self.main, self.main_version = main, self.main_version + 1

    def _cleanup(self, previous_accessor):
        """Destroy the previous accessor, delete staging, then stamp the
        success heartbeat and mark the cleanup done. Safe to repeat: a run
        that finds a half-finished cleanup runs all of it again."""
        self._fence()
        if previous_accessor:
            _call("cleanup_failed", "the previous accessor could not be destroyed", self.bao.destroy_secret_id_accessor, self.approle, previous_accessor)
        self._fence()
        _call("cleanup_failed", "the staging record could not be deleted", self.bao.delete_staging, staging_path(self.entry))
        self._stamp_heartbeat()
        self.tx.update(cleanup_done=True)

    def _stamp_heartbeat(self):
        now = self.now()
        main = dict(self.main)
        main["last_successful_rotation_at"] = now
        if self.context.kind == "scheduled":
            main["last_scheduled_success_at"] = now
        main.update(self._attribution("success", now))
        self._fence()
        self._write_main(main)

    # -- safe abort
    def _abort(self, reason_code, message, cause=""):
        """Abort the transaction, or return when a destination carries it.

        First every destination is fenced: a write that changes its version,
        so a delivery that is still in flight fails instead of installing a
        SecretID that is about to be destroyed. Only then are the markers
        read again. With none present the abort is recorded first, so a crash
        cannot make the next run create the destroyed candidate again, then
        the replacement is destroyed and staging deleted. Raises
        RotationError(reason_code, cause) after an abort."""
        if any(self._markers()):
            self._mark_delivered()
            return
        self._fence()
        for destination in self.destinations:
            self._guard_adapter(destination.fence, self.tx.data["transaction_id"])
        if any(self._markers()):
            self._mark_delivered()
            return
        self._fence()
        self.tx.update(state=ABORTED)
        self._finish_abort()
        raise RotationError(reason_code, message, cause)

    def _finish_abort(self):
        """Resolve the abort by lookup, not by the recorded accessor. A create
        that landed late may have registered the candidate under an accessor
        that no record names. So: look up the staged candidate, destroy the
        accessor the lookup returns, look it up again, and delete staging only
        once the candidate is not registered. With staging already gone there
        is nothing to resolve."""
        candidate = self._staged()
        if candidate is None:
            return
        for attempt in range(ABORT_RESOLVE_ATTEMPTS + 1):
            self._fence()
            found = self._lookup(candidate)
            if found is None:
                self._fence()
                _call("cleanup_failed", "the staging record could not be deleted", self.bao.delete_staging, staging_path(self.entry))
                return
            accessor = found.get("secret_id_accessor") if isinstance(found, dict) else None
            if not accessor or attempt == ABORT_RESOLVE_ATTEMPTS:
                break
            self._fence()
            _call("cleanup_failed", "the replacement could not be destroyed", self.bao.destroy_secret_id_accessor, self.approle, accessor)
        raise RotationError("cleanup_failed", "the aborted candidate is still registered")

    # -- metadata
    def _attribution(self, outcome, now):
        context = self.context
        return {
            "latest_run_kind": context.kind,
            "latest_run_outcome": outcome,
            "latest_run_at": now,
            "actor": context.actor,
            "role": context.role,
            "request_id": context.request_id,
            "reason": context.reason,
        }

    def _record_failure(self, reason_code, cause=""):
        """Record the failure in both records. The v1 record gets the reason
        code only. The cause goes in the transaction record. A failed write is
        logged and never replaces the original reason."""
        now = self.now()
        LOG.warning("target %s failed with %s", self.target, reason_code)
        try:
            self.lease.stop_if_stale()
            if self.main is not None:
                main = {**self.main, "last_failure_at": now, "last_failure_reason_code": reason_code}
                main.update(self._attribution("failure", now))
                _call("metadata_failed", "the metadata could not be written", self.bao.write_kv2, self.main_path, main, self.main_version)
            if self.tx is not None and self.tx.data is not None:
                self.tx.update(last_failure_at=now, last_failure_reason_code=reason_code, last_failure_cause=cause or None)
        except RotationError:
            LOG.warning("target %s failure could not be recorded", self.target)


# ---- Public entry points -----------------------------------------------------

def _run_target(registry_data, target, context, clients, config, now, lease):
    entry = registry_data["runtime_targets"][target]
    return _TargetRun(target, entry, context, clients, config, now, lease).run()


def rotate_target(registry_data, target, context, clients, config, now):
    """Run the transaction for one runtime target under the lifecycle Lease.

    Raises RotationError for a bad context, an unknown target or a Lease held
    by another run, before any write. Otherwise returns a TargetResult."""
    validate_context(context)
    validate_config(config, clients)
    if target not in registry_data["runtime_targets"]:
        raise RotationError("invalid_state", "the target is not a runtime target")
    lease = _Lease(clients.kubernetes, config, context, now)
    lease.acquire()
    try:
        return _run_target(registry_data, target, context, clients, config, now, lease)
    finally:
        lease.release()


def rotate_all(registry_data, context, clients, config, now):
    """Run every runtime target under one Lease, in name order. A failed
    target does not stop the sweep. Returns a SweepResult."""
    validate_context(context)
    validate_config(config, clients)
    lease = _Lease(clients.kubernetes, config, context, now)
    lease.acquire()
    results = []
    try:
        for target in sorted(registry_data["runtime_targets"]):
            lease.renew()
            results.append(_run_target(registry_data, target, context, clients, config, now, lease))
    finally:
        lease.release()
    return SweepResult(tuple(results))
