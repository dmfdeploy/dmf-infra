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
  the CA Secret or its `ca.crt` is missing.
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
}
# Role files that read the variable.
ROLE_USES = {
    "roles/stack/operator/zot/defaults/main.yml": "dmf_registry_host_effective",
    "roles/stack/operator/cms/tasks/main.yml": "dmf_registry_host_effective",
}


def _render(variables):
    env = Environment()
    raw = yaml.safe_load(HOSTS_FILE.read_text())
    out = {}
    for name, template in raw.items():
        out[name] = env.from_string(template).render(**variables)
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
