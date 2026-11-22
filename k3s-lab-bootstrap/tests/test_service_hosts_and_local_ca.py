"""The registry and console names agree everywhere, and 321 refuses a missing CA.

Calls that verify a certificate use a host name. The node's /etc/hosts and
CoreDNS zone (321), the Zot ingress, the seeding play (630), the console smoke
test (699) and the console image check must all use the same name, or a call
fails on DNS or on the certificate name before it reaches the service.

The names come from one file, `vars/service-hosts.yml`. This test checks that:

* the file yields the default and the overridden name for each service,
* every play that maps, serves or calls those names loads the file and uses the
  variable instead of writing its own expression, and
* on the local-ca lane 321 fails, with a message that names the Secret, when
  the CA Secret or its `ca.crt` is missing,
* 321 refuses a console or registry name that the platform certificate (the
  apex and one label under it) does not cover,
* the console image the pod pulls, the name 630 seeds and the name the image
  check calls are the same registry name, and
* path-mode exposure refuses an https base URL whose host is an IP address.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]
HOSTS_FILE = ROOT / "vars" / "service-hosts.yml"
LOCAL_CA = ROOT / "playbooks" / "321-local-ca-trust.yml"

# Files that must load vars/service-hosts.yml, and the variable each one uses.
CONSUMERS = {
    "playbooks/321-local-ca-trust.yml": ["dmf_registry_host_effective", "dmf_console_host_effective"],
    "playbooks/630-zot-seed-platform.yml": ["dmf_registry_host_effective"],
    "playbooks/699-cms-smoke-test.yml": ["dmf_console_host_effective"],
    "playbooks/650-dmf-cms.yml": [],
    "playbooks/331-registry-zot.yml": [],
    "playbooks/vertical-security/191-zot-oidc.yml": [],
    "playbooks/694-born-inventory.yml": [],
}
# Role files that read the variable.
ROLE_USES = {
    "roles/stack/operator/zot/defaults/main.yml": "dmf_registry_host_effective",
    "roles/stack/operator/cms/defaults/main.yml": "dmf_registry_host_effective",
}


def _render(variables):
    env = Environment()
    env.tests["match"] = lambda value, pattern: re.match(pattern, value) is not None
    raw = yaml.safe_load(HOSTS_FILE.read_text())
    out = {}
    for name, template in raw.items():
        # Later names may read earlier ones, as they do in the play.
        out[name] = env.from_string(template).render(**variables, **out)
    return out


class ServiceHostNames(unittest.TestCase):
    def test_defaults(self):
        out = _render({"cert_manager_cluster_domain": "lab.test"})
        self.assertEqual(out["dmf_registry_host_effective"], "registry.lab.test")
        self.assertEqual(out["dmf_console_host_effective"], "console.lab.test")

    def test_overrides(self):
        out = _render({"cert_manager_cluster_domain": "lab.test",
                       "registry_host": "reg.other.test", "cms_host": "ui.other.test"})
        self.assertEqual(out["dmf_registry_host_effective"], "reg.other.test")
        self.assertEqual(out["dmf_console_host_effective"], "ui.other.test")

    def test_empty_override_falls_back(self):
        out = _render({"cert_manager_cluster_domain": "lab.test", "registry_host": ""})
        self.assertEqual(out["dmf_registry_host_effective"], "registry.lab.test")

    def test_consumers_load_the_file_and_use_the_variable(self):
        for rel, names in CONSUMERS.items():
            text = (ROOT / rel).read_text()
            self.assertIn("service-hosts.yml", text, f"{rel} does not load service-hosts.yml")
            for name in names:
                self.assertIn(name, text, f"{rel} does not use {name}")

    def test_role_files_use_the_variable(self):
        for rel, name in ROLE_USES.items():
            self.assertIn(name, (ROOT / rel).read_text(), f"{rel} does not use {name}")

    def test_no_play_writes_its_own_registry_or_console_expression(self):
        own = re.compile(r"'(registry|console)\.'\s*~")
        allowed_files = {"vars/service-hosts.yml"}
        # These read the same names for other purposes and are not part of the
        # mapped-and-verified path: blueprint templates, the landing page, the
        # Operate probes and the role defaults for cms_host itself.
        skip = ("roles/stack/operator/authentik/", "roles/stack/operator/landing-page/",
                "playbooks/lifecycle/", "roles/stack/operator/cms/defaults/")
        for rel in list(CONSUMERS) + list(ROLE_USES):
            if rel in allowed_files or rel.startswith(skip):
                continue
            self.assertIsNone(own.search((ROOT / rel).read_text()),
                              f"{rel} builds its own registry or console name")


def _run_task(task, variables, with_hosts_file=False):
    """Run one task as written, in a one-task play, through ansible-playbook."""
    with tempfile.TemporaryDirectory() as tmp:
        play = {"hosts": "localhost", "gather_facts": False, "vars": variables, "tasks": [task]}
        if with_hosts_file:
            play["vars_files"] = [str(HOSTS_FILE)]
        book = Path(tmp) / "p.yml"
        book.write_text(yaml.safe_dump([play]))
        env = dict(os.environ, ANSIBLE_LOCALHOST_WARNING="False",
                   ANSIBLE_INVENTORY_UNPARSED_WARNING="False")
        return subprocess.run(
            [shutil.which("ansible-playbook") or str(Path(sys.executable).parent / "ansible-playbook"),
             str(book), "-i", "localhost,", "-c", "local"],
            capture_output=True, text=True, env=env, cwd=tmp)


def _task(path, name):
    docs = yaml.safe_load(Path(path).read_text())
    stack = list(docs)
    while stack:
        node = stack.pop()
        if isinstance(node, dict):
            if node.get("name") == name:
                return node
            stack.extend(v for v in node.values() if isinstance(v, (dict, list)))
        elif isinstance(node, list):
            stack.extend(node)
    raise AssertionError(f"no task {name!r} in {path}")


class PlatformCertificateCoverage(unittest.TestCase):
    """Run 321's coverage check as written for each kind of name."""

    task = None

    @classmethod
    def setUpClass(cls):
        cls.task = _task(LOCAL_CA, "Assert the registry and console names are covered by the platform certificate")

    def _run(self, console, registry, names=None, local_ca=True):
        apex = "lab.test"
        variables = {
            "_local_ca": local_ca,
            "_platform_cert_apex": apex,
            "_platform_cert_names": names or [apex, "*." + apex],
            "dmf_console_host_effective": console,
            "dmf_registry_host_effective": registry,
        }
        return _run_task(self.task, variables)

    def test_default_names_pass(self):
        r = self._run("console.lab.test", "registry.lab.test")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_single_label_override_passes(self):
        r = self._run("ui.lab.test", "reg.lab.test")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_apex_passes(self):
        r = self._run("lab.test", "registry.lab.test")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_nested_console_name_fails(self):
        r = self._run("a.b.lab.test", "registry.lab.test")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("a.b.lab.test is not covered", r.stdout + r.stderr)

    def test_foreign_console_name_fails(self):
        r = self._run("ui.other.test", "registry.lab.test")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("ui.other.test is not covered", r.stdout + r.stderr)

    def test_foreign_registry_name_fails(self):
        r = self._run("console.lab.test", "reg.other.test")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("reg.other.test is not covered", r.stdout + r.stderr)

    def test_a_name_added_to_the_certificate_passes(self):
        r = self._run("ui.other.test", "registry.lab.test",
                      names=["lab.test", "*.lab.test", "ui.other.test"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_other_lanes_skip_the_check(self):
        r = self._run("ui.other.test", "reg.other.test", local_ca=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_the_play_defines_the_names_it_checks(self):
        play = yaml.safe_load(LOCAL_CA.read_text())[0]
        self.assertIn("_platform_cert_names", play["vars"])
        self.assertIn("cert_manager_local_ca_dns_names", play["vars"]["_platform_cert_names"])


class ConsoleImageRegistry(unittest.TestCase):
    """The pod pulls from the registry name that 630 seeds and Zot serves."""

    def _render(self, template, variables):
        env = Environment()
        hosts = _render(variables)
        return env.from_string(template).render(**variables, **hosts)

    def test_default_repository_uses_the_effective_registry(self):
        defaults = yaml.safe_load((ROOT / "roles/stack/operator/cms/defaults/main.yml").read_text())
        for variables, host in [
            ({"cert_manager_cluster_domain": "lab.test"}, "registry.lab.test"),
            ({"cert_manager_cluster_domain": "lab.test", "registry_host": "reg.lab.test"}, "reg.lab.test"),
        ]:
            repo = self._render(defaults["cms_image_repository"], variables)
            self.assertEqual(repo, host + "/dmf-cms")

    def test_values_fallback_matches_the_default(self):
        text = (ROOT / "roles/stack/operator/cms/templates/values.yml.j2").read_text()
        line = next(l for l in text.splitlines() if l.strip().startswith("repository:"))
        self.assertIn("dmf_registry_host_effective", line)

    def test_image_check_calls_the_repository_the_pod_pulls(self):
        task = _task(ROOT / "roles/stack/operator/cms/tasks/main.yml",
                     "Assert dmf-cms image is present in cluster-internal Zot")
        url = task["ansible.builtin.uri"]["url"]
        for repo in ["reg.lab.test/dmf-cms", "registry.lab.test/team/dmf-cms"]:
            out = self._render(url, {"cms_image_repository": repo, "cms_image_tag": "1.2.3"})
            host, path = repo.split("/", 1)
            self.assertEqual(out, f"https://{host}/v2/{path}/manifests/1.2.3")

    def test_630_seeds_the_same_repository(self):
        text = (ROOT / "playbooks/630-zot-seed-platform.yml").read_text()
        self.assertIn('zot_repo: "{{ zot_registry_host }}/dmf-cms"', text)
        self.assertIn('zot_registry_host: "{{ dmf_registry_host_effective }}"', text)


class PathModeBaseUrl(unittest.TestCase):
    """Path-mode exposure refuses an https base URL with an IP host."""

    def _cases(self, task, extra=None):
        base = dict(extra or {})
        def run(url=None, vip="198.51.100.5", **more):
            variables = dict(base, **more)
            if url is not None:
                variables["external_base_url"] = url
            if vip:
                variables["metallb_vip"] = vip
            return _run_task(task, variables, with_hosts_file=True)
        return run

    def _check(self, task, expose):
        run = self._cases(task, expose)
        self.assertNotEqual(run("https://198.51.100.5").returncode, 0)
        self.assertNotEqual(run("https://198.51.100.5:8443/x").returncode, 0)
        self.assertNotEqual(run("https://[2001:db8::1]").returncode, 0)
        self.assertEqual(run("https://console.lab.test").returncode, 0)
        self.assertEqual(run("http://198.51.100.5").returncode, 0)
        self.assertEqual(run(None).returncode, 0)  # falls back to http://<vip>

    def test_console_and_awx_smoke_test(self):
        task = _task(ROOT / "playbooks/699-cms-smoke-test.yml",
                     "Assert path-mode exposure does not use an https IP address")
        self._check(task, {"cms_exposure": "path"})
        run = self._cases(task, {"cms_exposure": "host", "awx_exposure": "host"})
        self.assertEqual(run("https://198.51.100.5").returncode, 0)  # host mode: not used

    def test_born_inventory(self):
        task = _task(ROOT / "roles/common/dmf-born-inventory/tasks/main.yml",
                     "Assert path-mode exposure does not use an https IP address")
        self._check(task, {"netbox_exposure": "path"})
        run = self._cases(task, {"netbox_exposure": "host"})
        self.assertEqual(run("https://198.51.100.5").returncode, 0)

    def test_born_inventory_play_loads_the_names_file(self):
        self.assertIn("service-hosts.yml", (ROOT / "playbooks/694-born-inventory.yml").read_text())


class LocalCaAssertion(unittest.TestCase):
    """Run 321's CA check as written, against a Secret that is missing or present."""

    @classmethod
    def setUpClass(cls):
        play = yaml.safe_load(LOCAL_CA.read_text())[0]
        cls.task = next(t for t in play["tasks"] if t.get("name") == "Assert the local CA Secret holds ca.crt")

    def _run(self, secret_result, local_ca=True):
        with tempfile.TemporaryDirectory() as tmp:
            book = Path(tmp) / "p.yml"
            book.write_text(yaml.safe_dump([{
                "hosts": "localhost", "gather_facts": False,
                "vars": {"_local_ca": local_ca, "_ca_namespace": "cert-manager",
                         "_ca_secret": "dmf-local-ca", "_local_ca_secret": secret_result},
                "tasks": [self.task],
            }]))
            env = dict(os.environ, ANSIBLE_LOCALHOST_WARNING="False",
                       ANSIBLE_INVENTORY_UNPARSED_WARNING="False")
            return subprocess.run(
                [shutil.which("ansible-playbook") or str(Path(sys.executable).parent / "ansible-playbook"), str(book),
                 "-i", "localhost,", "-c", "local"],
                capture_output=True, text=True, env=env, cwd=tmp)

    def test_missing_secret_fails_with_its_name(self):
        r = self._run({"resources": []})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cert-manager/dmf-local-ca", r.stdout + r.stderr)

    def test_secret_without_ca_crt_fails(self):
        r = self._run({"resources": [{"data": {}}]})
        self.assertNotEqual(r.returncode, 0)

    def test_secret_with_ca_crt_passes(self):
        r = self._run({"resources": [{"data": {"ca.crt": "QQ=="}}]})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_other_lanes_skip_the_check(self):
        r = self._run({"resources": []}, local_ca=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
