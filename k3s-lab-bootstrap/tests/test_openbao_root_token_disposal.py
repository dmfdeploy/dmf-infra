"""Regression guards for bootstrap root-token disposal (#11)."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
OPENBAO_TASKS = ROOT / "roles/stack/operator/openbao/tasks/main.yml"
CONVERGENCE = ROOT / "playbooks/verify-bootstrap-convergence.yml"


class OpenBaoRootTokenDisposalTests(unittest.TestCase):
    def test_init_response_is_never_written_to_breakglass_file(self) -> None:
        """Old code persisted the raw init JSON, including its root token."""
        source = OPENBAO_TASKS.read_text()

        self.assertNotIn('content: "{{ openbao_init_raw.stdout }}"', source)
        self.assertIn("Write automation file (shares 1+2+3 and operator creds)", source)

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


if __name__ == "__main__":
    unittest.main()
