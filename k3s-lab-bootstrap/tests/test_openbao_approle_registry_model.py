"""Registry model: the shipped registry validates, thresholds derive from it,
and each kind of malformed entry is refused."""

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))

from approle_lifecycle import registry  # noqa: E402

REGISTRY_PATH = ROOT / "vars/openbao-approle-lifecycle-registry.json"


def shipped():
    return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))


class RegistryModelTests(unittest.TestCase):
    def test_shipped_registry_is_well_formed(self):
        self.assertEqual(registry.validate(shipped(), ROOT), [])
        self.assertEqual(registry.load(REGISTRY_PATH, ROOT)["schema_version"], 1)

    def test_every_runtime_target_names_its_metadata_record(self):
        for target, entry in shipped()["runtime_targets"].items():
            self.assertEqual(entry["metadata_path"], f"secret/data/platform/approle-rotation/{target}")
            self.assertTrue(entry["consumer_type"])

    def test_thresholds_follow_the_registry_fractions(self):
        entry = shipped()["runtime_targets"]["external-secrets"]
        limits = registry.thresholds(entry)
        self.assertEqual(limits.ttl, 720 * 3600)
        self.assertEqual(limits.rotation_due_after, int(720 * 3600 * entry["rotation_due_fraction"]))
        self.assertEqual(limits.warning_remaining, 180 * 3600)
        self.assertEqual(limits.critical_remaining, 72 * 3600)
        self.assertLess(limits.critical_remaining, limits.warning_remaining)
        self.assertLess(limits.rotation_due_after, limits.ttl)

    def test_a_ttl_that_is_not_finite_hours_is_refused(self):
        for bad in ("0h", "720", "30d", "", None, "-1h"):
            with self.assertRaises(registry.RegistryError, msg=repr(bad)):
                registry.ttl_seconds(bad)

    def refused(self, mutate, needle, root=None):
        broken = copy.deepcopy(shipped())
        mutate(broken)
        errors = registry.validate(broken, root)
        self.assertTrue(any(needle in error for error in errors), errors)

    def test_each_malformed_entry_is_refused(self):
        target = "object-storage-app-backups"
        self.refused(lambda r: r["runtime_targets"][target].pop("metadata_path"), "metadata_path")
        self.refused(lambda r: r["runtime_targets"][target].pop("consumer_type"), "consumer_type")
        self.refused(lambda r: r["runtime_targets"][target]["destinations"][0].update(kind="s3_bucket"), "unsupported destination kind")
        self.refused(lambda r: r["runtime_targets"][target]["destinations"].clear(), "destination evidence")
        self.refused(lambda r: r["runtime_targets"][target]["readiness_gates"][0].update(kind="Deployment"), "unsupported readiness gate kind")
        self.refused(lambda r: r["runtime_targets"][target]["readiness_gates"].clear(), "readiness evidence")
        self.refused(lambda r: r["runtime_targets"][target].update(critical_remaining_fraction=0.5), "critical fraction")
        self.refused(lambda r: r["runtime_targets"][target].update(rotation_due_fraction=1.5), "rotation_due_fraction")
        self.refused(lambda r: r["runtime_targets"][target].update(approle_name="other"), "AppRole name differ")
        self.refused(lambda r: r["runtime_targets"][target].update(secret_id_ttl="30d"), "TTL")
        self.refused(lambda r: r.update(schema_version=2), "schema_version")

    def test_a_role_in_both_inventories_is_refused(self):
        def both(r):
            r["dispositions"]["external-secrets"] = copy.deepcopy(
                r["dispositions"]["born-inventory-netbox-reader"]
            )
            r["dispositions"]["external-secrets"]["approle_name"] = "external-secrets"

        self.refused(both, "both runtime and setup-only")

    def test_a_setup_only_entry_must_exclude_continuous_rotation(self):
        self.refused(
            lambda r: r["dispositions"]["born-inventory-netbox-reader"].update(continuous_rotation=True),
            "setup-only exclusion",
        )


class SetupOnlyDispositionTests(unittest.TestCase):
    NAME = "born-inventory-netbox-reader"

    def refused(self, mutate, needle, root=None):
        broken = copy.deepcopy(shipped())
        mutate(broken["dispositions"][self.NAME])
        errors = registry.validate(broken, root)
        self.assertTrue(any(needle in error for error in errors), errors)

    def test_the_shipped_disposition_names_entrypoints_the_requirement_and_its_evidence(self):
        entry = shipped()["dispositions"][self.NAME]
        self.assertTrue(entry["fresh_on_rerun_required"])
        self.assertIn("playbooks/694-born-inventory.yml", entry["entrypoints"])
        for path in entry["entrypoints"]:
            self.assertTrue((ROOT / path).is_file(), path)
        for field in ("issuance_verification", "revocation_verification"):
            self.assertTrue((ROOT / entry[field]["evidence"]).is_file(), field)
        self.assertTrue((ROOT / entry["create_site"]).is_file())

    def test_a_disposition_missing_any_required_field_is_refused(self):
        self.refused(lambda e: e.pop("entrypoints"), "entrypoints")
        self.refused(lambda e: e.update(entrypoints=[]), "entrypoints")
        self.refused(lambda e: e.pop("fresh_on_rerun_required"), "fresh-on-rerun")
        self.refused(lambda e: e.update(fresh_on_rerun_required=False), "fresh-on-rerun")
        self.refused(lambda e: e.pop("issuance_verification"), "issuance_verification")
        self.refused(lambda e: e.pop("revocation_verification"), "revocation_verification")
        self.refused(lambda e: e["issuance_verification"].pop("evidence"), "issuance_verification")
        self.refused(lambda e: e["revocation_verification"].pop("requirement"), "revocation_verification")
        self.refused(lambda e: e.pop("create_site"), "create_site")

    def test_a_named_path_that_does_not_exist_is_refused(self):
        self.refused(lambda e: e["entrypoints"].append("playbooks/does-not-exist.yml"), "does not exist", ROOT)
        self.refused(lambda e: e["issuance_verification"].update(evidence="tests/missing.py"), "does not exist", ROOT)
        self.refused(lambda e: e.update(create_site="roles/missing/tasks/main.yml"), "does not exist", ROOT)

    def test_a_runtime_target_needs_a_gate_that_shows_a_consumer_used_the_replacement(self):
        broken = copy.deepcopy(shipped())
        gates = broken["runtime_targets"]["external-secrets"]["readiness_gates"]
        gates[:] = [gate for gate in gates if gate["kind"] != "ExternalSecret"]
        errors = registry.validate(broken)
        self.assertTrue(any("no readiness gate shows" in error for error in errors), errors)

    def test_every_runtime_target_has_such_a_gate_and_names_its_create_site(self):
        for target, entry in shipped()["runtime_targets"].items():
            kinds = {gate["kind"] for gate in entry["readiness_gates"]}
            self.assertIn("ExternalSecret", kinds, target)
            self.assertTrue((ROOT / entry["create_site"]).is_file(), target)

    def test_a_runtime_target_without_the_fresh_authentication_field_is_refused(self):
        for target in shipped()["runtime_targets"]:
            with self.subTest(target):
                broken = copy.deepcopy(shipped())
                broken["runtime_targets"][target].pop("fresh_authentication_proof")
                errors = registry.validate(broken)
                self.assertTrue(any("fresh_authentication_proof" in e for e in errors), errors)
                broken = copy.deepcopy(shipped())
                broken["runtime_targets"][target]["fresh_authentication_proof"] = {"status": "missing"}
                self.assertTrue(any("evidence_gap" in e for e in registry.validate(broken)))

    def test_a_provided_fresh_authentication_status_is_refused_in_this_step(self):
        for target in shipped()["runtime_targets"]:
            with self.subTest(target):
                broken = copy.deepcopy(shipped())
                broken["runtime_targets"][target]["fresh_authentication_proof"] = {"status": "provided"}
                errors = registry.validate(broken)
                self.assertTrue(any("step 3" in e and "fresh_authentication_proof" in e for e in errors), errors)
                broken["runtime_targets"][target]["fresh_authentication_proof"] = {
                    "status": "provided",
                    "evidence_gap": "step 3",
                }
                self.assertTrue(any("fresh_authentication_proof" in e for e in registry.validate(broken)))

    def test_an_external_secret_gate_needs_a_namespace(self):
        broken = copy.deepcopy(shipped())
        gate = next(g for g in broken["runtime_targets"]["external-secrets"]["readiness_gates"] if g["kind"] == "ExternalSecret")
        gate.pop("namespace")
        errors = registry.validate(broken)
        self.assertTrue(any("namespaces_from or namespace" in error for error in errors), errors)


if __name__ == "__main__":
    unittest.main()
