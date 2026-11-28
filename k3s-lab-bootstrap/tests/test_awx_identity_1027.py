"""One AWX identity for the NetBox seed, both monitors and the deployer
(dmfdeploy/dmfdeploy#1027).

The case ids in the docstrings (A1, E10, M3, S7 and so on) are the ids in
docs/awx-probe-lane-cases.md. Each case runs the real roles of the real playbooks
(694 born-inventory, 100 prometheus, 120 grafana, 640 awx) through
`ansible-playbook`; see awx_role_path_1027.py for how. The stamp cases run the
real app-service.yml against a stub NetBox. The tests need `ansible-playbook`
and `promtool` on the PATH."""
import pathlib
import shutil
import subprocess
import tempfile
import unittest

import yaml

import awx_role_path_1027 as real

ROOT = real.ROOT
ALL = list(real.CONSUMERS)
PRESENCE = "awx_namespace and awx_instance_name must both be set"
SHAPE = "because the alert rules and the dashboard embed them"


def identity(name="awx", namespace="awx"):
    """The expected values of one identity: name, namespace, service, port, instance."""
    return {"name": name, "namespace": namespace, "service": f"{name}-service", "port": 80,
            "instance": f"{name}-service.{namespace}.svc.cluster.local:80"}


DEFAULT = identity()
NAMESPACE_ONLY = identity(namespace="lab")
NAME_ONLY = identity(name="tower")
BOTH = identity(name="tower", namespace="lab")

# Names that no reader reads any more, with the value each case sets.
REMOVED = {
    "awx_probe_instance": "other.awx.svc.cluster.local:80",
    "awx_probe_service": "other",
    "awx_probe_port": 8080,
    "prometheus_alert_awx_namespace": "lab",
    "prometheus_alert_awx_instance_name": "tower",
    "prometheus_alert_awx_probe_instance": "other.lab.svc.cluster.local:80",
    "grafana_awx_namespace": "lab",
    "grafana_awx_instance_name": "tower",
    "grafana_awx_probe_instance": "other.lab.svc.cluster.local:80",
}
E_ROWS = dict(zip([f"E{n}" for n in range(1, 10)], REMOVED))

UNSAFE = ['a"b', "a\\b", "a{b", "a$b", "awx\n"]


def netbox_field_types():
    """The type the NetBox custom field definitions declare, by field name."""
    defaults = yaml.safe_load(
        (ROOT / "roles/stack/operator/netbox-sot/defaults/main.yml").read_text())
    return {f["name"]: f["type"] for f in defaults["netbox_sot_monitoring_custom_fields"]}


PY_TYPES = {"integer": int, "text": str}


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.counter = 0

    def dir(self):
        self.counter += 1
        return self.tmp / f"run{self.counter}"

    def run_all(self, inventory=None, extra=None, consumers=ALL, stub=None, **kw):
        if stub is not None:
            runs = {c: real.run(c, self.dir(), inventory, extra_vars=extra, stub=stub, **kw)
                    for c in consumers if c == "seed"}
            runs.update(real.run_all([c for c in consumers if c != "seed"], self.dir(),
                                     inventory, extra_vars=extra, **kw))
            return runs
        return real.run_all(consumers, self.dir(), inventory, extra_vars=extra, **kw)

    def assertAllFour(self, expected, inventory=None, extra=None, label=""):
        """Run the four readers and compare each against one expected set of values."""
        runs = self.run_all(inventory, extra)
        for consumer, run in runs.items():
            self.assertEqual(run.returncode, 0, f"{label} {consumer}\n{run.output}")
        seen = {c: real.observed(r) for c, r in runs.items()}
        e = expected
        self.assertEqual(seen["seed"], {"service": e["service"], "namespace": e["namespace"],
                                        "port": e["port"]}, f"{label} seed")
        self.assertEqual(seen["alert"], {"name": {e["name"]}, "namespace": {e["namespace"]},
                                         "instance": e["instance"]}, f"{label} alert")
        self.assertEqual(seen["dashboard"], {"name": {e["name"]}, "namespace": {e["namespace"]},
                                             "instance": {e["instance"]}}, f"{label} dashboard")
        self.assertEqual(seen["deployer"], {"name": e["name"], "namespace": e["namespace"],
                                            "service": {e["service"]}, "port": {e["port"]}},
                         f"{label} deployer")

    def assertFailsNaming(self, run, message):
        self.assertNotEqual(run.returncode, 0, run.output)
        self.assertIn(message, run.output)
        self.assertIsNone(run.rendered, "the consumer rendered output after the check failed")


class SameIdentityEverywhere(Base):
    """A1 to A5: the four readers agree for every way of setting the two inputs."""

    CASES = {"A1": ({}, DEFAULT), "A2": ({"awx_namespace": "lab"}, NAMESPACE_ONLY),
             "A3": ({"awx_instance_name": "tower"}, NAME_ONLY),
             "A4": ({"awx_namespace": "lab", "awx_instance_name": "tower"}, BOTH)}

    def check(self, row, channels=("inventory", "extra")):
        values, expected = self.CASES[row]
        for channel in channels:
            with self.subTest(row=row, channel=channel):
                inventory, extra = (values, None) if channel == "inventory" else (None, values)
                self.assertAllFour(expected, inventory, extra, f"{row} {channel}")

    def test_default_identity_all_four(self):
        """A1 (default inputs, all four readers, inventory and -e)."""
        self.check("A1")

    def test_namespace_only_all_four(self):
        """A2 (namespace only)."""
        self.check("A2")

    def test_name_only_all_four(self):
        """A3 (name only)."""
        self.check("A3")

    def test_both_all_four(self):
        """A4 (both). The alert rule reads name tower in namespace lab in both rules."""
        self.check("A4")

    def test_extra_vars_give_the_same_identity_as_the_inventory(self):
        """A5: each of A1 to A4 by both channels, as one comparison of the two channels."""
        for row, (values, expected) in self.CASES.items():
            with self.subTest(row=row):
                by_inventory = self.run_all(values)
                by_extra = self.run_all(None, values)
                for consumer in ALL:
                    self.assertEqual(real.observed(by_inventory[consumer]),
                                     real.observed(by_extra[consumer]), f"{row} {consumer}")

    def test_ingress_routes_use_the_filters_and_the_rendered_port_is_80(self):
        """A1 to A4, deployer column: both IngressRoute definitions call the filters.

        No `-service` literal and no literal port remain in either definition; the
        two expressions are the ones the A rows render."""
        routes = real._ingress_route_exprs()
        self.assertEqual(set(routes), {"private", "public"})
        for lane, route in routes.items():
            self.assertEqual(route["name"], "{{ awx_instance_name | awx_service_name }}", lane)
            self.assertEqual(route["port"], "{{ awx_instance_name | awx_service_port }}", lane)
        text = (ROOT / "roles/stack/operator/awx/tasks/main.yml").read_text()
        self.assertNotIn("-service\"", text.replace("| awx_service_name }}\"", ""))
        run = real.run("deployer", self.dir(), {"awx_namespace": "lab", "awx_instance_name": "tower"})
        self.assertEqual(real.observed(run)["port"], {80})


class UnsafeValues(Base):
    """A6: a value that could break out of a PromQL string stops every reader."""

    def test_every_consumer_rejects_each_value(self):
        for var in ("awx_namespace", "awx_instance_name"):
            for value in UNSAFE:
                for channel in ("inventory", "extra"):
                    with self.subTest(var=var, value=value, channel=channel):
                        values = {var: value}
                        stub = real.NetBoxStub()
                        runs = self.run_all(values if channel == "inventory" else None,
                                            values if channel == "extra" else None, stub=stub)
                        for consumer, run in runs.items():
                            self.assertFailsNaming(run, SHAPE)
                        self.assertEqual(stub.requests, [])

    def test_the_filters_reject_each_value_when_the_dependency_is_dropped(self):
        """A6 (section 3 item 1): the shape check is inside the filters too.

        The instance name goes through a filter in all four readers. The namespace
        goes through one only in the probe instance, which the alert rules and the
        dashboard build; the seed and the deployer pass it on as plain data, so
        its shape check is the identity role's, which A6 above covers."""
        for var, other in (("awx_namespace", "awx_instance_name"), ("awx_instance_name", "awx_namespace")):
            for value in UNSAFE:
                with self.subTest(var=var, value=value):
                    # Without the dependency nothing loads the plugin, so point at it.
                    plugins = str(ROOT / "roles" / real.IDENTITY_ROLE / "filter_plugins")
                    runs = self.run_all({var: value, other: "awx"}, drop_dependency=True,
                                        env_extra={"ANSIBLE_FILTER_PLUGINS": plugins})
                    for consumer, run in runs.items():
                        if var == "awx_namespace" and consumer in ("seed", "deployer"):
                            continue
                        self.assertNotEqual(run.returncode, 0, f"{consumer}\n{run.output}")
                        self.assertIn("must be a plain Kubernetes name", run.output, consumer)
                        self.assertIsNone(run.rendered, consumer)


class RemovedNames(Base):
    """E1 to E9, E12: a name that no reader reads is inert."""

    def removed_name_is_inert(self, row, name):
        """E1 to E9 (generated as test_removed_name_is_inert_<id>): one name, set
        through the inventory and through -e, compared with A1."""
        for channel in ("inventory", "extra"):
            with self.subTest(row=row, name=name, channel=channel):
                values = {name: REMOVED[name]}
                self.assertAllFour(DEFAULT, values if channel == "inventory" else None,
                                   values if channel == "extra" else None, f"{row} {channel}")

    def test_all_removed_names_with_a_non_default_identity(self):
        """E12: E1 to E9 together with A4."""
        for channel in ("inventory", "extra"):
            with self.subTest(channel=channel):
                values = {**REMOVED, "awx_namespace": "lab", "awx_instance_name": "tower"}
                self.assertAllFour(BOTH, values if channel == "inventory" else None,
                                   values if channel == "extra" else None, f"E12 {channel}")

    def test_awx_key_in_coords_override_fails_before_any_request(self):
        """E10: the seed's first tasks run the real validation, before app-service.yml."""
        for coords in ({"awx": {"service": "awx-service", "namespace": "awx", "port": 80}},
                       {"awx": {"service": "x", "namespace": "y", "port": 1}, "forgejo": {}}):
            with self.subTest(coords=coords):
                stub = real.NetBoxStub()
                run = real.run("seed", self.dir(),
                               {"dmf_born_inventory_app_cluster_coords": coords}, stub=stub)
                self.assertFailsNaming(run, "must not have an awx entry")
                for text in ("awx_namespace", "awx_instance_name"):
                    self.assertIn(text, run.output)
                self.assertIn("Check that the coordinates override has no AWX entry", run.output)
                self.assertEqual(stub.requests, [])

    def test_coords_override_without_awx_still_stamps_awx(self):
        """E11: a dictionary of other apps only still gives AWX its stamp."""
        stub = real.NetBoxStub()
        run = real.run("seed", self.dir(), {"dmf_born_inventory_app_cluster_coords": {
            "forgejo": {"service": "forgejo-http", "namespace": "forgejo", "port": 3000}}},
            stub=stub)
        self.assertEqual(run.returncode, 0, run.output)
        self.assertEqual(real.observed(run), {"service": "awx-service", "namespace": "awx", "port": 80})


for _row, _name in E_ROWS.items():
    setattr(RemovedNames, f"test_removed_name_is_inert_{_row}",
            (lambda r, n: lambda self: self.removed_name_is_inert(r, n))(_row, _name))


class MissingInputs(Base):
    """M1 to M9: an empty, null or undefined input fails the reader before it stamps or renders."""

    BAD = {"empty": "", "null": None}

    def failing(self, consumer, var, kind, channel="inventory", stub=None):
        other = {"awx_namespace": "awx_instance_name", "awx_instance_name": "awx_namespace"}[var]
        if kind == "undefined":
            return real.run(consumer, self.dir(), {other: "awx"}, drop_dependency=True, stub=stub)
        values = {var: self.BAD[kind]}
        return real.run(consumer, self.dir(), values if channel == "inventory" else None,
                        extra_vars=values if channel == "extra" else None, stub=stub)

    def check_seed(self, var):
        for kind in ("empty", "null", "undefined"):
            for channel in ("inventory", "extra"):
                if kind == "undefined" and channel == "extra":
                    continue
                with self.subTest(var=var, kind=kind, channel=channel):
                    stub = real.NetBoxStub()
                    run = self.failing("seed", var, kind, channel, stub)
                    self.assertFailsNaming(run, PRESENCE)
                    self.assertEqual(stub.requests, [])
                    if kind == "undefined":
                        self.assertIn("dmf-born-inventory : Check that the AWX namespace", run.output)

    def test_seed_namespace(self):
        """M1 (empty), M2 (null), M3 (undefined): the seed's namespace."""
        self.check_seed("awx_namespace")

    def test_seed_instance_name(self):
        """M4: the seed's instance name, empty, null and undefined."""
        self.check_seed("awx_instance_name")

    def check_consumer(self, consumer, task):
        for var in ("awx_namespace", "awx_instance_name"):
            for kind in ("empty", "null", "undefined"):
                for channel in ("inventory", "extra"):
                    if kind == "undefined" and channel == "extra":
                        continue
                    with self.subTest(consumer=consumer, var=var, kind=kind, channel=channel):
                        run = self.failing(consumer, var, kind, channel)
                        self.assertFailsNaming(run, PRESENCE)
                        if kind == "undefined":
                            self.assertIn(task, run.output)

    def test_alert_rules(self):
        """M5: the alert rules, each input empty, null and undefined; no rules are written."""
        self.check_consumer("alert", "prometheus : Check that the AWX namespace")

    def test_dashboard(self):
        """M6: the dashboard; no ConfigMap is rendered."""
        self.check_consumer("dashboard", "grafana : Check that the AWX namespace")

    def test_deployer(self):
        """M7: the deployer; nothing is rendered."""
        self.check_consumer("deployer", "stack/operator/awx : Check that the AWX namespace")

    def test_a_dropped_dependency_with_both_inputs_fails_at_the_first_filter(self):
        """M8: both inputs supplied independently, dependency removed, a template calls a filter.

        The reader's own check passes, and the play fails at the render that
        calls the filter. This is a second line behind M3 to M7, and it holds
        only because nothing else in this process provides the filters. The
        invariant is M9."""
        values = {"awx_namespace": "awx", "awx_instance_name": "awx"}
        for consumer in ALL:
            with self.subTest(consumer=consumer):
                run = real.run(consumer, self.dir(), values, drop_dependency=True)
                self.assertNotEqual(run.returncode, 0, run.output)
                self.assertIn("No filter named", run.output)
                self.assertIn("Check that the AWX namespace and instance name are set", run.output)
                self.assertNotIn(PRESENCE, run.output)

    def test_every_consumer_depends_on_the_identity_role(self):
        """M9: the structure that holds even where a run cannot see a dropped dependency."""
        for consumer, (_, role) in real.CONSUMERS.items():
            with self.subTest(consumer=consumer):
                meta = yaml.safe_load((ROOT / "roles" / role / "meta/main.yml").read_text())
                deps = [d["role"] if isinstance(d, dict) else d for d in meta["dependencies"]]
                self.assertIn(real.IDENTITY_ROLE, deps)
                tasks = yaml.safe_load((ROOT / "roles" / role / "tasks/main.yml").read_text())
                # The seed keeps its token refusal as the first task; the check follows it.
                first = tasks[1] if consumer == "seed" else tasks[0]
                self.assertEqual(first["name"], "Check that the AWX namespace and instance name are set")
                self.assertIn("awx_namespace", " ".join(first["ansible.builtin.assert"]["that"]))
                self.assertIn("awx_instance_name", " ".join(first["ansible.builtin.assert"]["that"]))
        defaults = yaml.safe_load((ROOT / "roles" / real.IDENTITY_ROLE / "defaults/main.yml").read_text())
        self.assertEqual(defaults, {"awx_namespace": "awx", "awx_instance_name": "awx"})
        deployer = yaml.safe_load((ROOT / "roles/stack/operator/awx/defaults/main.yml").read_text())
        self.assertNotIn("awx_namespace", deployer)
        self.assertNotIn("awx_instance_name", deployer)


class Stamp(Base):
    """S1 to S10: the PATCH that the real app-service.yml sends to the stub NetBox."""

    def stamp(self, inventory=None, extra=None, app="awx", stub=None):
        stub = stub or real.NetBoxStub()
        run = real.run("seed", self.dir(), inventory, extra_vars=extra, stub=stub, app=app)
        self.assertEqual(run.returncode, 0, run.output)
        return stub

    def assertStamp(self, stub, service_id, service, namespace, port):
        (stamp,) = stub.stamps()
        self.assertEqual(stamp["path"], f"/api/ipam/services/{service_id}/")
        self.assertEqual(stamp["body"], {"custom_fields": {
            "cluster_service": service, "cluster_namespace": namespace, "cluster_port": port}})
        types = netbox_field_types()
        for field, value in stamp["body"]["custom_fields"].items():
            self.assertIsInstance(value, PY_TYPES[types[field]],
                                  f"{field} is declared {types[field]} in NetBox")

    def check(self, row, inventory, expected):
        stub = self.stamp(inventory)
        self.assertStamp(stub, 7, expected["service"], expected["namespace"], expected["port"])
        self.assertAllFour(expected, inventory, None, row)

    def test_stamp_default(self):
        """S1: default inputs. The port is the integer that the NetBox field declares."""
        self.check("S1", {}, DEFAULT)

    def test_stamp_non_default(self):
        """S2: lab and tower."""
        self.check("S2", {"awx_namespace": "lab", "awx_instance_name": "tower"}, BOTH)

    def test_stamp_namespace_only(self):
        """S3: namespace only."""
        self.check("S3", {"awx_namespace": "lab"}, NAMESPACE_ONLY)

    def test_stamp_name_only(self):
        """S4: name only."""
        self.check("S4", {"awx_instance_name": "tower"}, NAME_ONLY)

    def test_stamp_with_dictionary_omitting_awx(self):
        """S5: the dictionary is replaced and has no awx key; the stamp still happens."""
        stub = self.stamp({"dmf_born_inventory_app_cluster_coords": {
            "forgejo": {"service": "forgejo-http", "namespace": "forgejo", "port": 3000}}})
        self.assertStamp(stub, 7, "awx-service", "awx", 80)

    def test_stamp_when_the_service_is_created(self):
        """S6: the lookup is empty and the create returns id 9."""
        stub = self.stamp(stub=real.NetBoxStub(lookup_results=[], create_body={"id": 9}))
        self.assertStamp(stub, 9, "awx-service", "awx", 80)

    def test_no_stamp_without_service_id(self):
        """S7: lookup empty and a create reply without an id. No coordinate stamp is sent.

        The stub answers the create as the task expects (status 201, JSON body);
        the other tag and service requests still happen."""
        stub = self.stamp(stub=real.NetBoxStub(lookup_results=[], create_body={}))
        self.assertEqual(stub.stamps(), [])
        self.assertIn("POST", [r["method"] for r in stub.requests])

    def test_stamp_ignores_the_removed_names(self):
        """S8: -e awx_probe_instance and -e awx_probe_service, on the real task."""
        stub = self.stamp(extra={"awx_probe_instance": "other.awx.svc.cluster.local:80",
                                 "awx_probe_service": "other"})
        self.assertStamp(stub, 7, "awx-service", "awx", 80)
        self.assertAllFour(DEFAULT, None, {"awx_probe_instance": "other.awx.svc.cluster.local:80",
                                           "awx_probe_service": "other"}, "S8")

    def test_other_app_stamp_unchanged(self):
        """S9: forgejo keeps its own dictionary entry."""
        defaults = yaml.safe_load(
            (ROOT / "roles/common/dmf-born-inventory/defaults/main.yml").read_text())
        entry = defaults["dmf_born_inventory_app_cluster_coords"]["forgejo"]
        stub = self.stamp(app="forgejo")
        self.assertStamp(stub, 7, entry["service"], entry["namespace"], entry["port"])
        self.assertEqual(entry["service"], "forgejo-http")

    def test_other_app_with_empty_service_is_not_stamped(self):
        """S10: the existing guard on an empty service."""
        stub = self.stamp({"dmf_born_inventory_app_cluster_coords": {
            "forgejo": {"service": "", "namespace": "forgejo", "port": 3000}}}, app="forgejo")
        self.assertEqual(stub.stamps(), [])

# Instance names for the name rule: a DNS-1035 label of at most 48 characters
# (63 minus the 15 characters of "-admin-password").
NAME_ACCEPTED = ["awx", "a", "a" * 48, "tower-blue", "a1-b2"]
NAME_REJECTED = ["a" * 49, "tower.blue", "1awx", "-awx", "awx-", "Awx", ""]


class InstanceNameRule(Base):
    """The instance name must make a valid name for every derived Service, Deployment and Secret."""

    def run_alert(self, name, **kw):
        return real.run("alert", self.dir(), {"awx_instance_name": name, "awx_namespace": "awx"}, **kw)

    def test_the_role_check_applies_the_rule(self):
        for name in NAME_ACCEPTED:
            with self.subTest(accepted=name):
                run = self.run_alert(name)
                self.assertEqual(run.returncode, 0, run.output)
        for name in NAME_REJECTED:
            with self.subTest(rejected=name):
                run = self.run_alert(name)
                self.assertNotEqual(run.returncode, 0, run.output)
                self.assertIsNone(run.rendered)

    def test_the_filters_apply_the_rule(self):
        plugins = str(ROOT / "roles" / real.IDENTITY_ROLE / "filter_plugins")
        env = {"ANSIBLE_FILTER_PLUGINS": plugins}
        for name in NAME_ACCEPTED:
            with self.subTest(accepted=name):
                run = self.run_alert(name, drop_dependency=True, env_extra=env)
                self.assertEqual(run.returncode, 0, run.output)
        for name in NAME_REJECTED[:-1]:  # an empty value fails the presence check first
            with self.subTest(rejected=name):
                run = self.run_alert(name, drop_dependency=True, env_extra=env)
                self.assertNotEqual(run.returncode, 0, run.output)
                self.assertIn("must be a plain Kubernetes name", run.output)

    def test_the_filters_and_the_role_check_agree(self):
        import importlib.util
        import re
        spec = importlib.util.spec_from_file_location(
            "awx_identity_filters", ROOT / "roles" / real.IDENTITY_ROLE / "filter_plugins/awx_identity.py")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        tasks = yaml.safe_load((ROOT / "roles" / real.IDENTITY_ROLE / "tasks/main.yml").read_text())
        condition = next(c for t in tasks for c in t["ansible.builtin.assert"]["that"]
                         if c.startswith("awx_instance_name is match("))
        pattern = re.compile(condition[len("awx_instance_name is match('"):-2])
        for name in NAME_ACCEPTED + NAME_REJECTED + ["a" * 63, "a" * 253, "a.b.c"]:
            with self.subTest(name=name):
                self.assertEqual(bool(pattern.match(name)), bool(module._NAME.match(name)))


class FilterDiscovery(unittest.TestCase):
    def test_ci_promtool_renders_resolve_the_filters(self):
        """Filter discovery: the CI step that renders the rules for both promtool files.

        The test runs that step's script from the workflow file, so the commands
        are the CI's own. Without ANSIBLE_FILTER_PLUGINS the same render fails."""
        workflow = yaml.safe_load((ROOT.parent / ".github/workflows/ci.yml").read_text())
        steps = [s for job in workflow["jobs"].values() for s in job["steps"]
                 if s.get("name") == "Prometheus alert rule tests"]
        self.assertEqual(len(steps), 1)
        step = steps[0]
        cwd = ROOT.parent / step["working-directory"] if "working-directory" in step else ROOT.parent
        script = ["bash", "-c", step["run"]]
        done = subprocess.run(script, cwd=cwd, capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("rendered_alerts_tower.yml", step["run"])
        self.assertIn("rendered_alerts.yml", step["run"])
        bare = step["run"].replace("ANSIBLE_FILTER_PLUGINS=", "UNUSED_FILTER_PLUGINS=")
        done = subprocess.run(["bash", "-c", bare], cwd=cwd, capture_output=True, text=True)
        self.assertNotEqual(done.returncode, 0, "the render worked without the filter plugin")
        self.assertIn("awx_probe_instance", done.stdout + done.stderr)


if __name__ == "__main__":
    unittest.main()
