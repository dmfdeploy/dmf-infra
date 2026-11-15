"""A run of the installed-environment play that ends with the host UNREACHABLE.

Ansible runs no further task for an unreachable host, so the `always` section never
revokes anything. The play therefore must not rely on it for the generate-root token,
which has no lifetime: it trades that token for a short-lived orphan token and revokes
it before the first write. These tests end the play at three points, with the fake
kubernetes.core returning an unreachable result, and read what the fake bao recorded:

  - Pod/openbao-0: before any call with the token. Nothing was minted, and the generate-root token
    is still valid. This is the residual window; the play header says what to do.
  - ServiceAccount/setup-issuer: after the config write, before the role writes. The generate-root
    token is revoked and only the short-lived token is left.
  - ConfigMap: after every write. The same state.

The fake kubernetes.core can make only its own tasks unreachable. A lost connection
during a shell task, the way a real run ends, is not modeled here.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(FIXTURES))
import token_lifecycle  # noqa: E402

ROOT_TOKEN = "fake-root-token-1"
# ansible-playbook exit status when a host is unreachable
UNREACHABLE_STATUS = 4


class UnreachableRunTest(unittest.TestCase):
    def run_play(self, at):
        state = Path(tempfile.mkdtemp(prefix="k8s-auth-unreachable-"))
        self.addCleanup(shutil.rmtree, state, True)
        playbook = shutil.which("ansible-playbook")
        self.assertIsNotNone(playbook, "ansible-playbook is not on PATH")
        env = {
            **os.environ,
            "PATH": "%s:%s" % (FIXTURES / "fake-kubectl", os.environ["PATH"]),
            "ANSIBLE_COLLECTIONS_PATH": "%s:%s" % (FIXTURES / "fake-collections", Path.home() / ".ansible/collections"),
        }
        result = subprocess.run(
            [playbook, "tests/_openbao-k8s-auth-unreachable-play.yml",
             "-e", "unreachable_state=%s" % state, "-e", "unreachable_token=%s" % ROOT_TOKEN,
             "-e", "unreachable_at=%s" % at],
            cwd=ROOT, env=env, capture_output=True, text=True,
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, UNREACHABLE_STATUS, output)
        self.assertIn("UNREACHABLE", output)
        return state / "0"

    def test_unreachable_before_the_token_is_used_leaves_the_generate_root_token(self):
        state = self.run_play("Pod/openbao-0")
        self.assertEqual(token_lifecycle.problems(str(state), ROOT_TOKEN, 0, "no", "no"), [])
        self.assertFalse((state / "bao-calls.log").exists())

    def test_unreachable_between_the_writes_leaves_only_the_short_lived_token(self):
        state = self.run_play("ServiceAccount/setup-issuer")
        self.assertEqual(token_lifecycle.problems(str(state), ROOT_TOKEN, 1, "yes", "no"), [])
        self.assertTrue((state / "k8s-auth" / "config.json").exists())
        self.assertFalse((state / "k8s-auth" / "role-setup-issuer.json").exists())

    def test_unreachable_after_the_writes_leaves_only_the_short_lived_token(self):
        state = self.run_play("ConfigMap/openbao-kubernetes-auth-record")
        self.assertEqual(token_lifecycle.problems(str(state), ROOT_TOKEN, 1, "yes", "no"), [])
        self.assertTrue((state / "k8s-auth" / "role-setup-issuer.json").exists())


if __name__ == "__main__":
    unittest.main()
