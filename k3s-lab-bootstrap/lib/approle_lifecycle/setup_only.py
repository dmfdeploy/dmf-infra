"""Fresh-on-rerun SecretID path for the setup-only AppRole.

Each run validates an immutable request receipt, claims it once, logs in as
the dedicated issuer identity, mints one one-use SecretID with a one-hour
limit, hands it to a caller-supplied `use` function, and destroys its accessor
in a `finally` block. This path is separate from periodic rotation.

What the code guarantees: the accessor is destroyed on every exit path that
the process survives, and a failure to destroy it raises. What it cannot
guarantee: after a crash, or when no response arrives, the accessor is not
known. The bound on what remains (one use, one hour) holds only when the
create response confirmed it. The issuer policy that enforces the bound is
files/policies/setup-issuer.hcl in the openbao role, which is not yet verified
on a live cluster. The messages say "unconfirmed" whenever the response did not
confirm it, and no message renders a value that OpenBao returned.
The code never reports success when the SecretID could not be destroyed.

Every exception from a transport or from `use` is replaced by a fixed-message
error, raised outside the handler, so the SecretID cannot appear in a
traceback through the cause or the context. That includes `BaseException`: an
interrupt is replaced by a fresh instance of its class with no arguments, so
it still interrupts and carries no text. When cleanup then fails, the
interrupt stays an interrupt and carries one fixed note about the failure. The
result carries no credential and no accessor.

This module checks the contract. The calling playbook supplies the clients
and the `use` function. The clients are listed on `SetupOnlyClients`.
"""

import re
from dataclasses import dataclass
from typing import Callable

ACTION = "born-inventory-secretid-issue"
RECEIPT_NAMESPACE = "dmf-setup-requests"
SCHEMA_VERSION = "1"
MAX_RECEIPT_LIFETIME = 900
SECRET_ID_TTL_SECONDS = 3600
SECRET_ID_NUM_USES = 1
_REQUEST_ID = re.compile(r"[0-9a-f]{32}")
_DATA_KEYS = {
    "schema_version", "request_id", "action", "env_id", "cluster_uid",
    "actor", "role", "reason", "issued_at", "expires_at",
}
_C5_FIELDS = ("actor", "role", "reason")
CLEANUP_NOTE = "cleanup failed, so the residue is unconfirmed"
_UNCONFIRMED = "the bounds are unconfirmed"
_CONFIRMED_RESIDUE = "the response confirmed the bounds, so the SecretID stays valid for at most one use and one hour"


class SetupOnlyError(RuntimeError):
    """The run failed closed. `stage` names where, and no message carries a
    credential."""

    def __init__(self, stage, message):
        super().__init__(message)
        self.stage = stage


class ClaimConflict(Exception):
    """The claim already exists, so the request was consumed."""


@dataclass
class SetupOnlyClients:
    """Methods the path calls.

    kubernetes.get_receipt(namespace, name) -> None or a dict with `name`,
        `namespace`, `uid`, `immutable` and `data`
    kubernetes.create_claim(namespace, name, annotations) -> None; raises
        ClaimConflict when the Lease exists
    kubernetes.kube_system_uid() -> the live kube-system Namespace UID
    kubernetes.request_token(namespace, service_account, audiences) -> a JWT
    openbao.login_kubernetes(role, jwt) -> a client token
    openbao.issue_secret_id(token, approle_name, params) -> a dict with
        `secret_id`, `secret_id_accessor`, `secret_id_ttl`, `secret_id_num_uses`
    openbao.destroy_secret_id_accessor(token, approle_name, accessor) -> None
    openbao.revoke_self(token) -> None
    """

    kubernetes: object
    openbao: object


@dataclass(frozen=True)
class SetupOnlyConfig:
    env_id: str
    approle_name: str
    issuer_namespace: str
    issuer_service_account: str
    issuer_openbao_role: str
    audiences: tuple
    max_clock_skew: int = 0


@dataclass(frozen=True)
class SetupOnlyResult:
    request_id: str
    actor: str
    role: str
    reason: str
    outcome: str
    cleanup: str


def _has_control(text):
    return any(ord(character) < 32 or ord(character) == 127 for character in text)


def validate_receipt(receipt, request_id, config, cluster_uid, now):
    """Raise SetupOnlyError unless the receipt is exactly what the contract
    allows. Return the C5 fields."""
    stage = "receipt"
    if not isinstance(request_id, str) or not _REQUEST_ID.fullmatch(request_id):
        raise SetupOnlyError(stage, "the request id is not 32 lowercase hexadecimal characters")
    if receipt is None:
        raise SetupOnlyError(stage, "the receipt is missing or unreadable")
    if receipt.get("name") != f"request-{request_id}" or receipt.get("namespace") != RECEIPT_NAMESPACE:
        raise SetupOnlyError(stage, "the receipt name or namespace does not match the request id")
    if receipt.get("immutable") is not True or not receipt.get("uid"):
        raise SetupOnlyError(stage, "the receipt is mutable or has no UID")
    data = receipt.get("data")
    if not isinstance(data, dict) or set(data) != _DATA_KEYS:
        raise SetupOnlyError(stage, "the receipt has missing or extra data keys")
    if any(not isinstance(value, str) for value in data.values()):
        raise SetupOnlyError(stage, "the receipt data must hold strings only")
    if data["schema_version"] != SCHEMA_VERSION or data["action"] != ACTION:
        raise SetupOnlyError(stage, "the receipt has the wrong schema version or action")
    if data["request_id"] != request_id:
        raise SetupOnlyError(stage, "the receipt request id differs from the request")
    if data["env_id"] != config.env_id:
        raise SetupOnlyError(stage, "the receipt is bound to another environment")
    if not cluster_uid or data["cluster_uid"] != cluster_uid:
        raise SetupOnlyError(stage, "the receipt is bound to another cluster")
    for field in _C5_FIELDS:
        if not data[field].strip() or _has_control(data[field]):
            raise SetupOnlyError(stage, f"the receipt {field} is empty or holds control characters")
    if not data["issued_at"].isdigit() or not data["expires_at"].isdigit():
        raise SetupOnlyError(stage, "the receipt times are not decimal seconds")
    issued = int(data["issued_at"])
    expires = int(data["expires_at"])
    if issued > now + config.max_clock_skew:
        raise SetupOnlyError(stage, "the receipt is issued in the future")
    if expires <= now:
        raise SetupOnlyError(stage, "the receipt has expired")
    if expires <= issued or expires - issued > MAX_RECEIPT_LIFETIME:
        raise SetupOnlyError(stage, "the receipt window is empty or longer than 15 minutes")
    return {field: data[field] for field in _C5_FIELDS}


def _call(stage, message, function, *args):
    """Call a transport or the caller's function. An `Exception` becomes a
    SetupOnlyError with a fixed message. A `BaseException` such as
    KeyboardInterrupt becomes a fresh instance of its class with no
    arguments. Both are raised after the handler, so neither has a cause or
    a context."""
    interrupt = None
    failed = False
    try:
        return function(*args)
    except Exception:
        failed = True
    except BaseException as caught:
        interrupt = type(caught)
    if interrupt is not None:
        raise interrupt()
    if failed:
        raise SetupOnlyError(stage, message)


def _attempt(function, *args):
    """Run a cleanup call. Return (succeeded, interrupt class or None). Nothing
    escapes, so the next cleanup call still runs."""
    try:
        function(*args)
        return True, None
    except Exception:
        return False, None
    except BaseException as caught:
        return False, type(caught)


def _exact(value, expected):
    """True for the integer, or the exact decimal string the API documents.
    Booleans, other types and every other string are not a match."""
    if isinstance(value, bool):
        return False
    return value == expected if isinstance(value, int) else value == str(expected)


def _bounds_confirmed(issued):
    return (
        isinstance(issued, dict)
        and _exact(issued.get("secret_id_ttl"), SECRET_ID_TTL_SECONDS)
        and _exact(issued.get("secret_id_num_uses"), SECRET_ID_NUM_USES)
    )


def _residue(issued):
    """Say what may remain. The one-use, one-hour bound is claimed only when
    the create response confirmed both values exactly. Otherwise the message
    says the bounds are unconfirmed and renders nothing that the response
    held."""
    if _bounds_confirmed(issued):
        return _CONFIRMED_RESIDUE
    return _UNCONFIRMED


def issue_use_revoke(request_id, config, clients, use, now):
    """Run one fresh-on-rerun issuance.

    `use` receives the SecretID and returns nothing. `now` returns Unix
    seconds. Every failure before the claim leaves OpenBao and Kubernetes
    untouched. Once a SecretID exists with a known accessor, the accessor is
    destroyed on every exit path the process survives. A result is returned
    only after the destroy call succeeded.
    """
    kube, bao = clients.kubernetes, clients.openbao
    receipt = kube.get_receipt(RECEIPT_NAMESPACE, f"request-{request_id}")
    cluster_uid = kube.kube_system_uid()
    c5 = validate_receipt(receipt, request_id, config, cluster_uid, now())

    try:
        kube.create_claim(
            RECEIPT_NAMESPACE,
            f"claim-{request_id}",
            {
                "schema_version": SCHEMA_VERSION,
                "request_id": request_id,
                "action": ACTION,
                "receipt_name": receipt["name"],
                "receipt_uid": receipt["uid"],
                "claimed_at": str(now()),
            },
        )
    except ClaimConflict:
        raise SetupOnlyError("claim", "the request was already consumed") from None

    token = _login(kube, bao, config)
    accessor = None
    issued = None
    failure = None
    try:
        issued = _call(
            "issue",
            f"the issue call failed; {_UNCONFIRMED} because no response arrived",
            bao.issue_secret_id,
            token,
            config.approle_name,
            {"ttl": "1h", "num_uses": SECRET_ID_NUM_USES},
        )
        accessor = issued.get("secret_id_accessor") if isinstance(issued, dict) else None
        _confirm_bounds(issued)
        _call("use", "the caller's use function failed; the SecretID is destroyed afterwards", use, issued["secret_id"])
    except BaseException as caught:
        # Only a SetupOnlyError or a fresh interrupt can arrive here, because
        # every call above is wrapped. Keep it and raise it after cleanup.
        failure = caught
    problem, interrupt = _destroy(bao, token, config.approle_name, accessor, issued)
    # Everything below raises outside a handler, so nothing carries a context.
    if interrupt is None and failure is not None and not isinstance(failure, Exception):
        interrupt = type(failure)
    if interrupt is not None:
        error = interrupt()
        if problem is not None:
            error.add_note(CLEANUP_NOTE)
        raise error
    if problem is not None:
        raise SetupOnlyError("cleanup", problem) from failure
    if failure is not None:
        raise failure
    return SetupOnlyResult(request_id, c5["actor"], c5["role"], c5["reason"], "success", "revoked")


def _login(kube, bao, config):
    jwt = _call(
        "login",
        "the issuer login failed",
        kube.request_token,
        config.issuer_namespace,
        config.issuer_service_account,
        list(config.audiences),
    )
    return _call("login", "the issuer login failed", bao.login_kubernetes, config.issuer_openbao_role, jwt)


def _confirm_bounds(issued):
    """Fail closed unless OpenBao confirmed a SecretID, an accessor and both
    bounds. No message renders a returned value."""
    if not isinstance(issued, dict) or not issued.get("secret_id"):
        raise SetupOnlyError("issue", f"the create response has no SecretID; {_residue(issued)}")
    if not issued.get("secret_id_accessor"):
        raise SetupOnlyError(
            "issue",
            f"the create response has no accessor, so the SecretID cannot be destroyed; {_residue(issued)}",
        )
    if not _bounds_confirmed(issued):
        raise SetupOnlyError("issue", f"OpenBao did not confirm the one-hour, one-use bounds; {_UNCONFIRMED}")


def _destroy(bao, token, approle_name, accessor, issued):
    """Destroy the accessor and revoke the issuer token.

    Return (message, interrupt class). The message is None when both calls
    succeeded, otherwise it says what remains, and it states the one-use,
    one-hour bound only when the create response confirmed it."""
    problems = []
    interrupt = None
    if accessor:
        done, caught = _attempt(bao.destroy_secret_id_accessor, token, approle_name, accessor)
        interrupt = interrupt or caught
        if not done:
            problems.append(f"the SecretID accessor could not be destroyed; {_residue(issued)}")
    done, caught = _attempt(bao.revoke_self, token)
    interrupt = interrupt or caught
    if not done:
        problems.append("the issuer token could not be revoked; it expires on its own")
    return ("; ".join(problems) if problems else None), interrupt
