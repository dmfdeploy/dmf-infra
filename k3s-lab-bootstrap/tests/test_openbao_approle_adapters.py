"""Delivery and readiness adapters: each registry kind has one, delivery stamps
the transaction, readiness is checked against the cluster state, and no result
or error carries a SecretID."""

import json
import sys
import traceback
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from approle_lifecycle import adapters, registry  # noqa: E402
from openbao_fakes import SECRET, FakeKubernetes, FakeOpenBao  # noqa: E402

REGISTRY = json.loads((ROOT / "vars/openbao-approle-lifecycle-registry.json").read_text(encoding="utf-8"))
TXN = "txn-0001"
SINCE = 1_700_000_000
INVENTORY = {"object_storage_consumer_namespaces": {"openbao_snapshots": ["openbao"], "app_backups": ["app-a", "app-b"]}}


def condition(status):
    return {"status": {"conditions": [{"type": "Ready", "status": status}]}}


class DeliveryAdapterTests(unittest.TestCase):
    def setUp(self):
        self.kube = FakeKubernetes()
        self.bao = FakeOpenBao()
        self.clients = {"kubernetes": self.kube, "openbao": self.bao}

    def test_secret_key_delivery_sets_the_key_and_stamps_the_transaction(self):
        destination = adapters.build_destination(
            {"kind": "kubernetes_secret_key", "namespace": "external-secrets", "name": "openbao-approle", "key": "id"},
            self.clients,
        )
        self.assertFalse(destination.has_marker(TXN))
        destination.deliver(SECRET, TXN, 4)
        self.assertTrue(destination.has_marker(TXN))
        self.assertFalse(destination.has_marker("another"))
        stored = self.kube.secrets[("external-secrets", "openbao-approle")]
        self.assertEqual(stored["data"]["id"], SECRET)
        self.assertEqual(stored["annotations"][adapters.VERSION_ANNOTATION], "4")
        self.assertEqual(destination.marker(), {"transaction_id": TXN, "target_version": "4"})

    def test_record_delivery_keeps_the_other_keys_and_uses_check_and_set(self):
        path = "secret/data/platform/eso-bindings/object-storage-app-backups"
        self.bao.records[path] = ({"role_id": "synthetic-role-id", "secret_id": "old"}, 3)
        destination = adapters.build_destination(
            {"kind": "openbao_kv2_record", "path": path, "key": "secret_id"}, self.clients
        )
        destination.deliver(SECRET, TXN, 7)
        data, version = self.bao.records[path]
        self.assertEqual(version, 4)
        self.assertEqual(data["role_id"], "synthetic-role-id")
        self.assertEqual(data["secret_id"], SECRET)
        self.assertEqual(data[adapters.TRANSACTION_KEY], TXN)
        self.assertEqual(data[adapters.VERSION_KEY], 7)
        self.assertIn(("write_kv2", path, 3), self.bao.calls)
        self.assertTrue(destination.has_marker(TXN))

    def test_a_first_write_to_an_empty_record_uses_version_zero(self):
        path = "secret/data/platform/eso-bindings/object-storage-openbao-snapshots"
        destination = adapters.build_destination(
            {"kind": "openbao_kv2_record", "path": path, "key": "secret_id"}, self.clients
        )
        destination.deliver(SECRET, TXN, 1)
        self.assertEqual(self.bao.records[path][1], 1)

    def test_a_failed_delivery_names_the_destination_and_leaks_nothing(self):
        self.kube.fail_patch = True
        destination = adapters.build_destination(
            {"kind": "kubernetes_secret_key", "namespace": "n", "name": "s", "key": "id"}, self.clients
        )
        with self.assertRaises(adapters.AdapterError) as caught:
            destination.deliver(SECRET, TXN, 1)
        error = caught.exception
        self.assertEqual(error.reason_code, "delivery_failed")
        self.assertIsNone(error.__cause__)
        self.assertIsNone(error.__context__)
        formatted = "".join(traceback.format_exception(error))
        self.assertNotIn(SECRET, formatted)
        self.assertNotIn("transport said", formatted)
        self.assertIn("secret n/s key id", str(error))

    def test_a_failed_record_write_leaves_no_exception_chain_to_the_secret(self):
        self.bao.fail_write = True
        destination = adapters.build_destination(
            {"kind": "openbao_kv2_record", "path": "secret/data/platform/eso-bindings/x", "key": "secret_id"},
            self.clients,
        )
        with self.assertRaises(adapters.AdapterError) as caught:
            destination.deliver(SECRET, TXN, 1)
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertNotIn(SECRET, "".join(traceback.format_exception(caught.exception)))

    def test_a_check_and_set_conflict_is_a_delivery_failure(self):
        path = "secret/data/platform/eso-bindings/x"
        self.bao.records[path] = ({"secret_id": "old"}, 5)
        destination = adapters.build_destination({"kind": "openbao_kv2_record", "path": path, "key": "secret_id"}, self.clients)
        original = self.bao.read_kv2

        def stale(read_path):
            data, _ = original(read_path)
            return data, 4

        self.bao.read_kv2 = stale
        with self.assertRaises(adapters.AdapterError) as caught:
            destination.deliver(SECRET, TXN, 2)
        self.assertEqual(caught.exception.reason_code, "delivery_failed")
        self.assertEqual(self.bao.records[path][0]["secret_id"], "old")

    def test_a_malformed_transaction_is_refused_before_any_write(self):
        destination = adapters.build_destination(
            {"kind": "kubernetes_secret_key", "namespace": "n", "name": "s", "key": "id"}, self.clients
        )
        for transaction, version in (("", 1), ("bad id", 1), (TXN, 0), (TXN, True), (None, 1)):
            with self.assertRaises(adapters.AdapterError) as caught:
                destination.deliver(SECRET, transaction, version)
            self.assertEqual(caught.exception.reason_code, "invalid_state")
        self.assertEqual(self.kube.patches, [])

    def test_unknown_kinds_and_missing_clients_are_errors_not_skips(self):
        with self.assertRaises(adapters.AdapterError):
            adapters.build_destination({"kind": "s3_bucket"}, self.clients)
        with self.assertRaises(adapters.AdapterError):
            adapters.build_gate({"kind": "Deployment"}, self.clients)
        with self.assertRaises(adapters.AdapterError):
            adapters.build_destination(
                {"kind": "openbao_kv2_record", "path": "p", "key": "k"}, {"kubernetes": self.kube}
            )

    def test_every_kind_in_the_registry_has_an_adapter(self):
        self.assertEqual(registry.validate(REGISTRY), [])
        for target, entry in REGISTRY["runtime_targets"].items():
            destinations, gates = adapters.adapters_for(entry, self.clients)
            self.assertEqual(len(destinations), len(entry["destinations"]), target)
            self.assertEqual(len(gates), len(entry["readiness_gates"]), target)
        self.assertEqual(
            {d["kind"] for e in REGISTRY["runtime_targets"].values() for d in e["destinations"]},
            set(registry.DESTINATION_KINDS),
        )
        self.assertEqual(
            {g["kind"] for e in REGISTRY["runtime_targets"].values() for g in e["readiness_gates"]},
            set(registry.GATE_KINDS),
        )


EXTERNAL_SECRET_GATE = {
    "kind": "ExternalSecret",
    "namespace": "authentik",
    "name_pattern": "authentik-runtime",
    "condition": "Ready=True",
}
STORE_GATE = {"kind": "ClusterSecretStore", "name": "openbao", "condition": "Ready=True"}
LATER = "2023-11-14T22:14:00Z"
STALE = "2023-11-14T22:13:19Z"
EQUAL = "2023-11-14T22:13:20Z"  # the same second as SINCE


class ReadinessAdapterTests(unittest.TestCase):
    def setUp(self):
        self.kube = FakeKubernetes()
        self.clients = {"kubernetes": self.kube, "openbao": FakeOpenBao()}

    def gate(self, entry_gate, store=None):
        return adapters.build_gate(entry_gate, self.clients, store)

    def external_secret(self, name, status, refreshed, store="openbao", kind="ClusterSecretStore"):
        item = condition(status)
        item["metadata"] = {"name": name}
        item["spec"] = {"secretStoreRef": {"name": store, "kind": kind}}
        item["status"]["refreshTime"] = refreshed
        return item

    def test_cluster_secret_store_gate(self):
        gate = self.gate(STORE_GATE)
        self.assertFalse(gate.check(SINCE).ok)
        self.kube.objects[("ClusterSecretStore", None, "openbao")] = condition("False")
        self.assertEqual(gate.check(SINCE).reason_code, "readiness_failed")
        self.kube.objects[("ClusterSecretStore", None, "openbao")] = condition("True")
        self.assertTrue(gate.check(SINCE).ok)
        self.assertFalse(gate.proves_acceptance)

    def test_external_secret_gate_needs_ready_and_a_refresh_strictly_after_delivery(self):
        gate = self.gate(
            {
                "kind": "ExternalSecret",
                "namespaces_from": "object_storage_consumer_namespaces.app_backups",
                "name_pattern": "s3-creds-app-backups",
                "condition": "Ready=True",
            },
            store="openbao-app-backups",
        )
        make = lambda status, when: [self.external_secret("s3-creds-app-backups", status, when, store="openbao-app-backups")]
        self.assertFalse(gate.check(SINCE, INVENTORY).ok)
        self.kube.lists[("ExternalSecret", "app-a")] = make("True", LATER)
        self.assertFalse(gate.check(SINCE, INVENTORY).ok)  # app-b has none
        self.kube.lists[("ExternalSecret", "app-b")] = make("True", STALE)
        self.assertFalse(gate.check(SINCE, INVENTORY).ok)  # refreshed before the delivery
        self.kube.lists[("ExternalSecret", "app-b")] = make("False", LATER)
        self.assertFalse(gate.check(SINCE, INVENTORY).ok)  # not Ready
        self.kube.lists[("ExternalSecret", "app-b")] = make("True", LATER)
        self.assertTrue(gate.check(SINCE, INVENTORY).ok)
        self.assertFalse(gate.check(SINCE, {}).ok)  # namespaces not resolved

    def test_a_refresh_equal_to_the_delivery_time_does_not_count(self):
        gate = self.gate(EXTERNAL_SECRET_GATE, store="openbao")
        self.kube.lists[("ExternalSecret", "authentik")] = [self.external_secret("authentik-runtime", "True", EQUAL)]
        result = gate.check(SINCE)
        self.assertFalse(result.ok)
        self.assertIn("not refreshed after the delivery", result.detail)

    def test_an_external_secret_that_reads_another_store_fails(self):
        gate = self.gate(EXTERNAL_SECRET_GATE, store="openbao")
        self.kube.objects[("ClusterSecretStore", None, "openbao")] = condition("True")
        self.kube.lists[("ExternalSecret", "authentik")] = [self.external_secret("authentik-runtime", "True", LATER, store="other-store")]
        result = gate.check(SINCE)
        self.assertFalse(result.ok)
        self.assertIn("expected store", result.detail)
        self.kube.lists[("ExternalSecret", "authentik")] = [self.external_secret("authentik-runtime", "True", LATER, kind="SecretStore")]
        self.assertFalse(gate.check(SINCE).ok)

    def test_an_external_secret_gate_with_no_expected_store_fails_closed(self):
        gate = self.gate(EXTERNAL_SECRET_GATE)
        self.kube.lists[("ExternalSecret", "authentik")] = [self.external_secret("authentik-runtime", "True", LATER)]
        self.assertFalse(gate.check(SINCE).ok)

    def test_secret_key_gate(self):
        gate = self.gate({"kind": "KubernetesSecret", "namespace": "longhorn-system", "name": "longhorn-s3-creds", "required_key": "AWS_ACCESS_KEY_ID"})
        self.assertFalse(gate.check(SINCE).ok)
        self.kube.secrets[("longhorn-system", "longhorn-s3-creds")] = {"data": {"AWS_ACCESS_KEY_ID": ""}, "annotations": {}}
        self.assertFalse(gate.check(SINCE).ok)
        self.kube.secrets[("longhorn-system", "longhorn-s3-creds")] = {"data": {"AWS_ACCESS_KEY_ID": "synthetic"}, "annotations": {}}
        self.assertTrue(gate.check(SINCE).ok)
        self.assertFalse(gate.proves_acceptance)

    def test_every_gate_fails_closed_without_a_delivery_time(self):
        self.kube.objects[("ClusterSecretStore", None, "openbao")] = condition("True")
        self.kube.secrets[("n", "s")] = {"data": {"k": "v"}, "annotations": {}}
        self.kube.lists[("ExternalSecret", "authentik")] = [self.external_secret("authentik-runtime", "True", LATER)]
        gates = [
            STORE_GATE,
            EXTERNAL_SECRET_GATE,
            {"kind": "KubernetesSecret", "namespace": "n", "name": "s", "required_key": "k"},
        ]
        for entry in gates:
            result = self.gate(entry, store="openbao").check()
            self.assertFalse(result.ok, entry["kind"])
            self.assertEqual(result.reason_code, "readiness_failed")
            self.assertIn("no delivery time", result.detail)

    def entry(self, proof="missing"):
        entry = {"readiness_gates": [STORE_GATE, EXTERNAL_SECRET_GATE]}
        if proof:
            entry["fresh_authentication_proof"] = {"status": proof, "evidence_gap": "step 3"}
        return entry

    def test_a_store_that_is_ready_never_accepts_a_replacement_alone(self):
        self.kube.objects[("ClusterSecretStore", None, "openbao")] = condition("True")
        self.kube.secrets[("n", "s")] = {"data": {"k": "v"}, "annotations": {}}
        store = self.gate(STORE_GATE)
        key = self.gate({"kind": "KubernetesSecret", "namespace": "n", "name": "s", "required_key": "k"})
        entry = {"readiness_gates": [STORE_GATE], "fresh_authentication_proof": {"status": "provided"}}
        self.assertFalse(adapters.evaluate_target(entry, [store], SINCE).ok)
        self.assertTrue(store.check(SINCE).ok)
        self.assertFalse(adapters.evaluate_target(entry, [store, key], SINCE).ok)

    def test_a_passing_gate_still_leaves_the_external_secrets_target_not_accepted(self):
        self.kube.objects[("ClusterSecretStore", None, "openbao")] = condition("True")
        self.kube.lists[("ExternalSecret", "authentik")] = [self.external_secret("authentik-runtime", "True", LATER)]
        entry = self.entry("missing")
        _, gates = adapters.adapters_for({**REGISTRY["runtime_targets"]["external-secrets"]}, self.clients)
        self.assertTrue(all(result.ok for result in (gate.check(SINCE) for gate in gates)))
        verdict = adapters.evaluate_target(REGISTRY["runtime_targets"]["external-secrets"], gates, SINCE)
        self.assertFalse(verdict.ok)
        self.assertEqual(verdict.reason_code, adapters.FRESH_AUTHENTICATION_MISSING)
        self.assertEqual(adapters.evaluate_target(entry, gates, SINCE).reason_code, adapters.FRESH_AUTHENTICATION_MISSING)
        self.assertEqual(adapters.evaluate_target(self.entry(None), gates, SINCE).reason_code, adapters.FRESH_AUTHENTICATION_MISSING)

    def passing_cluster(self, entry, store, namespaces):
        """Make every gate of a registry entry pass, as the real cluster would."""
        self.kube.objects[("ClusterSecretStore", None, store)] = condition("True")
        for gate in entry["readiness_gates"]:
            if gate["kind"] == "ExternalSecret":
                for namespace in namespaces:
                    self.kube.lists[("ExternalSecret", namespace)] = [
                        self.external_secret(gate["name_pattern"], "True", LATER, store=store)
                    ]
            if gate["kind"] == "KubernetesSecret":
                self.kube.secrets[(gate["namespace"], gate["name"])] = {
                    "data": {gate["required_key"]: "synthetic"},
                    "annotations": {},
                }

    def test_every_runtime_target_declares_the_missing_proof_and_is_never_accepted(self):
        shapes = {
            "external-secrets": ("openbao", ["authentik"]),
            "object-storage-openbao-snapshots": ("openbao-openbao-snapshots", INVENTORY["object_storage_consumer_namespaces"]["openbao_snapshots"]),
            "object-storage-app-backups": ("openbao-app-backups", INVENTORY["object_storage_consumer_namespaces"]["app_backups"]),
        }
        self.assertEqual(set(shapes), set(REGISTRY["runtime_targets"]))
        for target, (store, namespaces) in shapes.items():
            with self.subTest(target):
                entry = REGISTRY["runtime_targets"][target]
                self.assertEqual(entry["fresh_authentication_proof"]["status"], "missing")
                self.assertTrue(entry["fresh_authentication_proof"]["evidence_gap"])
                self.assertEqual(adapters.store_name(entry), store)
                self.passing_cluster(entry, store, namespaces)
                _, gates = adapters.adapters_for(entry, self.clients)
                self.assertTrue(all(g.check(SINCE, INVENTORY).ok for g in gates), target)
                verdict = adapters.evaluate_target(entry, gates, SINCE, INVENTORY)
                self.assertFalse(verdict.ok)
                self.assertEqual(verdict.reason_code, adapters.FRESH_AUTHENTICATION_MISSING)

    def test_a_failing_gate_still_returns_the_missing_proof_reason_and_the_gate_results(self):
        shapes = {
            "external-secrets": "openbao",
            "object-storage-openbao-snapshots": "openbao-openbao-snapshots",
            "object-storage-app-backups": "openbao-app-backups",
        }
        for target, store in shapes.items():
            with self.subTest(target):
                entry = REGISTRY["runtime_targets"][target]
                # Nothing exists in the cluster, so every gate fails.
                _, gates = adapters.adapters_for(entry, self.clients)
                verdict = adapters.evaluate_target(entry, gates, SINCE, INVENTORY)
                self.assertFalse(verdict.ok)
                self.assertEqual(verdict.reason_code, adapters.FRESH_AUTHENTICATION_MISSING)
                gate_results = verdict.results[:-1]
                self.assertEqual(len(gate_results), len(entry["readiness_gates"]))
                self.assertTrue(all(not result.ok and result.reason_code == "readiness_failed" for result in gate_results))
                self.assertEqual(verdict.results[-1].reason_code, adapters.FRESH_AUTHENTICATION_MISSING)

    def test_one_failing_gate_among_passing_ones_returns_the_missing_proof_reason(self):
        entry = REGISTRY["runtime_targets"]["external-secrets"]
        self.passing_cluster(entry, "openbao", ["authentik"])
        self.kube.lists[("ExternalSecret", "authentik")] = [self.external_secret("authentik-runtime", "True", STALE)]
        _, gates = adapters.adapters_for(entry, self.clients)
        verdict = adapters.evaluate_target(entry, gates, SINCE)
        self.assertEqual(verdict.reason_code, adapters.FRESH_AUTHENTICATION_MISSING)
        self.assertEqual([r.ok for r in verdict.results[:-1]], [True, False])

    def test_a_forced_provided_status_with_every_gate_passing_is_still_not_accepted(self):
        # The validator refuses `provided`, so this test bypasses it on purpose.
        consumers = INVENTORY["object_storage_consumer_namespaces"]
        shapes = {
            "external-secrets": ("openbao", ["authentik"]),
            "object-storage-openbao-snapshots": ("openbao-openbao-snapshots", consumers["openbao_snapshots"]),
            "object-storage-app-backups": ("openbao-app-backups", consumers["app_backups"]),
        }
        for target, (store, namespaces) in shapes.items():
            with self.subTest(target):
                entry = {**REGISTRY["runtime_targets"][target], "fresh_authentication_proof": {"status": "provided"}}
                self.passing_cluster(entry, store, namespaces)
                _, gates = adapters.adapters_for(entry, self.clients)
                verdict = adapters.evaluate_target(entry, gates, SINCE, INVENTORY)
                self.assertFalse(verdict.ok)
                self.assertEqual(verdict.reason_code, adapters.FRESH_AUTHENTICATION_MISSING)
                gate_results = verdict.results[:-1]
                self.assertEqual(len(gate_results), len(entry["readiness_gates"]))
                self.assertTrue(all(result.ok for result in gate_results))

    def test_the_openbao_snapshots_shape_has_a_store_and_an_external_secret_gate_only(self):
        entry = REGISTRY["runtime_targets"]["object-storage-openbao-snapshots"]
        self.assertEqual([g["kind"] for g in entry["readiness_gates"]], ["ClusterSecretStore", "ExternalSecret"])
        self.assertEqual(len(adapters.adapters_for(entry, self.clients)[0]), 2)

    def test_the_app_backups_shape_adds_a_secret_key_gate_and_is_still_not_accepted(self):
        entry = REGISTRY["runtime_targets"]["object-storage-app-backups"]
        self.assertEqual(
            [g["kind"] for g in entry["readiness_gates"]],
            ["ClusterSecretStore", "ExternalSecret", "KubernetesSecret"],
        )
        self.passing_cluster(entry, "openbao-app-backups", INVENTORY["object_storage_consumer_namespaces"]["app_backups"])
        _, gates = adapters.adapters_for(entry, self.clients)
        self.assertFalse(adapters.evaluate_target(entry, gates, SINCE, INVENTORY).ok)
        self.assertEqual(
            adapters.evaluate_target(entry, gates, SINCE, INVENTORY).reason_code,
            adapters.FRESH_AUTHENTICATION_MISSING,
        )

    def test_the_external_secrets_target_declares_a_probe_that_exists_in_the_roles(self):
        entry = REGISTRY["runtime_targets"]["external-secrets"]
        probes = [g for g in entry["readiness_gates"] if g["kind"] == "ExternalSecret"]
        self.assertEqual(len(probes), 1)
        self.assertEqual(probes[0]["name_pattern"], "authentik-runtime")
        self.assertEqual(adapters.store_name(entry), "openbao")
        self.assertEqual(entry["fresh_authentication_proof"]["status"], "missing")
        role = (ROOT / "roles/stack/operator/authentik/tasks/main.yml").read_text(encoding="utf-8")
        self.assertIn("kind: ExternalSecret\n      metadata:\n        name: authentik-runtime\n", role)
        defaults = (ROOT / "roles/stack/operator/authentik/defaults/main.yml").read_text(encoding="utf-8")
        self.assertIn("authentik_namespace: authentik", defaults)
        self.assertIn("eso_cluster_secret_store_name | default('openbao')", defaults)

    def test_a_gate_result_holds_no_secret_value(self):
        self.kube.secrets[("n", "s")] = {"data": {"k": SECRET}, "annotations": {}}
        result = self.gate({"kind": "KubernetesSecret", "namespace": "n", "name": "s", "required_key": "k"}).check(SINCE)
        self.assertNotIn(SECRET, repr(result))


class SanitizedClientCallTests(unittest.TestCase):
    def setUp(self):
        self.kube = FakeKubernetes()
        self.bao = FakeOpenBao()
        self.clients = {"kubernetes": self.kube, "openbao": self.bao}

    def formatted(self, error):
        return "".join(traceback.format_exception(error))

    def test_a_secret_in_a_marker_read_error_never_reaches_the_traceback(self):
        self.kube.read_error = RuntimeError(f"the response body was {SECRET}")
        destination = adapters.build_destination(
            {"kind": "kubernetes_secret_key", "namespace": "n", "name": "s", "key": "id"}, self.clients
        )
        with self.assertRaises(adapters.AdapterError) as caught:
            destination.marker()
        self.assertEqual(caught.exception.reason_code, "lookup_failed")
        self.assertNotIn(SECRET, self.formatted(caught.exception))
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)

    def test_a_secret_in_a_record_read_error_never_reaches_the_traceback(self):
        self.bao.read_error = RuntimeError(f"the response body was {SECRET}")
        destination = adapters.build_destination(
            {"kind": "openbao_kv2_record", "path": "secret/data/x", "key": "secret_id"}, self.clients
        )
        for call in (destination.marker, lambda: destination.deliver(SECRET, TXN, 1)):
            with self.assertRaises(adapters.AdapterError) as caught:
                call()
            self.assertNotIn(SECRET, self.formatted(caught.exception))
            self.assertIsNone(caught.exception.__context__)

    def test_a_secret_in_a_readiness_read_error_never_reaches_the_result(self):
        self.kube.read_error = RuntimeError(f"the response body was {SECRET}")
        gates = [
            adapters.build_gate(STORE_GATE, self.clients),
            adapters.build_gate(EXTERNAL_SECRET_GATE, self.clients, "openbao"),
            adapters.build_gate({"kind": "KubernetesSecret", "namespace": "n", "name": "s", "required_key": "k"}, self.clients),
        ]
        for gate in gates:
            result = gate.check(SINCE)
            self.assertFalse(result.ok)
            self.assertNotIn(SECRET, repr(result))
            self.assertIn("read failed", result.detail)

    def test_an_interrupt_in_a_client_call_keeps_its_class_and_drops_its_text(self):
        self.kube.read_error = KeyboardInterrupt(SECRET)
        destination = adapters.build_destination(
            {"kind": "kubernetes_secret_key", "namespace": "n", "name": "s", "key": "id"}, self.clients
        )
        with self.assertRaises(KeyboardInterrupt) as caught:
            destination.marker()
        self.assertNotIn(SECRET, self.formatted(caught.exception))
        self.assertIsNone(caught.exception.__context__)
        gate = adapters.build_gate({"kind": "KubernetesSecret", "namespace": "n", "name": "s", "required_key": "k"}, self.clients)
        with self.assertRaises(KeyboardInterrupt) as caught:
            gate.check(SINCE)
        self.assertNotIn(SECRET, self.formatted(caught.exception))


if __name__ == "__main__":
    unittest.main()
