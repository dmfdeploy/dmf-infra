"""One AWX identity for the NetBox seed and both monitors (dmfdeploy/dmfdeploy#1027).

Each case runs the real roles of the real playbooks (694 born-inventory, 100
prometheus, 120 grafana) through `ansible-playbook`, with the values set in the
inventory as an environment sets them. See awx_role_path_1027.py for how. The
tests need `ansible-playbook` on the PATH."""
import json
import pathlib
import re
import shutil
import tempfile
import unittest

import yaml

import awx_role_path_1027 as real

ROOT = real.ROOT
DEFAULT = "awx-service.awx.svc.cluster.local:80"
LAB = "tower-service.lab.svc.cluster.local:80"
LABEL = r"label_replace\(\s*max\(.*?\) == 0,\s*\"instance\", \"([^\"]*)\", \"\", \"\"\s*\)"


def alert_instance(rendered):
    rules = real.alert_rules(rendered)
    expr = next(r["expr"] for g in rules["groups"] for r in g["rules"]
                if r.get("alert") == "NetboxProbeLaneDown")
    return re.search(LABEL, expr, re.S).group(1)


def dashboard_instances(rendered):
    key = "  dmf-platform-services.json: |\n"
    lines = rendered.splitlines(keepends=True)
    body = []
    for line in lines[lines.index(key) + 1:]:
        if not line.startswith("    ") and line.strip():
            break
        body.append(line)
    text = "".join(body)
    return set(re.findall(r'\\"instance\\", \\"([^\\"]*)\\", \\"\\", \\"\\"', text))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_consumer(self, consumer, inventory=None, render=True):
        d = self.tmp / consumer
        d.mkdir(exist_ok=True)
        return real.run(consumer, d, inventory, render=render)

    def effective(self, inventory=None):
        """What the seed, the rule and the dashboard each end up using."""
        out = {}
        for consumer in real.CONSUMERS:
            result, rendered = self.run_consumer(consumer, inventory)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            out[consumer] = rendered
        dashboard = dashboard_instances(out["dashboard"])
        self.assertEqual(len(dashboard), 1, dashboard)
        return {"seed": real.seed_instance(out["seed"]),
                "alert": alert_instance(out["alert"]),
                "dashboard": dashboard.pop()}

    def assertAgree(self, inventory, expected):
        self.assertEqual(self.effective(inventory),
                         {"seed": expected, "alert": expected, "dashboard": expected})


class OneIdentity(Base):
    def test_no_overrides(self):
        self.assertAgree({}, DEFAULT)

    def test_namespace_and_instance_name_from_the_inventory(self):
        self.assertAgree({"awx_namespace": "lab", "awx_instance_name": "tower"}, LAB)

    def test_service_and_port_from_the_inventory(self):
        self.assertAgree({"awx_probe_service": "tower-web", "awx_probe_port": 8052},
                         "tower-web.awx.svc.cluster.local:8052")

    def test_coordinates_override_that_names_the_shared_identity(self):
        coords = {"awx": {"service": "tower-service", "namespace": "lab", "port": 80}}
        self.assertAgree({"awx_namespace": "lab", "awx_instance_name": "tower",
                          "dmf_born_inventory_app_cluster_coords": coords}, LAB)


class CoordinatesOverride(Base):
    """The dictionary override cannot point AWX where the monitors do not look."""

    DIVERGENT = {"dmf_born_inventory_app_cluster_coords": {
        "awx": {"service": "tower-web", "namespace": "elsewhere", "port": 8052}}}

    def test_a_divergent_override_fails_every_consumer(self):
        for consumer in real.CONSUMERS:
            with self.subTest(consumer=consumer):
                result, _ = self.run_consumer(consumer, self.DIVERGENT, render=False)
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("must equal the shared AWX identity", result.stdout + result.stderr)

    def test_each_field_is_checked(self):
        same = {"service": "awx-service", "namespace": "awx", "port": 80}
        for field, value in (("service", "x"), ("namespace", "x"), ("port", 81)):
            with self.subTest(field=field):
                coords = {"awx": {**same, field: value}}
                result, _ = self.run_consumer(
                    "seed", {"dmf_born_inventory_app_cluster_coords": coords}, render=False)
                self.assertNotEqual(result.returncode, 0, result.stdout)


class MalformedInput(Base):
    """Values that could break out of the PromQL string stop every consumer."""

    BAD = {
        "awx_namespace": ['a"b', "awx\n", "Awx", "a.b", ""],
        "awx_instance_name": ['a"b', "awx\n", "a..b", "$1", "a b", ""],
        "awx_probe_service": ['a"b', "awx\n", "a.*", "$1", ""],
        "awx_probe_port": ['80"', "80\n", 123456, "", "8 0"],
    }

    def test_every_consumer_rejects_each_value(self):
        for var, values in self.BAD.items():
            for value in values:
                for consumer in real.CONSUMERS:
                    with self.subTest(var=var, value=value, consumer=consumer):
                        result, _ = self.run_consumer(consumer, {var: value}, render=False)
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        self.assertIn("because the alert rules and the dashboard embed them",
                                      result.stdout + result.stderr)

    def test_valid_values_pass(self):
        for inventory in ({"awx_namespace": "a" * 63}, {"awx_probe_service": "x0", "awx_probe_port": 1}):
            with self.subTest(inventory=inventory):
                for consumer in real.CONSUMERS:
                    result, _ = self.run_consumer(consumer, inventory, render=False)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


class Structure(unittest.TestCase):
    def test_every_consumer_depends_on_the_identity_role(self):
        for _, role in real.CONSUMERS.values():
            with self.subTest(role=role):
                meta = yaml.safe_load((ROOT / "roles" / role / "meta/main.yml").read_text())
                deps = [d["role"] if isinstance(d, dict) else d for d in meta["dependencies"]]
                self.assertIn("common/awx-identity", deps)


if __name__ == "__main__":
    unittest.main()
