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


if __name__ == "__main__":
    unittest.main()
