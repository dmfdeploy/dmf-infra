"""Static checks for the approval executor's identity in OpenBao and the cluster.

In this slice the executor is an identity only. These checks read the repository
as text and guard these properties:

- the executor's Kubernetes-auth role carries the generate-root policy and no
  other, with a login token of at most 5 minutes and a maximum of 10 minutes;
- the fresh install and the installed-environment path both write the role,
  because the role is in openbao_standing_k8s_auth_roles;
- the generate-root policy is written before any role that names it;
- the role creates a ServiceAccount and nothing that runs;
- the OpenBao ingress rule for the executor admits its labelled pods on the API
  port only, and nothing but the OpenBao server pods reaches the Raft port;
- the executor's own pods have an ingress policy that admits pods inside the
  cluster and no ipBlock.
"""
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OPENBAO = ROOT / "roles/stack/operator/openbao"
NETPOL = ROOT / "roles/base/network-policies"

MAX_TOKEN_TTL_SECONDS = 5 * 60
MAX_TOKEN_MAX_TTL_SECONDS = 10 * 60
UNITS = {"s": 1, "m": 60, "h": 3600}


def load(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def resolve(value, variables):
    """Replace a whole-value `{{ name }}` reference until none is left."""
    while isinstance(value, str):
        match = re.fullmatch(r"\{\{ (\w+) \}\}", value)
        if not match:
            return value
        value = variables[match.group(1)]
    return value


def seconds(duration):
    return int(duration[:-1]) * UNITS[duration[-1]]


class ExecutorRoleTest(unittest.TestCase):
    defaults = load(OPENBAO / "defaults/main.yml")

    def role(self):
        mapping = self.defaults["openbao_executor_k8s_auth_role"]
        return {key: resolve(value, self.defaults) for key, value in mapping.items()}

    def test_the_only_policy_is_generate_root(self):
        role = self.role()
        self.assertEqual(role["policy"], "generate-root")
        self.assertEqual(set(role) & {"policies", "token_policies"}, set())
        policy = (OPENBAO / "files/policies/generate-root.hcl").read_text()
        self.assertEqual(
            sorted(re.findall(r'(?m)^path "([^"]+)"', policy)),
            ["sys/generate-root-token/attempt", "sys/generate-root-token/update"],
        )

    def test_the_login_token_is_short_and_bounded(self):
        role = self.role()
        self.assertLessEqual(seconds(role["token_ttl"]), MAX_TOKEN_TTL_SECONDS)
        self.assertLessEqual(seconds(role["token_max_ttl"]), MAX_TOKEN_MAX_TTL_SECONDS)
        self.assertLessEqual(seconds(role["token_ttl"]), seconds(role["token_max_ttl"]))

    def test_the_role_binds_one_serviceaccount_in_the_openbao_namespace(self):
        role = self.role()
        self.assertEqual(role["service_account"], "approval-executor")
        self.assertEqual(role["name"], "approval-executor")
        self.assertEqual(role["audience"], "openbao")
        self.assertEqual(role["namespace"], "openbao")

    def test_both_install_paths_write_the_role(self):
        standing = self.defaults["openbao_standing_k8s_auth_roles"]
        self.assertIn("{{ openbao_executor_k8s_auth_role }}", standing)
        setup = (OPENBAO / "tasks/setup-executor.yml").read_text()
        self.assertIn('openbao_k8s_auth_role: "{{ openbao_executor_k8s_auth_role }}"', setup)
        self.assertIn("kubernetes-auth-role.yml", setup)
        self.assertNotIn("bao write", setup)

    def test_the_policy_is_written_before_any_role_that_names_it(self):
        main = (OPENBAO / "tasks/main.yml").read_text()
        policy = main.index("- name: Write the generate-root policy")
        for include in ["setup-issuer.yml", "setup-rotation-scheduler.yml", "setup-executor.yml"]:
            with self.subTest(include=include):
                self.assertLess(policy, main.index("include_tasks: " + include))

    def test_the_identity_creates_no_workload(self):
        setup = load(OPENBAO / "tasks/setup-executor.yml")
        kinds = [
            task["kubernetes.core.k8s"]["definition"]["kind"]
            for task in setup
            if "kubernetes.core.k8s" in task
        ]
        self.assertEqual(kinds, ["ServiceAccount"])


class ExecutorNetworkPolicyTest(unittest.TestCase):
    tasks = load(NETPOL / "tasks/main.yml")
    netpol_defaults = load(NETPOL / "defaults/main.yml")

    def policy(self):
        found = [
            task["kubernetes.core.k8s"]["definition"]
            for task in self.tasks
            if task["kubernetes.core.k8s"]["definition"]["metadata"]["name"]
            == "openbao-allow-from-approval-executor"
        ]
        self.assertEqual(len(found), 1)
        return found[0]

    def test_the_rule_admits_only_the_labelled_pods_on_the_api_port(self):
        spec = self.policy()["spec"]
        self.assertEqual(spec["policyTypes"], ["Ingress"])
        self.assertEqual(len(spec["ingress"]), 1)
        rule = spec["ingress"][0]
        self.assertEqual(len(rule["from"]), 1)
        peer = rule["from"][0]
        self.assertEqual(set(peer), {"podSelector"})
        self.assertEqual(
            peer["podSelector"]["matchLabels"],
            {"app.kubernetes.io/name": "{{ network_policies_openbao_allow_executor_pod_label }}"},
        )
        self.assertEqual(rule["ports"], [{"protocol": "TCP", "port": 8200}])

    def test_the_rule_selects_the_openbao_pods_and_adds_no_egress(self):
        spec = self.policy()["spec"]
        self.assertEqual(
            spec["podSelector"]["matchLabels"],
            {"app.kubernetes.io/name": "{{ network_policies_openbao_service_name }}"},
        )
        self.assertNotIn("egress", spec)

    def named(self, name):
        found = [
            task["kubernetes.core.k8s"]["definition"]
            for task in self.tasks
            if task["kubernetes.core.k8s"]["definition"]["metadata"]["name"] == name
        ]
        self.assertEqual(len(found), 1, name + " is missing or defined twice")
        return found[0]

    def test_only_the_openbao_server_pods_reach_the_raft_port(self):
        spec = self.named("openbao-allow-from-eso-and-peers")["spec"]
        for rule in spec["ingress"]:
            if any(port["port"] == 8201 for port in rule["ports"]):
                for peer in rule["from"]:
                    with self.subTest(peer=peer):
                        self.assertEqual(
                            peer.get("podSelector", {}).get("matchLabels"),
                            {"app.kubernetes.io/name": "{{ network_policies_openbao_service_name }}"},
                        )
                        self.assertNotIn("ipBlock", peer)

    def test_the_executor_pods_take_connections_from_inside_the_cluster_only(self):
        policy = self.named("approval-executor-allow-cluster-internal")
        spec = policy["spec"]
        self.assertEqual(spec["policyTypes"], ["Ingress"])
        self.assertEqual(
            spec["podSelector"]["matchLabels"],
            {"app.kubernetes.io/name": "{{ network_policies_openbao_allow_executor_pod_label }}"},
        )
        self.assertEqual(spec["ingress"], [{"from": [{"namespaceSelector": {}}]}])
        self.assertNotIn("ipBlock", yaml.safe_dump(policy))

    def test_the_label_matches_the_label_of_the_openbao_role(self):
        openbao = load(OPENBAO / "defaults/main.yml")
        self.assertEqual(
            self.netpol_defaults["network_policies_openbao_allow_executor_pod_label"],
            openbao["openbao_executor_pod_label"],
        )


if __name__ == "__main__":
    unittest.main()
