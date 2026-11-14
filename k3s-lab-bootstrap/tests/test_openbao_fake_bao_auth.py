"""The fake bao must refuse a call that carries no valid token.

The tests that run the real plays against tests/fixtures/fake-bao/bao assume that it
behaves like OpenBao 2.6 and later. This file checks the assumption directly: a missing,
an unknown and a revoked token get refused, and the paths that need a root token refuse
the ops-admin login token. Set FAKE_BAO_PATH to run the same checks against another copy
of the fake, for example one with its authorization check removed.
"""
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKE = Path(os.environ.get("FAKE_BAO_PATH", ROOT / "tests/fixtures/fake-bao/bao"))
LOGIN = "fake-ops-token"
ROOT_TOKEN = "fake-root-genroot"
GENERATE_OTP = ["operator", "generate-root", "-generate-otp", "-format=json"]
POLICIES_WRITE = ["write", "auth/userpass/users/ops-admin/policies", "-"]
PASSWORD_WRITE = ["write", "auth/userpass/users/ops-admin", "-"]
USER_READ = ["read", "-format=json", "auth/userpass/users/ops-admin"]
LOOKUP = ["token", "lookup", "-format=json"]


class FakeBaoAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = Path(tempfile.mkdtemp(prefix="fake-bao-auth-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.state)]))

    def call(self, args, token=None, authed=True):
        env = {**os.environ, "FAKE_STATE": str(self.state), "FAKE_POD": "openbao-0"}
        env.pop("BAO_TOKEN", None)
        env.pop("FAKE_GENROOT_AUTH", None)
        if authed:
            env["FAKE_GENROOT_AUTH"] = "1"
        if token is not None:
            env["BAO_TOKEN"] = token
        return subprocess.run([str(FAKE), *args], input="{}", capture_output=True, text=True, env=env)

    def revoke(self, token) -> None:
        (self.state / f"revoked-{token}").touch()

    def test_generate_root_refuses_a_missing_an_unknown_and_a_revoked_token(self) -> None:
        self.assertNotEqual(self.call(GENERATE_OTP).returncode, 0)
        self.assertNotEqual(self.call(GENERATE_OTP, token="unknown-token").returncode, 0)
        self.revoke(LOGIN)
        self.assertNotEqual(self.call(GENERATE_OTP, token=LOGIN).returncode, 0)

    def test_generate_root_accepts_the_live_login_token(self) -> None:
        self.assertEqual(self.call(GENERATE_OTP, token=LOGIN).returncode, 0)

    def test_a_2_5_2_server_ignores_the_token(self) -> None:
        self.assertEqual(self.call(GENERATE_OTP, authed=False).returncode, 0)

    def test_userpass_paths_need_a_live_root_token(self) -> None:
        for args in (POLICIES_WRITE, PASSWORD_WRITE, USER_READ):
            with self.subTest(args=args[1:3]):
                self.assertNotEqual(self.call(args).returncode, 0)
                self.assertNotEqual(self.call(args, token="unknown-token").returncode, 0)
                self.assertNotEqual(self.call(args, token=LOGIN).returncode, 0)
                self.assertEqual(self.call(args, token=ROOT_TOKEN).returncode, 0)
                self.revoke(ROOT_TOKEN)
                self.assertNotEqual(self.call(args, token=ROOT_TOKEN).returncode, 0)
                (self.state / f"revoked-{ROOT_TOKEN}").unlink()

    def test_lookup_refuses_a_missing_and_a_revoked_token(self) -> None:
        self.assertNotEqual(self.call(LOOKUP).returncode, 0)
        self.revoke(LOGIN)
        self.assertNotEqual(self.call(LOOKUP, token=LOGIN).returncode, 0)

    def test_a_subcommand_that_the_real_cli_lacks_is_rejected(self) -> None:
        for args in (["token", "lookup-self", "-format=json"], ["frobnicate"], ["token", "renew-all"]):
            with self.subTest(args=args):
                result = self.call(args, token=ROOT_TOKEN)
                self.assertEqual(result.returncode, 127)
                self.assertIn("unknown command", result.stderr)

    def test_the_user_record_applies_the_fields_of_each_write(self) -> None:
        def user(args=None, body="{}", token=ROOT_TOKEN):
            env = {**os.environ, "FAKE_STATE": str(self.state), "FAKE_POD": "openbao-0", "BAO_TOKEN": token}
            env["FAKE_OPS_POLICIES"] = "a,b"
            run = subprocess.run([str(FAKE), *(args or USER_READ)], input=body, capture_output=True, text=True, env=env)
            return run

        import json as _json
        self.assertEqual(_json.loads(user().stdout)["data"]["token_policies"], ["a", "b"])
        user(PASSWORD_WRITE, '{"password": "x"}')
        record = _json.loads(user().stdout)["data"]
        self.assertEqual((record["token_policies"], record["password_writes"]), (["a", "b"], 1))
        user(PASSWORD_WRITE, '{"password": "y", "token_policies": "generate-root"}')
        record = _json.loads(user().stdout)["data"]
        self.assertEqual((record["token_policies"], record["password_writes"]), (["generate-root"], 2))
        user(POLICIES_WRITE, '{"token_policies": "a,b,generate-root"}')
        record = _json.loads(user().stdout)["data"]
        self.assertEqual((record["token_policies"], record["password_writes"]), (["a", "b", "generate-root"], 2))


if __name__ == "__main__":
    unittest.main()
