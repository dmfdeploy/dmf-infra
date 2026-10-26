"""Regression guards for bootstrap root-token disposal (#11)."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
OPENBAO_TASKS = ROOT / "roles/stack/operator/openbao/tasks/main.yml"
CONVERGENCE = ROOT / "playbooks/verify-bootstrap-convergence.yml"
OPENBAO_PLAYBOOK = ROOT / "playbooks/vertical-security/100-openbao.yml"


class OpenBaoRootTokenDisposalTests(unittest.TestCase):
    def test_init_response_is_never_written_to_breakglass_file(self) -> None:
        """Old code persisted the raw init JSON, including its root token."""
        source = OPENBAO_TASKS.read_text()

        self.assertNotIn('content: "{{ openbao_init_raw.stdout }}"', source)
        self.assertNotIn("Write automation file (shares 1+2+3 and operator creds)", source)

    def test_sanitized_recovery_artifact_precedes_fallible_distribution(self) -> None:
        """A failed USB/JuiceFS/Keychain preflight must retain the quorum."""
        source = OPENBAO_TASKS.read_text()

        recovery = source.index("Save root-token-free recovery artifact before share distribution")
        distribution = source.index("Assert OPENBAO_A USB is mounted")
        self.assertLess(recovery, distribution)

    def test_rerun_and_convergence_reject_any_root_token_field(self) -> None:
        """Aliases beyond the historical three must fail both safeguards."""
        pattern = "(?i)(root.*token|token.*root)"

        self.assertGreaterEqual(
            OPENBAO_TASKS.read_text().count(pattern),
            1,
        )
        self.assertGreaterEqual(
            CONVERGENCE.read_text().count(pattern),
            1,
        )

    def test_playbook_disposes_a_fresh_root_when_role_bootstrap_fails(self) -> None:
        """A failure before the role's final revoke must not strand root."""
        source = OPENBAO_PLAYBOOK.read_text()

        self.assertIn("Run OpenBao bootstrap with fresh-root failure disposal", source)
        self.assertIn("public: true", source)
        self.assertIn("rescue:", source)
        self.assertIn("Unseal OpenBao before failed-init root disposal", source)
        self.assertIn("Revoke fresh-init root token after failed bootstrap", source)
        self.assertIn("Assert failed-bootstrap root disposal succeeded", source)
        self.assertIn("Re-fail after fresh-root cleanup preserves bootstrap evidence", source)
        self.assertLess(
            source.index("Unseal OpenBao before failed-init root disposal"),
            source.index("Revoke fresh-init root token after failed bootstrap"),
        )


if __name__ == "__main__":
    unittest.main()
