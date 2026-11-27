"""The Nodes dashboard selects node exporter targets by the `node` label.

`node_uname_info` carries two names for a machine. `nodename` is the OS
hostname, which can differ from the Kubernetes node name. `node` is the
Kubernetes node name, set by the `kubernetes-service-endpoints` scrape jobs.
The dashboard's `$node` variable lists Kubernetes node names, so every query
that filters on a node name must use `node`, or the `instance` variable stays
empty and the node panels show "No data".
"""

import json
import re
import unittest
from pathlib import Path

import yaml
from jinja2 import Environment, FileSystemLoader

ROLES = Path(__file__).resolve().parents[1] / "roles" / "base"
DASHBOARD = ROLES / "grafana" / "files" / "k8s-views-nodes.json"
TEMPLATES = ROLES / "prometheus" / "templates"
ENDPOINT_JOBS = ("kubernetes-service-endpoints", "kubernetes-service-endpoints-slow")


def load_dashboard():
    return json.loads(DASHBOARD.read_text())


def walk_panels(panels):
    for panel in panels:
        yield panel
        yield from walk_panels(panel.get("panels", []))


def variable(name):
    return next(v for v in load_dashboard()["templating"]["list"] if v["name"] == name)


EXACT_QUERY = 'label_values(node_uname_info{node="$node"}, instance)'


def selector_matchers(query, node):
    """Return {label: (op, value)} from the query's selector, with $node substituted."""
    body = re.search(r"\{(.*)\}", query).group(1).replace("$node", node)
    return {m[0]: (m[1], m[2]) for m in re.findall(r'(\w+)(=~|=)"([^"]*)"', body)}


def select_instances(series, matchers):
    """Select series the way Prometheus does: `=` is exact, `=~` is a full regex."""
    ops = {"=": lambda value, want: value == want, "=~": lambda value, want: re.fullmatch(want, value)}
    return [
        s["instance"]
        for s in series
        if all(ops[op](s[label], want) for label, (op, want) in matchers.items())
    ]


class NodesDashboardLabelTests(unittest.TestCase):
    def test_instance_variable_selects_by_the_node_label(self):
        var = variable("instance")
        for key in ("query", "definition"):
            text = var["query"]["query"] if key == "query" else var[key]
            with self.subTest(field=key):
                self.assertEqual(text, EXACT_QUERY)
                self.assertIn('node="$node"', text)
                self.assertNotIn("=~", text)
                self.assertNotIn("nodename", text)

    def test_node_variable_stays_single_valued(self):
        var = variable("node")
        self.assertFalse(var.get("multi"))
        self.assertFalse(var.get("includeAll"))

    def test_dotted_node_name_selects_only_its_own_instance(self):
        # A dot is a regex wildcard, so `node=~"worker.1"` also matches
        # `worker-1`. The exact matcher must select one node only.
        for label, hostnames in (
            ("hostnames differ from node names", {"worker.1": "host-a", "worker-1": "host-b"}),
            ("hostnames match node names", {"worker.1": "worker.1", "worker-1": "worker-1"}),
        ):
            series = [
                {"node": node, "nodename": host, "instance": f"{node}-exporter:9100"}
                for node, host in hostnames.items()
            ]
            with self.subTest(case=label):
                matchers = selector_matchers(variable("instance")["definition"], "worker.1")
                self.assertEqual(select_instances(series, matchers), ["worker.1-exporter:9100"])
                regex_form = {"node": ("=~", "worker.1")}
                self.assertEqual(
                    select_instances(series, regex_form),
                    ["worker.1-exporter:9100", "worker-1-exporter:9100"],
                )

    def test_no_query_filters_on_the_os_hostname(self):
        queries = [t.get("expr", "") for p in walk_panels(load_dashboard()["panels"]) for t in p.get("targets", [])]
        queries += [str(v.get("query")) for v in load_dashboard()["templating"]["list"]]
        self.assertEqual([q for q in queries if re.search(r"\bnodename\s*(=|!=|=~|!~)", q)], [])

    def test_scrape_config_sets_the_node_label_on_node_exporter_targets(self):
        env = Environment(loader=FileSystemLoader(str(TEMPLATES)), keep_trailing_newline=True)
        env.filters["to_json"] = json.dumps  # Ansible provides this filter
        text = env.get_template("scrape_configs.yml.j2").render(
            prometheus_path="/prometheus",
            prometheus_static_probe_targets=[],
            prometheus_static_probe_interval="60s",
        )
        jobs = {job["job_name"]: job for job in yaml.safe_load(text)}
        for name in ENDPOINT_JOBS:
            with self.subTest(job=name):
                self.assertIn(
                    {"action": "replace", "source_labels": ["__meta_kubernetes_pod_node_name"], "target_label": "node"},
                    jobs[name]["relabel_configs"],
                )


if __name__ == "__main__":
    unittest.main()
