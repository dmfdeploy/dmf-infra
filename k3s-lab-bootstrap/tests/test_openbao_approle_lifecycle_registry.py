"""Completeness checks for the finite-TTL OpenBao AppRole registry."""

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REGISTRY_PATH = ROOT / "vars/openbao-approle-lifecycle-registry.json"
OPENBAO_DEFAULTS = ROOT / "roles/stack/operator/openbao/defaults/main.yml"
OBJECT_STORAGE_DEFAULTS = ROOT / "roles/base/object-storage-credentials/defaults/main.yml"
ESO_DEFAULTS = ROOT / "roles/base/external-secrets/defaults/main.yml"
LONGHORN_DEFAULTS = ROOT / "roles/base/longhorn-backup-target/defaults/main.yml"
OPENBAO_TASKS = ROOT / "roles/stack/operator/openbao/tasks/main.yml"
OBJECT_STORAGE_TASKS = ROOT / "roles/base/object-storage-credentials/tasks/main.yml"


def scalar(text, name):
    match = re.search(rf"^{re.escape(name)}:\s*['\"]?([^\s'\"]+)", text, re.M)
    if not match:
        raise AssertionError(f"missing configured value {name}")
    return match.group(1)


def object_storage_logicals(text):
    match = re.search(
        r"^object_storage_logicals:\s*\n((?:\s+-\s+[^\n]+\n)+)", text, re.M
    )
    if not match:
        raise AssertionError("missing object_storage_logicals")
    return [value.strip() for value in re.findall(r"^\s+-\s+([^\s#]+)", match.group(1), re.M)]


def ttl_assignment_inventory(*texts):
    expressions = set()
    count = 0
    for text in texts:
        assignments = re.findall(
            r"\bsecret_id_ttl\s*=\s*(\{\{[^}]+\}\})", text
        )
        occurrences = re.findall(r"\bsecret_id_ttl\s*=", text)
        if len(assignments) != len(occurrences):
            raise AssertionError("every AppRole TTL assignment must use a registry expression")
        expressions.update(assignments)
        count += len(occurrences)
    return expressions, count


def current_code_inventory():
    openbao_defaults = OPENBAO_DEFAULTS.read_text(encoding="utf-8")
    object_defaults = OBJECT_STORAGE_DEFAULTS.read_text(encoding="utf-8")
    eso_defaults = ESO_DEFAULTS.read_text(encoding="utf-8")
    longhorn_defaults = LONGHORN_DEFAULTS.read_text(encoding="utf-8")
    openbao_tasks = OPENBAO_TASKS.read_text(encoding="utf-8")
    object_tasks = OBJECT_STORAGE_TASKS.read_text(encoding="utf-8")

    runtime = {
        scalar(openbao_defaults, "openbao_eso_role_name"),
        *(
            "object-storage-" + logical.replace("_", "-")
            for logical in object_storage_logicals(object_defaults)
        ),
    }
    setup_only = {scalar(openbao_defaults, "openbao_born_inventory_role_name")}
    task_files = [
        *ROOT.glob("roles/**/*.yml"),
        *ROOT.glob("roles/**/*.yaml"),
        *ROOT.glob("roles/**/*.j2"),
        *ROOT.glob("playbooks/**/*.yml"),
        *ROOT.glob("playbooks/**/*.yaml"),
        *ROOT.glob("playbooks/**/*.j2"),
    ]
    task_texts = [path.read_text(encoding="utf-8") for path in task_files]
    if any(
        re.search(r"^\s*secret_id_ttl:\s*['\"]?[0-9]+[smhdw]", text, re.M)
        for text in task_texts
    ):
        raise AssertionError("literal finite SecretID TTLs must live in the registry")
    ttl_expressions, ttl_assignment_count = ttl_assignment_inventory(*task_texts)
    return runtime, setup_only, ttl_expressions, ttl_assignment_count


def registry_coverage_errors(
    registry, runtime, setup_only, ttl_expressions, ttl_assignment_count
):
    registry_runtime = set(registry.get("runtime_targets", {}))
    registry_setup = set(registry.get("dispositions", {}))
    errors = []
    if registry_runtime != runtime:
        errors.append(
            f"runtime targets differ: missing={sorted(runtime - registry_runtime)}, "
            f"extra={sorted(registry_runtime - runtime)}"
        )
    if registry_setup != setup_only:
        errors.append(
            f"setup dispositions differ: missing={sorted(setup_only - registry_setup)}, "
            f"extra={sorted(registry_setup - setup_only)}"
        )
    if registry_runtime & registry_setup:
        errors.append("a role appears as both runtime and setup-only")

    expected_ttl_expressions = {
        "{{openbao_approle_lifecycle_registry.runtime_targets['external-secrets'].secret_id_ttl}}",
        "{{openbao_approle_lifecycle_registry.runtime_targets['object-storage-' ~ item.logical_item.logical_dashed].secret_id_ttl}}",
        "{{openbao_approle_lifecycle_registry.dispositions['born-inventory-netbox-reader'].secret_id_ttl}}",
    }
    if ttl_expressions != expected_ttl_expressions:
        errors.append(
            f"TTL write sites must read registry values: expressions={sorted(ttl_expressions)}"
        )
    if ttl_assignment_count != 3:
        errors.append(
            f"expected exactly three finite-TTL role write sites, found {ttl_assignment_count}"
        )

    for target, entry in registry.get("runtime_targets", {}).items():
        if entry.get("approle_name") != target:
            errors.append(f"{target}: target and AppRole name differ")
        if not re.fullmatch(r"[1-9][0-9]*h", entry.get("secret_id_ttl", "")):
            errors.append(f"{target}: finite SecretID TTL is missing or malformed")
        if not entry.get("destinations") or not entry.get("readiness_gates"):
            errors.append(f"{target}: destination/readiness evidence is missing")
        if any(gate.get("status") == "required_rotation_gate" and not gate.get("evidence_gap")
               for gate in entry.get("readiness_gates", [])):
            errors.append(f"{target}: a new readiness gate is claimed without current evidence")
    for target, entry in registry.get("dispositions", {}).items():
        if entry.get("approle_name") != target:
            errors.append(f"{target}: disposition and AppRole name differ")
        if not re.fullmatch(r"[1-9][0-9]*h", entry.get("secret_id_ttl", "")):
            errors.append(f"{target}: finite SecretID TTL is missing or malformed")
        if entry.get("disposition") != "setup_only" or entry.get("continuous_rotation") is not False:
            errors.append(f"{target}: setup-only exclusion is not explicit")
    return errors


class OpenBaoAppRoleLifecycleRegistryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        cls.inventory = current_code_inventory()

    def test_registry_covers_each_finite_ttl_role_exactly_once(self):
        self.assertEqual(self.registry["schema_version"], 1)
        self.assertEqual(
            registry_coverage_errors(self.registry, *self.inventory), []
        )
        for relative_role, levels_up in (
            ("roles/stack/operator/openbao/tasks/main.yml", "../../../../"),
            ("roles/base/object-storage-credentials/tasks/main.yml", "../../../"),
        ):
            task_text = (ROOT / relative_role).read_text(encoding="utf-8")
            self.assertIn(
                f'file: "{{{{ role_path }}}}/{levels_up}vars/openbao-approle-lifecycle-registry.json"',
                task_text,
            )
            self.assertIn("name: openbao_approle_lifecycle_registry", task_text)

    def test_an_added_runtime_role_fails_closed_until_registered(self):
        runtime, setup_only, ttl_expressions, ttl_count = self.inventory
        errors = registry_coverage_errors(
            self.registry,
            runtime | {"new-finite-role"},
            setup_only,
            ttl_expressions,
            ttl_count + 1,
        )
        self.assertTrue(any("new-finite-role" in error for error in errors))

    def test_an_added_ttl_assignment_is_detected_even_if_it_reuses_a_variable(self):
        runtime, setup_only, ttl_expressions, ttl_count = self.inventory
        errors = registry_coverage_errors(
            self.registry, runtime, setup_only, ttl_expressions, ttl_count + 1
        )
        self.assertTrue(any("write sites" in error for error in errors))

    def test_setup_only_role_has_no_rotation_target_or_monitoring_record(self):
        runtime = set(self.registry["runtime_targets"])
        disposition = self.registry["dispositions"]["born-inventory-netbox-reader"]
        self.assertNotIn("born-inventory-netbox-reader", runtime)
        self.assertFalse(disposition["continuous_rotation"])
        self.assertFalse(disposition["continuous_expiry_monitoring"])
        self.assertTrue(disposition["fresh_issuance_per_invocation"])

    def test_object_storage_readiness_gap_is_explicit(self):
        for target in (
            "object-storage-openbao-snapshots",
            "object-storage-app-backups",
        ):
            gates = self.registry["runtime_targets"][target]["readiness_gates"]
            deferred = [gate for gate in gates if gate["status"] == "required_rotation_gate"]
            self.assertTrue(deferred, target)
            self.assertTrue(all(gate.get("evidence_gap") for gate in deferred))

    def test_destinations_and_existing_readiness_checks_match_consumer_configuration(self):
        object_defaults = OBJECT_STORAGE_DEFAULTS.read_text(encoding="utf-8")
        eso_defaults = ESO_DEFAULTS.read_text(encoding="utf-8")
        longhorn_defaults = LONGHORN_DEFAULTS.read_text(encoding="utf-8")
        external = self.registry["runtime_targets"]["external-secrets"]
        external_destination = external["destinations"][0]
        self.assertEqual(external_destination["namespace"], scalar(eso_defaults, "eso_namespace"))
        self.assertEqual(external_destination["name"], scalar(eso_defaults, "eso_approle_secret_name"))
        self.assertEqual(external_destination["key"], "id")
        self.assertTrue(any(gate.get("status") == "existing_check"
                            for gate in external["readiness_gates"]))
        self.assertEqual(
            next(gate["name"] for gate in external["readiness_gates"]
                 if gate["kind"] == "ClusterSecretStore"),
            scalar(eso_defaults, "eso_cluster_secret_store_name"),
        )

        logicals = object_storage_logicals(object_defaults)
        for logical in logicals:
            dashed = logical.replace("_", "-")
            target = f"object-storage-{dashed}"
            entry = self.registry["runtime_targets"][target]
            self.assertEqual(entry["logical_name"], logical)
            destinations = entry["destinations"]
            self.assertIn(
                {
                    "kind": "openbao_kv2_record",
                    "path": f"secret/data/platform/eso-bindings/{target}",
                    "key": "secret_id",
                },
                destinations,
            )
            self.assertEqual(
                next(gate["name"] for gate in entry["readiness_gates"]
                     if gate["kind"] == "ClusterSecretStore"),
                f"openbao-{dashed}",
            )
            external_secret_gate = next(
                gate for gate in entry["readiness_gates"]
                if gate["kind"] == "ExternalSecret"
            )
            self.assertEqual(
                external_secret_gate["namespaces_from"],
                f"object_storage_consumer_namespaces.{logical}",
            )
            self.assertEqual(
                external_secret_gate["name_pattern"], f"s3-creds-{dashed}"
            )
            self.assertIn(
                {
                    "kind": "kubernetes_secret_key",
                    "namespace": scalar(object_defaults, "object_storage_eso_namespace"),
                    "name": f"{dashed}-openbao-approle",
                    "key": "id",
                },
                destinations,
            )

        backup_target = self.registry["runtime_targets"]["object-storage-app-backups"]
        longhorn_gate = next(
            gate for gate in backup_target["readiness_gates"]
            if gate["kind"] == "KubernetesSecret"
        )
        self.assertEqual(longhorn_gate["namespace"], scalar(longhorn_defaults, "longhorn_backup_target_namespace"))
        self.assertEqual(longhorn_gate["name"], scalar(longhorn_defaults, "longhorn_backup_target_secret_name"))
        self.assertEqual(longhorn_gate["required_key"], "AWS_ACCESS_KEY_ID")


if __name__ == "__main__":
    unittest.main()
