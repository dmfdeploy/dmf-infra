"""Fixture tests for the supported Forgejo full user-list CLI format."""

import importlib.util
import json
from pathlib import Path
import unittest


HELPER = Path(__file__).parents[1] / "playbooks/files/audit_forgejo_identities.py"
spec = importlib.util.spec_from_file_location("audit_forgejo_identities", HELPER)
audit_module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
spec.loader.exec_module(audit_module)


CLI_FIXTURE = """ID    Username            Email                                  IsActive  IsAdmin  2FA
1     forgejo-break-glass  break-glass@example.invalid           true      true     true
2     forgejo-svc          forgejo-svc@example.invalid            true      false    false
3     operator-user        operator@example.invalid               true      true     true
4     contributor-user     contributor@example.invalid            true      false    false
"""


def payload(output=CLI_FIXTURE, ops_admins=None):
    return {
        "users_output": output,
        "users_rc": 0,
        "ops_admins_rc": 0,
        "ops_admin_usernames": ["operator-user"] if ops_admins is None else ops_admins,
    }


class AuditForgejoIdentitiesTests(unittest.TestCase):
    def test_breakglass_service_and_oidc_admin_policy_passes(self):
        result = audit_module.audit(payload())
        self.assertTrue(result["passed"])
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["inventory"]["user_count"], 4)
        self.assertEqual(result["inventory"]["active_user_count"], 4)
        self.assertEqual(result["inventory"]["admin_count"], 2)

    def test_unexpected_admin_fails_even_when_expected_breakglass_is_present(self):
        extra_admin = "5     unexpected-admin     unexpected@example.invalid            true      true     false\n"
        result = audit_module.audit(payload(CLI_FIXTURE + extra_admin))
        self.assertFalse(result["passed"])
        self.assertIn("unexpected Forgejo administrator present", result["findings"])

    def test_expected_ops_admin_missing_admin_role_fails(self):
        output = CLI_FIXTURE.replace("operator@example.invalid               true      true", "operator@example.invalid               true      false")
        result = audit_module.audit(payload(output))
        self.assertFalse(result["passed"])
        self.assertIn("active expected ops-admin account lacks Forgejo administrator rights", result["findings"])

    def test_disabled_forgejo_user_is_not_required_to_carry_current_ops_admin_role(self):
        output = CLI_FIXTURE.replace("operator@example.invalid               true      true", "operator@example.invalid               false     false")
        result = audit_module.audit(payload(output))
        self.assertTrue(result["passed"])

    def test_breakglass_and_service_exception_are_checked(self):
        output = CLI_FIXTURE.replace("forgejo-break-glass  ", "removed-break-glass  ")
        result = audit_module.audit(payload(output))
        self.assertFalse(result["passed"])
        self.assertIn("expected break-glass account is missing", result["findings"])

        service_admin = CLI_FIXTURE.replace("true      false    false\n3", "true      true     false\n3")
        result = audit_module.audit(payload(service_admin))
        self.assertFalse(result["passed"])
        self.assertIn("Forgejo service account has administrator rights", result["findings"])

    def test_unknown_cli_format_fails_closed(self):
        result = audit_module.audit(payload(CLI_FIXTURE.replace("2FA", "TwoFA", 1)))
        self.assertFalse(result["passed"])
        self.assertIn("unsupported Forgejo CLI user-list format", result["findings"])

    def test_empty_email_cell_is_supported_without_exposing_data(self):
        output = CLI_FIXTURE.replace("1     forgejo-break-glass  break-glass@example.invalid           true      true     true",
                                     "1     forgejo-break-glass  true      true     true")
        result = audit_module.audit(payload(output))
        self.assertTrue(result["passed"])

    def test_malformed_authentik_identity_input_fails_closed(self):
        result = audit_module.audit(payload(ops_admins=["", "operator-user"]))
        self.assertFalse(result["passed"])
        self.assertIn("ops-admin identity list is missing or malformed", result["findings"])

    def test_failed_authentik_query_fails_closed(self):
        input_payload = payload()
        input_payload["ops_admins_rc"] = 1
        result = audit_module.audit(input_payload)
        self.assertFalse(result["passed"])
        self.assertIn("Authentik ops-admin group query failed", result["findings"])

    def test_report_discards_emails_and_usernames_and_states_binding_gap(self):
        rendered = json.dumps(audit_module.audit(payload()))
        for private_value in (
            "break-glass@example.invalid",
            "operator@example.invalid",
            "contributor@example.invalid",
            "operator-user",
            "forgejo-break-glass",
        ):
            self.assertNotIn(private_value, rendered)
        self.assertIn("pending supported read-only source-key query", rendered)
        self.assertIn("not available from the supported CLI output", rendered)


if __name__ == "__main__":
    unittest.main()
