"""An imported local CA, the approval page certificate, and the LAN-mode mDNS
announcement.

* With `dmf_local_ca_import` the cert-manager role skips the self-signed root
  (it would overwrite the imported secret) and creates the CA secret from the
  controller's files. Without the flag nothing changes.
* An existing secret with a different certificate stops the play; a matching one
  changes nothing. This runs the import tasks against a stand-in for the
  cluster.
* In LAN mode the node announces the page name with its internal address, and
  the firewall opens UDP 5353. Public mode does neither.

Run with Ansible's Python: the checks use Ansible's own filters and the
ansible-playbook of the same environment.
"""
import base64
import json
import os
import pathlib
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import jinja2
import yaml
from ansible.plugins.filter.core import FilterModule
from ansible.plugins.filter.core import b64decode as _b64decode

import test_local_ca_node_address as nat

ROOT = pathlib.Path(__file__).resolve().parent.parent
ROLE = ROOT / "roles" / "base" / "cert-manager"
MAIN = ROLE / "tasks" / "main.yml"
IMPORT = ROLE / "tasks" / "import-local-ca.yml"
APPROVAL = ROLE / "tasks" / "approval-page-certificate.yml"
MDNS_ROLE = ROOT / "roles" / "base" / "lan-mdns" / "tasks" / "main.yml"
MDNS_PLAYBOOK = ROOT / "playbooks" / "323-lan-mdns.yml"
NFT = ROOT / "roles" / "base" / "harden" / "templates" / "nftables.conf.j2"
HARDEN_DEFAULTS = ROOT / "roles" / "base" / "harden" / "defaults" / "main.yml"
SANDBOX_PRE_SEED = ROOT / "bootstrap-sandbox-provision-pre-seed.yml"

PAGE_NAME = "dmf-abcd-1234.local"
CA_PEM = "-----BEGIN CERTIFICATE-----\nIMPORTED\n-----END CERTIFICATE-----\n"
# Built at run time so the source holds no private-key-shaped literal.
_KEY_LABEL = "EC " + "PRIVATE " + "KEY"
KEY_PEM = f"-----BEGIN {_KEY_LABEL}-----\nIMPORTEDKEY\n-----END {_KEY_LABEL}-----\n"
OTHER_PEM = "-----BEGIN CERTIFICATE-----\nOTHER\n-----END CERTIFICATE-----\n"


def _env():
    env = jinja2.Environment(trim_blocks=True)
    env.filters.update(FilterModule().filters())
    return env


def _task(path, name_start):
    tasks = yaml.safe_load(path.read_text())
    return next(t for t in tasks if t["name"].startswith(name_start))


def _when_holds(task, variables):
    """Evaluate a task's `when` the way Ansible does: every item must hold."""
    when = task.get("when", True)
    items = when if isinstance(when, list) else [when]
    env = _env()
    for item in items:
        if isinstance(item, bool):
            if not item:
                return False
            continue
        if not env.compile_expression(item)(**variables):
            return False
    return True


class ImportSkipsSelfSignedRootTest(unittest.TestCase):
    ROOT_TASKS = (
        "Create self-signed bootstrap ClusterIssuer",
        "Create local root CA Certificate",
        "Wait for local root CA to become Ready",
    )
    LANE = {"cert_manager_local_ca_enabled": True}

    def test_import_skips_every_self_signed_root_task(self):
        # Catches a role that keeps the self-signed root on import: cert-manager
        # would then overwrite the imported secret with its own CA.
        for name in self.ROOT_TASKS:
            with self.subTest(task=name):
                task = _task(MAIN, name)
                self.assertFalse(_when_holds(
                    task, {**self.LANE, "cert_manager_local_ca_import": True}))

    def test_without_the_flag_the_root_tasks_still_run(self):
        # Catches a skip that is too broad and changes existing installs.
        for name in self.ROOT_TASKS:
            with self.subTest(task=name):
                task = _task(MAIN, name)
                self.assertTrue(_when_holds(
                    task, {**self.LANE, "cert_manager_local_ca_import": False}))

    def test_import_flag_defaults_to_off(self):
        defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
        expr = defaults["cert_manager_local_ca_import"]
        value = _env().from_string(expr).render()
        self.assertEqual(value, "False")
        self.assertEqual(
            _env().from_string(expr).render(dmf_local_ca_import=True), "True")

    def test_import_task_runs_only_with_the_flag(self):
        task = _task(MAIN, "Import the install's local CA")
        self.assertTrue(_when_holds(task, {"cert_manager_local_ca_import": True}))
        self.assertFalse(_when_holds(task, {"cert_manager_local_ca_import": False}))

    def test_ca_issuer_and_default_certificate_are_kept_and_renew_early(self):
        for name in ("Create local CA ClusterIssuer",
                     "Create cluster default Certificate from local CA"):
            with self.subTest(task=name):
                task = _task(MAIN, name)
                self.assertTrue(_when_holds(
                    task, {**self.LANE, "cert_manager_local_ca_import": True}))
        spec = _task(MAIN, "Create cluster default Certificate from local CA")[
            "kubernetes.core.k8s"]["definition"]["spec"]
        self.assertEqual(spec["renewBefore"], "{{ cert_manager_local_ca_renew_before }}")
        defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
        self.assertEqual(defaults["cert_manager_local_ca_renew_before"], "720h")


class ImportSecretShapeTest(unittest.TestCase):
    def setUp(self):
        self.tasks = yaml.safe_load(IMPORT.read_text())

    def test_secret_carries_ca_crt_tls_crt_tls_key_and_hides_them(self):
        # Catches a secret without ca.crt (321 reads it), with the key in
        # ca.crt, or without no_log (the key would print on failure).
        task = next(t for t in self.tasks if t["name"].startswith("Create the CA secret"))
        definition = task["kubernetes.core.k8s"]["definition"]
        self.assertEqual(definition["type"], "kubernetes.io/tls")
        data = definition["stringData"]
        self.assertEqual(set(data), {"tls.crt", "tls.key", "ca.crt"})
        self.assertIn("cert_file", data["tls.crt"])
        self.assertIn("cert_file", data["ca.crt"])
        self.assertIn("key_file", data["tls.key"])
        self.assertNotIn("key_file", data["ca.crt"])
        self.assertIs(task.get("no_log"), True)

    def test_every_task_that_carries_the_key_sets_no_log(self):
        # The existing-secret read and the Secret create hold the key.
        carriers = 0
        for task in self.tasks:
            text = json.dumps(task)
            if ("cert_manager_local_ca_import_key_file" in text and "lookup(" in text
                    or "k8s_info" in text):
                carriers += 1
                with self.subTest(task=task["name"]):
                    self.assertIs(task.get("no_log"), True)
        self.assertEqual(carriers, 2)

    def test_refusal_task_shows_its_message(self):
        # The comparison reads the certificate only. With no_log the operator
        # who hits the refusal would see a censored failure.
        task = next(t for t in self.tasks if t["name"].startswith("Refuse to replace"))
        self.assertNotIn("no_log", task)
        self.assertIn("An existing install keeps its CA", task["ansible.builtin.assert"]["fail_msg"])
        condition = json.dumps(task["ansible.builtin.assert"]["that"]).replace("tls.crt", "")
        self.assertNotIn("key", condition)


def _fake_cluster(workdir):
    """A stand-in `k8s_info` and `k8s` that keep Secrets in one file."""
    lib = workdir / "library"
    lib.mkdir()
    store = workdir / "store.json"
    common = (
        "#!/usr/bin/python\n# WANT_JSON\nimport base64, json, os, sys\n"
        f"STORE = {str(store)!r}\n"
        "args = json.load(open(sys.argv[1]))\n"
        "data = json.load(open(STORE)) if os.path.exists(STORE) else {}\n"
    )
    (lib / "k8s_info.py").write_text(common + (
        "name = args['name']\n"
        "item = data.get(name)\n"
        "print(json.dumps({'changed': False, 'resources': [item] if item else []}))\n"
    ))
    (lib / "k8s.py").write_text(common + (
        "d = args['definition']\n"
        "name = d['metadata']['name']\n"
        "new = {'type': d['type'], 'data': {k: base64.b64encode(v.encode()).decode()\n"
        "       for k, v in d['stringData'].items()}}\n"
        "changed = data.get(name) != new\n"
        "data[name] = new\n"
        "json.dump(data, open(STORE, 'w'))\n"
        "print(json.dumps({'changed': changed}))\n"
    ))
    return lib, store


class ImportNeverClobbersTest(unittest.TestCase):
    def setUp(self):
        self.workdir = pathlib.Path(tempfile.mkdtemp(prefix="ca-import-"))
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)
        self.cert = self.workdir / "ca.crt"
        self.key = self.workdir / "ca.key"
        self.cert.write_text(CA_PEM)
        self.key.write_text(KEY_PEM)
        self.library, self.store = _fake_cluster(self.workdir)

    def _run(self):
        tasks = yaml.safe_load(IMPORT.read_text())
        for task in tasks:
            action = nat._action(task)
            if action.startswith("kubernetes.core."):
                task[action.split(".")[-1]] = task.pop(action)
        play = {
            "hosts": "all", "gather_facts": False,
            "vars": {
                "cert_manager_local_ca_enabled": True,
                "cert_manager_local_ca_secret": "dmf-local-ca",
                "cert_manager_namespace": "cert-manager",
                "cert_manager_local_ca_import_cert_file": str(self.cert),
                "cert_manager_local_ca_import_key_file": str(self.key),
            },
            "tasks": tasks,
        }
        playbook = self.workdir / "play.yml"
        playbook.write_text(yaml.safe_dump([play], sort_keys=False))
        inventory = self.workdir / "inv.json"
        inventory.write_text(json.dumps({"all": {"hosts": {"n": {
            "ansible_connection": "local",
            "ansible_python_interpreter": sys.executable}}}}))
        config = self.workdir / "ansible.cfg"
        config.write_text("[defaults]\n")
        env = dict(os.environ, ANSIBLE_CONFIG=str(config),
                   ANSIBLE_HOME=str(self.workdir / "home"),
                   ANSIBLE_LOCAL_TEMP=str(self.workdir / "tmp"),
                   ANSIBLE_NOCOLOR="1", ANSIBLE_LIBRARY=str(self.library))
        return subprocess.run(
            [nat._ansible_playbook(), "-i", str(inventory), str(playbook)],
            cwd=self.workdir, env=env, capture_output=True, text=True)

    def _stored(self):
        return json.loads(self.store.read_text())["dmf-local-ca"]

    def _put(self, pem):
        encoded = lambda text: base64.b64encode(text.encode()).decode()
        self.store.write_text(json.dumps({"dmf-local-ca": {
            "type": "kubernetes.io/tls",
            "data": {"tls.crt": encoded(pem), "tls.key": encoded("old"),
                     "ca.crt": encoded(pem)}}}))

    def test_new_install_creates_the_secret_from_the_files(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        data = {k: base64.b64decode(v).decode() for k, v in self._stored()["data"].items()}
        # lookup('file') drops the final newline of each PEM.
        self.assertEqual(data, {"tls.crt": CA_PEM.rstrip("\n"),
                                "tls.key": KEY_PEM.rstrip("\n"),
                                "ca.crt": CA_PEM.rstrip("\n")})
        self.assertNotIn("IMPORTEDKEY", result.stdout + result.stderr)

    def test_a_different_existing_ca_stops_the_play_and_is_kept(self):
        # Catches an import that overwrites a running install's CA.
        self._put(OTHER_PEM)
        before = self.store.read_text()
        result = self._run()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("An existing install keeps its CA", result.stdout + result.stderr)
        self.assertEqual(self.store.read_text(), before)

    def test_a_matching_existing_ca_changes_nothing(self):
        # Catches a re-run that rewrites the secret (a Manage session would then
        # report a change and replace the key).
        self._put(CA_PEM)
        before = self.store.read_text()
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.store.read_text(), before)
        self.assertRegex(result.stdout, r"changed=0")


class ApprovalPageCertificateTest(unittest.TestCase):
    def test_certificate_is_for_the_page_name_from_the_ca_issuer(self):
        tasks = yaml.safe_load(APPROVAL.read_text())
        cert = next(t for t in tasks if t["name"].startswith("Create the approval page Certificate"))
        spec = cert["kubernetes.core.k8s"]["definition"]["spec"]
        self.assertEqual(spec["dnsNames"], ["{{ cert_manager_approval_page_name }}"])
        self.assertEqual(spec["issuerRef"]["name"], "{{ cert_manager_local_ca_issuer }}")
        self.assertEqual(spec["duration"], "{{ cert_manager_approval_page_duration }}")
        self.assertEqual(spec["renewBefore"], "{{ cert_manager_local_ca_renew_before }}")
        defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
        self.assertEqual(defaults["cert_manager_approval_page_duration"], "8760h")
        self.assertEqual(defaults["cert_manager_approval_page_secret"], "dmf-approval-page-tls")
        self.assertEqual(defaults["cert_manager_approval_page_namespace"], "dmf-approval")
        self.assertTrue(any("Wait for the approval page" in t["name"] for t in tasks))

    def test_issued_only_with_import_and_a_page_name(self):
        task = _task(MAIN, "Issue the approval page certificate")
        base = {"cert_manager_local_ca_enabled": True,
                "cert_manager_local_ca_import": True,
                "cert_manager_approval_page_name": PAGE_NAME}
        self.assertTrue(_when_holds(task, base))
        self.assertFalse(_when_holds(task, {**base, "cert_manager_local_ca_import": False}))
        self.assertFalse(_when_holds(task, {**base, "cert_manager_approval_page_name": ""}))


def _avahi_hosts(workdir, node_ip, mode, play_vars=None, existing=None):
    """Run 323's address tasks and the role's hosts task; return the file text.

    `existing` is the text of /etc/avahi/hosts before the run.
    """
    play = yaml.safe_load(MDNS_PLAYBOOK.read_text())[0]
    role_tasks = yaml.safe_load(MDNS_ROLE.read_text())
    hosts_task = next(t for t in role_tasks if t["name"].startswith("Announce the approval page name"))
    out = workdir / "avahi-hosts"
    if existing is not None:
        out.write_text(existing)
    tasks = [t for t in map(nat._kept_task, play["tasks"]) if t is not None]
    include = next(t for t in play["tasks"] if nat._action(t) == "ansible.builtin.include_role")
    tasks.append({
        "name": hosts_task["name"],
        "ansible.builtin.blockinfile": {
            **{k: v for k, v in hosts_task["ansible.builtin.blockinfile"].items()
               if k not in ("owner", "group", "path")},
            "path": str(out),
        },
        "vars": {**include["vars"],
                 "lan_mdns_page_name": PAGE_NAME,
                 "lan_mdns_enabled": mode == "lan"},
        "when": hosts_task["when"],
    })
    play = dict(play)
    play["vars"] = {**play.get("vars", {}), **(play_vars or {})}
    nat._run(play, tasks, node_ip, workdir)
    return out.read_text() if out.exists() else None


class LanMdnsTest(unittest.TestCase):
    def setUp(self):
        self.workdir = pathlib.Path(tempfile.mkdtemp(prefix="lan-mdns-"))
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)

    def _run(self, node_ip, mode, existing=None):
        # The harness inventory carries no network mode; set it for the run.
        original = nat._inventory

        def inventory(node_ip_, external_ip=None):
            inv = original(node_ip_, external_ip)
            inv["all"]["hosts"]["node1"]["dmf_network_mode"] = mode
            inv["all"]["hosts"]["node1"]["dmf_approval_page_name"] = PAGE_NAME
            return inv

        nat._inventory = inventory
        self.addCleanup(setattr, nat, "_inventory", original)
        return _avahi_hosts(self.workdir, node_ip, mode, existing=existing)

    MARK_BEGIN = "# BEGIN DMF approval page (managed by dmf-infra)"
    MARK_END = "# END DMF approval page (managed by dmf-infra)"

    def _block(self, node_ip):
        return f"{self.MARK_BEGIN}\n{node_ip} {PAGE_NAME}\n{self.MARK_END}\n"

    def test_hosts_line_is_internal_address_and_page_name(self):
        # Catches a line that holds the declared public address on a NAT node,
        # or one without the page name.
        self.assertEqual(self._run(nat.INTERNAL_IP, "lan"), self._block(nat.INTERNAL_IP))

    def test_nat_node_announces_the_internal_address(self):
        text = self._run(nat.PUBLIC_IP, "lan")
        self.assertEqual(text, self._block(nat.INTERNAL_IP))
        self.assertNotIn(nat.PUBLIC_IP, text)

    def test_task_uses_blockinfile_with_the_dmf_marker(self):
        # Catches a return to `copy`, which replaces the whole file.
        task = next(t for t in yaml.safe_load(MDNS_ROLE.read_text())
                    if t["name"].startswith("Announce the approval page name"))
        self.assertNotIn("ansible.builtin.copy", task)
        self.assertEqual(task["ansible.builtin.blockinfile"]["marker"],
                         "# {mark} DMF approval page (managed by dmf-infra)")

    def test_other_entries_in_the_hosts_file_survive(self):
        # Catches a task that overwrites /etc/avahi/hosts.
        other = "192.0.2.77 printer.local\n"
        text = self._run(nat.INTERNAL_IP, "lan", existing=other)
        self.assertIn(other, text)
        self.assertIn(self._block(nat.INTERNAL_IP), text)

    def test_a_second_run_changes_nothing_and_updates_in_place(self):
        # Catches a task that appends a new block on every run.
        once = self._run(nat.INTERNAL_IP, "lan", existing="192.0.2.77 printer.local\n")
        twice = self._run(nat.INTERNAL_IP, "lan", existing=once)
        self.assertEqual(once, twice)
        self.assertEqual(twice.count(PAGE_NAME), 1)
        stale = self._run(nat.INTERNAL_IP, "lan",
                          existing=f"{self.MARK_BEGIN}\n198.51.100.9 {PAGE_NAME}\n{self.MARK_END}\n")
        self.assertNotIn("198.51.100.9", stale)

    def test_public_mode_writes_nothing(self):
        self.assertIsNone(self._run(nat.INTERNAL_IP, "public"))

    def test_role_does_nothing_outside_lan_mode(self):
        defaults = yaml.safe_load(
            (ROOT / "roles/base/lan-mdns/defaults/main.yml").read_text())
        expr = defaults["lan_mdns_enabled"]
        env = _env()
        self.assertEqual(env.from_string(expr).render(dmf_network_mode="public"), "False")
        self.assertEqual(env.from_string(expr).render(), "False")
        self.assertEqual(env.from_string(expr).render(dmf_network_mode="lan"), "True")
        for task in yaml.safe_load(MDNS_ROLE.read_text()):
            with self.subTest(task=task["name"]):
                self.assertFalse(_when_holds(task, {"lan_mdns_enabled": False}))

    def test_role_installs_enables_and_starts_avahi(self):
        tasks = yaml.safe_load(MDNS_ROLE.read_text())
        apt = next(t for t in tasks if "ansible.builtin.apt" in t)
        self.assertEqual(apt["ansible.builtin.apt"]["name"], "avahi-daemon")
        svc = next(t for t in tasks if "ansible.builtin.service" in t)
        self.assertEqual(svc["ansible.builtin.service"],
                         {"name": "avahi-daemon", "enabled": True, "state": "started"})


HARDEN_TASKS = ROOT / "roles" / "base" / "harden" / "tasks" / "main.yml"
LAN_IFACE = "enp0s1"
LAN_NET = "192.0.2.0"
LAN_PREFIX = "24"
LAN_CIDR = f"{LAN_NET}/{LAN_PREFIX}"
LAN_ADDRESS = "192.0.2.10"


def _iface_facts(name=LAN_IFACE, with_ipv4=True):
    facts = {"ansible_default_ipv4": {"address": LAN_ADDRESS, "interface": name}}
    entry = {"device": name}
    if with_ipv4:
        entry["ipv4"] = {"address": LAN_ADDRESS, "network": LAN_NET,
                         "netmask": "255.255.255.0", "prefix": LAN_PREFIX}
    facts[name.replace("-", "_")] = entry
    return facts


def _render_nftables(workdir, facts, variables):
    """Run the harden role's scoping tasks, then render the ruleset template.

    The tasks come from the role as written. The run uses the role's own
    defaults, so the interface lookup is the real one.
    """
    tasks = yaml.safe_load(HARDEN_TASKS.read_text())
    wanted = ("Resolve the LAN interface and network for the mDNS rule",
              "Refuse to open mDNS without a LAN interface and network")
    kept = [t for t in tasks if t["name"] in wanted]
    assert len(kept) == 2
    out = workdir / "nftables.conf"
    play = {
        "name": "render",
        "hosts": "k3s",
        "gather_facts": False,
        "vars": {**yaml.safe_load(HARDEN_DEFAULTS.read_text()), **variables},
        "tasks": [
            {"fake_setup": {"gather_subset": ["network"], "facts": facts}},
            *kept,
            {"name": "render",
             "ansible.builtin.template": {
                 "src": str(NFT), "dest": str(out), "mode": "0640"}},
        ],
    }
    nat._run_plays([nat._with_tasks(play, play["tasks"])], nat.INTERNAL_IP, workdir)
    return out.read_text()


class NftablesMdnsRuleTest(unittest.TestCase):
    def setUp(self):
        self.workdir = pathlib.Path(tempfile.mkdtemp(prefix="nft-mdns-"))
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)

    def _render(self, mode, facts=None, **extra):
        variables = dict(extra)
        if mode:
            variables["dmf_network_mode"] = mode
        return _render_nftables(self.workdir, facts or _iface_facts(), variables)

    def _mdns_lines(self, text):
        return [ln.strip() for ln in text.splitlines()
                if "5353" in ln and not ln.strip().startswith("#")]

    def test_rule_renders_only_in_lan_mode(self):
        # Catches a rule that is always on (public nodes would answer mDNS) or
        # never on.
        self.assertTrue(self._mdns_lines(self._render("lan")))
        self.assertEqual(self._mdns_lines(self._render("public")), [])
        self.assertEqual(self._mdns_lines(self._render(None)), [])

    def test_rule_is_scoped_to_the_interface_and_its_derived_network(self):
        # Catches a rule with no interface match, or one that ignores the
        # network the interface facts give.
        lines = self._mdns_lines(self._render("lan"))
        self.assertIn(
            f'iifname "{LAN_IFACE}" ip saddr {LAN_CIDR} ip daddr 224.0.0.251 udp dport 5353 accept',
            lines)
        self.assertIn(
            f'iifname "{LAN_IFACE}" ip saddr {LAN_CIDR} ip daddr {LAN_ADDRESS} udp dport 5353 accept',
            lines)

    def test_no_unscoped_5353_rule_can_render(self):
        # Catches any accept for 5353 without the interface, the source
        # network and a destination.
        for kwargs in ({}, {"harden_lan_cidr": "198.51.100.0/24"},
                       {"harden_lan_interface": "br-lan"}):
            facts = _iface_facts("br-lan") if "harden_lan_interface" in kwargs else None
            for line in self._mdns_lines(self._render("lan", facts, **kwargs)):
                with self.subTest(line=line):
                    self.assertRegex(line, r'^iifname "[^"]+" ip saddr [0-9.]+/[0-9]+ ip daddr [0-9.]+ udp dport 5353 accept$')

    def test_explicit_cidr_and_interface_win_over_the_facts(self):
        text = self._render("lan", _iface_facts("br-lan"),
                            harden_lan_interface="br-lan", harden_lan_cidr="198.51.100.0/24")
        lines = self._mdns_lines(text)
        self.assertTrue(all('iifname "br-lan" ip saddr 198.51.100.0/24 ' in ln for ln in lines))

    def test_interface_follows_the_k3s_lookup(self):
        # Catches an interface chosen by a different rule than 300-k3s uses.
        facts = {**_iface_facts("eth9"), "ansible_default_ipv4": {"interface": "eth0"}}
        lines = self._mdns_lines(self._render("lan", facts, k3s_private_interface="eth9"))
        self.assertTrue(lines and all('iifname "eth9"' in ln for ln in lines))

    def test_missing_interface_fails_the_play(self):
        # Catches a fallback to a rule open on every interface.
        facts = {"ansible_default_ipv4": {}}
        with self.assertRaisesRegex(AssertionError, "never opened to every source"):
            self._render("lan", facts)

    def test_missing_network_fails_the_play(self):
        # Catches a fallback to a rule open to every source address.
        with self.assertRaisesRegex(AssertionError, "never opened to every source"):
            self._render("lan", _iface_facts(with_ipv4=False))

    def test_public_mode_needs_no_interface(self):
        self.assertEqual(self._mdns_lines(self._render("public", {"ansible_default_ipv4": {}})), [])

    def test_rule_comes_before_the_drop(self):
        text = self._render("lan")
        self.assertLess(text.index("5353"), text.index("drop\n"))


class PreSeedChainTest(unittest.TestCase):
    def test_playbook_runs_after_321_and_before_the_registry(self):
        chain = [item["import_playbook"] for item in yaml.safe_load(SANDBOX_PRE_SEED.read_text())
                 if "import_playbook" in item]
        a = chain.index("playbooks/321-local-ca-trust.yml")
        b = chain.index("playbooks/323-lan-mdns.yml")
        self.assertEqual(b, a + 1)

    def test_playbook_number_is_unused(self):
        numbers = [p.name for p in (ROOT / "playbooks").glob("323-*")]
        self.assertEqual(numbers, ["323-lan-mdns.yml"])


if __name__ == "__main__":
    unittest.main()
