"""Run the real AWX consumers through Ansible for the #1027 tests.

The NetBox seed, the Prometheus rules and the Grafana dashboard all take the AWX
identity from `common/awx-identity`, which each of their roles lists as a
dependency. This helper takes the role list from the real playbooks
(`694-born-inventory.yml`, `100-prometheus.yml`, `120-grafana.yml`) and runs it
with `ansible-playbook`, so role dependencies, variable precedence and the
identity checks are Ansible's own. Inventory values reach the roles the way an
environment's inventory does.

`--tags awx_identity` runs only the dependency's tasks (the checks). The roles'
own tasks need a cluster, so a small post task renders what each consumer would
use: the seed's coordinates for AWX, the rules in the Prometheus Helm values, and
the dashboard ConfigMap. The tests need `ansible-playbook` on the PATH."""
import json
import pathlib
import subprocess

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[1]

CONSUMERS = {
    "seed": ("playbooks/694-born-inventory.yml", "common/dmf-born-inventory"),
    "alert": ("playbooks/vertical-monitoring/100-prometheus.yml", "base/prometheus"),
    "dashboard": ("playbooks/vertical-monitoring/120-grafana.yml", "base/grafana"),
}


def real_roles(consumer):
    """The roles of the real play that runs the consumer's role."""
    playbook, role = CONSUMERS[consumer]
    for play in yaml.safe_load((ROOT / playbook).read_text()):
        if role in play.get("roles", []):
            return play["roles"]
    raise AssertionError(f"{playbook} has no play with {role}")


def _render_task(consumer, workdir):
    role_dir = ROOT / "roles" / CONSUMERS[consumer][1]
    out = workdir / f"{consumer}.out"
    if consumer == "seed":
        # The stamp task in dmf-born-inventory/tasks/app-service.yml reads
        # dmf_born_inventory_app_cluster_coords[<app key>] for the app.
        return out, {"ansible.builtin.copy": {
            "dest": str(out),
            "content": "{{ dmf_born_inventory_app_cluster_coords['awx'] | to_json }}"}}
    src = {"alert": "values.yml.j2", "dashboard": "configmap-dashboards.yml.j2"}[consumer]
    return out, {"ansible.builtin.template": {"src": str(role_dir / "templates" / src),
                                              "dest": str(out)},
                 "vars": {"role_path": str(role_dir)}}


def run(consumer, workdir, inventory_vars=None, *, render=True):
    """Run the consumer's real roles; return (process, rendered output or None)."""
    workdir = pathlib.Path(workdir)
    inventory = workdir / "inventory.yml"
    inventory.write_text(yaml.safe_dump({"all": {
        "hosts": {"localhost": {"ansible_connection": "local"}},
        "vars": inventory_vars or {}}}))
    out, task = _render_task(consumer, workdir)
    play = {"hosts": "localhost", "gather_facts": False, "roles": real_roles(consumer),
            "post_tasks": [{"name": f"Render what the {consumer} uses", "tags": ["always"], **task}]
            if render else []}
    playbook = workdir / f"{consumer}.play.yml"
    playbook.write_text(yaml.safe_dump([play]))
    result = subprocess.run(
        ["ansible-playbook", "-i", str(inventory), str(playbook), "--tags", "awx_identity"],
        cwd=ROOT, capture_output=True, text=True,
        env={**__import__("os").environ, "ANSIBLE_CONFIG": str(ROOT / "ansible.cfg"),
             "ANSIBLE_NOCOLOR": "1"})
    return result, (out.read_text() if render and out.exists() else None)


def seed_instance(rendered):
    """The probe target promsd composes from the three seeded fields."""
    c = json.loads(rendered)
    return f"{c['service']}.{c['namespace']}.svc.cluster.local:{c['port']}"


def alert_rules(rendered):
    """The rule groups inside the Prometheus Helm values."""
    return yaml.safe_load(rendered)["serverFiles"]["alerting_rules.yml"]
