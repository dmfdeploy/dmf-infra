"""Run playbooks/vertical-orchestration/120-ops-admin-rotation.yml against the fake cluster.

The play runs for real, with the fake kubectl and the fake bao of tests/fixtures, which
behave like OpenBao 2.6 and later: root token generation needs a live token. The test
copies the play, drops the role and `become` that need a cluster, and runs it on localhost.

Cases:
- a rotation writes the password once, sets the policies, and revokes both tokens;
- an attach-only run sets the policies, writes no password, leaves the break-glass file
  as it was, and revokes both tokens;
- the attach keeps a policy that ops-admin already holds, and writes the same list twice;
- a failure after the root token exists still revokes the root token and the login token;
- a revoke that fails every try stops the play with the accessor, and one that fails once
  works on the retry;
- a failure between the last share and the decode says that a root token may exist;
- no token, OTP, share or password reaches the arguments of kubectl;
- a failed login runs no generate-root call.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PLAY = ROOT / "playbooks/vertical-orchestration/120-ops-admin-rotation.yml"
FIXTURES = ROOT / "tests/fixtures"
LOGIN_TOKEN = "fake-ops-token"
ROOT_TOKEN = "fake-root-genroot"
SHARES = ["share-one-SECRET", "share-two-SECRET", "share-three-SECRET"]
PASSWORD = "ops-admin-password-SECRET"
OTP = "otp-SENTINEL-SECRET"


@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook is not on PATH")
class RotationRunTests(unittest.TestCase):
    def run_play(self, extra_vars=None, env=None):
        work = Path(tempfile.mkdtemp(prefix="ops-admin-rotation-"))
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        state = work / "state"
        (state / "k8s").mkdir(parents=True)
        (state / "k8s/Pod-all.json").write_text(json.dumps([{"metadata": {"name": n}} for n in ("openbao-0", "openbao-1", "openbao-2")]))
        breakglass = work / "breakglass.json"
        breakglass.write_text(json.dumps({
            "ops_admin_username": "ops-admin",
            "ops_admin_password": PASSWORD,
            "unseal_keys_hex": SHARES,
        }))
        play = yaml.safe_load(PLAY.read_text())
        play[0].update(hosts="localhost", connection="local")
        play[0].pop("become")
        play[0].pop("roles")
        playbook = work / "play.yml"
        playbook.write_text(yaml.safe_dump(play))
        variables = {
            "eso_openbao_breakglass_file": str(breakglass),
            "ops_admin_rotation_revoke_delay": 0,
            **(extra_vars or {}),
        }
        run_env = {
            **os.environ,
            "PATH": f"{FIXTURES / 'fake-kubectl'}:{os.environ['PATH']}",
            "ANSIBLE_COLLECTIONS_PATH": str(FIXTURES / "fake-collections"),
            "FAKE_STATE": str(state),
            "FAKE_PASSWORD": PASSWORD,
            "FAKE_GENROOT_AUTH": "1",
            "FAKE_OTP": OTP,
            "FAKE_ACTIVE_POD": "openbao-1",
            **(env or {}),
        }
        result = subprocess.run(
            ["ansible-playbook", str(playbook), "-e", json.dumps(variables)],
            capture_output=True, text=True, env=run_env, cwd=ROOT,
        )
        return result, state, breakglass

    def revoked(self, state, token):
        return (state / f"revoked-{token}").exists()

    def test_rotation_writes_one_password_and_revokes_both_tokens(self) -> None:
        result, state, breakglass = self.run_play()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.user(state), {"token_policies": ["app-admin-writer", "app-runtime-writer", "generate-root"],
                                            "password_writes": 1})
        self.assertNotEqual(json.loads(breakglass.read_text())["ops_admin_password"], PASSWORD)
        self.assertEqual((state / "genroot-shares.log").read_text().split(), SHARES)
        self.assertTrue(self.revoked(state, ROOT_TOKEN))
        self.assertTrue(self.revoked(state, LOGIN_TOKEN))

    def test_attach_only_never_writes_a_password(self) -> None:
        result, state, breakglass = self.run_play({"ops_admin_rotation_attach_only": True})

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.user(state), {"token_policies": ["app-admin-writer", "app-runtime-writer", "generate-root"],
                                            "password_writes": 0})
        self.assertEqual(json.loads(breakglass.read_text())["ops_admin_password"], PASSWORD)
        events = (state / "events.log").read_text()
        self.assertNotIn("write auth/userpass/users/ops-admin -", events)
        self.assertTrue(self.revoked(state, ROOT_TOKEN))
        self.assertTrue(self.revoked(state, LOGIN_TOKEN))

    def test_attach_only_works_on_the_legacy_endpoints_of_2_5_2(self) -> None:
        result, state, _ = self.run_play({"ops_admin_rotation_attach_only": True}, {"FAKE_GENROOT_AUTH": ""})

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.user(state)["password_writes"], 0)

    def test_failure_after_the_root_token_exists_revokes_both_tokens(self) -> None:
        for name, extra, env in [
            ("policies write", {}, {"FAKE_POLICIES_WRITE_FAILS": "1"}),
            ("password write", {}, {"FAKE_PASSWORD_WRITE_FAILS": "1"}),
        ]:
            with self.subTest(failure=name):
                result, state, breakglass = self.run_play(extra, env)

                self.assertNotEqual(result.returncode, 0)
                self.assertTrue((state / "genroot-shares.log").exists())
                self.assertTrue(self.revoked(state, ROOT_TOKEN), result.stdout)
                self.assertTrue(self.revoked(state, LOGIN_TOKEN), result.stdout)
                self.assertEqual(json.loads(breakglass.read_text())["ops_admin_password"], PASSWORD)

    def user(self, state):
        """The user record that the fake keeps: the effective policies and the password writes."""
        return json.loads((state / "userpass-user.json").read_text())

    def test_both_modes_keep_a_policy_that_ops_admin_already_holds(self) -> None:
        for mode in (False, True):
            with self.subTest(attach_only=mode):
                result, state, _ = self.run_play(
                    {"ops_admin_rotation_attach_only": mode},
                    {"FAKE_OPS_POLICIES": "app-admin-writer,extra-policy,app-runtime-writer"},
                )

                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertEqual(self.user(state)["token_policies"],
                                 ["app-admin-writer", "extra-policy", "app-runtime-writer", "generate-root"])
                self.assertEqual(self.user(state)["password_writes"], 0 if mode else 1)

    def test_a_user_that_holds_the_policy_gets_the_same_list_again(self) -> None:
        held = "app-admin-writer,generate-root,extra-policy"
        result, state, _ = self.run_play({"ops_admin_rotation_attach_only": True}, {"FAKE_OPS_POLICIES": held})

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.user(state), {"token_policies": held.split(","), "password_writes": 0})

    def test_no_secret_reaches_the_arguments_of_kubectl(self) -> None:
        result, state, _ = self.run_play()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        argv = (state / "kubectl-argv.log").read_text()
        self.assertIn("generate-root", argv)
        for secret in [OTP, LOGIN_TOKEN, ROOT_TOKEN, PASSWORD, *SHARES, "fake-encoded"]:
            self.assertNotIn(secret, argv)

    def test_a_root_revoke_that_fails_every_try_stops_the_play_with_the_accessor(self) -> None:
        result, state, _ = self.run_play(env={"FAKE_REVOKE_FAILS": "root"})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("a live root token may", result.stdout)
        self.assertIn(ROOT_TOKEN + "-accessor", result.stdout)
        self.assertEqual((state / "count-revoke-root").read_text().strip(), "3")
        self.assertTrue(self.revoked(state, LOGIN_TOKEN))
        self.assertFalse(self.revoked(state, ROOT_TOKEN))

    def test_a_login_revoke_that_fails_every_try_stops_the_play_with_the_accessor(self) -> None:
        result, state, _ = self.run_play(env={"FAKE_REVOKE_FAILS": "login"})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("login token failed", result.stdout)
        self.assertIn("fake-ops-accessor", result.stdout)
        self.assertEqual((state / "count-revoke-login").read_text().strip(), "3")
        self.assertTrue(self.revoked(state, ROOT_TOKEN))

    def test_a_revoke_that_fails_once_works_on_the_retry(self) -> None:
        result, state, _ = self.run_play(env={"FAKE_REVOKE_FAIL_FIRST": "2"})

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue(self.revoked(state, ROOT_TOKEN))
        self.assertTrue(self.revoked(state, LOGIN_TOKEN))

    def test_a_failure_between_the_last_share_and_the_decode_says_a_root_token_may_exist(self) -> None:
        result, state, breakglass = self.run_play(env={"FAKE_DECODE_FAILS": "1"})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("a root token may exist", result.stdout)
        self.assertIn("bao list auth/token/accessors", result.stdout)
        self.assertTrue(self.revoked(state, LOGIN_TOKEN))
        self.assertFalse((state / "userpass-user.json").exists())
        self.assertEqual(json.loads(breakglass.read_text())["ops_admin_password"], PASSWORD)

    def test_a_lost_answer_to_the_last_share_still_says_a_root_token_may_exist(self) -> None:
        for name, env in [
            ("answer lost", {"FAKE_GENROOT_LOSE_RESPONSE": "1"}),
            ("answer lost and login revoke fails", {"FAKE_GENROOT_LOSE_RESPONSE": "1", "FAKE_REVOKE_FAILS": "login"}),
        ]:
            with self.subTest(case=name):
                result, state, _ = self.run_play(env=env)

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("a root token may exist", result.stdout)
                self.assertIn("bao list auth/token/accessors", result.stdout)
                self.assertIn("fake-ops-accessor", result.stdout)
                self.assertEqual((state / "genroot-shares.log").read_text().split(), SHARES)
                self.assertFalse(self.revoked(state, ROOT_TOKEN))

    def generate_root_pods(self, state):
        events = (state / "events.log").read_text().splitlines()
        return {line.split()[1] for line in events if " operator generate-root " in line}

    def test_the_play_runs_generate_root_in_the_active_pod_and_not_the_first_one(self) -> None:
        # A 2.5.2 standby answers the legacy generate-root endpoint with a 500.
        result, state, _ = self.run_play({"ops_admin_rotation_attach_only": True}, {"FAKE_GENROOT_AUTH": ""})

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.generate_root_pods(state), {"openbao-1"})

    def test_the_play_follows_the_active_pod_when_it_moves(self) -> None:
        result, state, _ = self.run_play({"ops_admin_rotation_attach_only": True},
                                         {"FAKE_GENROOT_AUTH": "", "FAKE_ACTIVE_POD": "openbao-2"})

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.generate_root_pods(state), {"openbao-2"})

    def test_the_play_stops_when_no_pod_or_two_pods_are_active(self) -> None:
        for active in ("", "openbao-0 openbao-1"):
            with self.subTest(active=active):
                result, state, _ = self.run_play(env={"FAKE_ACTIVE_POD": active})

                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Expected exactly one active OpenBao pod", result.stdout)
                self.assertFalse((state / "genroot-shares.log").exists())

    def test_a_failed_login_runs_no_generate_root_call(self) -> None:
        result, state, _ = self.run_play(env={"FAKE_PASSWORD": "another-password"})

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("operator generate-root", (state / "events.log").read_text() if (state / "events.log").exists() else "")
        self.assertFalse(self.revoked(state, ROOT_TOKEN))


if __name__ == "__main__":
    unittest.main()
