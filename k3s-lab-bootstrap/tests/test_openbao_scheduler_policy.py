"""The rotation scheduler's OpenBao policy and Kubernetes RBAC.

Four properties are tested.

- The policy rendered from the current registry equals the golden file and the
  file the openbao role loads, so a registry change shows up as a reviewed
  change to both.
- No path is outside the ones the engine needs (the negative property), and no
  path is a glob.
- The policy is enough: a full rotation of each target, run against the
  engine's fakes, makes only calls that the policy allows.
- The RBAC grants writes only on named objects.
"""

import copy
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from approle_lifecycle import registry, rotation, scheduler_policy  # noqa: E402
from test_openbao_rotation_transaction import ONE, THREE, TWO, Harness  # noqa: E402

REGISTRY_PATH = ROOT / "vars/openbao-approle-lifecycle-registry.json"
REGISTRY = registry.load(REGISTRY_PATH)
GOLDEN = ROOT / "tests/golden/rotation-scheduler.hcl"
ROLE_FILE = ROOT / "roles/stack/operator/openbao" / "files/policies/rotation-scheduler.hcl"
INVENTORY = {"object_storage_consumer_namespaces": {"openbao_snapshots": ["openbao-system"], "app_backups": ["awx", "netbox"]}}


GOOD_CREATE = {"secret_id": "a-candidate", "ttl": "2592000s", "num_uses": 0}


def parse_policy(text):
    """{path: {"capabilities": [...], "required": [...], "allowed": {...}}}"""
    found = {}
    for path, body in re.findall(r'^path\s+"([^"]+)"\s*\{(.*?)\n\}', text, re.S | re.M):
        assert path not in found, f"{path} appears twice"
        capabilities = re.search(r"capabilities\s*=\s*\[([^\]]*)\]", body).group(1)
        required = re.search(r"required_parameters\s*=\s*\[([^\]]*)\]", body)
        allowed = re.search(r"allowed_parameters\s*=\s*\{(.*?)\}", body, re.S)
        found[path] = {
            "capabilities": re.findall(r'"(\w+)"', capabilities),
            "required": re.findall(r'"(\w+)"', required.group(1)) if required else [],
            "allowed": {
                key: re.findall(r'"([^"]*)"', values)
                for key, values in re.findall(r'"(\w+)"\s*=\s*\[([^\]]*)\]', allowed.group(1))
            } if allowed else None,
        }
    return found


def expected_paths(registry_data):
    """The paths the engine can touch, listed from the engine's own path rules."""
    paths = {"auth/token/revoke-self"}
    for entry in registry_data["runtime_targets"].values():
        role = f"auth/approle/role/{entry['approle_name']}"
        paths |= {
            role, f"{role}/secret-id/lookup", f"{role}/custom-secret-id", f"{role}/secret-id-accessor/destroy",
            entry["metadata_path"], rotation.transaction_path(entry), rotation.staging_path(entry),
            rotation.staging_path(entry).replace("/data/", "/metadata/", 1),
        }
        paths |= {d["path"] for d in entry["destinations"] if d["kind"] == "openbao_kv2_record"}
    return paths


class GoldenTests(unittest.TestCase):
    def test_the_render_equals_the_golden_file_and_the_role_file(self):
        rendered = scheduler_policy.render_policy(REGISTRY)
        self.assertEqual(rendered, GOLDEN.read_text(encoding="utf-8"))
        self.assertEqual(rendered, ROLE_FILE.read_text(encoding="utf-8"))

    def test_the_render_is_deterministic_and_sorted_by_target(self):
        first = scheduler_policy.render_policy(REGISTRY)
        reordered = {**REGISTRY, "runtime_targets": dict(reversed(list(REGISTRY["runtime_targets"].items())))}
        self.assertEqual(first, scheduler_policy.render_policy(reordered))
        targets = re.findall(r"^# ([a-z-]+)$", first, re.M)
        self.assertEqual(targets, sorted(REGISTRY["runtime_targets"]))


class NegativePropertyTests(unittest.TestCase):
    POLICY = parse_policy(scheduler_policy.render_policy(REGISTRY))

    def test_no_path_outside_the_listed_ones(self):
        self.assertEqual(set(self.POLICY), expected_paths(REGISTRY))

    def test_no_glob_no_sudo_and_only_the_capabilities_the_engine_uses(self):
        for path, grant in self.POLICY.items():
            with self.subTest(path=path):
                self.assertNotRegex(path, r"[*+]")
                self.assertTrue(set(grant["capabilities"]) <= {"create", "read", "update", "delete"}, grant)
        text = scheduler_policy.render_policy(REGISTRY)
        for word in ("sudo", "list", "patch", "deny"):
            self.assertNotIn(f'"{word}"', text)

    def test_nothing_reaches_sys_auth_administration_or_application_data(self):
        for path in self.POLICY:
            with self.subTest(path=path):
                self.assertFalse(path.startswith("sys/"))
                self.assertFalse(path.startswith("auth/kubernetes"))
                self.assertFalse(path.startswith("secret/data/apps/"))
                self.assertFalse(path.endswith("/secret-id"), "the random SecretID endpoint")
                self.assertNotIn("secret-id-accessor/lookup", path)
                self.assertNotIn("/list", path)

    def test_every_approle_path_names_a_registry_target(self):
        names = {e["approle_name"] for e in REGISTRY["runtime_targets"].values()}
        for path in self.POLICY:
            match = re.match(r"auth/approle/role/([^/]+)", path)
            if match:
                self.assertIn(match.group(1), names)
        self.assertNotIn("born-inventory-netbox-reader", scheduler_policy.render_policy(REGISTRY))

    def test_the_custom_secret_id_grant_is_bounded_by_the_registry(self):
        for entry in REGISTRY["runtime_targets"].values():
            grant = self.POLICY[f"auth/approle/role/{entry['approle_name']}/custom-secret-id"]
            self.assertEqual(grant["capabilities"], ["update"])
            self.assertEqual(grant["required"], ["secret_id", "ttl", "num_uses"])
            self.assertEqual(grant["allowed"], {
                "secret_id": [], "ttl": [f"{registry.ttl_seconds(entry['secret_id_ttl'])}s"], "num_uses": ["0"],
            })

    def test_staging_has_no_update_and_only_metadata_deletes(self):
        for entry in REGISTRY["runtime_targets"].values():
            staging = rotation.staging_path(entry)
            self.assertEqual(self.POLICY[staging]["capabilities"], ["create", "read"])
            self.assertEqual(self.POLICY[staging.replace("/data/", "/metadata/", 1)]["capabilities"], ["delete"])
            for path, grant in self.POLICY.items():
                if "delete" in grant["capabilities"]:
                    self.assertIn("/metadata/platform/approle-rotation-staging/", path)

    def test_a_new_target_adds_only_its_own_paths(self):
        extra = copy.deepcopy(REGISTRY)
        entry = copy.deepcopy(extra["runtime_targets"][ONE])
        entry.update(approle_name="another-target", metadata_path=registry.METADATA_PREFIX + "another-target")
        extra["runtime_targets"]["another-target"] = entry
        added = set(parse_policy(scheduler_policy.render_policy(extra))) - set(self.POLICY)
        self.assertTrue(added)
        for path in added:
            self.assertIn("another-target", path)

    def test_an_approle_outside_the_registry_gets_nothing(self):
        smaller = copy.deepcopy(REGISTRY)
        del smaller["runtime_targets"][THREE]
        policy = scheduler_policy.render_policy(smaller)
        self.assertNotIn(THREE, policy)
        self.assertEqual(set(parse_policy(policy)), expected_paths(smaller))


def permitted(policy, path, capability, params=None):
    grant = policy.get(path)
    if grant is None or capability not in grant["capabilities"]:
        return False
    if grant["required"] and not all(name in (params or {}) for name in grant["required"]):
        return False
    if grant["allowed"] is not None:
        for name, value in (params or {}).items():
            if name not in grant["allowed"]:
                return False  # OpenBao denies a parameter that the map leaves out
            values = grant["allowed"][name]
            if values and str(value) not in values:
                return False  # an empty list allows any value
    return True


class Recorder:
    """Wraps the engine's OpenBao fake and records the API call behind each client call."""

    def __init__(self, bao):
        self.api = []
        self.bao = bao
        for name in (
            "read_role", "lookup_secret_id", "create_custom_secret_id", "destroy_secret_id_accessor",
            "read_kv2", "write_kv2", "create_staging", "read_staging", "delete_staging",
        ):
            setattr(bao, name, self.wrap(name, getattr(bao, name)))

    def wrap(self, name, function):
        def call(*args):
            self.record(name, args)
            return function(*args)
        return call

    def record(self, name, args):
        role = lambda approle: f"auth/approle/role/{approle}"  # noqa: E731
        if name == "read_role":
            self.api.append((role(args[0]), "read", None))
        elif name == "lookup_secret_id":
            self.api.append((role(args[0]) + "/secret-id/lookup", "update", None))
        elif name == "create_custom_secret_id":
            self.api.append((role(args[0]) + "/custom-secret-id", "update", {"secret_id": args[1], **args[2]}))
        elif name == "destroy_secret_id_accessor":
            self.api.append((role(args[0]) + "/secret-id-accessor/destroy", "update", None))
        elif name in ("read_kv2", "read_staging"):
            self.api.append((args[0], "read", None))
        elif name == "write_kv2":
            self.api.append((args[0], "create" if args[2] == 0 else "update", None))
        elif name == "create_staging":
            self.api.append((args[0], "create", None))
        elif name == "delete_staging":
            self.api.append((args[0].replace("/data/", "/metadata/", 1), "delete", None))


class SufficiencyTests(unittest.TestCase):
    """The policy lets the engine finish. A missing grant would make a
    rotation fail on the live cluster, where no test can see it."""

    POLICY = parse_policy(scheduler_policy.render_policy(REGISTRY))

    def assert_all_permitted(self, recorder):
        self.assertTrue(recorder.api)
        for path, capability, params in recorder.api:
            self.assertTrue(permitted(self.POLICY, path, capability, params), (path, capability, params))

    def test_a_rotation_of_each_target_makes_only_permitted_calls(self):
        for target in (ONE, TWO, THREE):
            with self.subTest(target=target):
                harness = Harness(seed=(target,))
                recorder = Recorder(harness.bao)
                self.assertEqual(harness.run(target).outcome, "rotated")
                self.assert_all_permitted(recorder)
                self.assertIn("create_custom_secret_id", {name for name, *_ in harness.events})

    def test_a_target_with_no_prior_accessor_makes_only_permitted_calls(self):
        harness = Harness(seed=())
        recorder = Recorder(harness.bao)
        self.assertEqual(harness.run(ONE).outcome, "rotated")
        self.assert_all_permitted(recorder)
        self.assertIn((registry.METADATA_PREFIX + ONE, "create", None), recorder.api)

    def test_a_failed_readiness_and_the_later_retry_make_only_permitted_calls(self):
        harness = Harness(seed=(ONE,))
        recorder = Recorder(harness.bao)
        harness.ready.ok = False
        self.assertEqual(harness.run(ONE).outcome, "failed")
        harness.ready.ok = True
        harness.clock.t += 60
        harness.wait_out_the_lease()
        self.assertEqual(harness.run(ONE).outcome, "rotated")
        self.assert_all_permitted(recorder)

    def test_the_policy_check_accepts_the_create_the_engine_makes(self):
        self.assertTrue(permitted(self.POLICY, "auth/approle/role/external-secrets/custom-secret-id", "update", GOOD_CREATE))

    def test_the_policy_check_itself_rejects_a_call_outside_the_policy(self):
        for call in (
            ("auth/approle/role/external-secrets/secret-id", "update", None),
            ("auth/approle/role/born-inventory-netbox-reader/custom-secret-id", "update", GOOD_CREATE),
            ("auth/approle/role/external-secrets/custom-secret-id", "update", {**GOOD_CREATE, "ttl": "1s"}),
            ("auth/approle/role/external-secrets/custom-secret-id", "update", {**GOOD_CREATE, "num_uses": 1}),
            ("auth/approle/role/external-secrets/custom-secret-id", "update", {"secret_id": "c", "ttl": "2592000s"}),
            ("auth/approle/role/external-secrets/custom-secret-id", "update", {"ttl": "2592000s", "num_uses": 0}),
            ("auth/approle/role/external-secrets/custom-secret-id", "update", {**GOOD_CREATE, "metadata": "x"}),
            ("secret/data/apps/netbox/runtime", "read", None),
            ("sys/policies/acl/x", "update", None),
            ("secret/data/platform/approle-rotation-staging/external-secrets", "update", None),
        ):
            with self.subTest(call=call):
                self.assertFalse(permitted(self.POLICY, *call))


def rules_by_namespace(manifests):
    return {m["metadata"]["namespace"]: m["rules"] for m in manifests if m["kind"] == "Role"}


class RbacTests(unittest.TestCase):
    ARGS = dict(
        namespace="openbao", service_account="approle-rotation-scheduler",
        lease_namespace="openbao", lease_name="approle-rotation",
    )
    MANIFESTS = scheduler_policy.render_rbac(REGISTRY, INVENTORY, **ARGS)

    def rules(self):
        return [(m["metadata"].get("namespace"), rule) for m in self.MANIFESTS if m["kind"] in ("Role", "ClusterRole")
                for rule in m["rules"]]

    def test_every_write_names_its_object(self):
        for namespace, rule in self.rules():
            writes = set(rule["verbs"]) & {"update", "patch", "delete"}
            if writes:
                with self.subTest(namespace=namespace, rule=rule):
                    self.assertTrue(rule.get("resourceNames"))

    def test_create_is_only_for_leases_in_the_lease_namespace(self):
        for namespace, rule in self.rules():
            if "create" in rule["verbs"]:
                self.assertEqual((namespace, rule["resources"]), ("openbao", ["leases"]))

    def test_no_wildcard_and_only_the_expected_verbs(self):
        for namespace, rule in self.rules():
            for field in ("apiGroups", "resources", "verbs", "resourceNames"):
                self.assertNotIn("*", rule.get(field, []))
            self.assertTrue(set(rule["verbs"]) <= {"get", "list", "update", "patch", "delete", "create"})

    def test_secrets_may_be_updated_only_where_the_registry_delivers_them(self):
        delivered = {
            (d["namespace"], d["name"])
            for e in REGISTRY["runtime_targets"].values() for d in e["destinations"]
            if d["kind"] == "kubernetes_secret_key"
        }
        updated = {
            (ns, name)
            for ns, rule in self.rules() if rule["resources"] == ["secrets"] and set(rule["verbs"]) & {"update", "patch"}
            for name in rule["resourceNames"]
        }
        self.assertEqual(updated, delivered)

    def test_the_lease_rule_names_the_one_lease(self):
        rules = rules_by_namespace(self.MANIFESTS)["openbao"]
        named = [r for r in rules if r["resources"] == ["leases"] and r.get("resourceNames")]
        self.assertEqual([(r["resourceNames"], r["verbs"]) for r in named], [(["approle-rotation"], ["get", "update", "delete"])])

    def test_gate_reads_are_get_and_list_only(self):
        for namespace, rule in self.rules():
            if rule["resources"] in (["externalsecrets"], ["clustersecretstores"]):
                self.assertTrue(set(rule["verbs"]) <= {"get", "list"})
        stores = [r for _, r in self.rules() if r["resources"] == ["clustersecretstores"]]
        self.assertEqual([r["resourceNames"] for r in stores], [["openbao", "openbao-app-backups", "openbao-openbao-snapshots"]])

    def test_each_binding_has_only_the_scheduler_as_subject_and_names_its_own_role(self):
        for manifest in self.MANIFESTS:
            if manifest["kind"] in ("RoleBinding", "ClusterRoleBinding"):
                self.assertEqual(
                    manifest["subjects"], [{"kind": "ServiceAccount", "name": "approle-rotation-scheduler", "namespace": "openbao"}]
                )
                self.assertEqual(manifest["roleRef"]["name"], "approle-rotation-scheduler")

    def test_the_namespaces_come_from_the_inventory_and_an_unknown_gate_fails_closed(self):
        namespaces = set(rules_by_namespace(self.MANIFESTS))
        self.assertTrue({"awx", "netbox", "openbao-system", "authentik", "external-secrets", "longhorn-system", "openbao"} <= namespaces)
        self.assertNotIn("forgejo", namespaces)
        with self.assertRaises(ValueError):
            scheduler_policy.render_rbac(REGISTRY, {}, **self.ARGS)

    def test_the_identity_part_is_the_service_account_and_the_lease_namespace_only(self):
        identity, consumers = scheduler_policy.split_rbac(self.MANIFESTS, "openbao")
        self.assertEqual(identity[0]["kind"], "ServiceAccount")
        self.assertEqual({m["metadata"].get("namespace") for m in identity}, {"openbao"})
        self.assertEqual(len(identity) + len(consumers), len(self.MANIFESTS))
        self.assertFalse([m for m in consumers if m["kind"] == "ServiceAccount"])
        self.assertEqual({m["metadata"].get("namespace") for m in identity if m["kind"] != "ServiceAccount"}, {"openbao"})


class CommandLineTests(unittest.TestCase):
    def test_the_policy_command_prints_the_render(self):
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(scheduler_policy.main(["policy", "--registry", str(REGISTRY_PATH)]), 0)
        self.assertEqual(out.getvalue(), GOLDEN.read_text(encoding="utf-8"))

    def test_the_rbac_command_prints_json_for_each_scope(self):
        import contextlib
        import io
        counts = {}
        for scope in ("all", "identity", "consumers"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                scheduler_policy.main([
                    "rbac", "--registry", str(REGISTRY_PATH), "--inventory-json", json.dumps(INVENTORY),
                    "--namespace", "openbao", "--service-account", "sa", "--lease-namespace", "openbao", "--scope", scope,
                ])
            counts[scope] = len(json.loads(out.getvalue()))
        self.assertEqual(counts["identity"] + counts["consumers"], counts["all"])


if __name__ == "__main__":
    unittest.main()
