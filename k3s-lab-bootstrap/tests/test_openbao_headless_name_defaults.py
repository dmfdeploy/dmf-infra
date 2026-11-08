"""The openbao role and the cert-manager role both default openbao_headless_service_name.
The StatefulSet and the Raft peer addresses read it in the openbao role, and the
certificate's peer names read it in the cert-manager role. The two defaults must
stay equal, so that a site that sets nothing gets a certificate that covers the
pods' names."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT = re.compile(r"^openbao_headless_service_name:\s*(\S+)\s*$", re.M)


def default_of(relative_path):
    matches = DEFAULT.findall((ROOT / relative_path).read_text())
    assert len(matches) == 1, f"{relative_path} must set the default exactly once"
    return matches[0]


class HeadlessNameDefaults(unittest.TestCase):
    def test_both_roles_default_to_the_same_name(self):
        self.assertEqual(
            default_of("roles/stack/operator/openbao/defaults/main.yml"),
            default_of("roles/base/cert-manager/defaults/main.yml"),
        )


if __name__ == "__main__":
    unittest.main()
