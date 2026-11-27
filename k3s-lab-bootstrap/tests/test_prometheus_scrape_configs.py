"""The node scrape jobs reach the kubelet directly, not through the API proxy.

Prometheus chart 30 grants its ClusterRole `nodes` and `nodes/metrics` but no
longer `nodes/proxy`. A job that scrapes
`kubernetes.default.svc/api/v1/nodes/<node>/proxy/...` gets 403 from the API
server, and `TargetDown` fires. The kubelet answers the same service account
on its own port, so the jobs scrape the node address that `role: node` yields.
"""

import json
import unittest
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader

TEMPLATES = Path(__file__).resolve().parents[1] / "roles" / "base" / "prometheus" / "templates"
NODE_JOBS = ("kubernetes-nodes", "kubernetes-nodes-cadvisor")


def render_scrape_jobs():
    env = Environment(loader=FileSystemLoader(str(TEMPLATES)), keep_trailing_newline=True)
    env.filters["to_json"] = json.dumps  # Ansible provides this filter
    text = env.get_template("scrape_configs.yml.j2").render(
        prometheus_path="/prometheus",
        prometheus_static_probe_targets=[],
        prometheus_static_probe_interval="60s",
    )
    return {job["job_name"]: job for job in yaml.safe_load(text)}


class NodeScrapeJobTests(unittest.TestCase):
    def test_node_jobs_do_not_go_through_the_api_server_proxy(self):
        jobs = render_scrape_jobs()
        for name in NODE_JOBS:
            with self.subTest(job=name):
                job = jobs[name]
                self.assertNotIn("kubernetes.default.svc", str(job.get("relabel_configs", [])))
                self.assertNotIn("/proxy/", str(job))
                self.assertNotIn("__metrics_path__", str(job.get("relabel_configs", [])))

    def test_node_jobs_use_node_discovery_and_keep_the_discovered_address(self):
        jobs = render_scrape_jobs()
        for name in NODE_JOBS:
            with self.subTest(job=name):
                self.assertEqual(
                    [sd.get("role") for sd in jobs[name]["kubernetes_sd_configs"]], ["node"]
                )
                targets = {r.get("target_label") for r in jobs[name].get("relabel_configs", [])}
                self.assertFalse(targets & {"__address__", "__metrics_path__"}, targets)

    def test_cadvisor_job_sets_the_cadvisor_metrics_path(self):
        self.assertEqual(render_scrape_jobs()["kubernetes-nodes-cadvisor"].get("metrics_path"), "/metrics/cadvisor")

    def test_node_jobs_authenticate_with_the_service_account_token(self):
        jobs = render_scrape_jobs()
        for name in NODE_JOBS:
            with self.subTest(job=name):
                self.assertEqual(
                    jobs[name].get("bearer_token_file"),
                    "/var/run/secrets/kubernetes.io/serviceaccount/token",
                )
                self.assertEqual(jobs[name].get("scheme"), "https")

    def test_node_jobs_verify_the_kubelet_against_the_cluster_ca(self):
        jobs = render_scrape_jobs()
        for name in NODE_JOBS:
            with self.subTest(job=name):
                tls = jobs[name].get("tls_config", {})
                self.assertEqual(tls.get("ca_file"), "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
                self.assertNotIn("insecure_skip_verify", tls)


if __name__ == "__main__":
    unittest.main()
