#!/usr/bin/env python3
"""Validate and atomically consume the one-use born-inventory setup receipt."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import uuid
from typing import Any, Callable


NAMESPACE = "dmf-setup-requests"
ACTION = "born-inventory-secretid-issue"
SCHEMA_VERSION = "1"
RECEIPT_TTL_SECONDS = 900
REQUEST_ID_RE = re.compile(r"^[0-9a-f]{32}$")
CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
DATA_KEYS = frozenset(
    {
        "schema_version",
        "request_id",
        "action",
        "env_id",
        "cluster_uid",
        "actor",
        "role",
        "reason",
        "issued_at",
        "expires_at",
    }
)
CLAIM_KEYS = frozenset(
    {"schema_version", "request_id", "action", "receipt_name", "receipt_uid", "claimed_at"}
)


class ReceiptError(RuntimeError):
    """A safe, non-sensitive receipt validation or claim error."""


def _kubectl(kubeconfig: str, args: list[str], *, input_text: str | None = None) -> str:
    result = subprocess.run(
        ["kubectl", "--kubeconfig", kubeconfig, *args],
        input=input_text,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode:
        raise ReceiptError("receipt is unavailable or already consumed")
    return result.stdout


def validate_receipt(
    receipt: dict[str, Any],
    *,
    request_id: str,
    env_id: str,
    cluster_uid: str,
    now: int,
) -> dict[str, str]:
    metadata = receipt.get("metadata")
    data = receipt.get("data")
    if not isinstance(metadata, dict) or not isinstance(data, dict):
        raise ReceiptError("receipt schema is invalid")
    if receipt.get("apiVersion") != "v1" or receipt.get("kind") != "ConfigMap":
        raise ReceiptError("receipt schema is invalid")
    if receipt.get("immutable") is not True or set(data) != DATA_KEYS:
        raise ReceiptError("receipt schema is invalid")
    name = f"request-{request_id}"
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise ReceiptError("a valid setup request id is required")
    if metadata.get("namespace") != NAMESPACE or metadata.get("name") != name:
        raise ReceiptError("receipt name or namespace does not match")
    receipt_uid = metadata.get("uid")
    if not isinstance(receipt_uid, str):
        raise ReceiptError("receipt identity is invalid")
    try:
        uuid.UUID(receipt_uid)
    except ValueError as exc:
        raise ReceiptError("receipt identity is invalid") from exc
    if data["request_id"] != request_id or data["action"] != ACTION:
        raise ReceiptError("receipt action or request id does not match")
    if data["schema_version"] != SCHEMA_VERSION:
        raise ReceiptError("receipt schema is unsupported")
    if data["env_id"] != env_id or data["cluster_uid"] != cluster_uid:
        raise ReceiptError("receipt environment binding does not match")
    if any(not isinstance(value, str) for value in data.values()):
        raise ReceiptError("receipt schema is invalid")
    for field, maximum in (("actor", 256), ("role", 32), ("reason", 240)):
        value = data[field]
        if not value or len(value) > maximum or CONTROL_RE.search(value):
            raise ReceiptError("receipt attribution is invalid")
    if data["role"] not in {"operator", "engineer", "admin"}:
        raise ReceiptError("receipt authorization role is invalid")
    if not data["env_id"] or not data["cluster_uid"]:
        raise ReceiptError("receipt binding is invalid")
    try:
        issued_at = int(data["issued_at"])
        expires_at = int(data["expires_at"])
    except (TypeError, ValueError) as exc:
        raise ReceiptError("receipt time is invalid") from exc
    if (
        str(issued_at) != data["issued_at"]
        or str(expires_at) != data["expires_at"]
        or issued_at <= 0
        or expires_at <= issued_at
        or issued_at > now
        or expires_at <= now
        or expires_at - issued_at > RECEIPT_TTL_SECONDS
    ):
        raise ReceiptError("receipt is expired or has an invalid time window")
    return {key: data[key] for key in ("actor", "role", "request_id", "reason")}


def make_claim(request_id: str, receipt_uid: str, *, claimed_at: int) -> dict[str, Any]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"name": f"claim-{request_id}", "namespace": NAMESPACE},
        "immutable": True,
        "data": {
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "action": ACTION,
            "receipt_name": f"request-{request_id}",
            "receipt_uid": receipt_uid,
            "claimed_at": str(claimed_at),
        },
    }


def consume_request(
    *,
    request_id: str,
    env_id: str,
    kubeconfig: str = "/etc/rancher/k3s/k3s.yaml",
    now: int | None = None,
    kubectl: Callable[..., str] = _kubectl,
) -> dict[str, str]:
    if not REQUEST_ID_RE.fullmatch(request_id):
        raise ReceiptError("a valid setup request id is required")
    if not env_id or not isinstance(env_id, str):
        raise ReceiptError("the canonical environment id is unavailable")
    live_namespace = json.loads(
        kubectl(kubeconfig, ["get", "namespace", "kube-system", "-o", "json"])
    )
    live_metadata = live_namespace.get("metadata")
    cluster_uid = live_metadata.get("uid") if isinstance(live_metadata, dict) else None
    if not isinstance(cluster_uid, str):
        raise ReceiptError("the current cluster binding is unavailable")
    try:
        uuid.UUID(cluster_uid)
    except ValueError as exc:
        raise ReceiptError("the current cluster binding is invalid") from exc

    receipt_name = f"request-{request_id}"
    receipt = json.loads(
        kubectl(
            kubeconfig,
            ["-n", NAMESPACE, "get", "configmap", receipt_name, "-o", "json"],
        )
    )
    c5 = validate_receipt(
        receipt,
        request_id=request_id,
        env_id=env_id,
        cluster_uid=cluster_uid,
        now=int(time.time()) if now is None else int(now),
    )
    receipt_uid = receipt["metadata"]["uid"]
    claim = make_claim(
        request_id,
        receipt_uid,
        claimed_at=int(time.time()) if now is None else int(now),
    )
    if set(claim["data"]) != CLAIM_KEYS:
        raise ReceiptError("claim schema is invalid")
    # `create` is atomic. A 409 (including a competing executor) is a consumed
    # request and must never be treated as a successful replay.
    kubectl(
        kubeconfig,
        ["-n", NAMESPACE, "create", "-f", "-"],
        input_text=json.dumps(claim, separators=(",", ":")),
    )
    return c5


def main() -> int:
    try:
        request_id = os.environ.get("DMF_SETUP_REQUEST_ID", "")
        env_id = os.environ.get("DMF_SETUP_REQUEST_ENV_ID", "")
        result = consume_request(request_id=request_id, env_id=env_id)
    except (ReceiptError, json.JSONDecodeError, OSError):
        print("setup request could not be validated or consumed", file=sys.stderr)
        return 1
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
