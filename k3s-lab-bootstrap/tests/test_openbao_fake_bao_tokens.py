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
        first = self.call(SELF_LOOKUP, token=token)
        self.assertEqual(json.loads(first.stdout)["data"]["num_uses"], 1)
        self.assertEqual(self.call(["token", "capabilities", "-format=json", "secret/x"], token=token).returncode, 0)
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
        # token, one that leaves a child that was not made as an orphan, and a repeat that
        # fails where the server only warns.
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
        again = self.call(args, token=ROOT_TOKEN)
        self.assertEqual(again.returncode, 0)
        self.assertIn("No token found with this accessor", again.stderr)

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

    EARLY_READS = [
        ("tcap", ["token", "capabilities", "-format=json", "secret/x"]),
        ("tauto", ["read", "-format=json", "sys/storage/raft/autopilot/state"]),
        ("traft", ["read", "-format=json", "sys/storage/raft/configuration"]),
        ("tkv", ["kv", "metadata", "get", "-mount=secret", "-format=json", "x"]),
        ("fake-approle-token", ["read", "-format=json", "secret/data/apps/netbox/runtime"]),
        ("fake-policy-t", ["read", "-format=json", "sys/policies/acl/eso-reader"]),
        ("fake-scheduler-t", ["read", "-format=json", "auth/approle/role/external-secrets"]),
    ]

    def seed(self, name, **fields):
        (self.state / "tokens").mkdir(exist_ok=True)
        record = {"policies": ["root"], "orphan": True, "created": NOW, "ttl": 0, "uses_left": None, **fields}
        (self.state / "tokens" / f"{name}.json").write_text(json.dumps(record))
        (self.state / f"revoked-{name}").unlink(missing_ok=True)

    def test_every_token_authenticated_read_refuses_a_used_up_or_expired_scoped_token(self) -> None:
        # Catches a handler that accepts any non-empty BAO_TOKEN: a scoped token whose last use
        # is spent, or whose lifetime is over, would still read through it.
        policy_file = self.state / "eso-policy.hcl"
        policy_file.write_text("path \"secret/*\" { capabilities = [\"read\"] }\n")
        env = {"FAKE_ESO_POLICY_FILE": str(policy_file)}
        for name, args in self.EARLY_READS:
            with self.subTest(args=args, case="second request"):
                self.seed(name, uses_left=1)
                self.assertEqual(self.call(args, token=name, **env).returncode, 0)
                self.assertNotEqual(self.call(args, token=name, **env).returncode, 0)
            with self.subTest(args=args, case="expired"):
                self.seed(name, created=NOW - 600, ttl=60)
                self.assertNotEqual(self.call(args, token=name, **env).returncode, 0)
            with self.subTest(args=args, case="live"):
                self.seed(name)
                self.assertEqual(self.call(args, token=name, **env).returncode, 0)

    def mint(self, *flags, token):
        result = self.call(["token", "create", *flags, "-format=json"], token=token)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)["auth"]

    def lookup_accessor(self, auth):
        result = self.call(["write", "-format=json", "auth/token/lookup-accessor", f"accessor={auth['accessor']}"],
                           token=ROOT_TOKEN)
        return json.loads(result.stdout)["data"]

    def test_a_token_with_a_use_limit_cannot_create_tokens_even_with_the_root_policy(self) -> None:
        # Catches a fake that lets a limited-use root token mint a child, which would escape
        # the limit. The last use leaves nothing to look up, so the server says no parent.
        for limit, reason in (("3", "restricted use token cannot generate child tokens"),
                              ("1", "parent token lookup failed: no parent found")):
            with self.subTest(limit=limit):
                limited = self.create("-policy=root", f"-use-limit={limit}")["client_token"]
                result = self.call(["token", "create", "-policy=netbox-write", "-format=json"], token=limited)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(reason, result.stderr)
        unlimited = self.create("-policy=root", "-use-limit=0")["client_token"]
        self.assertEqual(self.call(["token", "create", "-policy=netbox-write", "-format=json"], token=unlimited).returncode, 0)

    def test_revoking_a_token_revokes_the_whole_tree_of_non_orphan_descendants(self) -> None:
        # Catches a revoke that stops at the direct children: a grandchild would stay usable.
        # An orphan below the revoked token keeps its own tree.
        top = self.create("-policy=root", "-explicit-max-ttl=1h")
        middle = self.mint("-policy=root", "-explicit-max-ttl=1h", token=top["client_token"])
        grandchild = self.mint("-policy=netbox-write", "-use-limit=5", token=middle["client_token"])
        orphan = self.mint("-orphan", "-policy=netbox-write", "-use-limit=5", token=middle["client_token"])
        args = ["write", "auth/token/revoke-accessor", f"accessor={top['accessor']}"]
        self.assertEqual(self.call(args, token=ROOT_TOKEN).returncode, 0)
        for auth, alive in ((top, False), (middle, False), (grandchild, False), (orphan, True)):
            with self.subTest(token=auth["client_token"]):
                result = self.call(SELF_LOOKUP, token=auth["client_token"])
                self.assertEqual(result.returncode == 0, alive, result.stderr)

    def test_policies_are_sorted_with_default_and_root_stands_alone(self) -> None:
        # Catches policies kept in request order with default first, a missing
        # -no-default-policy, and a root token that also carries default.
        cases = [(["-policy=zeta", "-policy=Alpha"], ["alpha", "default", "zeta"]),
                 (["-policy=zeta", "-policy=alpha", "-no-default-policy"], ["alpha", "zeta"]),
                 (["-policy=zeta", "-policy=default", "-no-default-policy"], ["default", "zeta"]),
                 (["-policy=zeta", "-policy=root", "-use-limit=2"], ["root"])]
        for flags, expected in cases:
            with self.subTest(flags=flags):
                auth = self.create("-orphan", *flags)
                self.assertEqual(auth["policies"], expected)
                self.assertEqual(self.lookup_accessor(auth)["policies"], expected)

    def test_a_root_policy_token_without_a_ttl_never_expires_and_is_not_renewable(self) -> None:
        # Catches a fake that gives a root token the 32-day default lifetime.
        auth = self.create("-policy=root", "-use-limit=0")
        later = NOW + 100 * 24 * 3600
        data = json.loads(self.call(["write", "-format=json", "auth/token/lookup-accessor",
                                     f"accessor={auth['accessor']}"], token=ROOT_TOKEN, now=later).stdout)["data"]
        self.assertEqual((data["creation_ttl"], data["ttl"], data["renewable"]), (0, 0, False))
        self.assertEqual((auth["lease_duration"], auth["renewable"]), (0, False))
        self.assertEqual(self.call(SELF_LOOKUP, token=auth["client_token"], now=later).returncode, 0)

    def test_a_ttl_longer_than_the_longest_lifetime_is_cut_to_it(self) -> None:
        # Catches a fake that honors a TTL past the system maximum of 32 days.
        auth = self.scoped("-ttl=1000h")
        self.assertEqual(auth["lease_duration"], 32 * 24 * 3600)

    def test_a_token_goes_with_its_parent_when_the_parent_lifetime_is_over(self) -> None:
        # Catches a child that outlives the lifetime of its parent. An orphan does not.
        parent = self.create("-policy=root", "-explicit-max-ttl=1m")
        child = self.mint("-policy=netbox-write", "-use-limit=9", token=parent["client_token"])
        orphan = self.mint("-orphan", "-policy=netbox-write", "-use-limit=9", token=parent["client_token"])
        later = NOW + 120
        self.assertNotEqual(self.call(SELF_LOOKUP, token=parent["client_token"], now=later).returncode, 0)
        self.assertNotEqual(self.call(SELF_LOOKUP, token=child["client_token"], now=later).returncode, 0)
        self.assertEqual(self.call(SELF_LOOKUP, token=orphan["client_token"], now=later).returncode, 0)
        self.assertEqual(self.call(SELF_LOOKUP, token=child["client_token"], now=NOW + 30).returncode, 0)

    def test_the_last_use_of_a_limited_token_cannot_look_the_token_up(self) -> None:
        # Catches a fake that answers a lookup on the use that spends the last one: the
        # server has no token left to look up and answers bad token.
        token = self.scoped("-use-limit=1")["client_token"]
        refused = self.call(SELF_LOOKUP, token=token)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("permission denied", refused.stderr)

    def test_a_user_missing_from_the_fixture_has_no_value_and_is_not_ops_admin(self) -> None:
        # Catches a userpass read that falls back to the ops-admin record, and a list that
        # adds ops-admin, when identity.json exists but does not name the user.
        (self.state / "identity.json").write_text(json.dumps({"userpass_users": {"alice": {"token_policies": ["p1"]}}}))
        token = self._verifier()
        for flags in (["-format=json"], []):
            with self.subTest(flags=flags):
                missing = self.call(["read", *flags, "auth/userpass/users/ops-admin"], token=token)
                self.assertEqual(missing.returncode, 2)
                self.assertIn("No value found at auth/userpass/users/ops-admin", missing.stderr)
                self.assertEqual(missing.stdout, "")
        listed = self.call(["list", "-format=json", "auth/userpass/users"], token=token)
        self.assertEqual(json.loads(listed.stdout), ["alice"])
        (self.state / "identity.json").write_text(json.dumps({"approle_roles": {}}))
        self.assertEqual(self.call(["list", "-format=json", "auth/userpass/users"], token=token).returncode, 2)
        self.assertEqual(self.call(["read", "auth/userpass/users/ops-admin"], token=token).returncode, 2)

    def test_without_a_fixture_only_ops_admin_is_a_userpass_user(self) -> None:
        # Catches a read of any user name that answers with the ops-admin record.
        self.assertEqual(self.call(["read", "-format=json", "auth/userpass/users/ops-admin"], token=ROOT_TOKEN).returncode, 0)
        typo = self.call(["read", "-format=json", "auth/userpass/users/ops-admn"], token=ROOT_TOKEN)
        self.assertEqual(typo.returncode, 2)
        self.assertIn("No value found", typo.stderr)

    def test_a_fixture_without_the_external_secrets_role_gives_no_value_to_an_inspector(self) -> None:
        # Catches a missing role that answers 403 or the scheduler's role to a token that may inspect.
        (self.state / "identity.json").write_text(json.dumps({"approle_roles": {"other": {"token_policies": ["p"]}}}))
        result = self.call(["read", "-format=json", "auth/approle/role/external-secrets"], token=self._verifier())
        self.assertEqual(result.returncode, 2)
        self.assertIn("No value found", result.stderr)

    def test_token_create_without_a_policy_says_root_and_is_root(self) -> None:
        # Catches a response that names default for a token that the fake then lets act as
        # root. With no -policy the child inherits the policies of its root parent.
        auth = self.mint(token=ROOT_TOKEN)
        self.assertEqual((auth["policies"], auth["token_policies"]), (["root"], ["root"]))
        self.assertEqual(self.lookup_accessor(auth)["policies"], ["root"])
        self.assertEqual(json.loads(self.call(SELF_LOOKUP, token=auth["client_token"]).stdout)["data"]["policies"], ["root"])
        self.assertEqual(self.call(["token", "create", "-policy=x", "-format=json"], token=auth["client_token"]).returncode, 0)

    def test_a_default_only_token_is_refused_on_root_only_endpoints(self) -> None:
        # Catches a token that holds only default but can create tokens or generate a root.
        token = self.mint("-policy=default", token=ROOT_TOKEN)["client_token"]
        created = self.call(["token", "create", "-policy=x", "-format=json"], token=token)
        self.assertNotEqual(created.returncode, 0)
        self.assertIn("permission denied", created.stderr)
        generated = self.call(GENERATE_OTP, token=token)
        self.assertNotEqual(generated.returncode, 0)
        self.assertIn("permission denied", generated.stderr)

    def test_a_stored_token_has_the_power_of_its_policies_not_of_its_name(self) -> None:
        # Catches a name that grants power to a token that has a stored record: each of
        # these names is a legacy token without one, and a stored default-only token is not.
        for name, args, env in (
                ("fake-root-x", ["token", "create", "-policy=x", "-format=json"], {}),
                ("fake-ops-token", GENERATE_OTP, {}),
                ("fake-scheduler-x", ["read", "-format=json", "auth/approle/role/external-secrets"], {}),
                ("fake-approle-token", ["read", "-format=json", "secret/data/apps/netbox/runtime"], {}),
                ("fake-policy-x", ["read", "-format=json", "sys/policies/acl/eso-reader"], {"FAKE_ESO_POLICY_FILE": "/dev/null"})):
            with self.subTest(name=name):
                self.seed(name, policies=["default"])
                self.assertNotEqual(self.call(args, token=name, **env).returncode, 0)
        self.seed("fake-eso-x", policies=["default"])
        caps = self.call(["token", "capabilities", "-format=json", "pki/x"], token="fake-eso-x")
        self.assertEqual(json.loads(caps.stdout), ["read"])   # the generic answer, not the eso grant of the name
        self.assertEqual(json.loads(self.call(["token", "capabilities", "-format=json", "pki/x"],
                                              token="fake-eso-legacy").stdout), ["deny"])

    def test_a_root_policy_token_with_only_a_ttl_expires_for_every_call_and_in_the_accessor_store(self) -> None:
        # Catches a root-policy token that stays authorized after its TTL because it is kept
        # in a second form that the liveness check does not read.
        auth = self.create("-policy=root", "-ttl=10m")
        token = auth["client_token"]
        self.assertTrue(token.startswith("fake-root-short-"))
        early = NOW + 300
        self.assertEqual(self.call(["token", "create", "-policy=x", "-format=json"], token=token, now=early).returncode, 0)
        late = NOW + 700
        created = self.call(["token", "create", "-policy=x", "-format=json"], token=token, now=late)
        self.assertNotEqual(created.returncode, 0)
        self.assertIn("permission denied", created.stderr)
        self.assertNotEqual(self.call(SELF_LOOKUP, token=token, now=late).returncode, 0)
        self.assertNotEqual(self.call(["token", "capabilities", "-format=json", "secret/x"], token=token, now=late).returncode, 0)
        listed = self.call(LIST_ACCESSORS + ["-format=json"], token=ROOT_TOKEN, now=late)
        self.assertNotIn(auth["accessor"], listed.stdout)
        looked = self.call(["write", "auth/token/lookup-accessor", f"accessor={auth['accessor']}"], token=ROOT_TOKEN, now=late)
        self.assertNotEqual(looked.returncode, 0)

    def test_a_revoked_root_policy_token_is_refused_by_every_call(self) -> None:
        # Catches a revoked fake-root-short token that an early handler still serves.
        token = self.create("-policy=root")["client_token"]
        self.assertEqual(self.call(["token", "revoke", "-self"], token=token).returncode, 0)
        calls = [["token", "create", "-policy=x", "-format=json"], SELF_LOOKUP, LIST_ACCESSORS,
                 ["token", "capabilities", "-format=json", "secret/x"],
                 ["read", "-format=json", "sys/storage/raft/autopilot/state"],
                 ["kv", "metadata", "get", "-mount=secret", "-format=json", "x"]]
        for args in calls:
            with self.subTest(args=args):
                self.assertNotEqual(self.call(args, token=token).returncode, 0)

    def test_every_token_that_token_create_makes_is_one_record_in_the_token_store(self) -> None:
        # Catches a second storage form: the records of root-policy tokens in token-NAME.json.
        short = self.create("-policy=root")["client_token"]
        scoped = self.scoped()["client_token"]
        self.assertEqual(sorted(p.name for p in (self.state / "tokens").glob("*.json")),
                         sorted([f"{short}.json", f"{scoped}.json"]))
        self.assertEqual(list(self.state.glob("token-*.json")), [])

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
