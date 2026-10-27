"""Static checks for the least-privilege dmf-promsd OpenBao observer wiring."""

import json
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "vars/openbao-approle-lifecycle-registry.json"
OPENBAO_DEFAULTS = ROOT / "roles/stack/operator/openbao/defaults/main.yml"
OPENBAO_TASKS = ROOT / "roles/stack/operator/openbao/tasks/main.yml"
OBSERVER_POLICY = ROOT / "roles/stack/operator/openbao/templates/approle-rotation-observer-policy.hcl.j2"
PROMSD_DEFAULTS = ROOT / "roles/stack/operator/dmf-promsd/defaults/main.yml"
PROMSD_TASKS = ROOT / "roles/stack/operator/dmf-promsd/tasks/main.yml"


def scalar(text, name):
    match = re.search(rf"^{re.escape(name)}:\s*['\"]?([^\s'\"]+)", text, re.M)
    if not match:
        raise AssertionError(f"missing config value {name}")
    return match.group(1)


class OpenBaoObserverInfraContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
        cls.openbao_defaults = OPENBAO_DEFAULTS.read_text(encoding="utf-8")
        cls.openbao_tasks = OPENBAO_TASKS.read_text(encoding="utf-8")
        cls.policy = OBSERVER_POLICY.read_text(encoding="utf-8")
        cls.promsd_defaults = PROMSD_DEFAULTS.read_text(encoding="utf-8")
        cls.promsd_tasks = PROMSD_TASKS.read_text(encoding="utf-8")

    def test_observer_policy_reads_exact_registry_data_paths_only(self):
        self.assertIn("for target in openbao_approle_lifecycle_registry.runtime_targets | dict2items", self.policy)
        self.assertIn('path "secret/data/platform/approle-rotation/{{ target.key }}"', self.policy)
        self.assertIn('capabilities = ["read"]', self.policy)
        self.assertNotIn("secret/metadata/", self.policy)
        self.assertNotIn("capabilities = [\"list\"]", self.policy)
        self.assertNotIn("secret-id", self.policy)
        self.assertEqual(
            set(self.registry["runtime_targets"]),
            {
                "external-secrets",
                "object-storage-openbao-snapshots",
                "object-storage-app-backups",
            },
        )

    def test_openbao_kubernetes_role_is_bound_to_one_sa_namespace_and_audience(self):
        service_account = scalar(
            self.openbao_defaults, "openbao_approle_rotation_observer_service_account"
        )
        namespace = scalar(
            self.openbao_defaults, "openbao_approle_rotation_observer_namespace"
        )
        audience = scalar(
            self.openbao_defaults, "openbao_approle_rotation_observer_audience"
        )
        auth_role = scalar(
            self.openbao_defaults, "openbao_approle_rotation_observer_auth_role"
        )
        policy = scalar(
            self.openbao_defaults, "openbao_approle_rotation_observer_policy_name"
        )
        self.assertEqual(service_account, scalar(self.promsd_defaults, "dmf_promsd_openbao_service_account"))
        self.assertEqual(namespace, scalar(self.promsd_defaults, "dmf_promsd_namespace"))
        self.assertEqual(audience, scalar(self.promsd_defaults, "dmf_promsd_openbao_token_audience"))
        self.assertEqual(auth_role, scalar(self.promsd_defaults, "dmf_promsd_openbao_auth_role"))
        self.assertIn(
            "auth/kubernetes/role/{{ openbao_approle_rotation_observer_auth_role }}",
            self.openbao_tasks,
        )
        self.assertIn(
            "bound_service_account_names={{ openbao_approle_rotation_observer_service_account }}",
            self.openbao_tasks,
        )
        self.assertIn(
            "bound_service_account_namespaces={{ openbao_approle_rotation_observer_namespace }}",
            self.openbao_tasks,
        )
        self.assertIn(
            "audience={{ openbao_approle_rotation_observer_audience }}",
            self.openbao_tasks,
        )
        self.assertIn(
            "token_policies={{ openbao_approle_rotation_observer_policy_name }}",
            self.openbao_tasks,
        )
        self.assertIn("token_ttl={{ openbao_approle_rotation_observer_token_ttl }}", self.openbao_tasks)
        self.assertIn("openbao_init_raw.changed | default(false)", self.openbao_tasks)

    def test_promsd_uses_a_dedicated_sa_and_projected_short_lived_audience_token(self):
        service_account = scalar(self.promsd_defaults, "dmf_promsd_openbao_service_account")
        audience = scalar(self.promsd_defaults, "dmf_promsd_openbao_token_audience")
        jwt_mount = scalar(self.promsd_defaults, "dmf_promsd_openbao_jwt_mount_path")
        self.assertEqual(jwt_mount, "/var/run/openbao-token")
        self.assertIn('mountPath: "{{ dmf_promsd_openbao_jwt_mount_path }}"', self.promsd_tasks)
        self.assertIn(
            'dmf_promsd_openbao_jwt_file: "{{ dmf_promsd_openbao_jwt_mount_path }}/token"',
            self.promsd_defaults,
        )
        self.assertIn('name: "{{ dmf_promsd_openbao_service_account }}"', self.promsd_tasks)
        self.assertIn("automountServiceAccountToken: false", self.promsd_tasks)
        self.assertIn("serviceAccountToken:", self.promsd_tasks)
        self.assertIn(
            'audience: "{{ dmf_promsd_openbao_token_audience }}"',
            self.promsd_tasks,
        )
        self.assertIn('expirationSeconds: "{{ dmf_promsd_openbao_token_expiration_seconds | int }}"', self.promsd_tasks)
        self.assertEqual(scalar(self.promsd_defaults, "dmf_promsd_openbao_token_expiration_seconds"), "600")
        self.assertIn(f'OPENBAO_SA_JWT_FILE', self.promsd_tasks)
        self.assertIn(
            'value: "{{ dmf_promsd_openbao_jwt_file }}"', self.promsd_tasks
        )

    def test_only_public_openbao_certificate_is_copied_and_ca_rotation_restarts_app(self):
        self.assertIn("data['tls.crt']", self.promsd_tasks)
        self.assertNotIn("data['tls.key']", self.promsd_tasks)
        self.assertIn("kind: ConfigMap", self.promsd_tasks)
        self.assertIn("ca.crt:", self.promsd_tasks)
        self.assertIn("OPENBAO_CA_FILE", self.promsd_tasks)
        self.assertIn("checksum/openbao-ca:", self.promsd_tasks)
        self.assertIn("name: openbao-ca", self.promsd_tasks)
        self.assertIn("readOnly: true", self.promsd_tasks)
        self.assertNotIn("verify=False", self.promsd_tasks)


if __name__ == "__main__":
    unittest.main()
