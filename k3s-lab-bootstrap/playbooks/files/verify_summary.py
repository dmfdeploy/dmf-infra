#!/usr/bin/env python3
"""Sandbox verify summary: report scrape targets, firing alerts, console reads
and Grafana health without ever failing the verify step.

The playbook runs this on the control node with one JSON argument. Collection
(kubectl, HTTP) goes through small functions that tests replace. Every stage
catches its own errors and turns them into a finding that starts with
"incomplete:", so a read that failed is never taken for a clean result. The
process always exits 0 and always prints a summary.

The console has no Grafana read, so the summary says so and checks Grafana's
own ``/api/health`` instead.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any, Callable

HTTP_TIMEOUT_SECONDS = 10
KUBECTL_TIMEOUT_SECONDS = 60
PROBE_TIMEOUT_SECONDS = 120
SERVICE_PORT_DEFAULT = 80  # both services publish port 80 on their ClusterIP

# Expected coverage comes from the scrape configuration Prometheus loaded, not
# from a fixed list. A job with fewer active targets than the configuration
# promises is a finding, so a job that vanishes or whose discovery broke cannot
# make the check look clean. "nodes" means one target per Kubernetes node.
#   static_configs      -> one target per listed address
#   role: node          -> one target per Kubernetes node
#   API server job      -> one target (its endpoint selection is fixed)
#   other *_sd_configs  -> driven by annotations or NetBox, so none can be
#                          legitimate; every target found is still checked
#   anything else       -> expected coverage cannot be established: incomplete
# The four jobs below must also appear in the loaded configuration.
APISERVER_JOB = "kubernetes-apiservers"
# Expectation for a job whose targets come from discovery other than the node role: at least one.
DISCOVERED = "discovered"
CORE_JOBS = ("prometheus", APISERVER_JOB, "kubernetes-nodes", "kubernetes-nodes-cadvisor")
# The lane that fails on purpose while AWX sleeps (dmfdeploy/dmfdeploy#1027).
AWX_LANE = {"job": "netbox-probe", "app": "awx"}

# Each route answers 200 with a reason: "" is healthy, any other token is an
# error state the console reports (forgejo-*, awx-*). awx-not-running is
# expected only while AWX sleeps on purpose.
CONSOLE_ROUTES = ("/api/changes/commits", "/api/changes/pulls", "/api/changes/jobs")
ALERT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Io:
    """The side effects, in one place so tests can replace them."""

    def __init__(self, kubeconfig: str) -> None:
        self.kubeconfig = kubeconfig

    def http_get(self, url: str) -> tuple[int, str]:
        """Return (status, body). Raises OSError on connection failure or timeout."""
        try:
            with urllib.request.urlopen(url, timeout=HTTP_TIMEOUT_SECONDS) as response:
                return response.status, response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            with exc:
                return exc.code, exc.read().decode("utf-8", "replace")

    def kubectl(self, args: list[str], stdin: str | None = None, timeout: int = KUBECTL_TIMEOUT_SECONDS) -> tuple[int, str]:
        done = subprocess.run(
            ["sudo", "kubectl", "--kubeconfig", self.kubeconfig, *args],
            input=stdin,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return done.returncode, done.stdout

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


# ── allowlist ────────────────────────────────────────────────────────────────


def load_allowlist(data: Any) -> tuple[dict[str, str], list[str]]:
    """Validate the parsed allowlist. Returns ({name: reason}, errors)."""
    errors: list[str] = []
    if not isinstance(data, dict) or set(data) != {"alerts"} or not isinstance(data["alerts"], list):
        return {}, ["the file must hold one key, alerts, with a list of entries"]
    allowed: dict[str, str] = {}
    for index, entry in enumerate(data["alerts"], start=1):
        label = "entry %d" % index
        if not isinstance(entry, dict) or set(entry) != {"name", "reason"}:
            errors.append("%s must have exactly the keys name and reason" % label)
            continue
        name, reason = entry["name"], entry["reason"]
        if not isinstance(name, str) or not ALERT_NAME.match(name):
            errors.append("%s: name must be one exact alert name, not a pattern" % label)
            continue
        if not isinstance(reason, str) or not reason.strip():
            errors.append("%s (%s): reason must not be empty" % (label, name))
            continue
        if name in allowed:
            errors.append("%s: %s is listed twice" % (label, name))
            continue
        allowed[name] = reason
    return (allowed, errors) if not errors else ({}, errors)


# ── pure checks ──────────────────────────────────────────────────────────────


def awx_state(helper: tuple[int, str], web: tuple[int, str]) -> str:
    """'asleep' only when the helper exists and awx-web wants 0 replicas.

    Returns 'awake' when the reads show it is not asleep on purpose, and
    'unknown' when a read failed or gave an answer that cannot be read.
    """
    helper_rc, helper_out = helper
    if helper_rc != 0:
        return "unknown"
    if not helper_out.strip():
        return "awake"
    web_rc, web_out = web
    if web_rc != 0 or not web_out.strip().isdigit():
        return "unknown"
    return "asleep" if int(web_out.strip()) == 0 else "awake"


def _active_targets(reply: Any) -> list[dict[str, Any]] | None:
    if not isinstance(reply, dict) or reply.get("status") != "success":
        return None
    data = reply.get("data")
    targets = data.get("activeTargets") if isinstance(data, dict) else None
    if not isinstance(targets, list) or not all(isinstance(t, dict) for t in targets):
        return None
    return targets


def expected_jobs(reply: Any) -> tuple[dict[str, Any], list[str]]:
    """Read the loaded scrape configuration.

    Returns ({job: count | "nodes" | DISCOVERED}, findings).
    """
    config = None
    data = reply.get("data") if isinstance(reply, dict) and reply.get("status") == "success" else None
    text = data.get("yaml") if isinstance(data, dict) else None
    if isinstance(text, str):
        try:
            import yaml  # present with cloud-init; a missing module is reported below

            config = yaml.safe_load(text)
        except Exception:  # noqa: BLE001 - unreadable config is one incomplete line
            config = None
    jobs = config.get("scrape_configs") if isinstance(config, dict) else None
    if not isinstance(jobs, list) or not jobs or not all(isinstance(j, dict) and isinstance(j.get("job_name"), str) for j in jobs):
        return {}, ["incomplete: the loaded scrape configuration could not be read, so expected jobs are unknown"]
    expected: dict[str, Any] = {}
    findings: list[str] = []
    for job in jobs:
        name = job["job_name"]
        static = job.get("static_configs")
        sd = [v for k, v in job.items() if k.endswith("_sd_configs") and k != "static_configs"]
        if isinstance(static, list) and static:
            addresses = [a for g in static if isinstance(g, dict) for a in (g.get("targets") or [])]
            if addresses:
                expected[name] = len(addresses)
        elif name == APISERVER_JOB:
            expected[name] = 1
        elif any(isinstance(c, dict) and c.get("role") == "node" for v in sd if isinstance(v, list) for c in v):
            expected[name] = "nodes"
        elif sd:
            # Discovery decides the targets, so the loaded config cannot say how many.
            # Zero discovered targets is reported rather than accepted.
            expected[name] = DISCOVERED
        else:
            findings.append("incomplete: expected targets of job %s cannot be established from the loaded configuration" % name)
    for name in CORE_JOBS:
        if not any(j["job_name"] == name for j in jobs):
            findings.append("targets: job %s is missing from the loaded scrape configuration" % name)
    return expected, findings


def check_targets(reply: Any, node_count: int | None, awx: str, expected: dict[str, Any]) -> list[str]:
    targets = _active_targets(reply)
    if targets is None:
        return ["incomplete: Prometheus targets reply is malformed or not successful"]
    if not targets:
        return ["incomplete: Prometheus reports no active targets"]
    findings: list[str] = []
    per_job: dict[str, int] = {}
    for target in targets:
        labels = target.get("labels") if isinstance(target.get("labels"), dict) else {}
        job = str(labels.get("job", "?"))
        per_job[job] = per_job.get(job, 0) + 1
        health = target.get("health")
        if health == "up":
            continue
        if awx == "asleep" and all(labels.get(key) == value for key, value in AWX_LANE.items()):
            continue
        where = "%s %s" % (job, labels.get("instance", "?"))
        if health == "unknown":
            findings.append("targets: %s was not scraped yet (still unknown after the wait)" % where)
        else:
            error = str(target.get("lastError") or "no error text").strip()
            findings.append("targets: %s is %s: %s" % (where, health, error))
    for job, want in expected.items():
        if want == DISCOVERED:
            if not per_job.get(job):
                findings.append("incomplete: %s: coverage not established (no targets discovered)" % job)
            continue
        if want == "nodes":
            if node_count is None:
                findings.append("incomplete: node count unknown, so targets of %s cannot be checked" % job)
                continue
            want = node_count
        have = per_job.get(job, 0)
        if have < want:
            findings.append("targets: expected %d target(s) for job %s, found %d" % (want, job, have))
    return findings


def check_alerts(reply: Any, allowed: dict[str, str]) -> tuple[list[str], int]:
    """Return (findings, pending count)."""
    data = reply.get("data") if isinstance(reply, dict) else None
    alerts = data.get("alerts") if isinstance(data, dict) else None
    if not isinstance(reply, dict) or reply.get("status") != "success" or not isinstance(alerts, list):
        return ["incomplete: Prometheus alerts reply is malformed or not successful"], 0
    firing: dict[str, int] = {}
    pending = 0
    for alert in alerts:
        labels = alert.get("labels") if isinstance(alert, dict) else None
        name = labels.get("alertname") if isinstance(labels, dict) else None
        if not isinstance(name, str):
            return ["incomplete: Prometheus alerts reply holds an alert without a name"], 0
        state = alert.get("state")
        if state == "pending":
            pending += 1
        elif state == "firing" and name not in allowed:
            firing[name] = firing.get(name, 0) + 1
    return ["alerts: %s is firing (%d)" % (name, count) for name, count in sorted(firing.items())], pending


def check_grafana(status: int, body: str) -> list[str]:
    if status != 200:
        return ["grafana: /api/health answered HTTP %d" % status]
    try:
        reply = json.loads(body)
    except ValueError:
        return ["grafana: /api/health reply is not JSON"]
    if not isinstance(reply, dict) or reply.get("database") != "ok":
        return ["grafana: /api/health does not report database ok"]
    return []


def check_console(result: Any, awx: str) -> list[str]:
    if not isinstance(result, dict):
        return ["incomplete: console probe printed no result"]
    if "error" in result:
        return ["console: probe could not run: %s" % result["error"]]
    routes = result.get("routes")
    if not isinstance(routes, dict):
        return ["incomplete: console probe result has no routes"]
    findings: list[str] = []
    for route in CONSOLE_ROUTES:
        reply = routes.get(route)
        if not isinstance(reply, dict):
            findings.append("console: %s was not read" % route)
        elif "status" not in reply or reply.get("status") != 200:
            findings.append("console: %s answered HTTP %s (%s)" % (route, reply.get("status"), reply.get("error", "no session accepted or route error")))
        elif "error" in reply:
            findings.append("console: %s: %s" % (route, reply["error"]))
        else:
            body = reply.get("body")
            reason = body.get("reason") if isinstance(body, dict) else None
            if not isinstance(reason, str):
                findings.append("console: %s reply has no reason field" % route)
            elif reason == "awx-not-running" and awx == "asleep":
                continue
            elif reason:
                findings.append("console: %s reports %s" % (route, reason))
    return findings


def render(findings: list[str], pending: int = 0) -> str:
    if not findings:
        extra = "; %d alert(s) still pending" % pending if pending else ""
        return (
            "Sandbox self-check: clean (targets, alerts, console reads, Grafana health). "
            "No console route reads Grafana, so Grafana was checked through its own /api/health. "
            "Alerts are read right after install, so a rule still inside its for: window "
            "(TargetDown needs 10 minutes) is not covered%s." % extra
        )
    lines = ["=" * 72, "SANDBOX SELF-CHECK: %d finding(s). The verify step still passes." % len(findings), "=" * 72]
    lines += ["  - " + finding for finding in findings]
    lines += [
        "Grafana is checked through its own /api/health: no console route reads Grafana.",
        "A clean summary covers only what was read; lines starting with incomplete: were not.",
        "=" * 72,
    ]
    return "\n".join(lines)


# ── collection ───────────────────────────────────────────────────────────────


def get_json(io: Io, url: str) -> tuple[Any, str | None]:
    try:
        status, body = io.http_get(url)
    except Exception as exc:  # noqa: BLE001 - any failure is reported, never raised
        return None, "request failed (%s)" % type(exc).__name__
    if status != 200:
        return None, "HTTP %d" % status
    try:
        return json.loads(body), None
    except ValueError:
        return None, "reply is not JSON"


def cluster_ip(io: Io, namespace: str, service: str) -> str | None:
    rc, out = io.kubectl(["-n", namespace, "get", "svc", service, "-o", "jsonpath={.spec.clusterIP}"])
    ip = out.strip()
    return ip if rc == 0 and ip and ip != "None" else None


def settle_targets(io: Io, url: str, tries: int, delay: float) -> tuple[Any, str | None]:
    """Poll until no target is unknown. Returns the last reply, so unknowns that
    remain after the last try are reported rather than waited on forever."""
    reply, error = None, None
    for attempt in range(max(tries, 1)):
        reply, error = get_json(io, url)
        targets = _active_targets(reply) if error is None else None
        if targets and not any(t.get("health") == "unknown" for t in targets):
            break
        if attempt + 1 < max(tries, 1):
            io.sleep(delay)
    return reply, error


def run_checks(params: dict[str, Any], io: Io) -> str:
    findings: list[str] = []
    pending = 0

    def stage(name: str, body: Callable[[], None]) -> None:
        try:
            body()
        except Exception as exc:  # noqa: BLE001 - a broken stage becomes a finding
            findings.append("incomplete: %s stage failed (%s)" % (name, type(exc).__name__))

    allowed: dict[str, str] = {}
    awx = "unknown"
    node_count: int | None = None

    def allowlist() -> None:
        nonlocal allowed
        if params.get("allowlist_error"):
            findings.append("incomplete: allowlist could not be read, so no alert is allowed")
            return
        allowed, errors = load_allowlist(params.get("allowlist"))
        findings.extend("incomplete: allowlist invalid, so no alert is allowed: %s" % e for e in errors)

    def facts() -> None:
        nonlocal awx, node_count
        awx_ns = params["awx_namespace"]
        helper = io.kubectl(["-n", awx_ns, "get", "deploy", "awx-autoscale", "--ignore-not-found", "-o", "name"])
        web = io.kubectl(["-n", awx_ns, "get", "deploy", "awx-web", "--ignore-not-found", "-o", "jsonpath={.spec.replicas}"])
        awx = awx_state(helper, web)
        if awx == "unknown":
            findings.append("incomplete: AWX sleep state could not be read, so AWX results are not discounted")
        rc, out = io.kubectl(["get", "nodes", "-o", "name"])
        if rc == 0 and out.strip():
            node_count = len(out.split())

    prom_base = ""

    def prometheus_address() -> None:
        nonlocal prom_base
        ip = cluster_ip(io, params["monitoring_namespace"], "prometheus-server")
        if ip is None:
            findings.append("incomplete: Prometheus service address could not be read")
            return
        prom_base = "http://%s:%s%s" % (ip, params.get("service_port", SERVICE_PORT_DEFAULT), params["prometheus_path"])

    def targets() -> None:
        if not prom_base:
            return
        config, config_error = get_json(io, prom_base + "/api/v1/status/config")
        if config_error:
            expected, expected_findings = {}, ["incomplete: Prometheus configuration read failed: %s" % config_error]
        else:
            expected, expected_findings = expected_jobs(config)
        findings.extend(expected_findings)
        url = prom_base + "/api/v1/targets?state=active"
        reply, error = settle_targets(io, url, params["settle_tries"], params["settle_delay"])
        if error:
            findings.append("incomplete: Prometheus targets read failed: %s" % error)
        else:
            findings.extend(check_targets(reply, node_count, awx, expected))

    def alerts() -> None:
        nonlocal pending
        if not prom_base:
            return
        reply, error = get_json(io, prom_base + "/api/v1/alerts")
        if error:
            findings.append("incomplete: Prometheus alerts read failed: %s" % error)
        else:
            alert_findings, pending = check_alerts(reply, allowed)
            findings.extend(alert_findings)

    def grafana() -> None:
        ip = cluster_ip(io, params["monitoring_namespace"], "grafana")
        if ip is None:
            findings.append("incomplete: Grafana service address could not be read")
            return
        try:
            status, body = io.http_get("http://%s:%s%s/api/health" % (ip, params.get("service_port", SERVICE_PORT_DEFAULT), params["grafana_prefix"]))
        except Exception as exc:  # noqa: BLE001
            findings.append("grafana: /api/health is unavailable (%s)" % type(exc).__name__)
            return
        findings.extend(check_grafana(status, body))

    def console() -> None:
        rc, out = io.kubectl(
            ["-n", params["cms_namespace"], "exec", "-i", "deploy/dmf-cms", "--", "python", "-I", "-", params["cms_port"]],
            stdin=params["probe_source"],
            timeout=PROBE_TIMEOUT_SECONDS,
        )
        if rc != 0:
            findings.append("console: probe failed to run in the console pod (kubectl exit %d)" % rc)
            return
        try:
            result = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            findings.append("incomplete: console probe printed no JSON")
            return
        findings.extend(check_console(result, awx))

    stage("allowlist", allowlist)
    stage("cluster facts", facts)
    stage("Prometheus address", prometheus_address)
    stage("targets", targets)
    stage("alerts", alerts)
    stage("Grafana", grafana)
    stage("console", console)
    return render(findings, pending)


def main(argv: list[str]) -> int:
    try:
        params = json.loads(argv[1])
        print(run_checks(params, Io(params["kubeconfig"])))
    except Exception as exc:  # noqa: BLE001 - report-only: never a failing exit
        print("Sandbox self-check: INCOMPLETE, the helper stopped early (%s). Nothing was checked." % type(exc).__name__)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
