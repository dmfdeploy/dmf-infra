"""The scoped-token, accessor, Kubernetes-login and identity parts of the fake bao.

The approval executor and the revoker rely on these behaviours of the fake, so each test
names the wrong fake that it would catch. Set FAKE_BAO_PATH to run the tests against
another copy of the fake.
"""
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FAKE = Path(os.environ.get("FAKE_BAO_PATH", ROOT / "tests/fixtures/fake-bao/bao"))
ROOT_TOKEN = "fake-root-genroot"
NOW = 1800000000
GENERATE_OTP = ["operator", "generate-root", "-generate-otp", "-format=json"]
LIST_ACCESSORS = ["list", "auth/token/accessors"]
SELF_LOOKUP = ["token", "lookup", "-format=json"]


class FakeBaoTokenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = Path(tempfile.mkdtemp(prefix="fake-bao-tokens-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.state)]))

    def call(self, args, token=None, now=NOW, genroot_auth=True, stdin="", **env):
        run_env = {**os.environ, "FAKE_STATE": str(self.state), "FAKE_POD": "openbao-0", "FAKE_NOW": str(now), **env}
        for name in ("BAO_TOKEN", "FAKE_GENROOT_AUTH"):
            run_env.pop(name, None)
        if genroot_auth:
            run_env["FAKE_GENROOT_AUTH"] = "1"
        if token is not None:
            run_env["BAO_TOKEN"] = token
        return subprocess.run([str(FAKE), *args], input=stdin, capture_output=True, text=True, env=run_env)

    def create(self, *flags, now=NOW, token=ROOT_TOKEN):
        result = self.call(["token", "create", *flags, "-format=json"], token=token, now=now)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["auth"]

    def scoped(self, *flags, **kwargs):
        return self.create("-orphan", "-policy=netbox-write", *flags, **kwargs)

    def role(self, name, policies, sa="executor", namespace="approvals") -> None:
        result = self.call(["write", f"auth/kubernetes/role/{name}", f"bound_service_account_names={sa}",
                            f"bound_service_account_namespaces={namespace}", f"token_policies={policies}",
                            "token_ttl=10m"], token=ROOT_TOKEN)
        self.assertEqual(result.returncode, 0, result.stderr)

    def login(self, role, jwt="fake-sa:approvals:executor"):
        return self.call(["write", "-format=json", "auth/kubernetes/login", f"role={role}", "jwt=-"], stdin=jwt)

    def revoker(self):
        """A live token that holds only the revoker's policy."""
        self.role("revoker", "token-revoker")
        return json.loads(self.login("revoker").stdout)["auth"]["client_token"]

    def test_the_use_limit_spends_one_use_per_request_then_revokes(self) -> None:
        # Catches a fake that ignores -use-limit, counts a use late or early (the token dies
        # on its first request or lives for one request too many), or never revokes.
        auth = self.scoped("-use-limit=2")
        token = auth["client_token"]
        self.assertEqual(auth["num_uses"], 2)
        self.assertEqual(self.call(SELF_LOOKUP, token=token).returncode, 0)
        self.assertEqual(self.call(SELF_LOOKUP, token=token).returncode, 0)
        refused = self.call(SELF_LOOKUP, token=token)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("permission denied", refused.stderr)
        self.assertTrue((self.state / f"revoked-{token}").exists())
        self.assertNotIn(auth["accessor"], self.call(LIST_ACCESSORS, token=ROOT_TOKEN).stdout)

    def test_a_refused_request_still_spends_a_use(self) -> None:
        # Catches a fake that counts only the requests that succeed: the token could then be
        # probed without end.
        token = self.scoped("-use-limit=1")["client_token"]
        self.assertNotEqual(self.call(LIST_ACCESSORS, token=token).returncode, 0)
        self.assertNotEqual(self.call(SELF_LOOKUP, token=token).returncode, 0)

    def test_a_last_use_spent_on_an_early_command_still_revokes(self) -> None:
        # Catches a fake whose exit handler runs before the revoke helper exists: a token
        # whose only request is `token capabilities`, which the first dispatcher answers
        # and exits on, would stay usable.
        auth = self.scoped("-use-limit=1")
        token = auth["client_token"]
        answered = self.call(["token", "capabilities", "-format=json", "secret/x"], token=token)
        self.assertEqual(answered.returncode, 0, answered.stderr)
        self.assertTrue((self.state / f"revoked-{token}").exists())
        self.assertNotIn(auth["accessor"], self.call(LIST_ACCESSORS, token=ROOT_TOKEN).stdout)

    def test_a_use_limit_of_zero_means_no_limit(self) -> None:
        # Catches a fake that stores 0 as a count of zero uses left and revokes the token
        # after its first request. OpenBao treats 0 as unlimited and reports num_uses 0.
        auth = self.scoped("-use-limit=0")
        token = auth["client_token"]
        self.assertEqual(auth["num_uses"], 0)
        for _ in range(4):
            self.assertEqual(self.call(SELF_LOOKUP, token=token).returncode, 0)
        self.assertFalse((self.state / f"revoked-{token}").exists())
        looked = self.call(["write", "-format=json", "auth/token/lookup-accessor", f"accessor={auth['accessor']}"],
                           token=ROOT_TOKEN)
        self.assertEqual(json.loads(looked.stdout)["data"]["num_uses"], 0)

    def test_a_token_without_a_use_limit_has_none(self) -> None:
        # Catches a fake that gives every scoped token a default limit.
        token = self.scoped()["client_token"]
        for _ in range(6):
            self.assertEqual(self.call(SELF_LOOKUP, token=token).returncode, 0)

    def test_create_records_the_flags_and_clamps_the_ttl_to_the_explicit_maximum(self) -> None:
        # Catches a fake that drops -ttl, -explicit-max-ttl or -renewable, or lets a TTL run
        # past the explicit maximum.
        auth = self.scoped("-ttl=15m", "-explicit-max-ttl=5m", "-renewable=false")
        self.assertEqual((auth["lease_duration"], auth["renewable"], auth["orphan"]), (300, False, True))
        self.assertEqual(auth["policies"], ["default", "netbox-write"])
        self.assertEqual(self.scoped("-ttl=10m", "-explicit-max-ttl=1h")["lease_duration"], 600)

    def test_a_scoped_token_cannot_create_tokens_or_act_as_root(self) -> None:
        # Catches a fake that treats every token that `token create` made as a root token.
        token = self.scoped()["client_token"]
        self.assertNotEqual(self.call(["token", "create", "-orphan", "-format=json"], token=token).returncode, 0)
        self.assertNotEqual(self.call(["write", "auth/userpass/users/ops-admin/policies", "-"], token=token, stdin="{}").returncode, 0)

    def test_a_root_policy_token_without_a_limit_keeps_its_old_form(self) -> None:
        # Catches a change to the tokens that the existing role tests count on.
        auth = self.create("-orphan", "-policy=root", "-renewable=false", "-ttl=15m")
        self.assertEqual(auth["client_token"], "fake-root-short-1")
        self.assertEqual(self.call(["token", "create", "-orphan", "-format=json"], token="fake-root-short-1").returncode, 0)

    def test_an_expired_token_is_refused(self) -> None:
        # Catches a fake that ignores the clock when it checks a token.
        token = self.scoped("-ttl=5m")["client_token"]
        self.assertEqual(self.call(SELF_LOOKUP, token=token, now=NOW + 299).returncode, 0)
        self.assertNotEqual(self.call(SELF_LOOKUP, token=token, now=NOW + 300).returncode, 0)

    def test_generate_root_refuses_an_orphan_token_with_only_a_catalogue_policy(self) -> None:
        # Catches a gate that lets any live token through, as the old rule did for names it
        # did not know.
        token = self.scoped()["client_token"]
        result = self.call(GENERATE_OTP, token=token)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("permission denied", result.stderr)

    def test_generate_root_needs_the_policy_not_a_kubernetes_login(self) -> None:
        # Catches a gate that checks how a token was made instead of what it may do, and one
        # that replaced the old rule: the ops-admin token still passes.
        self.role("executor", "generate-root")
        self.role("other", "netbox-write")
        executor = json.loads(self.login("executor").stdout)["auth"]["client_token"]
        other = json.loads(self.login("other").stdout)["auth"]["client_token"]
        self.assertEqual(self.call(GENERATE_OTP, token=executor).returncode, 0)
        self.assertNotEqual(self.call(GENERATE_OTP, token=other).returncode, 0)
        self.assertEqual(self.call(GENERATE_OTP, token="fake-ops-token").returncode, 0)
        self.assertEqual(self.call(GENERATE_OTP, token=ROOT_TOKEN).returncode, 0)

    def test_generate_root_ignores_the_token_on_a_2_5_2_server(self) -> None:
        # Catches a gate that applies without FAKE_GENROOT_AUTH.
        token = self.scoped()["client_token"]
        self.assertEqual(self.call(GENERATE_OTP, token=token, genroot_auth=False).returncode, 0)

    def test_listing_accessors_needs_root_or_the_revoker_policy(self) -> None:
        # Catches a list that any live token, or no token, may call.
        scoped = self.scoped()
        for token in (None, "unknown-token", scoped["client_token"]):
            with self.subTest(token=token):
                self.assertNotEqual(self.call(LIST_ACCESSORS, token=token).returncode, 0)
        for token in (ROOT_TOKEN, self.revoker()):
            with self.subTest(token=token):
                result = self.call(LIST_ACCESSORS, token=token)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(scoped["accessor"], result.stdout)
        listed = self.call(["list", "-format=json", "auth/token/accessors"], token=ROOT_TOKEN)
        self.assertIn(scoped["accessor"], json.loads(listed.stdout))

    def test_lookup_by_accessor_reports_the_creation_time_from_the_clock(self) -> None:
        # Catches a lookup that reports the time of the lookup as the creation time (a
        # revoker would then never find a token old), or a TTL that does not shrink.
        auth = self.scoped("-ttl=15m", "-use-limit=3")
        later = NOW + 400
        result = self.call(["write", "-format=json", "auth/token/lookup-accessor", f"accessor={auth['accessor']}"],
                           token=ROOT_TOKEN, now=later)
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(result.stdout)["data"]
        self.assertEqual((data["creation_time"], data["ttl"], data["num_uses"]), (NOW, 500, 3))
        self.assertEqual((data["policies"], data["orphan"]), (["default", "netbox-write"], True))

    def test_lookup_by_accessor_needs_the_policy_and_knows_only_live_tokens(self) -> None:
        # Catches a lookup open to any token, and one that still answers for a revoked token.
        auth = self.scoped()
        args = ["write", "auth/token/lookup-accessor", f"accessor={auth['accessor']}"]
        self.assertNotEqual(self.call(args, token=auth["client_token"]).returncode, 0)
        self.assertEqual(self.call(args, token=ROOT_TOKEN).returncode, 0)
        (self.state / f"revoked-{auth['client_token']}").touch()
        self.assertNotEqual(self.call(args, token=ROOT_TOKEN).returncode, 0)

    def test_revoke_by_accessor_revokes_that_token_and_its_children(self) -> None:
        # Catches a revoke that does nothing, one that revokes the caller, one open to any
        # token, and one that leaves a child that was not made as an orphan.
        revoker = self.revoker()
        target = self.scoped()
        child = self.create("-policy=netbox-write", "-use-limit=9", token=ROOT_TOKEN)
        (self.state / "tokens" / f"{child['client_token']}.json").write_text(json.dumps(
            {**json.loads((self.state / "tokens" / f"{child['client_token']}.json").read_text()),
             "parent": target["client_token"]}))
        args = ["write", "auth/token/revoke-accessor", f"accessor={target['accessor']}"]
        self.assertNotEqual(self.call(args, token=target["client_token"]).returncode, 0)
        self.assertEqual(self.call(args, token=revoker).returncode, 0)
        self.assertNotEqual(self.call(SELF_LOOKUP, token=target["client_token"]).returncode, 0)
        self.assertNotEqual(self.call(SELF_LOOKUP, token=child["client_token"]).returncode, 0)
        self.assertEqual(self.call(SELF_LOOKUP, token=revoker).returncode, 0)
        self.assertNotIn(target["accessor"], self.call(LIST_ACCESSORS, token=ROOT_TOKEN).stdout)
        self.assertNotEqual(self.call(args, token=ROOT_TOKEN).returncode, 0)

    def test_an_orphan_outlives_its_revoked_parent_and_a_child_does_not(self) -> None:
        # Catches a revoke that takes an orphan token down with its parent: the executor
        # revokes the generated root token before the scoped orphan token's first write.
        for how in ("accessor", "self"):
            with self.subTest(revoked_by=how):
                parent = self.create("-orphan", "-policy=root", "-renewable=false", "-ttl=15m")["client_token"]
                orphan = self.create("-orphan", "-policy=netbox-write", token=parent)["client_token"]
                child = self.create("-policy=netbox-write", token=parent)["client_token"]
                if how == "accessor":
                    args = ["write", "auth/token/revoke-accessor", f"accessor={parent}-accessor"]
                else:
                    args = ["token", "revoke", "-self"]
                self.assertEqual(self.call(args, token=parent if how == "self" else ROOT_TOKEN).returncode, 0)
                self.assertNotEqual(self.call(SELF_LOOKUP, token=parent).returncode, 0)
                self.assertEqual(self.call(SELF_LOOKUP, token=orphan).returncode, 0)
                self.assertNotEqual(self.call(SELF_LOOKUP, token=child).returncode, 0)

    def test_a_seeded_root_policy_token_acts_as_root_and_is_listed(self) -> None:
        # Catches a fake that cannot model the unrecorded root token that the revoker hunts.
        (self.state / "tokens").mkdir()
        (self.state / "tokens" / "stray.json").write_text(json.dumps(
            {"policies": ["root"], "orphan": True, "created": NOW - 600, "ttl": 0, "uses_left": None}))
        listed = self.call(LIST_ACCESSORS, token="stray")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("stray-accessor", listed.stdout)
        data = json.loads(self.call(["write", "auth/token/lookup-accessor", "accessor=stray-accessor"], token=ROOT_TOKEN).stdout)["data"]
        self.assertEqual((data["policies"], data["creation_time"]), (["root"], NOW - 600))

    def test_kubernetes_login_returns_the_policies_of_the_role(self) -> None:
        # Catches a login with a fixed policy list, one that mixes up roles, and one whose
        # token can do more than the role allows.
        self.role("executor", "generate-root")
        self.role("verifier", "identity-verifier,extra")
        executor = json.loads(self.login("executor").stdout)["auth"]
        verifier = json.loads(self.login("verifier").stdout)["auth"]
        self.assertEqual(executor["policies"], ["default", "generate-root"])
        self.assertEqual(verifier["policies"], ["default", "identity-verifier", "extra"])
        self.assertEqual((executor["orphan"], executor["lease_duration"]), (True, 600))
        looked_up = json.loads(self.call(SELF_LOOKUP, token=executor["client_token"]).stdout)["data"]
        self.assertEqual(looked_up["policies"], ["default", "generate-root"])
        self.assertNotEqual(self.call(LIST_ACCESSORS, token=executor["client_token"]).returncode, 0)

    def test_kubernetes_login_refuses_an_unknown_role_a_missing_jwt_and_the_wrong_account(self) -> None:
        # Catches a login that hands out a token without checking the role or the identity.
        self.role("executor", "generate-root")
        self.assertNotEqual(self.login("nobody").returncode, 0)
        self.assertNotEqual(self.login("executor", jwt="").returncode, 0)
        self.assertNotEqual(self.login("executor", jwt="fake-sa:approvals:intruder").returncode, 0)
        self.assertNotEqual(self.login("executor", jwt="fake-sa:other:executor").returncode, 0)
        self.assertEqual(self.login("executor").returncode, 0)
        self.assertEqual(self.login("executor", jwt="a-projected-token").returncode, 0)

    def test_kubernetes_roles_are_listed_and_read_by_the_verifier_policy(self) -> None:
        # Catches role reads that a token without a grant may make.
        self.role("executor", "generate-root")
        verifier = self.call(["write", "auth/kubernetes/role/v", "bound_service_account_names=v",
                              "bound_service_account_namespaces=n", "token_policies=identity-verifier"], token=ROOT_TOKEN)
        self.assertEqual(verifier.returncode, 0)
        token = json.loads(self.login("v", jwt="x").stdout)["auth"]["client_token"]
        self.assertEqual(json.loads(self.call(["list", "-format=json", "auth/kubernetes/role"], token=token).stdout), ["executor", "v"])
        read = self.call(["read", "-format=json", "auth/kubernetes/role/executor"], token=token)
        self.assertEqual(json.loads(read.stdout)["data"]["token_policies"], ["generate-root"])
        self.assertNotEqual(self.call(["list", "auth/kubernetes/role"], token=self.scoped()["client_token"]).returncode, 0)

    def test_identity_reads_come_from_the_fixture_and_need_the_verifier_policy(self) -> None:
        # Catches reads that invent data, ignore the fixture, or answer any token.
        (self.state / "identity.json").write_text(json.dumps({
            "auth_methods": {"token/": {"type": "token"}, "ldap/": {"type": "ldap"}},
            "userpass_users": {"alice": {"token_policies": ["p1"]}},
            "approle_roles": {"r": {"token_policies": ["p2"]}},
            "token_roles": {"t": {"allowed_policies": ["p3"]}},
            "entities": {"e1": {"name": "bob", "policies": ["p4"]}},
            "groups": {"g1": {"name": "ops", "policies": ["p5"], "member_group_ids": ["g2"]}}}))
        self.role("verifier", "identity-verifier")
        token = json.loads(self.login("verifier").stdout)["auth"]["client_token"]
        outsider = self.scoped()["client_token"]
        cases = [(["read", "sys/auth"], lambda d: sorted(d["data"])  == ["ldap/", "token/"]),
                 (["read", "-format=json", "auth/userpass/users/alice"], lambda d: d["data"]["token_policies"] == ["p1"]),
                 (["read", "auth/approle/role/r"], lambda d: d["data"]["token_policies"] == ["p2"]),
                 (["read", "auth/token/roles/t"], lambda d: d["data"]["allowed_policies"] == ["p3"]),
                 (["read", "identity/entity/id/e1"], lambda d: (d["data"]["policies"], d["data"]["id"]) == (["p4"], "e1")),
                 (["read", "identity/group/id/g1"], lambda d: d["data"]["member_group_ids"] == ["g2"])]
        for args, check in cases:
            with self.subTest(args=args):
                self.assertTrue(check(json.loads(self.call(args, token=token).stdout)))
                self.assertNotEqual(self.call(args, token=outsider).returncode, 0)
        lists = {"auth/userpass/users": ["alice"], "auth/approle/role": ["r"], "auth/token/roles": ["t"],
                 "identity/entity/id": ["e1"], "identity/group/id": ["g1"]}
        for path, keys in lists.items():
            with self.subTest(path=path):
                self.assertEqual(json.loads(self.call(["list", "-format=json", path], token=token).stdout), keys)
                self.assertNotEqual(self.call(["list", path], token=outsider).returncode, 0)

    def _verifier(self):
        self.role("verifier", "identity-verifier")
        return json.loads(self.login("verifier").stdout)["auth"]["client_token"]

    def test_the_verifier_reads_a_fixture_external_secrets_role_with_and_without_json(self) -> None:
        # Catches an exact-match handler for the external-secrets role that answers 403 to
        # the verifier even when identity.json defines the role.
        (self.state / "identity.json").write_text(json.dumps(
            {"approle_roles": {"external-secrets": {"token_policies": ["p9"]}}}))
        token = self._verifier()
        for flags in (["-format=json"], []):
            with self.subTest(flags=flags):
                result = self.call(["read", *flags, "auth/approle/role/external-secrets"], token=token)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(json.loads(result.stdout)["data"]["token_policies"], ["p9"])
        self.assertNotEqual(self.call(["read", "-format=json", "auth/approle/role/external-secrets"],
                                      token=self.scoped()["client_token"]).returncode, 0)

    def test_the_scheduler_still_reads_external_secrets_without_a_fixture(self) -> None:
        # Catches a fix that drops the scheduler answer when identity.json has no such role.
        result = self.call(["read", "-format=json", "auth/approle/role/external-secrets"], token="fake-scheduler-1")
        self.assertEqual(json.loads(result.stdout)["data"]["token_policies"], ["eso-reader"])
        self.assertNotEqual(self.call(["read", "-format=json", "auth/approle/role/external-secrets"],
                                      token=self._verifier()).returncode, 0)

    def test_userpass_and_kubernetes_role_reads_work_without_the_json_flag(self) -> None:
        # Catches exact-match handlers that serve only the -format=json form, which leaves
        # the plain read as an unexpected call.
        (self.state / "identity.json").write_text(json.dumps(
            {"userpass_users": {"alice": {"token_policies": ["p1"]}}}))
        self.role("executor", "generate-root")
        token = self._verifier()
        user = self.call(["read", "auth/userpass/users/alice"], token=token)
        self.assertEqual(json.loads(user.stdout)["data"]["token_policies"], ["p1"], user.stderr)
        role = self.call(["read", "auth/kubernetes/role/executor"], token=token)
        self.assertEqual(json.loads(role.stdout)["data"]["token_policies"], ["generate-root"], role.stderr)
        for args in (["read", "auth/userpass/users/alice"], ["read", "auth/kubernetes/role/executor"]):
            self.assertNotEqual(self.call(args, token=self.scoped()["client_token"]).returncode, 0)

    def test_without_a_fixture_the_userpass_reads_keep_their_old_answer(self) -> None:
        # Catches a change to the ops-admin record that the rotation tests read.
        listed = self.call(["list", "-format=json", "auth/userpass/users"], token=ROOT_TOKEN)
        self.assertEqual(json.loads(listed.stdout), ["ops-admin"])
        read = self.call(["read", "-format=json", "auth/userpass/users/ops-admin"], token=ROOT_TOKEN)
        self.assertIn("password_writes", json.loads(read.stdout)["data"])
        empty = self.call(["list", "-format=json", "auth/approle/role"], token=ROOT_TOKEN)
        self.assertEqual((empty.returncode, empty.stdout.strip()), (2, "{}"))
        self.assertIn("No value found", self.call(["list", "auth/approle/role"], token=ROOT_TOKEN).stderr)


if __name__ == "__main__":
    unittest.main()
