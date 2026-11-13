"""On a 1:1-NAT node the inventory's k3s_node_ip is the public address, which is
not a local interface. Run on its own, the local-CA play (321) must still put
only the node's internal address into the CoreDNS zone and the node's
/etc/hosts, and the born-inventory survey (694) must record the internal
address as the node's private address and the declared address as its public
address.

The test runs the address tasks of each playbook with ansible-playbook against
a fixture node. No network facts are preloaded: the play's own `setup` task
gathers them. The test swaps `ansible.builtin.setup` for a stand-in module that
takes the same `gather_subset`, returns NAT-shaped facts only when the subset
names the network, and keeps the task's `when`. Static checks also require the
gathering to come before the resolver under the same guards. The resolver and
the task that renders the host entries run as written. The two tasks that write
the zone to the cluster and the entries to /etc/hosts write to files in a
temporary directory instead. Every other task is dropped, because it needs a
cluster or changes the node.
"""
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
PLAYBOOKS = ROOT / "playbooks"
LOCAL_CA = PLAYBOOKS / "321-local-ca-trust.yml"
BORN_INVENTORY = PLAYBOOKS / "694-born-inventory.yml"
BORN_INVENTORY_ROLE = ROOT / "roles" / "common" / "dmf-born-inventory" / "tasks" / "main.yml"

PUBLIC_IP = "203.0.113.9"
INTERNAL_IP = "198.51.100.5"
IPV4 = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")
APP_HOSTS = {
    "authentik_host": "auth.example.test",
    "forgejo_host": "git.example.test",
    "grafana_host": "grafana.example.test",
    "dmf_cms_host": "console.example.test",
    "netbox_host": "netbox.example.test",
    "awx_host": "awx.example.test",
    "registry_host": "registry.example.test",
}
BASE_DOMAIN = "example.test"
OTHER_EXTERNAL_IP = "198.51.100.10"
FAKE_LIBRARY = pathlib.Path(__file__).resolve().parent / "fixtures" / "fake-library"

TASK_KEYWORDS = {
    "name", "when", "register", "become", "no_log", "vars", "changed_when",
    "failed_when", "delegate_to", "loop", "loop_control", "tags", "environment",
    "args", "ignore_errors", "check_mode",
}


def _action(task):
    return next(key for key in task if key not in TASK_KEYWORDS)


def _ansible_playbook():
    beside_python = pathlib.Path(sys.executable).parent / "ansible-playbook"
    if beside_python.exists():
        return str(beside_python)
    found = shutil.which("ansible-playbook")
    if found is None:
        raise unittest.SkipTest("ansible-playbook is not installed")
    return found


def _inventory(node_ip, external_ip=None):
    host = {
        "ansible_connection": "local",
        "ansible_python_interpreter": sys.executable,
        "ansible_host": PUBLIC_IP,
        "k3s_node_ip": node_ip,
        # The network facts the stand-in `setup` task returns. They are not
        # host facts until that task runs.
        "fixture_network_facts": {
            "ansible_all_ipv4_addresses": [INTERNAL_IP],
            "ansible_default_ipv4": {"address": INTERNAL_IP, "interface": "eth0"},
        },
        "dmf_tls_mode": "local-ca",
        "cert_manager_cluster_domain": BASE_DOMAIN,
        **APP_HOSTS,
    }
    if external_ip is not None:
        host["k3s_node_external_ip"] = external_ip
    return {
        "all": {
            "hosts": {"node1": host},
            "children": {
                "k3s": {"hosts": {"node1": {}}},
                "k3s_control": {"hosts": {"node1": {}}},
            },
        }
    }


def _kept_task(task):
    """Return the task to run, or None to drop it.

    `set_fact` and `import_tasks` run as written. `setup` runs as the stand-in
    module with the same arguments and guards. Everything else is dropped,
    because it needs a cluster or changes the node.
    """
    action = _action(task)
    if action in ("ansible.builtin.set_fact", "ansible.builtin.import_tasks"):
        task = dict(task)
        if action == "ansible.builtin.import_tasks":
            task[action] = str((PLAYBOOKS / task[action]).resolve())
        return task
    if action == "ansible.builtin.setup":
        args = task[action] or {}
        task = {key: value for key, value in task.items() if key != action}
        task["fake_setup"] = {**args, "facts": "{{ fixture_network_facts }}"}
        return task
    return None


def _run(play, tasks, node_ip, workdir, external_ip=None):
    """Run `tasks` as one play against the fixture node."""
    return _run_plays([_with_tasks(play, tasks)], node_ip, workdir, external_ip)


def _with_tasks(play, tasks):
    play = dict(play)
    play["become"] = False
    play["tasks"] = tasks
    return play


def _run_plays(plays, node_ip, workdir, external_ip=None):
    """Run the plays one after another, in one playbook, on the fixture node."""
    playbook = workdir / "playbook.yml"
    playbook.write_text(yaml.safe_dump(plays, sort_keys=False))
    inventory = workdir / "inventory.json"
    inventory.write_text(json.dumps(_inventory(node_ip, external_ip)))
    env = dict(os.environ)
    config = workdir / "ansible.cfg"
    config.write_text("[defaults]\n")
    env["ANSIBLE_CONFIG"] = str(config)
    env["ANSIBLE_HOME"] = str(workdir / "ansible-home")
    env["ANSIBLE_LOCAL_TEMP"] = str(workdir / "tmp")
    env["ANSIBLE_NOCOLOR"] = "1"
    env["ANSIBLE_LIBRARY"] = str(FAKE_LIBRARY)
    result = subprocess.run(
        [_ansible_playbook(), "-i", str(inventory), str(playbook)],
        cwd=workdir, env=env, capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise AssertionError(
            "ansible-playbook failed\n" + result.stdout + result.stderr
        )
    return result


def _local_ca_tasks(play, zone, hosts):
    """The 321 tasks that render the zone and the /etc/hosts block."""
    tasks = []
    for task in play["tasks"]:
        action = _action(task)
        args = task[action] if isinstance(task[action], dict) else {}
        definition = args.get("definition", {})
        if (action == "kubernetes.core.k8s"
                and definition.get("metadata", {}).get("name") == "coredns-custom"):
            tasks.append({
                "name": task["name"],
                "ansible.builtin.copy": {
                    "content": definition["data"]["dmf-local.server"],
                    "dest": str(zone),
                },
                "when": task.get("when", True),
            })
        elif (action == "ansible.builtin.blockinfile"
                and args.get("path") == "/etc/hosts"):
            tasks.append({
                "name": task["name"],
                "ansible.builtin.blockinfile": {
                    **args, "path": str(hosts), "create": True,
                },
                "when": task.get("when", True),
            })
        else:
            kept = _kept_task(task)
            if kept is not None:
                tasks.append(kept)
    return tasks


def _render_local_ca(node_ip, workdir):
    """Run the 321 tasks that render the zone and the /etc/hosts block."""
    play = yaml.safe_load(LOCAL_CA.read_text())[0]
    zone = workdir / "zone"
    hosts = workdir / "hosts"
    _run(play, _local_ca_tasks(play, zone, hosts), node_ip, workdir)
    return zone.read_text(), hosts.read_text()


class LocalCaNodeAddressTest(unittest.TestCase):
    def setUp(self):
        self.workdir = pathlib.Path(tempfile.mkdtemp(prefix="local-ca-node-"))
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)

    def _assert_only_internal(self, node_ip):
        zone, hosts = _render_local_ca(node_ip, self.workdir)
        for name, text in (("CoreDNS zone", zone), ("/etc/hosts block", hosts)):
            addresses = set(IPV4.findall(text))
            self.assertEqual(
                addresses, {INTERNAL_IP},
                f"{name} must hold only the internal address:\n{text}",
            )
            for app_host in [*APP_HOSTS.values(), BASE_DOMAIN]:
                self.assertRegex(
                    text, rf"(?m)^\s*{re.escape(INTERNAL_IP)} {re.escape(app_host)}$",
                    f"{name} has no entry for {app_host}:\n{text}",
                )

    def test_nat_node_run_alone_maps_hosts_to_internal_address(self):
        self._assert_only_internal(PUBLIC_IP)

    def test_node_with_local_address_is_unchanged(self):
        self._assert_only_internal(INTERNAL_IP)

    def _survey_addresses(self, node_ip, external_ip):
        """Return (public_ip, private_ip) as the born-inventory role records them.

        The play's tasks run first. The two addresses are then rendered from the
        role's own expressions for the node record.
        """
        play = yaml.safe_load(BORN_INVENTORY.read_text())[0]
        role_tasks = yaml.safe_load(BORN_INVENTORY_ROLE.read_text())
        survey = next(
            t for t in role_tasks
            if t.get("name") == "Build live node survey records from gathered facts"
        )
        record = survey["vars"]["_dmf_node_record"]
        out = self.workdir / "addresses"
        tasks = [t for t in map(_kept_task, play.get("tasks", [])) if t is not None]
        tasks.append({
            "name": "Record the addresses the role would write",
            "ansible.builtin.copy": {
                "content": record["public_ip"] + " " + record["private_ip"],
                "dest": str(out),
            },
            "loop": survey["loop"],
            "loop_control": survey["loop_control"],
            "vars": {
                key: survey["vars"][key]
                for key in ("dmf_node_host", "dmf_node_public_ip")
            },
        })
        _run(play, tasks, node_ip, self.workdir, external_ip)
        public_ip, private_ip = out.read_text().split()
        return public_ip, private_ip

    def test_born_inventory_records_internal_private_and_declared_public_address(self):
        # The declared address is recorded whether the inventory leaves
        # k3s_node_external_ip unset, sets it to the declared address, or sets
        # it to some other address.
        for external_ip in (None, PUBLIC_IP, OTHER_EXTERNAL_IP):
            with self.subTest(external_ip=external_ip):
                public_ip, private_ip = self._survey_addresses(PUBLIC_IP, external_ip)
                self.assertEqual(private_ip, INTERNAL_IP)
                self.assertEqual(public_ip, PUBLIC_IP)

    def test_born_inventory_for_node_with_local_address_is_unchanged(self):
        # A standalone run records what a full run records: the default-route
        # address as the public address, even when the inventory sets
        # k3s_node_external_ip to an SSH address that differs from it.
        for external_ip in (None, PUBLIC_IP):
            with self.subTest(external_ip=external_ip):
                addresses = self._survey_addresses(INTERNAL_IP, external_ip)
                self.assertEqual(addresses, (INTERNAL_IP, INTERNAL_IP))


def _gathering_and_resolver(play):
    """Return (index of the setup task, index of the resolver import, tasks)."""
    tasks = play["tasks"]
    actions = [_action(task) for task in tasks]
    return (
        actions.index("ansible.builtin.setup"),
        actions.index("ansible.builtin.import_tasks"),
        tasks,
    )


class StandalonePlaysGatherNetworkFactsTest(unittest.TestCase):
    """321 and 694 do not gather facts on their own, so each must gather the
    network facts, before the resolver, under the resolver's guards."""

    def _check(self, path, guards):
        play = yaml.safe_load(path.read_text())[0]
        self.assertFalse(play.get("gather_facts", True))
        setup, resolver, tasks = _gathering_and_resolver(play)
        self.assertLess(setup, resolver)
        self.assertEqual(tasks[setup]["ansible.builtin.setup"]["gather_subset"], ["network"])
        self.assertTrue(
            tasks[resolver]["ansible.builtin.import_tasks"].endswith("resolve-k3s-node-ip.yml")
        )
        for task in (tasks[setup], tasks[resolver]):
            when = task["when"] if isinstance(task["when"], list) else [task["when"]]
            self.assertEqual(sorted(when), sorted(guards))

    def test_local_ca_play(self):
        self._check(LOCAL_CA, ["_local_ca | bool", "k3s_node_ip is defined"])

    def test_born_inventory_play(self):
        self._check(BORN_INVENTORY, ["k3s_node_ip is defined"])

    def test_real_setup_returns_the_addresses_the_resolver_reads(self):
        # Run each play's own setup arguments against the real module. The
        # stand-in returns these facts; the real module must too, with an
        # address the resolver can use.
        baseline = self._real_setup_facts({"gather_subset": ["network"]})
        if not baseline["address"]:
            self.skipTest("this host has no default route to read an address from")
        for path in (LOCAL_CA, BORN_INVENTORY):
            with self.subTest(playbook=path.name):
                play = yaml.safe_load(path.read_text())[0]
                setup = next(
                    t for t in play["tasks"] if _action(t) == "ansible.builtin.setup"
                )
                facts = self._real_setup_facts(setup["ansible.builtin.setup"])
                self.assertRegex(facts["address"], r"^\d{1,3}(\.\d{1,3}){3}$")
                self.assertIn(facts["address"], facts["all"])

    def _real_setup_facts(self, args):
        workdir = pathlib.Path(tempfile.mkdtemp(prefix="real-setup-"))
        self.addCleanup(shutil.rmtree, workdir, ignore_errors=True)
        out = workdir / "facts.json"
        tasks = [
            {"ansible.builtin.setup": args},
            {"ansible.builtin.copy": {
                "content": (
                    "{{ {'address': ansible_default_ipv4.address | default(''),"
                    " 'all': ansible_all_ipv4_addresses | default([])} | to_json }}"
                ),
                "dest": str(out),
            }},
        ]
        _run({"hosts": "node1", "gather_facts": False}, tasks, INTERNAL_IP, workdir)
        return json.loads(out.read_text())


class RepeatedResolverCallsTest(unittest.TestCase):
    """300 resolves the address first; 321 and 694 then call the resolver again
    on the same host. The declared address, the internal address, the external
    address and the TLS SANs must not change after the first call."""

    def setUp(self):
        self.workdir = pathlib.Path(tempfile.mkdtemp(prefix="resolver-repeat-"))
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)

    def _snapshot(self, number):
        content = (
            "{{ {'internal': k3s_node_ip, 'declared': k3s_node_nat_declared_ip,"
            " 'external': k3s_node_external_ip, 'sans': k3s_server_tls_sans}"
            " | to_json }}"
        )
        return {
            "name": f"Record the address facts, call {number}",
            "ansible.builtin.copy": {
                "content": content,
                "dest": str(self.workdir / f"snapshot-{number}.json"),
            },
        }

    def _plays(self):
        k3s = yaml.safe_load((PLAYBOOKS / "300-k3s.yml").read_text())[0]
        local_ca = yaml.safe_load(LOCAL_CA.read_text())[0]
        born = yaml.safe_load(BORN_INVENTORY.read_text())[0]
        # 300 gathers facts itself (gather_facts: true); the stand-in does that
        # here, so the play's own gathering is turned off.
        k3s = {**k3s, "gather_facts": False}
        gather = {
            "name": "Gather facts as the play does",
            "fake_setup": {
                "gather_subset": ["all"], "facts": "{{ fixture_network_facts }}",
            },
        }
        first = [gather, *filter(None, map(_kept_task, k3s["tasks"]))]
        second = _local_ca_tasks(local_ca, self.workdir / "zone", self.workdir / "hosts")
        third = [t for t in map(_kept_task, born["tasks"]) if t is not None]
        return [
            _with_tasks(k3s, [*first, self._snapshot(1)]),
            _with_tasks(local_ca, [*second, self._snapshot(2)]),
            _with_tasks(born, [*third, self._snapshot(3)]),
        ]

    def test_nat_node_keeps_its_addresses_across_calls(self):
        for external_ip in (None, PUBLIC_IP, OTHER_EXTERNAL_IP):
            with self.subTest(external_ip=external_ip):
                _run_plays(self._plays(), PUBLIC_IP, self.workdir, external_ip)
                snapshots = [
                    json.loads((self.workdir / f"snapshot-{n}.json").read_text())
                    for n in (1, 2, 3)
                ]
                first = snapshots[0]
                self.assertEqual(first["declared"], PUBLIC_IP)
                self.assertEqual(first["internal"], INTERNAL_IP)
                self.assertEqual(first["external"], external_ip or PUBLIC_IP)
                self.assertIn(PUBLIC_IP, first["sans"])
                self.assertEqual(snapshots[1], first)
                self.assertEqual(snapshots[2], first)


if __name__ == "__main__":
    unittest.main()
