from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(
    0,
    str(
        Path(__file__).resolve().parents[1]
        / "roles/common/dmf-born-inventory/files"
    ),
)

from consume_setup_request import (  # noqa: E402
    ACTION,
    NAMESPACE,
    ReceiptError,
    consume_request,
    make_claim,
    validate_receipt,
)


REQUEST_ID = "a" * 32
ENV_ID = "env-1234"
CLUSTER_UID = "00000000-0000-4000-8000-000000000001"
RECEIPT_UID = "00000000-0000-4000-8000-000000000002"
NOW = 1_800_000_000


def receipt() -> dict[str, object]:
    return {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {
            "name": f"request-{REQUEST_ID}",
            "namespace": NAMESPACE,
            "uid": RECEIPT_UID,
        },
        "immutable": True,
        "data": {
            "schema_version": "1",
            "request_id": REQUEST_ID,
            "action": ACTION,
            "env_id": ENV_ID,
            "cluster_uid": CLUSTER_UID,
            "actor": "https://auth.example.test/users/operator",
            "role": "operator",
            "reason": "Initial inventory survey",
            "issued_at": str(NOW - 10),
            "expires_at": str(NOW + 890),
        },
    }


class FakeKubectl:
    def __init__(self, stored: dict[str, object] | None = None, *, claim_error: bool = False):
        self.stored = stored or receipt()
        self.claim_error = claim_error
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(self, _kubeconfig: str, args: list[str], *, input_text: str | None = None) -> str:
        self.calls.append((args, input_text))
        if args[:3] == ["get", "namespace", "kube-system"]:
            return json.dumps({"metadata": {"uid": CLUSTER_UID}})
        if "get" in args:
            return json.dumps(self.stored)
        if self.claim_error:
            raise ReceiptError("receipt is unavailable or already consumed")
        return "created"


class SetupRequestReceiptTests(unittest.TestCase):
    def test_valid_receipt_is_claimed_once_before_returning_c5(self):
        kubectl = FakeKubectl()
        result = consume_request(
            request_id=REQUEST_ID,
            env_id=ENV_ID,
            kubeconfig="/fake/kubeconfig",
            now=NOW,
            kubectl=kubectl,
        )
        self.assertEqual(set(result), {"actor", "role", "request_id", "reason"})
        self.assertEqual(result["request_id"], REQUEST_ID)
        self.assertEqual(len(kubectl.calls), 3)
        claim_args, claim_json = kubectl.calls[-1]
        self.assertEqual(claim_args, ["-n", NAMESPACE, "create", "-f", "-"])
        claim = json.loads(claim_json or "{}")
        self.assertTrue(claim["immutable"])
        self.assertEqual(claim["metadata"]["name"], f"claim-{REQUEST_ID}")
        self.assertEqual(claim["data"]["receipt_uid"], RECEIPT_UID)
        self.assertEqual(set(claim["data"]), {
            "schema_version", "request_id", "action", "receipt_name", "receipt_uid", "claimed_at"
        })

    def test_validation_rejects_schema_binding_identity_time_and_c5_changes(self):
        mutations = [
            lambda item: item["data"].update(extra="x"),
            lambda item: item.update(immutable=False),
            lambda item: item["metadata"].update(name="request-wrong"),
            lambda item: item["metadata"].update(uid="not-a-uid"),
            lambda item: item["data"].update(action="other"),
            lambda item: item["data"].update(env_id="other-env"),
            lambda item: item["data"].update(cluster_uid="other-cluster"),
            lambda item: item["data"].update(issued_at=str(NOW + 1)),
            lambda item: item["data"].update(expires_at=str(NOW)),
            lambda item: item["data"].update(expires_at=str(NOW + 901)),
            lambda item: item["data"].update(actor="operator\nforged"),
            lambda item: item["data"].update(role="unknown"),
            lambda item: item["data"].update(reason=""),
        ]
        for mutate in mutations:
            candidate = copy.deepcopy(receipt())
            mutate(candidate)
            with self.subTest(candidate=candidate):
                with self.assertRaises(ReceiptError):
                    validate_receipt(
                        candidate,
                        request_id=REQUEST_ID,
                        env_id=ENV_ID,
                        cluster_uid=CLUSTER_UID,
                        now=NOW,
                    )

    def test_wrong_current_cluster_fails_before_claim(self):
        kubectl = FakeKubectl()
        def other_cluster(_kubeconfig: str, args: list[str], *, input_text=None) -> str:
            kubectl.calls.append((args, input_text))
            if "namespace" in args:
                return json.dumps({"metadata": {"uid": "00000000-0000-4000-8000-000000000099"}})
            return json.dumps(receipt())
        with self.assertRaises(ReceiptError):
            consume_request(
                request_id=REQUEST_ID,
                env_id=ENV_ID,
                now=NOW,
                kubectl=other_cluster,
            )
        self.assertFalse(any("create" in args for args, _ in kubectl.calls))

    def test_claim_conflict_fails_closed_and_never_returns_c5(self):
        kubectl = FakeKubectl(claim_error=True)
        with self.assertRaises(ReceiptError):
            consume_request(
                request_id=REQUEST_ID,
                env_id=ENV_ID,
                now=NOW,
                kubectl=kubectl,
            )
        self.assertEqual(len(kubectl.calls), 3)

    def test_invalid_request_or_missing_canonical_env_fails_before_reads(self):
        kubectl = FakeKubectl()
        for request_id, env_id in (("A" * 32, ENV_ID), (REQUEST_ID, "")):
            with self.subTest(request_id=request_id, env_id=env_id):
                with self.assertRaises(ReceiptError):
                    consume_request(
                        request_id=request_id,
                        env_id=env_id,
                        now=NOW,
                        kubectl=kubectl,
                    )
        self.assertEqual(kubectl.calls, [])


if __name__ == "__main__":
    unittest.main()
