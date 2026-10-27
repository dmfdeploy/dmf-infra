"""Behavior checks for the bounded born-inventory setup issuer ACL."""

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
POLICY_TEMPLATE = (
    ROOT
    / "roles/stack/operator/openbao/templates/born-inventory-setup-issuer-policy.hcl.j2"
)
ROLE = "born-inventory-netbox-reader"
OPENBAO_TASKS = ROOT / "roles/stack/operator/openbao/tasks/main.yml"
INVENTORY_TASKS = ROOT / "roles/common/dmf-born-inventory/tasks/main.yml"
SETUP_TASKS = ROOT / "roles/common/dmf-born-inventory/tasks/setup_only_credential.yml"
BREAKGLASS_VERIFIER = ROOT / "playbooks/verify-bootstrap-convergence.yml"


def parse_policy(text):
    """Parse the deliberately small path/parameter subset used by this ACL."""
    rendered = text.replace("{{ openbao_born_inventory_role_name }}", ROLE)
    paths = {}
    for match in re.finditer(r'path\s+"([^"]+)"\s*\{', rendered):
        path = match.group(1)
        if path in paths:
            raise AssertionError(f"duplicate ACL path {path}")
        start = match.end()
        depth = 1
        cursor = start
        while cursor < len(rendered) and depth:
            if rendered[cursor] == "{":
                depth += 1
            elif rendered[cursor] == "}":
                depth -= 1
            cursor += 1
        if depth:
            raise AssertionError(f"unterminated ACL block for {path}")
        body = rendered[start:cursor - 1]
        capability_match = re.search(r'capabilities\s*=\s*\[([^]]*)\]', body)
        required_match = re.search(r'required_parameters\s*=\s*\[([^]]*)\]', body)
        allowed_match = re.search(r'allowed_parameters\s*=\s*\{([^}]*)\}', body, re.S)
        if not capability_match or not allowed_match:
            raise AssertionError(f"incomplete ACL block for {path}")
        capabilities = set(re.findall(r'"([^"]+)"', capability_match.group(1)))
        required = (set(re.findall(r'"([^"]+)"', required_match.group(1)))
                    if required_match else set())
        allowed = {
            key: re.findall(r'"([^"]*)"', values)
            for key, values in re.findall(
                r'"([^"]+)"\s*=\s*\[([^]]*)\]', allowed_match.group(1)
            )
        }
        paths[path] = (capabilities, required, allowed)
    return paths


def permits(paths, path, capability, parameters):
    """Model OpenBao's required/allowed parameter checks for exact paths."""
    rule = paths.get(path)
    if rule is None:
        return False
    capabilities, required, allowed = rule
    if capability not in capabilities or not required.issubset(parameters):
        return False
    if not set(parameters).issubset(allowed):
        return False
    return all(not allowed[key] or str(value) in allowed[key]
               for key, value in parameters.items())


class BornSetupIssuerPolicyTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.paths = parse_policy(POLICY_TEMPLATE.read_text(encoding="utf-8"))
        cls.custom = f"auth/approle/role/{ROLE}/custom-secret-id"
        cls.lookup = f"auth/approle/role/{ROLE}/secret-id/lookup"
        cls.destroy = f"auth/approle/role/{ROLE}/secret-id-accessor/destroy"

    def test_custom_id_accepts_only_required_one_use_one_hour_request(self):
        self.assertTrue(permits(self.paths, self.custom, "update", {
            "secret_id": "candidate-generated-by-caller",
            "ttl": "1h",
            "num_uses": "1",
        }))
        for parameters in (
            {"secret_id": "candidate", "num_uses": "1"},
            {"secret_id": "candidate", "ttl": "720h", "num_uses": "1"},
            {"secret_id": "candidate", "ttl": "1h", "num_uses": "0"},
            {"secret_id": "candidate", "ttl": "1h", "num_uses": "1",
             "metadata": "unexpected"},
        ):
            with self.subTest(parameters=parameters):
                self.assertFalse(permits(self.paths, self.custom, "update", parameters))

    def test_only_same_role_candidate_can_be_looked_up(self):
        self.assertTrue(permits(self.paths, self.lookup, "update", {
            "secret_id": "candidate-generated-by-caller",
        }))
        self.assertFalse(permits(self.paths, self.lookup, "update", {}))
        self.assertFalse(permits(self.paths, self.lookup, "update", {
            "secret_id": "candidate", "metadata": "unexpected",
        }))
        self.assertFalse(permits(
            self.paths,
            "auth/approle/role/another-role/secret-id/lookup",
            "update",
            {"secret_id": "candidate"},
        ))

    def test_destroy_requires_only_a_secret_id_accessor(self):
        self.assertTrue(permits(self.paths, self.destroy, "update", {
            "secret_id_accessor": "accessor-from-create-response",
        }))
        self.assertFalse(permits(self.paths, self.destroy, "update", {}))
        self.assertFalse(permits(self.paths, self.destroy, "update", {
            "secret_id_accessor": "accessor", "secret_id": "candidate",
        }))

    def test_issuer_has_no_role_config_or_random_secret_id_generation_path(self):
        self.assertEqual(set(self.paths), {
            self.custom,
            self.lookup,
            self.destroy,
        })
        self.assertFalse(permits(
            self.paths,
            f"auth/approle/role/{ROLE}/secret-id",
            "update",
            {},
        ))
        self.assertFalse(permits(
            self.paths,
            f"auth/approle/role/{ROLE}",
            "update",
            {"secret_id_ttl": "1h"},
        ))

    def test_fresh_setup_authentication_uses_the_fixed_service_identity_and_audience(self):
        defaults = (ROOT / "roles/stack/operator/openbao/defaults/main.yml").read_text()
        tasks = OPENBAO_TASKS.read_text()
        self.assertIn("openbao_born_inventory_setup_issuer_audience: dmf-openbao-setup-issuer", defaults)
        self.assertIn("audience={{ openbao_born_inventory_setup_issuer_audience }}", tasks)
        self.assertIn("token_ttl={{ openbao_born_inventory_setup_issuer_token_ttl }}", tasks)

    def test_setup_consumer_requires_claim_then_mints_bounded_id_and_destroys_accessor(self):
        tasks = SETUP_TASKS.read_text()
        inventory = INVENTORY_TASKS.read_text()
        self.assertIn("consume_setup_request.py", tasks)
        self.assertLess(tasks.index("Validate and atomically claim the setup request"),
                        tasks.index("Request a short-lived audience-bound issuer JWT"))
        self.assertIn("custom-secret-id", tasks)
        self.assertIn("ttl=1h num_uses=1", tasks)
        self.assertIn("secret-id-accessor/destroy", tasks)
        self.assertIn("always:", tasks)
        self.assertIn("Recover the SecretID accessor if issuance returned an incomplete response", tasks)
        self.assertIn("secret-id/lookup", tasks)
        self.assertIn("Clear setup credential material from Ansible facts", tasks)
        self.assertLess(tasks.index("Recover the SecretID accessor"), tasks.index("Revoke the consumed born-inventory SecretID accessor"))
        self.assertNotIn("_dmf_born_inventory_openbao_secret_id", inventory)
        self.assertNotIn("auth/approle/role/{{ openbao_born_inventory_role_name }}/secret-id'", OPENBAO_TASKS.read_text())

    def test_missing_setup_issuer_fails_with_privileged_migration_guidance(self):
        tasks = SETUP_TASKS.read_text()
        self.assertIn("Require the setup issuer ServiceAccount and TokenRequest to be available", tasks)
        self.assertIn("Require the setup issuer Kubernetes auth role to be provisioned", tasks)
        self.assertGreaterEqual(tasks.count("reviewed privileged setup-issuer migration"), 2)
        self.assertIn("the consumed receipt cannot", tasks)

    def test_cached_secret_id_is_neither_required_nor_retained(self):
        tasks = OPENBAO_TASKS.read_text()
        verifier = BREAKGLASS_VERIFIER.read_text()
        self.assertNotIn("born_inventory_secret_id:", tasks)
        self.assertNotIn("- born_inventory_secret_id", verifier)
        self.assertIn("Reject any cached setup-only born-inventory SecretID", verifier)


if __name__ == "__main__":
    unittest.main()
