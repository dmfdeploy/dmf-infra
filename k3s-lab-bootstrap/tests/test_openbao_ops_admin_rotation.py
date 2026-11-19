"""Run playbooks/vertical-orchestration/120-ops-admin-rotation.yml against the fake cluster.

The play runs for real, with the fake kubectl and the fake bao of tests/fixtures, which
behave like OpenBao 2.6 and later: root token generation needs a live token. The test
copies the play, drops the role and `become` that need a cluster, and runs it on localhost.

Cases:
- a rotation writes the password once, sets the policies, and revokes its three tokens;
- the generate-root token trades itself for a short-lived orphan token, is revoked and
  refused before the first write, and every write runs with the short token (checked by
  fixtures/token_lifecycle.py, which also needs a refusal of both tokens at the end);
- an attach-only run sets the policies, writes no password, leaves the break-glass file
  as it was, and revokes both tokens;
- the attach keeps a policy that ops-admin already holds, and writes the same list twice;
- a failure after the root token exists still revokes the root token and the login token;
- a revoke that fails every try stops the play with the accessor, and one that fails once
  works on the retry; a generate-root revoke that fails stops the play before any write;
- a lifetime outside 10 to 15 minutes, a failed mint and a revoke that the server cannot
  confirm each stop the play before any write;
- a mint whose answer is lost warns that a short-lived root-policy token may exist, ends the
  play failed, and still revokes the generate-root and the login token;
- a failed root-policy check still reports the accessor of the generate-root token;
- a run that ends UNREACHABLE leaves the generate-root token only before its revoke (with a
  short token too after the mint), and only the short token after it (the always section
  never runs);
- a failure between the last share and the decode says that a root token may exist;
- no token, OTP, share or password reaches the arguments of kubectl;
- a failed login runs no generate-root call.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
PLAY = ROOT / "playbooks/vertical-orchestration/120-ops-admin-rotation.yml"
FIXTURES = ROOT / "tests/fixtures"
sys.path.insert(0, str(FIXTURES))
import token_lifecycle  # noqa: E402

LOGIN_TOKEN = "fake-ops-token"
ROOT_TOKEN = "fake-root-genroot"
SHORT_TOKEN = "fake-root-short-1"
# The calls of the play that write: the policies and the password of the user.
WRITES = ("write auth/userpass/users/",)
# ansible-playbook exit status when a host is unreachable
UNREACHABLE_STATUS = 4
SHARES = ["share-one-SECRET", "share-two-SECRET", "share-three-SECRET"]
PASSWORD = "ops-admin-password-SECRET"
OTP = "otp-SENTINEL-SECRET"


@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook is not on PATH")
class RotationRunTests(unittest.TestCase):
    def run_play(self, extra_vars=None, env=None, unreachable_after=None):
        """Run the play. unreachable_after names a task of the token block: a task is added
        after it that makes the host UNREACHABLE, as a lost connection does."""
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
        if unreachable_after:
            block = next(t for t in play[0]["tasks"] if t.get("block"))["block"]
            at = next(i for i, task in enumerate(block) if task["name"] == unreachable_after)
            block.insert(at + 1, {"name": "Lose the connection", "kubernetes.core.k8s_info": {
                "kubeconfig": "/etc/rancher/k3s/k3s.yaml", "api_version": "v1", "kind": "Pod", "name": "lost"}})
            env = {**(env or {}), "FAKE_K8S_UNREACHABLE": "Pod/lost"}
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
        self.assertTrue(self.revoked(state, SHORT_TOKEN))

    def lifecycle(self, state, minted, root_revoked, short_revoked):
        return token_lifecycle.problems(str(state), ROOT_TOKEN, minted, root_revoked, short_revoked, WRITES)

    def write_calls(self, state):
        """The calls that write, as "TOKEN ARGS", in order."""
        lines = (state / "bao-calls.log").read_text().splitlines()
        return [line for line in lines if line.partition(" ")[2].startswith(WRITES)]

    def test_the_generate_root_token_is_revoked_before_the_first_write_and_the_short_token_writes(self) -> None:
        result, state, _ = self.run_play()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.lifecycle(state, 1, "yes", "yes"), [])
        writes = self.write_calls(state)
        self.assertEqual(len(writes), 2, writes)
        self.assertEqual({line.split()[0] for line in writes}, {SHORT_TOKEN})
        calls = (state / "bao-calls.log").read_text().splitlines()
        first_write = calls.index(writes[0])
        self.assertIn(f"{ROOT_TOKEN} =revoked", calls[:first_write])
        self.assertIn(f"{ROOT_TOKEN} =refused", calls[:first_write])
        self.assertEqual([line for line in calls if line.startswith(ROOT_TOKEN + " ") and not line.startswith(ROOT_TOKEN + " =")],
                         [f"{ROOT_TOKEN} token lookup -format=json",
                          f"{ROOT_TOKEN} token create -orphan -policy=root -renewable=false -ttl=10m -format=json",
                          f"{ROOT_TOKEN} token revoke -self",
                          f"{ROOT_TOKEN} token lookup -format=json"])
        self.assertIn(f"{ROOT_TOKEN} =refused", calls)
        self.assertIn(f"{SHORT_TOKEN} =refused", calls)

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
                self.assertTrue(self.revoked(state, SHORT_TOKEN), result.stdout)
                self.assertEqual(self.lifecycle(state, 1, "yes", "yes"), [])
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
        for secret in [OTP, LOGIN_TOKEN, ROOT_TOKEN, SHORT_TOKEN, PASSWORD, *SHARES, "fake-encoded"]:
            self.assertNotIn(secret, argv)

    def test_a_root_revoke_that_fails_every_try_stops_the_play_before_any_write(self) -> None:
        result, state, breakglass = self.run_play(env={"FAKE_REVOKE_FAILS": "root"})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("a live root token may remain", " ".join(result.stdout.split()))
        self.assertIn(ROOT_TOKEN + "-accessor", result.stdout)
        # Four tries in the revoke before the first write, four more in the always section.
        self.assertEqual((state / "count-revoke-root").read_text().strip(), "8")
        self.assertEqual(self.write_calls(state), [])
        self.assertFalse((state / "userpass-user.json").exists())
        self.assertTrue(self.revoked(state, LOGIN_TOKEN))
        self.assertTrue(self.revoked(state, SHORT_TOKEN))
        self.assertFalse(self.revoked(state, ROOT_TOKEN))
        self.assertEqual(self.lifecycle(state, 1, "no", "yes"), [])
        self.assertEqual(json.loads(breakglass.read_text())["ops_admin_password"], PASSWORD)

    def test_a_short_token_revoke_that_fails_every_try_stops_the_play_with_the_accessor(self) -> None:
        result, state, _ = self.run_play(env={"FAKE_REVOKE_FAILS": "short"})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("short-lived write token is NOT revoked", " ".join(result.stdout.split()))
        self.assertIn(SHORT_TOKEN + "-accessor", result.stdout)
        self.assertTrue(self.revoked(state, ROOT_TOKEN))
        self.assertTrue(self.revoked(state, LOGIN_TOKEN))
        self.assertFalse(self.revoked(state, SHORT_TOKEN))
        self.assertEqual(self.lifecycle(state, 1, "yes", "no"), [])

    def test_a_revoke_that_the_server_cannot_confirm_stops_the_play_before_any_write(self) -> None:
        # The lookup of the revoked token never reaches the server, so nothing proves the revoke.
        for failure in ("refused", "local-denied"):
            with self.subTest(failure=failure):
                result, state, _ = self.run_play(env={"FAKE_LOOKUP_TRANSPORT_FAILS": failure})

                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.write_calls(state), [])
                self.assertFalse((state / "userpass-user.json").exists())
                self.assertIn("The generate-root token could not be revoked", " ".join(result.stdout.split()))

    def test_a_lifetime_outside_10_to_15_minutes_stops_the_play_before_the_mint(self) -> None:
        for ttl in ("5m", "30m", "1h", "10s"):
            with self.subTest(ttl=ttl):
                result, state, _ = self.run_play({"ops_admin_rotation_write_token_ttl": ttl})

                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("token create", (state / "bao-calls.log").read_text())
                self.assertEqual(self.write_calls(state), [])
                self.assertEqual(self.lifecycle(state, 0, "yes", "no"), [])

    def test_a_failed_mint_stops_the_play_before_any_write(self) -> None:
        result, state, _ = self.run_play(env={"FAKE_TOKEN_CREATE_FAILS": "1"})

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.write_calls(state), [])
        self.assertEqual(self.lifecycle(state, 0, "yes", "no"), [])
        self.assertTrue(self.revoked(state, LOGIN_TOKEN))

    def test_a_lost_answer_to_the_mint_warns_fails_and_still_revokes_the_other_tokens(self) -> None:
        # The fake creates and records the short token, then loses the answer.
        result, state, _ = self.run_play(env={"FAKE_TOKEN_CREATE_LOST": "1"})

        text = " ".join(result.stdout.split())
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("a token with the root policy may exist that nothing revoked", text)
        self.assertIn("bao list auth/token/accessors", text)
        self.assertIn("did not read the token from the answer", text)
        self.assertNotIn(SHORT_TOKEN, result.stdout)
        self.assertEqual(self.write_calls(state), [])
        self.assertTrue((state / "tokens" / "fake-root-short-1.json").exists())
        self.assertTrue(self.revoked(state, ROOT_TOKEN))
        self.assertTrue(self.revoked(state, LOGIN_TOKEN))
        self.assertFalse(self.revoked(state, SHORT_TOKEN))
        self.assertEqual(self.lifecycle(state, 1, "yes", "no"), [])

    def test_a_run_that_minted_and_read_the_token_gives_no_mint_warning(self) -> None:
        result, _, _ = self.run_play()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("may exist that nothing revoked", result.stdout)

    def test_a_failed_root_policy_check_and_a_failed_revoke_still_report_the_accessor(self) -> None:
        result, state, _ = self.run_play(env={"FAKE_DECODED_TOKEN": "fake-plain-token", "FAKE_REVOKE_FAILS": "login"})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("could not be looked up, or does not hold the root policy", " ".join(result.stdout.split()))
        self.assertIn("fake-plain-token-accessor", result.stdout)
        self.assertNotIn("token create", (state / "bao-calls.log").read_text())

    def test_unreachable_after_the_mint_leaves_both_the_generate_root_and_the_short_token(self) -> None:
        result, state, _ = self.run_play(unreachable_after="Mint a short-lived orphan token with the root policy")

        self.assertEqual(result.returncode, UNREACHABLE_STATUS, result.stdout + result.stderr)
        self.assertEqual(self.lifecycle(state, 1, "no", "no"), [])
        self.assertEqual(self.write_calls(state), [])
        self.assertFalse(self.revoked(state, ROOT_TOKEN))
        self.assertFalse(self.revoked(state, SHORT_TOKEN))

    def test_a_token_that_is_not_root_stops_the_play_before_the_mint(self) -> None:
        # The fake treats a decoded token that does not start with fake-root as a plain token.
        result, state, _ = self.run_play(env={"FAKE_DECODED_TOKEN": "fake-plain-token"})

        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn("token create", (state / "bao-calls.log").read_text())
        self.assertEqual(self.write_calls(state), [])

    def test_unreachable_before_the_revoke_leaves_the_generate_root_token(self) -> None:
        result, state, _ = self.run_play(unreachable_after="Assert the mint returned a non-renewable token of 10 to 15 minutes")

        self.assertEqual(result.returncode, UNREACHABLE_STATUS, result.stdout + result.stderr)
        self.assertEqual(self.lifecycle(state, 1, "no", "no"), [])
        self.assertEqual(self.write_calls(state), [])

    def test_unreachable_after_the_revoke_leaves_only_the_short_token(self) -> None:
        result, state, _ = self.run_play(unreachable_after="Stop when the generate-root token is not revoked")

        self.assertEqual(result.returncode, UNREACHABLE_STATUS, result.stdout + result.stderr)
        self.assertEqual(self.lifecycle(state, 1, "yes", "no"), [])
        self.assertEqual(self.write_calls(state), [])

    def test_unreachable_after_the_writes_leaves_only_the_short_token(self) -> None:
        result, state, breakglass = self.run_play(unreachable_after="Persist rotated ops-admin password to break-glass file")

        self.assertEqual(result.returncode, UNREACHABLE_STATUS, result.stdout + result.stderr)
        self.assertEqual(self.lifecycle(state, 1, "yes", "no"), [])
        self.assertEqual(len(self.write_calls(state)), 2)
        self.assertNotEqual(json.loads(breakglass.read_text())["ops_admin_password"], PASSWORD)
        self.assertFalse(self.revoked(state, LOGIN_TOKEN))

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

    def test_every_kubectl_call_uses_the_namespace_of_the_play(self) -> None:
        # The active-pod search must look where the rest of the play runs.
        result, state, _ = self.run_play({"ops_admin_rotation_attach_only": True,
                                          "ops_admin_rotation_namespace": "vault-elsewhere"})

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        argv = (state / "kubectl-argv.log").read_text()
        self.assertIn("status -format=json", argv)
        self.assertEqual(set(re.findall(r"(?:^|\s)-n\s+(\S+)", argv)), {"vault-elsewhere"})

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
