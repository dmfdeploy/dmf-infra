"""The platform-services dashboard and the NetboxProbeLaneDown alert treat the
AWX probe lane as asleep, not failed, while AWX sleeps on purpose
(dmfdeploy/dmfdeploy#1027).

The case ids in the docstrings (C1, L5 and so on) are the ids in
docs/awx-probe-lane-cases.md.

The test renders the dashboard ConfigMap and the Prometheus rules by running the
real roles of the real playbooks (awx_role_path_1027.py), so the AWX identity
resolves the way a play resolves it. It runs the probe table's query and the
alert rule through `promtool test rules`. The input series carry the labels a
live scrape gives them.

Checks that the old rule also passed are named `regression_guard`: they hold
before and after the fix. The checks that tell old from new are the sleeping
and awake lane cases, and the two overbroad mutants of the rule, which must
fail. The test needs `ansible-playbook` and `promtool` on the PATH. Set
AWX_1027_EVIDENCE_DIR to keep the output of every promtool run."""
import atexit
import functools
import json
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import textwrap
import unittest

import yaml

import awx_role_path_1027 as real

ROOT = real.ROOT

KEY = "  dmf-platform-services.json: |\n"
AWX = "awx-service.awx.svc.cluster.local:80"
TOWER = "tower-service.lab.svc.cluster.local:80"
LOOKALIKES = ("awx-service-other.awx.svc.cluster.local:80",
              "awx-service.awx.svc.cluster.local:8080",
              "awx-service.awx.svc.cluster.local:80/path")
RUNBOOK = "https://github.com/dmfdeploy/dmf-runbooks/blob/main/docs/alerts.md#service-discovery-pipeline"

_SHARED = pathlib.Path(tempfile.mkdtemp())
atexit.register(shutil.rmtree, _SHARED, True)


@functools.lru_cache(maxsize=None)
def _rendered(consumer, inventory_json):
    workdir = _SHARED / f"{consumer}-{len(list(_SHARED.iterdir()))}"
    workdir.mkdir()
    run = real.run(consumer, workdir, json.loads(inventory_json))
    assert run.returncode == 0, run.output
    return run.rendered


def render_dashboard(**inventory):
    """The dashboard JSON the Grafana role's ConfigMap carries."""
    return json.loads(textwrap.dedent(
        real.dashboard_json_text(_rendered("dashboard", json.dumps(inventory, sort_keys=True)))))


def render_rules(**inventory):
    """The rule groups the Prometheus role puts in its Helm values."""
    return real.alert_rules(_rendered("alert", json.dumps(inventory, sort_keys=True)))


def probe_table(dashboard):
    found = [p for p in dashboard["panels"] if p.get("type") == "table"
             and 'job="netbox-probe"' in p["targets"][0]["expr"]]
    assert len(found) == 1, "expected one NetBox probe table"
    return found[0]


def alert_expr(rules):
    return real.alert_expr(rules, "NetboxProbeLaneDown")


def promtool_test(workdir, rule_files, tests, label="run"):
    path = workdir / "case.promtool.yml"
    path.write_text(json.dumps({"rule_files": rule_files,
                                "evaluation_interval": "1m", "tests": tests}))
    result = subprocess.run(["promtool", "test", "rules", str(path)],
                            capture_output=True, text=True)
    evidence = os.environ.get("AWX_1027_EVIDENCE_DIR")
    if evidence:
        out = pathlib.Path(evidence)
        out.mkdir(parents=True, exist_ok=True)
        name = re.sub(r"[^A-Za-z0-9]+", "-", label).strip("-")
        (out / f"promtool-{name}.txt").write_text(
            f"label: {label}\nrule files: {rule_files}\nexit code: {result.returncode}\n"
            f"--- cases ---\n{json.dumps(tests, indent=1)}\n--- stdout ---\n{result.stdout}"
            f"--- stderr ---\n{result.stderr}")
    return result


def series(name, labels, value, samples=5):
    body = ", ".join(f'{k}="{v}"' for k, v in labels.items())
    return {"series": f"{name}{{{body}}}", "values": f"{value}x{samples}"}


def probe(instance, value, samples=5):
    """A lane series as the live scrape gives it: no app label.

    A live series has exactly these three labels."""
    return series("probe_success", {"exposure": "private", "instance": instance,
                                    "job": "netbox-probe"}, value, samples)


# The CR series as kube-state-metrics exposes it, with the CR's own name and
# namespace left to the caller. The instance address is a documentation-range
# stand-in for the pod address.
LIVE_CR_LABELS = {
    "customresource_group": "awx.ansible.com", "customresource_kind": "AWX",
    "customresource_version": "v1beta1", "instance": "192.0.2.53:8080",
    "job": "kubernetes-service-endpoints", "node": "node-01",
    "service": "prometheus-kube-state-metrics", "app_kubernetes_io_component": "metrics",
    "app_kubernetes_io_instance": "prometheus", "app_kubernetes_io_managed_by": "Helm",
    "app_kubernetes_io_name": "kube-state-metrics", "app_kubernetes_io_part_of": "kube-state-metrics",
    "app_kubernetes_io_version": "2.20.0", "helm_sh_chart": "kube-state-metrics-9.1.0"}


def cr(namespace, name, replicas, samples=5, **labels):
    """awx_cr_web_replicas with the live label set; `labels` replace live ones."""
    return series("awx_cr_web_replicas",
                  {"name": name, "namespace": namespace, **{**LIVE_CR_LABELS, **labels}},
                  replicas, samples)


def lane(instance, value):
    return {"labels": f"dmf:lane_state{{exposure=\"private\", instance=\"{instance}\", job=\"netbox-probe\"}}",
            "value": value}



TOWER_IDENTITY = {"awx_namespace": "lab", "awx_instance_name": "tower"}

# Second-CR rows: (id, configured state, other state, which other CR, tower file).
# Other CR 1 has the configured namespace and another name; other CR 2 has the
# configured name in another namespace.
SECOND_CR = [("L1", 0, 2, 1, False), ("L2", 2, 0, 1, False), ("L3", 0, 2, 2, False),
             ("L4", 2, 0, 2, False), ("L5", 0, 2, 1, True), ("L6", 2, 0, 1, True),
             ("L7", 0, 2, 2, True), ("L8", 2, 0, 2, True)]


def other_cr(tower, kind):
    namespace, name = ("lab", "tower") if tower else ("awx", "awx")
    return (namespace, name + "2") if kind == 1 else (namespace + "2", name)


class DashboardLaneState(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def check(self, name, inputs, expected, **inventory):
        expr = probe_table(render_dashboard(**inventory))["targets"][0]["expr"]
        rules = self.tmp / "lane.rules.yml"
        rules.write_text(json.dumps({"groups": [{"name": "lane", "rules": [
            {"record": "dmf:lane_state", "expr": expr}]}]}))
        result = promtool_test(self.tmp, [str(rules)], [{
            "name": name, "interval": "1m", "input_series": inputs,
            "promql_expr_test": [{"expr": "dmf:lane_state", "eval_time": "3m",
                                  "exp_samples": expected}]}], label=f"dashboard {name}")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_awake_keeps_the_probe_result(self):
        """C1 (awake, probe 1 reads up) and C1b (awake, probe 0 reads failed)."""
        self.check("C1 awake, probe 1", [probe(AWX, 1), cr("awx", "awx", 2)], [lane(AWX, 1)])
        self.check("C1b awake, probe 0", [probe(AWX, 0), cr("awx", "awx", 2)], [lane(AWX, 0)])

    def test_asleep_reads_two_when_the_probe_fails(self):
        """C2 (asleep through autoscale). C3a, a hand scale of the CR, is the same series."""
        self.check("C2 asleep, probe 0", [probe(AWX, 0), cr("awx", "awx", 0)], [lane(AWX, 2)])

    def test_asleep_reads_two_when_the_probe_still_succeeds(self):
        """C2b (asleep, probe 1: a wake in progress)."""
        self.check("C2b asleep, probe 1", [probe(AWX, 1), cr("awx", "awx", 0)], [lane(AWX, 2)])

    def test_deployments_at_zero_with_the_cr_awake_reads_failed(self):
        """C3b: the CR still asks for 2, so the lane is not excused."""
        self.check("C3b CR at 2, probe 0", [probe(AWX, 0), cr("awx", "awx", 2)], [lane(AWX, 0)])

    def test_absent_metric_keeps_the_probe_result(self):
        """C4 (AWX not deployed: the CR metric is absent)."""
        self.check("C4 absent, probe 0", [probe(AWX, 0)], [lane(AWX, 0)])

    def test_regression_guard_lookalike_lanes_are_never_dropped(self):
        """C16: the earlier rule also passed this; the mutants of C16b tell old from new."""
        for other in LOOKALIKES:
            self.check(f"C16 {other}", [probe(other, 0), cr("awx", "awx", 0)], [lane(other, 0)])

    def test_configured_identity_is_the_one_that_counts(self):
        """C10 (the tower lane asleep reads 2)."""
        self.check("C10 tower asleep", [probe(TOWER, 0), cr("lab", "tower", 0)],
                   [lane(TOWER, 2)], **TOWER_IDENTITY)

    def test_regression_guard_lane_of_a_different_awx_service(self):
        """C10b: tower asleep, the lane at the default address at 0 reads failed."""
        self.check("C10b default lane, tower asleep", [probe(AWX, 0), cr("lab", "tower", 0)],
                   [lane(AWX, 0)], **TOWER_IDENTITY)

    def test_regression_guard_port_of_a_different_awx_service(self):
        """C17: tower asleep, the lane on port 8080 at 0 reads failed."""
        other = "tower-service.lab.svc.cluster.local:8080"
        self.check("C17 tower port 8080", [probe(other, 0), cr("lab", "tower", 0)],
                   [lane(other, 0)], **TOWER_IDENTITY)

    def second_cr(self, row, conf, oth, kind, tower):
        """L1 to L8 (generated as test_second_cr_<id> below): another CR never decides the lane."""
        namespace, name = ("lab", "tower") if tower else ("awx", "awx")
        target = TOWER if tower else AWX
        other = other_cr(tower, kind)
        self.check(f"{row} configured {conf}, other {oth}",
                   [probe(target, 0), cr(namespace, name, conf), cr(other[0], other[1], oth)],
                   [lane(target, 2 if conf == 0 else 0)], **(TOWER_IDENTITY if tower else {}))

    def test_second_cr_C19a(self):
        """C19a: two series of the configured CR, both 0: asleep, one lane series."""
        self.check("C19a both 0", [probe(AWX, 0), cr("awx", "awx", 0),
                                   cr("awx", "awx", 0, instance="192.0.2.54:8080")], [lane(AWX, 2)])

    def test_second_cr_C19b(self):
        """C19b: the same two series at 0 and 2: the larger value keeps the lane awake."""
        self.check("C19b 0 and 2", [probe(AWX, 0), cr("awx", "awx", 0),
                                    cr("awx", "awx", 2, instance="192.0.2.54:8080")], [lane(AWX, 0)])


for _row, _conf, _oth, _kind, _tower in SECOND_CR:
    setattr(DashboardLaneState, f"test_second_cr_{_row}",
            (lambda r, c, o, k, t: lambda self: self.second_cr(r, c, o, k, t))(_row, _conf, _oth, _kind, _tower))


def failing_labels(instance):
    return {"severity": "warning", "category": "sd-pipeline", "job": "netbox-probe",
            "exposure": "private", "instance": instance}


def failing_annotations(instance):
    return {"summary": f"Probe lane {instance} is failing",
            "description": f"The blackbox probe for {instance} has failed for 10 minutes. "
                           "The AWX lane does not alert while the AWX CR asks for 0 web "
                           "replicas (scale-to-zero sleep); a wake window or an install "
                           "without scale-to-zero alerts as usual.",
            "runbook_url": RUNBOOK}


def alert_case(name, lanes, crs, firing):
    """One alert case: 20 samples per series, evaluated after the 10m `for`."""
    return {"name": name, "interval": "1m",
            "input_series": [probe(i, v, 20) for i, v in lanes] + crs,
            "alert_rule_test": [{
                "eval_time": "15m", "alertname": "NetboxProbeLaneDown",
                "exp_alerts": [{"exp_labels": failing_labels(i),
                                "exp_annotations": failing_annotations(i)} for i in firing]}]}


class RuleRunner(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_rules(self, rules, cases, label):
        path = self.tmp / "rendered_alerts.yml"
        path.write_text(json.dumps(rules))
        return promtool_test(self.tmp, [str(path)], cases, label=label)


class TowerRuleFile(RuleRunner):
    """The tower promtool file, run against the rules the real roles render for it."""

    def test_tower_file_passes_on_the_rendered_rules(self):
        """C10, C10b, C17 and L5 to L8 in test_netbox_probe_lane_1027_tower.promtool.yml."""
        tests = ROOT / "roles/base/prometheus/tests"
        rendered = _rendered("alert", json.dumps(TOWER_IDENTITY, sort_keys=True))
        text = yaml.safe_load(rendered)["serverFiles"]["alerting_rules.yml"]
        (self.tmp / "rendered_alerts_tower.yml").write_text(yaml.safe_dump(text))
        shutil.copy(tests / "test_netbox_probe_lane_1027_tower.promtool.yml", self.tmp)
        done = subprocess.run(["promtool", "test", "rules",
                               str(self.tmp / "test_netbox_probe_lane_1027_tower.promtool.yml")],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


class RuleMutants(RuleRunner):
    """C16b: two overbroad versions of the rule must fail the lane cases.

    The old rule matched on a label the series never carry, so it dropped no lane
    and the lookalike cases cannot tell it from the fix. A rule that drops too
    much can be told apart: each mutant is built from the rendered rule by string
    replacement and must turn the cases that need the dropped lane red."""

    ASLEEP = "C16b AWX asleep, AWX lane at 0 -> silent"
    AWAKE = "C16b AWX awake, AWX lane at 0 -> fires"
    LOOKALIKE = "C16b AWX asleep, lookalike lane at 0 -> fires"
    CASES = [
        alert_case(ASLEEP, [(AWX, 0)], [cr("awx", "awx", 0, 20)], []),
        alert_case(AWAKE, [(AWX, 0)], [cr("awx", "awx", 2, 20)], [AWX]),
        alert_case(LOOKALIKE, [(LOOKALIKES[0], 0)], [cr("awx", "awx", 0, 20)], [LOOKALIKES[0]]),
    ]

    def mutated(self, old, new):
        rules = json.loads(json.dumps(render_rules()))
        for g in rules["groups"]:
            for r in g["rules"]:
                if r.get("alert") == "NetboxProbeLaneDown":
                    self.assertEqual(r["expr"].count(old), 1, r["expr"])
                    r["expr"] = r["expr"].replace(old, new)
        return rules

    def failed_cases(self, rules, label):
        result = self.run_rules(rules, self.CASES, label)
        names = [c["name"] for c in self.CASES]
        return result, {n for n in names if f"name: {n}," in result.stdout + result.stderr}

    def test_the_real_rule_passes_every_case(self):
        """C16b baseline: the real rule passes the cases the mutants fail."""
        result, failed = self.failed_cases(render_rules(), "lane cases, real rule")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(failed, set())

    def test_a_rule_that_drops_every_lane_while_the_cr_reads_0_fails(self):
        rules = self.mutated("unless on(instance)", "unless on()")
        result, failed = self.failed_cases(rules, "lane cases, mutant unless on()")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(failed, {self.LOOKALIKE})

    def test_a_rule_that_drops_every_awx_prefixed_lane_fails(self):
        rules = self.mutated('probe_success{job="netbox-probe"}',
                             'probe_success{job="netbox-probe", instance!~"awx-.*"}')
        result, failed = self.failed_cases(rules, 'lane cases, mutant instance!~"awx-.*"')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(failed, {self.AWAKE, self.LOOKALIKE})


if __name__ == "__main__":
    unittest.main()
