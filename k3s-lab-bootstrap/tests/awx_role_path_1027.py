"""Run the real AWX consumers through Ansible for the #1027 tests.

The NetBox seed, the Prometheus rules, the Grafana dashboard and the AWX deployer
all take the AWX identity (`awx_namespace`, `awx_instance_name`) from
`common/awx-identity`, which each of their roles lists as a dependency. This
helper takes the role list from the real playbooks (`694-born-inventory.yml`,
`100-prometheus.yml`, `120-grafana.yml`, `640-awx.yml`) and runs it with
`ansible-playbook`, so role dependencies, filter plugin discovery, variable
precedence and the identity checks are Ansible's own. Inventory values reach the
roles the way an environment's inventory does, and extra vars the way `-e` does.

`--tags awx_identity` runs the identity role's tasks and each consumer's first
task. The consumers' other tasks need a cluster, so a post task runs the real
template or task that uses the identity:

- seed: the real `tasks/app-service.yml`, against a stub NetBox that records the
  requests it receives;
- alert: the Prometheus Helm values template, which includes the alert rules;
- dashboard: the Grafana dashboards ConfigMap template;
- deployer: the AWX custom resource template, and the service name and port
  expressions of the two IngressRoute definitions in `tasks/main.yml`.

The case list for these tests is `docs/awx-probe-lane-cases.md`. The tests need
`ansible-playbook` on the PATH."""
import json
import os
import pathlib
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, HTTPServer

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]

CONSUMERS = {
    "seed": ("playbooks/694-born-inventory.yml", "common/dmf-born-inventory"),
    "alert": ("playbooks/vertical-monitoring/100-prometheus.yml", "base/prometheus"),
    "dashboard": ("playbooks/vertical-monitoring/120-grafana.yml", "base/grafana"),
    "deployer": ("playbooks/640-awx.yml", "stack/operator/awx"),
}

IDENTITY_ROLE = "common/awx-identity"


def real_roles(consumer):
    """The roles of the real play that runs the consumer's role."""
    playbook, role = CONSUMERS[consumer]
    for play in yaml.safe_load((ROOT / playbook).read_text()):
        if role in play.get("roles", []):
            return play["roles"]
    raise AssertionError(f"{playbook} has no play with {role}")


# ── Stub NetBox ───────────────────────────────────────────────────────────
class NetBoxStub:
    """The NetBox API as the seed's service task uses it, recording every request.

    The tag lookups answer with an id. The service lookup answers with
    `lookup_results`. The create answers 201 with `create_body`, which is what
    the real create task expects. Any PATCH answers 200."""

    def __init__(self, lookup_results=None, create_body=None):
        self.lookup_results = [{"id": 7}] if lookup_results is None else lookup_results
        self.create_body = {"id": 9} if create_body is None else create_body
        self.requests = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _reply(self, status, body):
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _record(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                stub.requests.append({"method": self.command, "path": self.path,
                                      "body": json.loads(raw) if raw else None})

            def do_GET(self):
                self._record()
                if self.path.startswith("/api/extras/tags/"):
                    self._reply(200, {"results": [{"id": 3}]})
                elif self.path.startswith("/api/ipam/services/"):
                    self._reply(200, {"results": stub.lookup_results})
                else:
                    self._reply(404, {})

            def do_POST(self):
                self._record()
                self._reply(201, stub.create_body)

            def do_PATCH(self):
                self._record()
                self._reply(200, {})

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def api_base(self):
        return f"http://127.0.0.1:{self.server.server_address[1]}/api"

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()

    def stamps(self):
        """The PATCH requests that carry the cluster coordinates."""
        return [r for r in self.requests if r["method"] == "PATCH"
                and "cluster_service" in ((r["body"] or {}).get("custom_fields") or {})]


# ── The post task each consumer runs ──────────────────────────────────────
def _ingress_route_exprs():
    """The service name and port expressions of the two IngressRoute definitions."""
    tasks = yaml.safe_load((ROOT / "roles/stack/operator/awx/tasks/main.yml").read_text())
    found = {}
    for task in tasks:
        definition = (task.get("kubernetes.core.k8s") or {}).get("definition") or {}
        if definition.get("kind") == "IngressRoute":
            service = definition["spec"]["routes"][0]["services"][0]
            lane = "private" if "dmf.exposure" in definition["metadata"].get("annotations", {}) else "public"
            found[lane] = {"name": service["name"], "port": service["port"]}
    return found


def _post_task(consumer, role_dir, out, app):
    if consumer == "seed":
        entry = {"key": app, "display_name": app.title(), "exposure": "private",
                 "oidc_client_id": ""}
        return {"name": "Stamp the service with the real app-service.yml",
                "ansible.builtin.include_tasks": {
                    "file": str(role_dir / "tasks/app-service.yml"),
                    "apply": {"tags": ["always"]}},
                "loop": [entry], "loop_control": {"loop_var": "dmf_app_entry"},
                "vars": {"dmf_app_service_key": "{{ dmf_app_entry.key }}",
                         "dmf_app_service_display": "{{ dmf_app_entry.display_name }}",
                         "dmf_app_service_exposure": "{{ dmf_app_entry.exposure }}",
                         "dmf_app_service_oidc_client": "",
                         "dmf_app_service_hostname": ""}}
    if consumer == "deployer":
        routes = _ingress_route_exprs()
        return {"name": "Render the AWX custom resource and the IngressRoute services",
                "ansible.builtin.copy": {"dest": str(out), "content": "{{ _doc | to_json }}"},
                "vars": {
                    **{f"route_{lane}_{k}": v for lane, route in routes.items()
                       for k, v in route.items()},
                    "_doc": {
                        "custom_resource": "{{ lookup('ansible.builtin.template', '"
                                           + str(role_dir / "templates/awx-instance.yml.j2") + "') }}",
                        "ingress": {lane: {"name": "{{ route_" + lane + "_name }}",
                                           "port": "{{ route_" + lane + "_port }}"}
                                    for lane in routes}}}}
    src = {"alert": "values.yml.j2", "dashboard": "configmap-dashboards.yml.j2"}[consumer]
    return {"name": f"Render what the {consumer} uses",
            "ansible.builtin.template": {"src": str(role_dir / "templates" / src), "dest": str(out)},
            "vars": {"role_path": str(role_dir)}}


class Run:
    """The outcome of one consumer run."""

    def __init__(self, result, rendered, requests, consumer):
        self.result = result
        self.rendered = rendered
        self.requests = requests
        self.consumer = consumer

    @property
    def returncode(self):
        return self.result.returncode

    @property
    def output(self):
        return self.result.stdout + self.result.stderr


def _scratch_roles(workdir, consumer):
    """A copy of the roles the play needs, with the identity dependency removed."""
    roles = workdir / "roles"
    for name in ("base/cluster-ready", CONSUMERS[consumer][1]):
        shutil.copytree(ROOT / "roles" / name, roles / name,
                        ignore=shutil.ignore_patterns("tests", "__pycache__"))
    meta = roles / CONSUMERS[consumer][1] / "meta/main.yml"
    meta.write_text("---\ndependencies: []\n")
    return roles


def run(consumer, workdir, inventory_vars=None, *, extra_vars=None, render=True,
        drop_dependency=False, stub=None, app="awx", roles=None, env_extra=None):
    """Run the consumer's real roles; return a Run.

    `inventory_vars` go in the inventory's `all` group, `extra_vars` on the
    command line. `drop_dependency` runs the role from a scratch copy of the roles
    whose `meta/main.yml` lists no dependency. `roles` replaces the play's role list."""
    workdir = pathlib.Path(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "inventory.yml").write_text(yaml.safe_dump({"all": {
        "hosts": {"localhost": {"ansible_connection": "local"}},
        "vars": inventory_vars or {}}}))
    roles_root = _scratch_roles(workdir, consumer) if drop_dependency else ROOT / "roles"
    role_dir = roles_root / CONSUMERS[consumer][1]
    out = workdir / f"{consumer}.out"
    play = {"hosts": "localhost", "gather_facts": False,
            "roles": roles or real_roles(consumer)}
    if render:
        play["post_tasks"] = [{"tags": ["always"], **_post_task(consumer, role_dir, out, app)}]
    if consumer == "seed":
        stub = stub or NetBoxStub()
        play["vars"] = {"dmf_born_inventory_netbox_api_base": stub.api_base,
                        "dmf_born_inventory_netbox_headers": {},
                        "dmf_born_inventory_netbox_validate_certs": False,
                        "dmf_born_inventory_tag_id": 1,
                        "dmf_born_inventory_load_balancer_id": 5,
                        "dmf_born_inventory_env_name": "test"}
    if consumer == "deployer":
        # Values the deployer's earlier tasks compute or an inventory supplies,
        # which the custom resource template reads. None is an identity value.
        play["vars"] = {"awx_saml_idp_certificate_effective": "test-certificate",
                        "awx_external_base_url": "https://awx.example.test",
                        "awx_path": "/awx"}
    playbook = workdir / f"{consumer}.play.yml"
    playbook.write_text(yaml.safe_dump([play]))
    command = ["ansible-playbook", "-i", str(workdir / "inventory.yml"), str(playbook),
               "--tags", "awx_identity"]
    if extra_vars:
        (workdir / "extra.json").write_text(json.dumps(extra_vars))
        command += ["-e", f"@{workdir / 'extra.json'}"]
    env = {**os.environ, "ANSIBLE_CONFIG": str(ROOT / "ansible.cfg"), "ANSIBLE_NOCOLOR": "1",
           "NO_PROXY": "127.0.0.1", "no_proxy": "127.0.0.1"}
    env.pop("ANSIBLE_FILTER_PLUGINS", None)
    env.update(env_extra or {})
    if drop_dependency:
        env["ANSIBLE_ROLES_PATH"] = str(roles_root)
    if stub:
        with stub:
            result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, env=env)
    else:
        result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, env=env)
    rendered = out.read_text() if render and out.exists() else None
    return Run(result, rendered, stub.requests if stub else [], consumer)


def run_all(consumers, workdir, inventory_vars=None, **kwargs):
    """Run several consumers at once; return {consumer: Run}."""
    workdir = pathlib.Path(workdir)
    with ThreadPoolExecutor(max_workers=len(consumers)) as pool:
        futures = {c: pool.submit(run, c, workdir / c, inventory_vars, **kwargs)
                   for c in consumers}
    return {c: f.result() for c, f in futures.items()}


# ── Reading what each consumer produced ───────────────────────────────────
def alert_rules(rendered):
    """The rule groups inside the Prometheus Helm values."""
    return yaml.safe_load(rendered)["serverFiles"]["alerting_rules.yml"]


def alert_expr(rules, alert):
    return next(r["expr"] for g in rules["groups"] for r in g["rules"]
                if r.get("alert") == alert)


LANE_INSTANCE = re.compile(r"label_replace\(\s*max\(.*?\) == 0,\s*\"instance\", \"([^\"]*)\", \"\", \"\"\s*\)", re.S)
CR_SELECTOR = re.compile(r'awx_cr_(?:web|task)_replicas\{namespace="([^"]*)", name="([^"]*)"\}')


def observed(run_):
    """What one consumer ends up using, as a dict of the identity values it holds.

    Keys: name, namespace, service, port, instance. Each consumer reports the
    keys it holds; a value that several places hold is a set of one."""
    rendered = run_.rendered
    if run_.consumer == "seed":
        (stamp,) = [r for r in run_.requests if r["method"] == "PATCH"
                    and "cluster_service" in ((r["body"] or {}).get("custom_fields") or {})]
        fields = stamp["body"]["custom_fields"]
        return {"service": fields["cluster_service"], "namespace": fields["cluster_namespace"],
                "port": fields["cluster_port"]}
    if run_.consumer == "alert":
        rules = alert_rules(rendered)
        lane = alert_expr(rules, "NetboxProbeLaneDown")
        selectors = set()
        for g in rules["groups"]:
            for r in g["rules"]:
                if r.get("alert") in ("NetboxProbeLaneDown", "AWXReplicaDivergence"):
                    selectors |= set(CR_SELECTOR.findall(r["expr"]))
        return {"namespace": {ns for ns, _ in selectors}, "name": {n for _, n in selectors},
                "instance": LANE_INSTANCE.search(lane).group(1)}
    if run_.consumer == "dashboard":
        text = dashboard_json_text(rendered)
        selectors = set(re.findall(r'awx_cr_web_replicas\{namespace=\\"([^\\"]*)\\", name=\\"([^\\"]*)\\"\}', text))
        instances = set(re.findall(r'\\"instance\\", \\"([^\\"]*)\\", \\"\\", \\"\\"', text))
        return {"namespace": {ns for ns, _ in selectors}, "name": {n for _, n in selectors},
                "instance": instances}
    if run_.consumer == "deployer":
        doc = json.loads(rendered)
        cr = yaml.safe_load(doc["custom_resource"])
        return {"name": cr["metadata"]["name"], "namespace": cr["metadata"]["namespace"],
                "service": {r["name"] for r in doc["ingress"].values()},
                "port": {r["port"] for r in doc["ingress"].values()}}
    raise AssertionError(run_.consumer)


def dashboard_json_text(rendered):
    """The text of the platform-services dashboard inside the ConfigMap."""
    key = "  dmf-platform-services.json: |\n"
    lines = rendered.splitlines(keepends=True)
    body = []
    for line in lines[lines.index(key) + 1:]:
        if not line.startswith("    ") and line.strip():
            break
        body.append(line)
    return "".join(body)
