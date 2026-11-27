"""The platform-services dashboard shows the AWX probe lane as asleep, not
failed, while AWX sleeps on purpose (dmfdeploy/dmfdeploy#1027).

The test renders the dashboard ConfigMap with the Grafana role's own template,
so the AWX placeholders are replaced the way the role replaces them. It then
runs the probe table's query through `promtool test rules` as a recording rule
and checks the value of every lane. It renders the alert rules with the
prometheus role's template for the non-default identity cases. The test needs
`ansible` and `promtool` on the PATH."""
import json
import pathlib
import shutil
import subprocess
import tempfile
import textwrap
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]
GRAFANA = ROOT / "roles/base/grafana"
PROMETHEUS = ROOT / "roles/base/prometheus"
KEY = "  dmf-platform-services.json: |\n"
AWX = "awx-service.awx.svc.cluster.local:80"
NETBOX = "netbox.netbox.svc.cluster.local:80"


def ansible_template(src, dest, extra, *extra_files):
    args = [arg for f in extra_files for arg in ("-e", f"@{f}")]
    subprocess.run(
        ["ansible", "localhost", "-m", "ansible.builtin.template",
         "-a", f"src={src} dest={dest}", *args, "-e", json.dumps(extra)],
        check=True, capture_output=True, text=True)


def render_dashboard(workdir, **overrides):
    """Render the ConfigMap the role installs and return the dashboard JSON.

    The role's defaults file supplies the AWX identity, so `overrides` reach the
    template through the same variable resolution a play uses."""
    dest = workdir / "configmap.yml"
    vars_ = {"role_path": str(GRAFANA), "grafana_namespace": "monitoring", **overrides}
    ansible_template(GRAFANA / "templates/configmap-dashboards.yml.j2", dest, vars_,
                     GRAFANA / "defaults/main.yml")
    lines = dest.read_text().splitlines(keepends=True)
    body = []
    for line in lines[lines.index(KEY) + 1:]:
        if not line.startswith("    ") and line.strip():
            break
        body.append(line)
    return json.loads(textwrap.dedent("".join(body)))


def probe_table(dashboard):
    found = [p for p in dashboard["panels"] if p.get("type") == "table"
             and 'job="netbox-probe"' in p["targets"][0]["expr"]]
    assert len(found) == 1, "expected one NetBox probe table"
    return found[0]


def promtool_test(workdir, rule_files, tests):
    path = workdir / "case.promtool.yml"
    path.write_text(json.dumps({"rule_files": rule_files,
                                "evaluation_interval": "1m", "tests": tests}))
    return subprocess.run(["promtool", "test", "rules", str(path)],
                          capture_output=True, text=True)


def series(name, labels, value, samples=5):
    body = ", ".join(f'{k}="{v}"' for k, v in labels.items())
    return {"series": f"{name}{{{body}}}", "values": f"{value}x{samples}"}


def probe(app, instance, value, samples=5):
    return series("probe_success", {"job": "netbox-probe", "app": app,
                                    "instance": instance}, value, samples)


def cr(namespace, name, replicas, samples=5):
    return series("awx_cr_web_replicas", {"namespace": namespace, "name": name},
                  replicas, samples)


def lane(app, instance, value):
    return {"labels": f"dmf:lane_state{{app=\"{app}\", instance=\"{instance}\", job=\"netbox-probe\"}}",
            "value": value}


class DashboardLaneState(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def check(self, name, inputs, expected, **identity):
        expr = probe_table(render_dashboard(self.tmp, **identity))["targets"][0]["expr"]
        rules = self.tmp / "lane.rules.yml"
        rules.write_text(json.dumps({"groups": [{"name": "lane", "rules": [
            {"record": "dmf:lane_state", "expr": expr}]}]}))
        result = promtool_test(self.tmp, [str(rules)], [{
            "name": name, "interval": "1m", "input_series": inputs,
            "promql_expr_test": [{"expr": "dmf:lane_state", "eval_time": "3m",
                                  "exp_samples": expected}]}])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_no_placeholder_is_left_in_the_rendered_query(self):
        expr = probe_table(render_dashboard(self.tmp))["targets"][0]["expr"]
        self.assertNotIn("__AWX_", expr)
        self.assertIn('namespace="awx", name="awx"', expr)

    def test_asleep_reads_two_when_the_probe_fails(self):
        self.check("asleep, probe 0", [probe("awx", AWX, 0), cr("awx", "awx", 0)],
                   [lane("awx", AWX, 2)])

    def test_asleep_reads_two_when_the_probe_still_succeeds(self):
        self.check("asleep, probe 1", [probe("awx", AWX, 1), cr("awx", "awx", 0)],
                   [lane("awx", AWX, 2)])

    def test_awake_keeps_the_probe_result(self):
        self.check("awake, probe 0", [probe("awx", AWX, 0), cr("awx", "awx", 2)],
                   [lane("awx", AWX, 0)])
        self.check("awake, probe 1", [probe("awx", AWX, 1), cr("awx", "awx", 2)],
                   [lane("awx", AWX, 1)])

    def test_absent_metric_keeps_the_probe_result(self):
        self.check("absent, probe 0", [probe("awx", AWX, 0)], [lane("awx", AWX, 0)])
        self.check("absent, probe 1", [probe("awx", AWX, 1)], [lane("awx", AWX, 1)])

    def test_each_lane_has_exactly_one_series(self):
        self.check("several lanes",
                   [probe("awx", AWX, 0), probe("netbox", NETBOX, 1),
                    cr("awx", "awx", 0)],
                   [lane("awx", AWX, 2), lane("netbox", NETBOX, 1)])
        self.check("several lanes, duplicate CR series",
                   [probe("awx", AWX, 1), probe("netbox", NETBOX, 0),
                    cr("awx", "awx", 0),
                    series("awx_cr_web_replicas",
                           {"namespace": "awx", "name": "awx", "pod": "b"}, 0)],
                   [lane("awx", AWX, 2), lane("netbox", NETBOX, 0)])

    def test_sleep_of_another_lane_is_not_hidden(self):
        self.check("other lane at 0 while AWX sleeps",
                   [probe("netbox", NETBOX, 0), cr("awx", "awx", 0)],
                   [lane("netbox", NETBOX, 0)])

    def test_configured_identity_is_the_one_that_counts(self):
        identity = {"prometheus_alert_awx_namespace": "lab",
                    "prometheus_alert_awx_instance_name": "tower"}
        self.check("configured CR asleep, probe 0",
                   [probe("awx", AWX, 0), cr("lab", "tower", 0)],
                   [lane("awx", AWX, 2)], **identity)
        self.check("configured CR asleep, probe 1",
                   [probe("awx", AWX, 1), cr("lab", "tower", 0)],
                   [lane("awx", AWX, 2)], **identity)
        self.check("unrelated awx/awx asleep, configured CR awake",
                   [probe("awx", AWX, 0), cr("awx", "awx", 0), cr("lab", "tower", 2)],
                   [lane("awx", AWX, 0)], **identity)
        self.check("only the unrelated awx/awx exists",
                   [probe("awx", AWX, 1), cr("awx", "awx", 0)],
                   [lane("awx", AWX, 1)], **identity)

    def test_mapping_names_each_state_and_asleep_has_its_own_colour(self):
        table = probe_table(render_dashboard(self.tmp))["fieldConfig"]["defaults"]
        options = {}
        for m in table["mappings"]:
            options.update(m["options"])
        self.assertEqual({k: v["text"] for k, v in options.items()},
                         {"0": "failed", "1": "up", "2": "asleep"})
        colours = {s["value"]: s["color"] for s in table["thresholds"]["steps"]}
        self.assertEqual(colours, {None: "red", 1: "green", 2: "blue"})


def alert_rules(workdir, **overrides):
    rules = workdir / "rendered_alerts.yml"
    ansible_template(PROMETHEUS / "templates/rules/alerts.yaml.j2", rules, overrides,
                     PROMETHEUS / "defaults/main.yml")
    return rules


class SharedIdentity(unittest.TestCase):
    """The alert and the dashboard resolve one namespace and name."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def selectors(self, **overrides):
        dashboard = probe_table(render_dashboard(self.tmp, **overrides))
        alert = alert_rules(self.tmp, **overrides).read_text()
        return dashboard["targets"][0]["expr"], alert

    def test_alert_override_moves_both(self):
        expr, alert = self.selectors(prometheus_alert_awx_namespace="lab",
                                     prometheus_alert_awx_instance_name="tower")
        self.assertIn('namespace="lab", name="tower"', expr)
        self.assertIn('awx_cr_web_replicas{namespace="lab", name="tower"}', alert)
        self.assertNotIn('namespace="awx", name="awx"', expr + alert)

    def test_awx_role_variables_move_both(self):
        expr, alert = self.selectors(awx_namespace="lab", awx_instance_name="tower")
        self.assertIn('namespace="lab", name="tower"', expr)
        self.assertIn('awx_cr_web_replicas{namespace="lab", name="tower"}', alert)

    def test_alert_override_wins_over_the_awx_role_variables(self):
        expr, alert = self.selectors(awx_namespace="one", awx_instance_name="two",
                                     prometheus_alert_awx_namespace="lab",
                                     prometheus_alert_awx_instance_name="tower")
        self.assertIn('namespace="lab", name="tower"', expr)
        self.assertIn('awx_cr_web_replicas{namespace="lab", name="tower"}', alert)

    def test_defaults_agree(self):
        expr, alert = self.selectors()
        self.assertIn('namespace="awx", name="awx"', expr)
        self.assertIn('awx_cr_web_replicas{namespace="awx", name="awx"}', alert)


def run_name_check(namespace, instance_name):
    """Run the Grafana role's own name-check task through Ansible."""
    tasks = yaml.safe_load((GRAFANA / "tasks/main.yml").read_text())
    task = next(t for t in tasks
                if t.get("name") == "Check the AWX namespace and name the dashboard reads")
    return subprocess.run(
        ["ansible", "localhost", "-m", "ansible.builtin.assert",
         "-a", json.dumps(task["ansible.builtin.assert"]),
         "-e", json.dumps({"grafana_awx_namespace": namespace,
                           "grafana_awx_instance_name": instance_name})],
        capture_output=True, text=True)


class NameCheck(unittest.TestCase):
    def test_accepts_valid_names(self):
        for name in ("awx", "tower", "a.b-c.d", "a" * 63, "0a"):
            with self.subTest(name=name):
                result = run_name_check(name.split(".")[0], name)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_rejects_invalid_instance_names(self):
        for name in ("awx\n", "a..b", "a.-b", "-a", "a-", "AWX", "a" * 64,
                     "a.b" + "." + "c" * 64, ("a." * 130)[:-1], 'a"b', "a b", ""):
            with self.subTest(name=name):
                self.assertNotEqual(run_name_check("awx", name).returncode, 0)

    def test_rejects_invalid_namespaces(self):
        for name in ("awx\n", "a.b", "-a", "a-", "Awx", "a" * 64, 'a"b', ""):
            with self.subTest(name=name):
                self.assertNotEqual(run_name_check(name, "awx").returncode, 0)


class AlertIdentity(unittest.TestCase):
    def test_alert_reads_the_configured_cr(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        rules = alert_rules(tmp, prometheus_alert_awx_namespace="lab",
                            prometheus_alert_awx_instance_name="tower")
        labels = {"severity": "warning", "category": "sd-pipeline",
                  "job": "netbox-probe", "app": "awx", "instance": AWX}

        annotations = {
            "summary": f"Probe lane {AWX} (awx) is failing",
            "description": f"The blackbox probe for {AWX} has failed for 10 minutes. "
                           "The AWX lane does not alert while the AWX CR asks for 0 web "
                           "replicas (scale-to-zero sleep); a wake window or an install "
                           "without scale-to-zero alerts as usual.",
            "runbook_url": "https://github.com/dmfdeploy/dmf-runbooks/blob/main/docs/alerts.md#service-discovery-pipeline"}

        def case(name, crs, fires):
            return {"name": name, "interval": "1m",
                    "input_series": [probe("awx", AWX, 0, 20)] + crs,
                    "alert_rule_test": [{
                        "eval_time": "15m", "alertname": "NetboxProbeLaneDown",
                        "exp_alerts": [{"exp_labels": labels, "exp_annotations": annotations}] if fires else []}]}

        result = promtool_test(tmp, [str(rules)], [
            case("configured CR asleep -> silent", [cr("lab", "tower", 0, 20)], False),
            case("only awx/awx asleep -> fires", [cr("awx", "awx", 0, 20)], True),
            case("configured CR awake, awx/awx asleep -> fires",
                 [cr("lab", "tower", 2, 20), cr("awx", "awx", 0, 20)], True)])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
