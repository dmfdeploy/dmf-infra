"""The platform-services dashboard and the NetboxProbeLaneDown alert treat the
AWX probe lane as asleep, not failed, while AWX sleeps on purpose
(dmfdeploy/dmfdeploy#1027).

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

import awx_role_path_1027 as real

KEY = "  dmf-platform-services.json: |\n"
AWX = "awx-service.awx.svc.cluster.local:80"
NMOS = "nmos-cpp-registry.nmos.svc.cluster.local:80"
LOOKALIKE = "awx-exporter.monitoring.svc.cluster.local:9100"
SUFFIXED = "awx-service.awx.svc.cluster.local:80/ready"
RUNBOOK = "https://github.com/dmfdeploy/dmf-runbooks/blob/main/docs/alerts.md#service-discovery-pipeline"

_SHARED = pathlib.Path(tempfile.mkdtemp())
atexit.register(shutil.rmtree, _SHARED, True)


@functools.lru_cache(maxsize=None)
def _rendered(consumer, inventory_json):
    workdir = _SHARED / f"{consumer}-{len(list(_SHARED.iterdir()))}"
    workdir.mkdir()
    result, rendered = real.run(consumer, workdir, json.loads(inventory_json))
    assert result.returncode == 0, result.stdout + result.stderr
    return rendered


def render_dashboard(**inventory):
    """The dashboard JSON the Grafana role's ConfigMap carries."""
    lines = _rendered("dashboard", json.dumps(inventory, sort_keys=True)).splitlines(keepends=True)
    body = []
    for line in lines[lines.index(KEY) + 1:]:
        if not line.startswith("    ") and line.strip():
            break
        body.append(line)
    return json.loads(textwrap.dedent("".join(body)))


def render_rules(**inventory):
    """The rule groups the Prometheus role puts in its Helm values."""
    return real.alert_rules(_rendered("alert", json.dumps(inventory, sort_keys=True)))


def probe_table(dashboard):
    found = [p for p in dashboard["panels"] if p.get("type") == "table"
             and 'job="netbox-probe"' in p["targets"][0]["expr"]]
    assert len(found) == 1, "expected one NetBox probe table"
    return found[0]


def alert_expr(rules):
    return next(r["expr"] for g in rules["groups"] for r in g["rules"]
                if r.get("alert") == "NetboxProbeLaneDown")


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

    Live, 2026-11-27: probe_success{exposure="private",
    instance="awx-service.awx.svc.cluster.local:80", job="netbox-probe"}"""
    return series("probe_success", {"exposure": "private", "instance": instance,
                                    "job": "netbox-probe"}, value, samples)


# The CR series as kube-state-metrics exposes it on the live sandbox,
# 2026-11-27, with the CR's own name and namespace left to the caller. The
# instance address is a documentation-range stand-in for the pod address.
LIVE_CR_LABELS = {
    "customresource_group": "awx.ansible.com", "customresource_kind": "AWX",
    "customresource_version": "v1beta1", "instance": "192.0.2.53:8080",
    "job": "kubernetes-service-endpoints", "node": "vrbs-exsz-01",
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

    def test_no_placeholder_is_left_in_the_rendered_query(self):
        expr = probe_table(render_dashboard())["targets"][0]["expr"]
        self.assertNotIn("__AWX_", expr)
        self.assertIn('namespace="awx", name="awx"', expr)
        self.assertIn(f'"instance", "{AWX}"', expr)

    def test_asleep_reads_two_when_the_probe_fails(self):
        self.check("asleep, probe 0", [probe(AWX, 0), cr("awx", "awx", 0)],
                   [lane(AWX, 2)])

    def test_asleep_reads_two_when_the_probe_still_succeeds(self):
        self.check("asleep, probe 1", [probe(AWX, 1), cr("awx", "awx", 0)],
                   [lane(AWX, 2)])

    def test_awake_keeps_the_probe_result(self):
        self.check("awake, probe 0", [probe(AWX, 0), cr("awx", "awx", 2)],
                   [lane(AWX, 0)])
        self.check("awake, probe 1", [probe(AWX, 1), cr("awx", "awx", 2)],
                   [lane(AWX, 1)])

    def test_absent_metric_keeps_the_probe_result(self):
        self.check("absent, probe 0", [probe(AWX, 0)], [lane(AWX, 0)])
        self.check("absent, probe 1", [probe(AWX, 1)], [lane(AWX, 1)])

    def test_each_lane_has_exactly_one_series(self):
        self.check("several lanes",
                   [probe(AWX, 0), probe(NMOS, 1), cr("awx", "awx", 0)],
                   [lane(AWX, 2), lane(NMOS, 1)])
        self.check("several lanes, duplicate CR series",
                   [probe(AWX, 1), probe(NMOS, 0), cr("awx", "awx", 0),
                    cr("awx", "awx", 0, instance="192.0.2.54:8080", node="vrbs-exsz-02")],
                   [lane(AWX, 2), lane(NMOS, 0)])

    def test_another_cr_does_not_decide_the_awx_lane(self):
        # A second AWX CR on the live label shape, awake while the configured
        # one sleeps, and the other way round.
        self.check("configured CR asleep, another CR awake",
                   [probe(AWX, 0), cr("awx", "awx", 0), cr("other", "second", 2)],
                   [lane(AWX, 2)])
        self.check("configured CR awake, another CR asleep",
                   [probe(AWX, 0), cr("awx", "awx", 2), cr("other", "second", 0)],
                   [lane(AWX, 0)])

    def test_sleep_of_another_lane_is_not_hidden(self):
        self.check("other lane at 0 while AWX sleeps",
                   [probe(NMOS, 0), cr("awx", "awx", 0)], [lane(NMOS, 0)])

    def test_regression_guard_lookalike_lanes_are_never_dropped(self):
        """The old rule also passed this: it dropped no lane at all."""
        for other in (LOOKALIKE, SUFFIXED, NMOS):
            self.check(f"{other} while AWX sleeps, probe 0",
                       [probe(other, 0), cr("awx", "awx", 0)], [lane(other, 0)])
            self.check(f"{other} while AWX sleeps, probe 1",
                       [probe(other, 1), cr("awx", "awx", 0)], [lane(other, 1)])

    def test_regression_guard_lane_of_a_different_awx_service(self):
        other = "awx-service.other.svc.cluster.local:80"
        self.check("same service name, other namespace",
                   [probe(other, 0), probe(AWX, 0), cr("awx", "awx", 0)],
                   [lane(other, 0), lane(AWX, 2)])

    def test_configured_identity_is_the_one_that_counts(self):
        identity = {"awx_namespace": "lab", "awx_instance_name": "tower"}
        lab = "tower-service.lab.svc.cluster.local:80"
        self.check("configured CR asleep, probe 0",
                   [probe(lab, 0), cr("lab", "tower", 0)], [lane(lab, 2)], **identity)
        self.check("configured CR asleep, probe 1",
                   [probe(lab, 1), cr("lab", "tower", 0)], [lane(lab, 2)], **identity)
        self.check("configured CR awake",
                   [probe(lab, 0), cr("lab", "tower", 2)], [lane(lab, 0)], **identity)
        self.check("unrelated awx/awx asleep, configured CR awake",
                   [probe(lab, 0), probe(AWX, 0), cr("awx", "awx", 0), cr("lab", "tower", 2)],
                   [lane(lab, 0), lane(AWX, 0)], **identity)
        self.check("only the unrelated awx/awx exists",
                   [probe(lab, 1), cr("awx", "awx", 0)], [lane(lab, 1)], **identity)

    def test_service_and_port_name_the_lane(self):
        identity = {"awx_probe_service": "tower-web", "awx_probe_port": 8052}
        seeded = "tower-web.awx.svc.cluster.local:8052"
        self.check("seeded lane asleep",
                   [probe(seeded, 0), probe(AWX, 0), cr("awx", "awx", 0)],
                   [lane(seeded, 2), lane(AWX, 0)], **identity)

    def test_mapping_names_each_state_and_asleep_has_its_own_colour(self):
        table = probe_table(render_dashboard())["fieldConfig"]["defaults"]
        options = {}
        for m in table["mappings"]:
            options.update(m["options"])
        self.assertEqual({k: v["text"] for k, v in options.items()},
                         {"0": "failed", "1": "up", "2": "asleep"})
        colours = {s["value"]: s["color"] for s in table["thresholds"]["steps"]}
        self.assertEqual(colours, {None: "red", 1: "green", 2: "blue"})


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


class AlertIdentity(RuleRunner):

    def test_alert_reads_the_configured_cr(self):
        lab = "tower-service.lab.svc.cluster.local:80"
        rules = render_rules(awx_namespace="lab", awx_instance_name="tower")
        result = self.run_rules(rules, [
            alert_case("configured CR asleep -> silent",
                       [(lab, 0), (LOOKALIKE, 1)], [cr("lab", "tower", 0, 20)], []),
            alert_case("only awx/awx asleep -> fires",
                       [(lab, 0), (LOOKALIKE, 1)], [cr("awx", "awx", 0, 20)], [lab]),
            alert_case("configured CR awake, awx/awx asleep -> fires",
                       [(lab, 0), (LOOKALIKE, 1)],
                       [cr("lab", "tower", 2, 20), cr("awx", "awx", 0, 20)], [lab])],
            "alert configured identity")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_the_other_alerts_follow_the_same_identity(self):
        expr = " ".join(r["expr"] for g in render_rules(awx_namespace="lab", awx_instance_name="tower")["groups"]
                        for r in g["rules"] if "awx" in r.get("alert", "").lower())
        self.assertIn('namespace="lab"', expr)
        self.assertIn('name="tower"', expr)
        self.assertIn('deployment="tower-web"', expr)
        self.assertNotIn('namespace="awx"', expr)


class RuleMutants(RuleRunner):
    """Two overbroad versions of the rule must fail the lane cases.

    The old rule matched on a label the series never carry, so it dropped no lane
    and the lookalike cases cannot tell it from the fix. A rule that drops too
    much can be told apart: each mutant is built from the rendered rule by string
    replacement and must turn the cases that need the dropped lane red."""

    CASES = [
        alert_case("AWX asleep, AWX lane at 0 -> silent", [(AWX, 0)], [cr("awx", "awx", 0, 20)], []),
        alert_case("AWX awake, AWX lane at 0 -> fires", [(AWX, 0)], [cr("awx", "awx", 2, 20)], [AWX]),
        alert_case("AWX asleep, other lane at 0 -> fires", [(NMOS, 0)], [cr("awx", "awx", 0, 20)], [NMOS]),
        alert_case("AWX asleep, lookalike lane at 0 -> fires", [(LOOKALIKE, 0)], [cr("awx", "awx", 0, 20)], [LOOKALIKE]),
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
        result, failed = self.failed_cases(render_rules(), "lane cases, real rule")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(failed, set())

    def test_a_rule_that_drops_every_lane_while_the_cr_reads_0_fails(self):
        rules = self.mutated("unless on(instance)", "unless on()")
        result, failed = self.failed_cases(rules, "lane cases, mutant unless on()")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(failed, {"AWX asleep, other lane at 0 -> fires",
                                  "AWX asleep, lookalike lane at 0 -> fires"})

    def test_a_rule_that_drops_every_awx_prefixed_lane_fails(self):
        rules = self.mutated('probe_success{job="netbox-probe"}',
                             'probe_success{job="netbox-probe", instance!~"awx-.*"}')
        result, failed = self.failed_cases(rules, 'lane cases, mutant instance!~"awx-.*"')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(failed, {"AWX awake, AWX lane at 0 -> fires",
                                  "AWX asleep, lookalike lane at 0 -> fires"})



if __name__ == "__main__":
    unittest.main()
