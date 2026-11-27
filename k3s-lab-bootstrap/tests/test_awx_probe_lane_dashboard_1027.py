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
import re
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
NMOS = "nmos-cpp-registry.nmos.svc.cluster.local:80"
LOOKALIKE = "awx-exporter.monitoring.svc.cluster.local:9100"
SUFFIXED = "awx-service.awx.svc.cluster.local:80/ready"


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


def probe(instance, value, samples=5):
    """A lane series with the labels the live scrape gives it: no app label."""
    return series("probe_success", {"exposure": "private", "instance": instance,
                                    "job": "netbox-probe"}, value, samples)


def cr(namespace, name, replicas, samples=5):
    return series("awx_cr_web_replicas", {"namespace": namespace, "name": name},
                  replicas, samples)


def lane(instance, value):
    return {"labels": f"dmf:lane_state{{exposure=\"private\", instance=\"{instance}\", job=\"netbox-probe\"}}",
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
                   [probe(AWX, 0), probe(NMOS, 1),
                    cr("awx", "awx", 0)],
                   [lane(AWX, 2), lane(NMOS, 1)])
        self.check("several lanes, duplicate CR series",
                   [probe(AWX, 1), probe(NMOS, 0),
                    cr("awx", "awx", 0),
                    series("awx_cr_web_replicas",
                           {"namespace": "awx", "name": "awx", "pod": "b"}, 0)],
                   [lane(AWX, 2), lane(NMOS, 0)])

    def test_sleep_of_another_lane_is_not_hidden(self):
        self.check("other lane at 0 while AWX sleeps",
                   [probe(NMOS, 0), cr("awx", "awx", 0)],
                   [lane(NMOS, 0)])

    def test_lookalike_lanes_are_never_dropped(self):
        for other in (LOOKALIKE, SUFFIXED, NMOS):
            self.check(f"{other} while AWX sleeps, probe 0",
                       [probe(other, 0), cr("awx", "awx", 0)], [lane(other, 0)])
            self.check(f"{other} while AWX sleeps, probe 1",
                       [probe(other, 1), cr("awx", "awx", 0)], [lane(other, 1)])

    def test_lane_of_a_different_awx_service_is_not_the_configured_lane(self):
        other = "awx-service.other.svc.cluster.local:80"
        self.check("same service name, other namespace",
                   [probe(other, 0), probe(AWX, 0), cr("awx", "awx", 0)],
                   [lane(other, 0), lane(AWX, 2)])

    def test_configured_identity_is_the_one_that_counts(self):
        identity = {"prometheus_alert_awx_namespace": "lab",
                    "prometheus_alert_awx_instance_name": "tower"}
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

    def test_seed_coordinates_name_the_lane(self):
        coords = {"dmf_born_inventory_app_cluster_coords": {
            "awx": {"service": "tower-web", "namespace": "lab", "port": 8052}}}
        seeded = "tower-web.lab.svc.cluster.local:8052"
        self.check("seeded lane asleep",
                   [probe(seeded, 0), probe(AWX, 0), cr("awx", "awx", 0)],
                   [lane(seeded, 2), lane(AWX, 0)], **coords)

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
        for text in (expr, alert):
            self.assertIn('namespace="lab", name="tower"', text.replace('\\"', '"'))
            self.assertIn("tower-service.lab.svc.cluster.local:80", text)
        self.assertNotIn(AWX, expr + alert)

    def test_awx_role_variables_move_both(self):
        expr, alert = self.selectors(awx_namespace="lab", awx_instance_name="tower")
        for text in (expr, alert):
            self.assertIn("tower-service.lab.svc.cluster.local:80", text)

    def test_alert_override_wins_over_the_awx_role_variables(self):
        expr, alert = self.selectors(awx_namespace="one", awx_instance_name="two",
                                     prometheus_alert_awx_namespace="lab",
                                     prometheus_alert_awx_instance_name="tower")
        for text in (expr, alert):
            self.assertIn("tower-service.lab.svc.cluster.local:80", text)

    def test_seed_coordinates_move_both(self):
        coords = {"awx": {"service": "tower-web", "namespace": "lab", "port": 8052}}
        expr, alert = self.selectors(dmf_born_inventory_app_cluster_coords=coords)
        for text in (expr, alert):
            self.assertIn("tower-web.lab.svc.cluster.local:8052", text)

    def test_defaults_agree(self):
        expr, alert = self.selectors()
        for text in (expr, alert):
            self.assertIn(AWX, text)

    def test_alert_and_dashboard_use_one_sleep_clause(self):
        """Both expressions embed the same label_replace(...) text."""
        clause = r"label_replace\(\s*max\(.*?\) == 0,\s*\"instance\", \"[^\"]*\", \"\", \"\"\s*\)"
        expr, alert = self.selectors()
        dash = re.findall(clause, expr.replace('\\"', '"'), re.S)
        rule = re.findall(clause, alert, re.S)
        squash = lambda t: re.sub(r"\s+", " ", t).replace("( ", "(").replace(" )", ")")
        self.assertEqual(len(dash), 2)
        self.assertEqual(len(rule), 1)
        self.assertEqual({squash(t) for t in dash}, {squash(t) for t in rule})


def run_name_check(namespace, instance_name, probe_instance=AWX):
    """Run the Grafana role's own name-check task through Ansible."""
    tasks = yaml.safe_load((GRAFANA / "tasks/main.yml").read_text())
    task = next(t for t in tasks
                if t.get("name") == "Check the AWX namespace, name and probe instance the dashboard reads")
    return subprocess.run(
        ["ansible", "localhost", "-m", "ansible.builtin.assert",
         "-a", json.dumps(task["ansible.builtin.assert"]),
         "-e", json.dumps({"grafana_awx_namespace": namespace,
                           "grafana_awx_instance_name": instance_name,
                           "grafana_awx_probe_instance": probe_instance})],
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


class ProbeInstanceCheck(unittest.TestCase):
    def test_accepts_service_addresses(self):
        for value in (AWX, "tower-web.lab.svc.cluster.local:8052", "a.b.svc.cluster.local:1"):
            with self.subTest(value=value):
                self.assertEqual(run_name_check("awx", "awx", value).returncode, 0)

    def test_rejects_values_that_could_widen_or_break_the_query(self):
        for value in (AWX + "\n", "awx-service.awx.svc.cluster.local", 'a".b.svc.cluster.local:80',
                      "a.b.svc.cluster.local:80/ready", "a.*.svc.cluster.local:80",
                      "$1.b.svc.cluster.local:80", "a.b.svc.cluster.local:", "A.b.svc.cluster.local:80",
                      "a.b.c.svc.cluster.local:80", "a.b.svc.cluster.local:123456", ""):
            with self.subTest(value=value):
                self.assertNotEqual(run_name_check("awx", "awx", value).returncode, 0)


class AlertIdentity(unittest.TestCase):
    def test_alert_reads_the_configured_cr(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        rules = alert_rules(tmp, prometheus_alert_awx_namespace="lab",
                            prometheus_alert_awx_instance_name="tower")
        LAB = "tower-service.lab.svc.cluster.local:80"
        labels = {"severity": "warning", "category": "sd-pipeline",
                  "job": "netbox-probe", "exposure": "private", "instance": LAB}

        annotations = {
            "summary": f"Probe lane {LAB} is failing",
            "description": f"The blackbox probe for {LAB} has failed for 10 minutes. "
                           "The AWX lane does not alert while the AWX CR asks for 0 web "
                           "replicas (scale-to-zero sleep); a wake window or an install "
                           "without scale-to-zero alerts as usual.",
            "runbook_url": "https://github.com/dmfdeploy/dmf-runbooks/blob/main/docs/alerts.md#service-discovery-pipeline"}

        def case(name, crs, fires):
            return {"name": name, "interval": "1m",
                    "input_series": [probe(LAB, 0, 20), probe(LOOKALIKE, 1, 20)] + crs,
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
