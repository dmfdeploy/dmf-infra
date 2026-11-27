"""Tests for the sandbox verify summary (dmfdeploy/dmfdeploy#1031).

Each check gets a broken input that must be reported and a good input that
must stay quiet. Collection is tested at its boundary: the real HTTP client
talks to a local server, and the real console probe runs against a local server
that checks the session cookie the way Starlette's session middleware does.
"""

import asyncio
import importlib.util
import json
import os
import re
import subprocess
import sys
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import yaml

ROOT = Path(__file__).parents[1]
FILES = ROOT / "playbooks/files"
FIXTURES = Path(__file__).parent / "fixtures/verify-summary"
ALLOWLIST = ROOT / "roles/base/prometheus/files/verify-allowlist.yml"
SCRAPE_TEMPLATE = ROOT / "roles/base/prometheus/templates/scrape_configs.yml.j2"


def load(name):
    spec = importlib.util.spec_from_file_location(name, FILES / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


vs = load("verify_summary")
probe = load("console_routes_probe")

SECRET = "unit-test-secret"


def target(job, instance, health="up", error="", **labels):
    return {"labels": {"job": job, "instance": instance, **labels}, "health": health, "lastError": error}


def targets_reply(*targets):
    return {"status": "success", "data": {"activeTargets": list(targets), "droppedTargets": []}}


def healthy():
    return [
        PROMSD_TARGET,
        target("prometheus", "localhost:9090"),
        target("kubernetes-apiservers", "192.0.2.5:6443"),
        target("kubernetes-nodes", "node-a"),
        target("kubernetes-nodes-cadvisor", "node-a"),
        target("kubernetes-pods", "192.0.2.30:9100"),
        target("netbox-probe", "https://netbox.example"),
    ]


def alerts_reply(*alerts):
    return {"status": "success", "data": {"alerts": [{"labels": {"alertname": n}, "state": s} for n, s in alerts]}}


def config_reply(*job_blocks):
    return {"status": "success", "data": {"yaml": "scrape_configs:\n" + "".join(job_blocks)}}


STATIC = "  - job_name: %s\n    static_configs:\n    - targets:\n%s"
SD = "  - job_name: %s\n    kubernetes_sd_configs:\n    - role: %s\n"
HTTP_SD = "  - job_name: %s\n    http_sd_configs:\n    - url: http://sd\n"
CORE_CONFIG = (
    STATIC % ("prometheus", "      - localhost:9090\n"),
    SD % ("kubernetes-apiservers", "endpoints"),
    SD % ("kubernetes-nodes", "node"),
    SD % ("kubernetes-nodes-cadvisor", "node"),
    SD % ("kubernetes-pods", "pod"),
    HTTP_SD % "netbox-probe",
)
DISCOVERY_JOBS = {
    "file": "  - job_name: sd-job\n    file_sd_configs:\n    - files: [x]\n",
    "kubernetes": SD % ("sd-job", "service"),
    "http": HTTP_SD % "sd-job",
}
PROMSD_JOB = STATIC % ("dmf-promsd-approle-rotation", "      - dmf-promsd.monitoring.svc.cluster.local:8000\n")
FULL_CONFIG = config_reply(*CORE_CONFIG, PROMSD_JOB)
EXPECTED = vs.expected_jobs(FULL_CONFIG)[0]
CORE_EXPECTED = vs.expected_jobs(config_reply(*CORE_CONFIG))[0]
PROMSD_TARGET = target("dmf-promsd-approle-rotation", "dmf-promsd.monitoring.svc.cluster.local:8000")

ALLOWED = {"Watchdog": "dead-man switch"}
AWX_DOWN = target("netbox-probe", "https://awx", "down", "refused", app="awx")


class TargetChecks(unittest.TestCase):
    def test_good_targets_are_quiet(self):
        self.assertEqual(vs.check_targets(targets_reply(*healthy()), 1, "awake", EXPECTED), [])

    def test_every_down_instance_is_named(self):
        down = healthy() + [target("kubernetes-pods", "192.0.2.14:80", "down", "boom"), target("kubernetes-pods", "192.0.2.15:80", "down", "bang")]
        findings = vs.check_targets(targets_reply(*down), 1, "awake", EXPECTED)
        self.assertEqual(len(findings), 2)
        self.assertTrue(all("kubernetes-pods" in f for f in findings))
        self.assertTrue(any("192.0.2.14" in f for f in findings) and any("192.0.2.15" in f for f in findings))

    def test_missing_expected_job_is_reported(self):
        without = [t for t in healthy() if t["labels"]["job"] != "kubernetes-apiservers"]
        findings = vs.check_targets(targets_reply(*without), 1, "awake", EXPECTED)
        self.assertEqual(findings, ["targets: expected 1 target(s) for job kubernetes-apiservers, found 0"])

    def test_vanished_component_job_is_reported_while_the_core_jobs_stay_healthy(self):
        without = [t for t in healthy() if t["labels"]["job"] != "dmf-promsd-approle-rotation"]
        findings = vs.check_targets(targets_reply(*without), 1, "awake", EXPECTED)
        self.assertEqual(findings, ["targets: expected 1 target(s) for job dmf-promsd-approle-rotation, found 0"])

    def test_node_jobs_need_one_target_per_node(self):
        findings = vs.check_targets(targets_reply(*healthy()), 2, "awake", EXPECTED)
        self.assertEqual(len(findings), 2)
        self.assertTrue(all("expected 2" in f for f in findings))

    def test_unknown_node_count_is_incomplete_not_clean(self):
        findings = vs.check_targets(targets_reply(*healthy()), None, "awake", EXPECTED)
        self.assertEqual(len(findings), 2)
        self.assertTrue(all(f.startswith("incomplete:") for f in findings))

    def test_unknown_health_after_the_wait_is_reported(self):
        unknown = healthy() + [target("kubernetes-pods", "192.0.2.14:80", "unknown")]
        findings = vs.check_targets(targets_reply(*unknown), 1, "awake", EXPECTED)
        self.assertEqual(len(findings), 1)
        self.assertIn("not scraped yet", findings[0])

    def test_empty_and_malformed_replies_are_incomplete(self):
        for reply in (targets_reply(), {"status": "error"}, {"status": "success", "data": {}}, "x", None, {"status": "success", "data": {"activeTargets": [1]}}):
            findings = vs.check_targets(reply, 1, "awake", EXPECTED)
            self.assertEqual(len(findings), 1, reply)
            self.assertTrue(findings[0].startswith("incomplete:"), reply)

    def test_asleep_awx_lane_is_dropped_only_when_asleep(self):
        reply = targets_reply(*healthy(), AWX_DOWN)
        self.assertEqual(vs.check_targets(reply, 1, "asleep", EXPECTED), [])
        for state in ("awake", "unknown"):
            findings = vs.check_targets(reply, 1, state, EXPECTED)
            self.assertEqual(len(findings), 1, state)
            self.assertIn("netbox-probe https://awx", findings[0])

    def test_shared_job_failures_of_other_lanes_stay_findings_while_asleep(self):
        other_app = target("netbox-probe", "https://netbox", "down", "refused", app="netbox")
        no_app = target("netbox-probe", "https://x", "down", "refused")
        other_job = target("dmf-static-probes", "https://awx", "down", "refused", app="awx")
        findings = vs.check_targets(targets_reply(*healthy(), AWX_DOWN, other_app, no_app, other_job), 1, "asleep", EXPECTED)
        self.assertEqual(len(findings), 3)
        self.assertFalse(any("https://awx" in f and "netbox-probe" in f for f in findings))

    def test_awx_state_needs_positive_evidence_to_be_asleep(self):
        cases = [
            (((0, "deployment.apps/awx-autoscale\n"), (0, "0")), "asleep"),
            (((0, "deployment.apps/awx-autoscale\n"), (0, "1")), "awake"),
            (((0, ""), (0, "0")), "awake"),  # no helper: a sleeping AWX is not by design
            (((1, ""), (0, "0")), "unknown"),
            (((0, "deployment.apps/awx-autoscale\n"), (1, "")), "unknown"),
            (((0, "deployment.apps/awx-autoscale\n"), (0, "")), "unknown"),
            (((0, "deployment.apps/awx-autoscale\n"), (0, "oops")), "unknown"),
        ]
        for (helper, web), expected in cases:
            self.assertEqual(vs.awx_state(helper, web), expected, (helper, web))

    def test_batch2_node_scrape_break_is_reported(self):
        # The break shows in targets as soon as the first scrape fails. The
        # TargetDown alert needs 10 minutes of failure, so the alert fixture
        # models a later state; the targets check is the early signal.
        node_targets = json.loads((FIXTURES / "batch2-targets.json").read_text())
        later_alerts = json.loads((FIXTURES / "batch2-alerts.json").read_text())
        target_findings = vs.check_targets(node_targets, 1, "awake", CORE_EXPECTED)
        self.assertEqual(len(target_findings), 2)
        self.assertTrue(any("kubernetes-nodes " in f and "403" in f for f in target_findings))
        self.assertTrue(any("kubernetes-nodes-cadvisor " in f and "403" in f for f in target_findings))
        alert_findings, _ = vs.check_alerts(later_alerts, ALLOWED)
        self.assertEqual(alert_findings, ["alerts: TargetDown is firing (2)"])
        banner = vs.render(target_findings + alert_findings)
        for needle in ("kubernetes-nodes ", "kubernetes-nodes-cadvisor ", "TargetDown", "SANDBOX SELF-CHECK"):
            self.assertIn(needle, banner)


class ExpectedJobs(unittest.TestCase):
    def test_full_config_gives_static_node_and_apiserver_expectations(self):
        expected, findings = vs.expected_jobs(FULL_CONFIG)
        self.assertEqual(findings, [])
        self.assertEqual(
            expected,
            {
                "prometheus": 1,
                "kubernetes-apiservers": 1,
                "kubernetes-nodes": "nodes",
                "kubernetes-nodes-cadvisor": "nodes",
                "kubernetes-pods": vs.DISCOVERED,
                "netbox-probe": vs.DISCOVERED,
                "dmf-promsd-approle-rotation": 1,
            },
        )

    def test_discovery_jobs_expect_at_least_one_target(self):
        self.assertEqual(EXPECTED["kubernetes-pods"], vs.DISCOVERED)
        self.assertEqual(EXPECTED["netbox-probe"], vs.DISCOVERED)

    def test_static_job_expects_every_listed_address(self):
        two = STATIC % ("dmf-static-probes", "      - a:1\n      - b:2\n")
        self.assertEqual(vs.expected_jobs(config_reply(*CORE_CONFIG, two))[0]["dmf-static-probes"], 2)

    def test_unreadable_config_is_incomplete_not_clean(self):
        for reply in (None, {"status": "error"}, {"status": "success", "data": {}}, {"status": "success", "data": {"yaml": ": : ["}}, config_reply(), {"status": "success", "data": {"yaml": "scrape_configs: []"}}):
            expected, findings = vs.expected_jobs(reply)
            self.assertEqual(expected, {}, reply)
            self.assertEqual(len(findings), 1, reply)
            self.assertTrue(findings[0].startswith("incomplete:"), reply)

    def test_missing_pyyaml_is_incomplete(self):
        with mock.patch.dict(sys.modules, {"yaml": None}):
            findings = vs.expected_jobs(FULL_CONFIG)[1]
        self.assertEqual(len(findings), 1)
        self.assertTrue(findings[0].startswith("incomplete:"))

    def test_job_whose_targets_cannot_be_expected_is_incomplete(self):
        bare = "  - job_name: bare\n"
        findings = vs.expected_jobs(config_reply(*CORE_CONFIG, bare))[1]
        self.assertEqual(findings, ["incomplete: expected targets of job bare cannot be established from the loaded configuration"])

    def test_every_discovery_kind_is_expected_to_find_a_target(self):
        for block in DISCOVERY_JOBS.values():
            expected, findings = vs.expected_jobs(config_reply(*CORE_CONFIG, block))
            self.assertEqual(findings, [])
            self.assertEqual(expected["sd-job"], vs.DISCOVERED, block)

    def test_core_job_missing_from_the_config_is_reported(self):
        findings = vs.expected_jobs(config_reply(*CORE_CONFIG[:3], *CORE_CONFIG[4:]))[1]
        self.assertEqual(findings, ["targets: job kubernetes-nodes-cadvisor is missing from the loaded scrape configuration"])

    def test_discovery_job_with_no_targets_is_incomplete(self):
        for kind, block in DISCOVERY_JOBS.items():
            expected = vs.expected_jobs(config_reply(*CORE_CONFIG, block))[0]
            findings = vs.check_targets(targets_reply(*healthy()), 1, "awake", expected)
            self.assertEqual(findings, ["incomplete: sd-job: coverage not established (no targets discovered)"], kind)

    def test_discovery_job_with_an_up_target_is_quiet(self):
        for kind, block in DISCOVERY_JOBS.items():
            expected = vs.expected_jobs(config_reply(*CORE_CONFIG, block))[0]
            reply = targets_reply(*healthy(), target("sd-job", "found:1"))
            self.assertEqual(vs.check_targets(reply, 1, "awake", expected), [], kind)


class AlertChecks(unittest.TestCase):
    def test_only_allowed_alerts_is_quiet(self):
        self.assertEqual(vs.check_alerts(alerts_reply(("Watchdog", "firing")), ALLOWED), ([], 0))

    def test_firing_alert_off_the_list_is_named(self):
        findings, _ = vs.check_alerts(alerts_reply(("Watchdog", "firing"), ("HostMemoryPressure", "firing")), ALLOWED)
        self.assertEqual(findings, ["alerts: HostMemoryPressure is firing (1)"])

    def test_pending_alerts_are_counted_not_reported(self):
        self.assertEqual(vs.check_alerts(alerts_reply(("TargetDown", "pending")), ALLOWED), ([], 1))

    def test_malformed_replies_are_incomplete(self):
        for reply in ({"status": "error"}, {"status": "success", "data": {"alerts": "x"}}, [], None, {"status": "success", "data": {"alerts": [{"state": "firing"}]}}):
            findings, _ = vs.check_alerts(reply, ALLOWED)
            self.assertEqual(len(findings), 1, reply)
            self.assertTrue(findings[0].startswith("incomplete:"), reply)

    def test_empty_allowlist_reports_watchdog_too(self):
        findings, _ = vs.check_alerts(alerts_reply(("Watchdog", "firing")), {})
        self.assertEqual(len(findings), 1)


class AllowlistValidation(unittest.TestCase):
    def test_repo_allowlist_is_valid_and_holds_only_by_design_alerts(self):
        allowed, errors = vs.load_allowlist(yaml.safe_load(ALLOWLIST.read_text()))
        self.assertEqual(errors, [])
        self.assertEqual(set(allowed), {"Watchdog"})

    def test_bad_entries_are_rejected(self):
        ok = {"name": "Watchdog", "reason": "by design"}
        bad = {
            "not a mapping": ["Watchdog"],
            "extra top-level key": {"alerts": [ok], "targets": []},
            "alerts not a list": {"alerts": {}},
            "empty reason": {"alerts": [{"name": "Watchdog", "reason": ""}]},
            "blank reason": {"alerts": [{"name": "Watchdog", "reason": "  "}]},
            "missing reason": {"alerts": [{"name": "Watchdog"}]},
            "extra key": {"alerts": [{**ok, "match": "x"}]},
            "pattern name": {"alerts": [{"name": "Host.*", "reason": "x"}]},
            "wildcard name": {"alerts": [{"name": "*", "reason": "x"}]},
            "duplicate": {"alerts": [ok, ok]},
            "entry not a mapping": {"alerts": ["Watchdog"]},
        }
        for label, data in bad.items():
            allowed, errors = vs.load_allowlist(data)
            self.assertTrue(errors, label)
            self.assertEqual(allowed, {}, label)


class ConsoleChecks(unittest.TestCase):
    @staticmethod
    def routes(**reasons):
        out = {}
        for route in vs.CONSOLE_ROUTES:
            out[route] = {"status": 200, "body": {"reason": reasons.get(route, "")}}
        return {"routes": out}

    def test_good_replies_are_quiet(self):
        self.assertEqual(vs.check_console(self.routes(), "awake"), [])

    def test_each_error_reason_is_named(self):
        for route, reason in [
            ("/api/changes/commits", "forgejo-unreachable"),
            ("/api/changes/commits", "forgejo-partial"),
            ("/api/changes/commits", "forgejo-unconfigured"),
            ("/api/changes/pulls", "forgejo-unreachable"),
            ("/api/changes/jobs", "awx-unreachable"),
            ("/api/changes/jobs", "awx-unconfigured"),
            ("/api/changes/jobs", "something-new"),
        ]:
            findings = vs.check_console(self.routes(**{route: reason}), "awake")
            self.assertEqual(len(findings), 1, reason)
            self.assertIn(route, findings[0])
            self.assertIn(reason, findings[0])

    def test_awx_not_running_is_expected_only_while_asleep(self):
        reply = self.routes(**{"/api/changes/jobs": "awx-not-running"})
        self.assertEqual(vs.check_console(reply, "asleep"), [])
        for state in ("awake", "unknown"):
            self.assertEqual(len(vs.check_console(reply, state)), 1, state)

    def test_route_errors_and_odd_results_are_reported(self):
        broken = self.routes()
        broken["routes"]["/api/changes/pulls"] = {"status": 401, "body": {"detail": "no"}}
        broken["routes"]["/api/changes/jobs"] = {"status": 200, "error": "reply is not JSON"}
        del broken["routes"]["/api/changes/commits"]
        self.assertEqual(len(vs.check_console(broken, "awake")), 3)
        for result in ({"error": "boom"}, {"routes": 3}, [], None):
            self.assertEqual(len(vs.check_console(result, "awake")), 1, result)
        no_reason = self.routes()
        no_reason["routes"]["/api/changes/pulls"]["body"] = {"pulls": []}
        self.assertEqual(len(vs.check_console(no_reason, "awake")), 1)


class GrafanaChecks(unittest.TestCase):
    def test_healthy_reply_is_quiet(self):
        self.assertEqual(vs.check_grafana(200, json.dumps({"commit": "x", "database": "ok", "version": "12"})), [])

    def test_broken_replies_are_reported(self):
        for status, body in [(503, ""), (200, "not json"), (200, json.dumps({"database": "failing"})), (200, "[]")]:
            self.assertEqual(len(vs.check_grafana(status, body)), 1, (status, body))


class Render(unittest.TestCase):
    def test_clean_run_prints_one_line(self):
        out = vs.render([])
        self.assertEqual(len(out.splitlines()), 1)
        self.assertTrue(out.startswith("Sandbox self-check: clean"))
        self.assertIn("No console route reads Grafana", out)
        self.assertIn("checked through its own /api/health", out)

    def test_findings_print_a_banner_naming_each(self):
        out = vs.render(["targets: a is down", "alerts: B is firing (1)"])
        self.assertIn("2 finding(s)", out)
        self.assertIn("targets: a is down", out)
        self.assertIn("alerts: B is firing (1)", out)
        self.assertIn("no console route reads Grafana", out)


# ── collection boundary: real HTTP client against a local server ─────────────


class Reply:
    def __init__(self, status=200, body="", delay=0.0):
        self.status, self.body, self.delay = status, body, delay


class Server:
    """Local HTTP server; `routes` maps a request path to a Reply."""

    def __init__(self):
        self.routes = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                reply = outer.routes.get(self.path.split("?")[0], Reply(404, "{}"))
                time.sleep(reply.delay)
                raw = reply.body
                body = raw if isinstance(raw, bytes) else str(raw).encode()
                try:
                    self.send_response(reply.status)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeIo(vs.Io):
    """Real HTTP, scripted kubectl. `kube` maps a keyword in the args to (rc, out)."""

    def __init__(self, kube):
        super().__init__("/unused")
        self.kube, self.calls, self.slept = kube, [], []

    def kubectl(self, args, stdin=None, timeout=60):
        self.calls.append((args, stdin))
        for keyword, answer in self.kube.items():
            if keyword in args or keyword in " ".join(args):
                return answer(args, stdin) if callable(answer) else answer
        return 1, ""

    def sleep(self, seconds):
        self.slept.append(seconds)


GOOD_KUBE = {
    "prometheus-server": (0, "127.0.0.1"),
    "svc grafana": (0, "127.0.0.1"),
    "awx-autoscale": (0, ""),
    "get nodes": (0, "node/node-a\n"),
}


class Boundary(unittest.TestCase):
    def setUp(self):
        self.server = Server()
        self.addCleanup(self.server.close)
        self.params = {
            "kubeconfig": "/unused",
            "monitoring_namespace": "monitoring",
            "prometheus_path": "/prometheus",
            "grafana_prefix": "/grafana",
            "cms_namespace": "dmf-cms",
            "cms_port": "8000",
            "awx_namespace": "awx",
            "settle_tries": 3,
            "settle_delay": 0,
            "allowlist": {"alerts": [{"name": "Watchdog", "reason": "by design"}]},
            "allowlist_error": False,
            "probe_source": "print('{}')",
            "service_port": self.server.port,
        }
        self.kube = dict(GOOD_KUBE)
        self.kube["exec"] = (0, json.dumps({"routes": {r: {"status": 200, "body": {"reason": ""}} for r in vs.CONSOLE_ROUTES}}))
        self.set_prom(targets_reply(*healthy()), alerts_reply(("Watchdog", "firing")))
        self.server.routes["/grafana/api/health"] = Reply(200, json.dumps({"database": "ok"}))

    def set_prom(self, targets, alerts, config=None):
        self.server.routes["/prometheus/api/v1/status/config"] = Reply(200, json.dumps(config or FULL_CONFIG))
        self.server.routes["/prometheus/api/v1/targets"] = Reply(200, json.dumps(targets))
        self.server.routes["/prometheus/api/v1/alerts"] = Reply(200, json.dumps(alerts))

    def run_summary(self):
        io = FakeIo(self.kube)
        return vs.run_checks(self.params, io), io

    def test_good_run_is_clean(self):
        out, _ = self.run_summary()
        self.assertTrue(out.startswith("Sandbox self-check: clean"), out)

    def test_good_run_says_grafana_has_no_console_read(self):
        out, _ = self.run_summary()
        self.assertIn("No console route reads Grafana", out)
        self.assertIn("own /api/health", out)

    def test_vanished_component_job_is_reported_through_the_whole_run(self):
        gone = [t for t in healthy() if t["labels"]["job"] != "dmf-promsd-approle-rotation"]
        self.set_prom(targets_reply(*gone), alerts_reply(("Watchdog", "firing")))
        out, _ = self.run_summary()
        self.assertIn("1 finding(s)", out)
        self.assertIn("job dmf-promsd-approle-rotation, found 0", out)

    def test_discovery_job_with_no_targets_is_incomplete_through_the_whole_run(self):
        for kind, block in DISCOVERY_JOBS.items():
            self.set_prom(targets_reply(*healthy()), alerts_reply(("Watchdog", "firing")), config_reply(*CORE_CONFIG, PROMSD_JOB, block))
            out, _ = self.run_summary()
            self.assertIn("incomplete: sd-job: coverage not established (no targets discovered)", out, kind)
            self.assertFalse(out.startswith("Sandbox self-check: clean"), out)
            self.set_prom(targets_reply(*healthy(), target("sd-job", "found:1")), alerts_reply(("Watchdog", "firing")), config_reply(*CORE_CONFIG, PROMSD_JOB, block))
            out, _ = self.run_summary()
            self.assertTrue(out.startswith("Sandbox self-check: clean"), (kind, out))

    def test_unreadable_config_is_an_incomplete_line_through_the_whole_run(self):
        self.server.routes["/prometheus/api/v1/status/config"] = Reply(500, "{}")
        out, _ = self.run_summary()
        self.assertIn("incomplete: Prometheus configuration read failed: HTTP 500", out)
        self.assertFalse(out.startswith("Sandbox self-check: clean"), out)

    def test_batch2_break_through_the_whole_run(self):
        self.set_prom(json.loads((FIXTURES / "batch2-targets.json").read_text()), json.loads((FIXTURES / "batch2-alerts.json").read_text()), config_reply(*CORE_CONFIG))
        out, _ = self.run_summary()
        for needle in ("kubernetes-nodes ", "kubernetes-nodes-cadvisor ", "TargetDown"):
            self.assertIn(needle, out)

    def test_prometheus_reply_failures_become_named_findings(self):
        replies = {
            "empty": Reply(200, json.dumps(targets_reply())),
            "malformed": Reply(200, "{not json"),
            "non-200": Reply(500, "{}"),
            "unsuccessful": Reply(200, json.dumps({"status": "error"})),
        }
        for label, reply in replies.items():
            self.server.routes["/prometheus/api/v1/targets"] = reply
            self.server.routes["/prometheus/api/v1/alerts"] = reply
            out, _ = self.run_summary()
            self.assertIn("SANDBOX SELF-CHECK", out, label)
            self.assertIn("incomplete:", out, label)

    def test_timeout_is_reported_and_the_run_finishes(self):
        self.server.routes["/prometheus/api/v1/targets"] = Reply(200, "{}", delay=1.5)
        self.server.routes["/grafana/api/health"] = Reply(200, "{}", delay=1.5)
        with mock.patch.object(vs, "HTTP_TIMEOUT_SECONDS", 0.3):
            out, _ = self.run_summary()
        self.assertIn("incomplete: Prometheus targets read failed: request failed", out)
        self.assertIn("grafana: /api/health is unavailable", out)

    def test_settle_polls_until_no_target_is_unknown(self):
        states = iter([healthy() + [target("kubernetes-pods", "p", "unknown")], healthy()])
        replies = []

        class Seq(Reply):
            @property
            def body(self):
                replies.append(1)
                return json.dumps(targets_reply(*next(states)))

            @body.setter
            def body(self, value):
                pass

        self.server.routes["/prometheus/api/v1/targets"] = Seq()
        out, io = self.run_summary()
        self.assertTrue(out.startswith("Sandbox self-check: clean"), out)
        self.assertEqual(len(replies), 2)
        self.assertEqual(len(io.slept), 1)

    def test_unknown_that_never_settles_is_reported_after_the_last_try(self):
        self.set_prom(targets_reply(*healthy(), target("kubernetes-pods", "p", "unknown")), alerts_reply())
        out, io = self.run_summary()
        self.assertIn("not scraped yet", out)
        self.assertEqual(len(io.slept), self.params["settle_tries"] - 1)

    def test_empty_target_set_never_settles_into_clean(self):
        self.set_prom(targets_reply(), alerts_reply())
        out, _ = self.run_summary()
        self.assertIn("incomplete: Prometheus reports no active targets", out)

    def test_grafana_unhealthy_and_invalid_replies(self):
        for reply in (Reply(503, "{}"), Reply(200, "nope"), Reply(200, json.dumps({"database": "failing"}))):
            self.server.routes["/grafana/api/health"] = reply
            out, _ = self.run_summary()
            self.assertIn("grafana:", out)

    def test_grafana_path_prefix_is_empty_in_host_mode(self):
        self.params["grafana_prefix"] = ""
        self.server.routes["/api/health"] = Reply(200, json.dumps({"database": "ok"}))
        out, _ = self.run_summary()
        self.assertTrue(out.startswith("Sandbox self-check: clean"), out)

    def test_awx_sleep_discounts_only_its_lane(self):
        self.kube["awx-autoscale"] = (0, "deployment.apps/awx-autoscale\n")
        self.kube["awx-web"] = (0, "0")
        lane = target("netbox-probe", "https://awx", "down", "refused", app="awx")
        self.set_prom(targets_reply(*healthy(), lane), alerts_reply())
        out, _ = self.run_summary()
        self.assertTrue(out.startswith("Sandbox self-check: clean"), out)
        self.kube["awx-web"] = (1, "")
        out, _ = self.run_summary()
        self.assertIn("AWX sleep state could not be read", out)
        self.assertIn("netbox-probe https://awx", out)

    def test_failed_collections_become_findings_and_output_still_renders(self):
        self.kube.clear()  # every kubectl call fails
        out, _ = self.run_summary()
        for needle in (
            "AWX sleep state could not be read",
            "Prometheus service address could not be read",
            "Grafana service address could not be read",
            "console: probe failed to run",
            "SANDBOX SELF-CHECK",
        ):
            self.assertIn(needle, out)

    def test_a_raising_collector_is_contained_per_stage(self):
        class Boom(FakeIo):
            def kubectl(self, args, stdin=None, timeout=60):
                if "exec" in args:
                    raise subprocess.TimeoutExpired("kubectl", 1)
                return super().kubectl(args, stdin, timeout)

        out = vs.run_checks(self.params, Boom(self.kube))
        self.assertIn("incomplete: console stage failed (TimeoutExpired)", out)
        self.assertNotIn("Sandbox self-check: clean", out)

    def test_allowlist_failures_hide_no_alert(self):
        self.params["allowlist_error"] = True
        out, _ = self.run_summary()
        self.assertIn("allowlist could not be read", out)
        self.assertIn("Watchdog is firing", out)
        self.params["allowlist_error"] = False
        self.params["allowlist"] = {"alerts": [{"name": "Watchdog", "reason": ""}]}
        out, _ = self.run_summary()
        self.assertIn("allowlist invalid", out)
        self.assertIn("Watchdog is firing", out)

    def test_console_output_that_is_not_json_is_incomplete(self):
        self.kube["exec"] = (0, "Traceback...\n")
        out, _ = self.run_summary()
        self.assertIn("incomplete: console probe printed no JSON", out)

    def test_probe_source_goes_to_the_pod_on_stdin(self):
        _, io = self.run_summary()
        exec_calls = [c for c in io.calls if "exec" in c[0]]
        self.assertEqual(exec_calls[0][1], "print('{}')")
        self.assertEqual(exec_calls[0][0][-4:], ["python", "-I", "-", "8000"])

    def test_main_never_fails_even_on_a_bad_argument(self):
        for argv in (["x", "{not json"], ["x"], ["x", "{}"]):
            result = subprocess.run([sys.executable, str(FILES / "verify_summary.py"), *argv[1:]], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, argv)
            self.assertIn("INCOMPLETE", result.stdout)


# ── the console probe against a local server ─────────────────────────────────


def starlette_accepts(cookie, secret):
    """Whether Starlette's real session middleware reads a user out of the cookie."""
    from starlette.middleware.sessions import SessionMiddleware

    seen = {}

    async def app(scope, receive, send):
        seen.update(scope["session"])

    async def run():
        wrapped = SessionMiddleware(app, secret_key=secret)
        scope = {"type": "http", "method": "GET", "path": "/", "headers": [(b"cookie", b"session=" + cookie.encode())], "query_string": b""}

        async def receive():
            return {"type": "http.request"}

        async def send(message):
            pass

        await wrapped(scope, receive, send)

    asyncio.run(run())
    return seen.get("user")


class ConsoleServer(Server):
    """Serves the changes routes behind a session check like the console's."""

    def __init__(self, secret, prefix="", replies=None):
        super().__init__()
        self.secret, self.prefix = secret, prefix
        self.replies = replies or {}
        self.cookies = []
        outer = self
        base = self.httpd.RequestHandlerClass

        class Handler(base):
            def do_GET(self):
                outer.cookies.append(self.headers.get("Cookie", ""))
                route = self.path
                if not route.startswith(outer.prefix + "/api/"):
                    return self._send(404, "{}")
                route = route[len(outer.prefix):]
                cookie = self.headers.get("Cookie", "").removeprefix("session=")
                if not starlette_accepts_safe(cookie, outer.secret):
                    return self._send(401, json.dumps({"detail": "Authentication required"}))
                status, body = outer.replies.get(route, (200, json.dumps({"reason": ""})))
                self._send(status, body)

            def _send(self, status, body):
                data = body.encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.httpd.RequestHandlerClass = Handler


def starlette_accepts_safe(cookie, secret):
    try:
        return starlette_accepts(cookie, secret)
    except Exception:  # noqa: BLE001
        return None


class ConsoleProbe(unittest.TestCase):
    def run_probe(self, server, env_secret=SECRET, prefix=""):
        env = {k: v for k, v in os.environ.items() if not k.startswith("DMF_CONSOLE")}
        if env_secret is not None:
            env["DMF_CONSOLE_SECRET_KEY"] = env_secret
        if prefix:
            env["DMF_CONSOLE_BASE_PATH"] = prefix
        done = subprocess.run(
            [sys.executable, "-", str(server.port)],
            input=(FILES / "console_routes_probe.py").read_text(),
            capture_output=True, text=True, env=env, timeout=60,
        )
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertNotIn(SECRET, done.stdout + done.stderr)
        return json.loads(done.stdout.strip().splitlines()[-1])

    def serve(self, **kw):
        server = ConsoleServer(**kw)
        self.addCleanup(server.close)
        return server

    def test_minted_cookie_is_accepted_by_starlettes_session_middleware(self):
        user = starlette_accepts(probe.session_cookie(SECRET), SECRET)
        self.assertEqual(user["role"], "viewer")
        self.assertIsNone(starlette_accepts(probe.session_cookie(SECRET), "another-secret"))

    def test_valid_session_and_good_replies_are_quiet(self):
        result = self.run_probe(self.serve(secret=SECRET))
        self.assertEqual(vs.check_console(result, "awake"), [])

    def test_base_path_is_honoured(self):
        result = self.run_probe(self.serve(secret=SECRET, prefix="/console"), prefix="/console/")
        self.assertEqual(vs.check_console(result, "awake"), [])

    def test_invalid_session_is_reported_per_route(self):
        result = self.run_probe(self.serve(secret="a-different-secret"))
        findings = vs.check_console(result, "awake")
        self.assertEqual(len(findings), 3)
        self.assertTrue(all("HTTP 401" in f for f in findings))

    def test_error_replies_are_reported(self):
        replies = {
            "/api/changes/commits": (200, json.dumps({"reason": "forgejo-unreachable"})),
            "/api/changes/pulls": (500, "boom"),
            "/api/changes/jobs": (200, "not json"),
        }
        findings = vs.check_console(self.run_probe(self.serve(secret=SECRET, replies=replies)), "awake")
        self.assertEqual(len(findings), 3)
        self.assertTrue(any("forgejo-unreachable" in f for f in findings))
        self.assertTrue(any("HTTP 500" in f for f in findings))

    def test_server_that_is_down_is_reported(self):
        server = self.serve(secret=SECRET)
        port = server.port
        server.close()
        server.port = port
        findings = vs.check_console(self.run_probe(server), "awake")
        self.assertEqual(len(findings), 3)
        self.assertTrue(all("request failed" in f for f in findings))

    def test_missing_secret_is_reported_without_a_request(self):
        server = self.serve(secret=SECRET)
        findings = vs.check_console(self.run_probe(server, env_secret=None), "awake")
        self.assertEqual(len(findings), 1)
        self.assertIn("DMF_CONSOLE_SECRET_KEY", findings[0])
        self.assertEqual(server.cookies, [])

    def test_probe_sends_only_to_loopback(self):
        self.assertIn("http://127.0.0.1:", (FILES / "console_routes_probe.py").read_text())


# ── wiring ───────────────────────────────────────────────────────────────────


class Wiring(unittest.TestCase):
    def test_core_jobs_exist_in_the_scrape_config(self):
        jobs = set(re.findall(r"job_name: (\S+)", SCRAPE_TEMPLATE.read_text()))
        self.assertLessEqual(set(vs.CORE_JOBS), jobs)
        self.assertIn("dmf-promsd-approle-rotation", jobs)

    def test_awx_lane_job_exists_in_the_scrape_config(self):
        self.assertIn("job_name: " + vs.AWX_LANE["job"], SCRAPE_TEMPLATE.read_text())

    def test_summary_is_the_last_import_of_the_sandbox_verify_wrapper(self):
        plays = yaml.safe_load((ROOT / "bootstrap-sandbox-verify.yml").read_text())
        self.assertEqual(plays[-1]["import_playbook"], "playbooks/verify-sandbox-summary.yml")
        self.assertIn("verify-summary", plays[-1]["tags"])

    def test_the_play_cannot_fail_the_verify_step(self):
        play = yaml.safe_load((ROOT / "playbooks/verify-sandbox-summary.yml").read_text())[0]
        self.assertTrue(play["ignore_unreachable"])
        block = play["tasks"][0]
        self.assertTrue(block["rescue"])
        run = block["block"][1]
        self.assertIs(run["failed_when"], False)
        self.assertIs(play["tasks"][1]["ignore_errors"], True)


if __name__ == "__main__":
    unittest.main()
