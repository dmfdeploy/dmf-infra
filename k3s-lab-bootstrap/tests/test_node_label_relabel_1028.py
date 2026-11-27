"""The kubelet and cAdvisor series carry a `node` label equal to the node name
(dmfdeploy/dmfdeploy#1028).

The Nodes dashboard selects on `node="$node"`, and its variable lists node names
from `kube_node_info`. The scrape jobs only copied Kubernetes node labels, so
the kubelet series had `kubernetes_io_hostname` and no `node`.

The test renders the real scrape template and runs the real `prometheus`
binary against it. It keeps each node job's `relabel_configs` exactly as
rendered and changes only what cannot run offline: `kubernetes_sd_configs`
becomes `static_configs` that set the same `__meta_kubernetes_*` labels the
discovery would, and the TLS and token settings go away so a local HTTP server
can answer. The relabel step is therefore Prometheus's own. What the test does
not cover is Kubernetes service discovery producing those meta labels; that
mapping is Prometheus documented behavior (`role: node` sets
`__meta_kubernetes_node_name` to the node object's name).

Two targets differ on purpose. On one, the `kubernetes.io/hostname` label
equals the node name. On the other, it differs, as on the sandbox node, where
the OS hostname is not the node name. The test needs `prometheus` on the PATH."""
import copy
import json
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import yaml

from test_prometheus_scrape_configs import NODE_JOBS, render_scrape_jobs

# (node name, kubernetes.io/hostname label, address host)
TARGETS = (
    ("node-same", "node-same", "127.0.0.1"),
    ("node-differs", "iZgw8differs", "localhost"),
)
# The kubelet exposes `kubelet_node_name{node=...}` itself, so this series
# shows how a scraped `node` label meets the relabeled one.
BODY = """\
# TYPE machine_memory_bytes gauge
machine_memory_bytes 1e9
# TYPE container_memory_working_set_bytes gauge
container_memory_working_set_bytes{image="img",pod="p"} 5
# TYPE kubelet_volume_stats_used_bytes gauge
kubelet_volume_stats_used_bytes{namespace="n",persistentvolumeclaim="c"} 9
# TYPE kubelet_node_name gauge
kubelet_node_name{node="scraped-name"} 1
"""


class Metrics(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(BODY.encode())

    def log_message(self, *args):
        pass


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def offline_config(port):
    """The rendered node jobs, with discovery replaced by static targets."""
    jobs = render_scrape_jobs()
    out = []
    for name in NODE_JOBS:
        job = copy.deepcopy(jobs[name])
        for key in ("kubernetes_sd_configs", "bearer_token_file", "tls_config"):
            job.pop(key, None)
        job["scheme"] = "http"
        job["scrape_interval"] = "1s"
        job["static_configs"] = [
            {
                "targets": [f"{host}:{port}"],
                "labels": {
                    "__meta_kubernetes_node_name": node,
                    "__meta_kubernetes_node_label_kubernetes_io_hostname": hostname,
                },
            }
            for node, hostname, host in TARGETS
        ]
        out.append(job)
    return {"global": {"scrape_interval": "1s"}, "scrape_configs": out}


def query(web, expr):
    url = f"http://{web}/api/v1/query?" + urllib.parse.urlencode({"query": expr})
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.load(resp)["data"]["result"]


class NodeLabelRelabelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        binary = shutil.which("prometheus")
        if not binary:
            raise RuntimeError("prometheus is not on the PATH; this test does not skip")
        cls.tmp = tempfile.TemporaryDirectory()
        tmp = Path(cls.tmp.name)
        server = HTTPServer(("127.0.0.1", 0), Metrics)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        cls.server = server
        cfg = tmp / "prometheus.yml"
        cfg.write_text(yaml.safe_dump(offline_config(server.server_port)))
        web_port = free_port()
        cls.web = f"127.0.0.1:{web_port}"
        cls.log = open(tmp / "prometheus.log", "w")
        cls.proc = subprocess.Popen(
            [binary, f"--config.file={cfg}", f"--storage.tsdb.path={tmp / 'data'}",
             f"--web.listen-address={cls.web}"],
            stdout=cls.log, stderr=subprocess.STDOUT)
        deadline = time.time() + 60
        while time.time() < deadline:
            if cls.proc.poll() is not None:
                raise RuntimeError("prometheus exited:\n" + (tmp / "prometheus.log").read_text())
            try:
                if len(query(cls.web, "up == 1")) == len(NODE_JOBS) * len(TARGETS):
                    return
            except OSError:
                pass
            time.sleep(0.5)
        raise RuntimeError("targets never came up:\n" + (tmp / "prometheus.log").read_text())

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait(timeout=15)
        cls.log.close()
        cls.server.shutdown()
        cls.tmp.cleanup()

    def series(self, job, metric):
        return {
            r["metric"].get("kubernetes_io_hostname"): r["metric"]
            for r in query(self.web, f'{metric}{{job="{job}"}}')
        }

    def test_node_label_is_the_node_name_whether_or_not_the_hostname_matches(self):
        cases = (
            ("kubernetes-nodes", "kubelet_volume_stats_used_bytes"),
            ("kubernetes-nodes-cadvisor", "machine_memory_bytes"),
            ("kubernetes-nodes-cadvisor", "container_memory_working_set_bytes"),
        )
        for job, metric in cases:
            found = self.series(job, metric)
            for node, hostname, _ in TARGETS:
                with self.subTest(job=job, metric=metric, node=node):
                    self.assertIn(hostname, found)
                    self.assertEqual(found[hostname].get("node"), node)

    def test_a_scraped_node_label_is_kept_as_exported_node(self):
        got = self.series("kubernetes-nodes", "kubelet_node_name")
        for node, hostname, _ in TARGETS:
            with self.subTest(node=node):
                self.assertEqual(got[hostname].get("node"), node)
                self.assertEqual(got[hostname].get("exported_node"), "scraped-name")


if __name__ == "__main__":
    unittest.main()
