"""Run the registry assertion tasks through ansible-playbook with real inventories."""

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "vars/openbao-approle-lifecycle-registry.json"
OBJECT_STORAGE_TASKS = ROOT / "roles/base/object-storage-credentials/tasks/main.yml"
OPENBAO_TASKS = ROOT / "roles/stack/operator/openbao/tasks/main.yml"
OBJECT_STORAGE_DEFAULTS = ROOT / "roles/base/object-storage-credentials/defaults/main.yml"
OPENBAO_DEFAULTS = ROOT / "roles/stack/operator/openbao/defaults/main.yml"
ASSERT_PREFIXES = ("Assert ",)


def assertion_blocks(path):
    """Return the raw text of each top-level `- name: Assert ...` task."""
    blocks, current = [], None
    for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
        if line.startswith("- name: "):
            name = line[len("- name: "):]
            current = [line] if name.startswith(ASSERT_PREFIXES) else None
            if current is not None:
                blocks.append(current)
        elif current is not None and not line.startswith("# "):
            current.append(line)
    return ["".join(block) for block in blocks]


def run_assertions(tasks_path, defaults_path, overrides):
    """Return (rc, output) for the assertion tasks under the given inventory vars."""
    blocks = assertion_blocks(tasks_path)
    if not blocks:
        raise AssertionError(f"no assertion tasks found in {tasks_path}")
    with tempfile.TemporaryDirectory() as tmp:
        tmpdir = Path(tmp)
        playbook = tmpdir / "play.yml"
        header = (
            "- hosts: localhost\n"
            "  connection: local\n"
            "  gather_facts: false\n"
            f"  vars_files: [{json.dumps(str(defaults_path))}]\n"
            "  tasks:\n"
            "    - ansible.builtin.include_vars:\n"
            f"        file: {json.dumps(str(REGISTRY))}\n"
            "        name: openbao_approle_lifecycle_registry\n"
        )
        body = "".join(
            "".join("    " + line if line.strip() else line for line in block.splitlines(keepends=True))
            for block in blocks
        )
        playbook.write_text(header + body, encoding="utf-8")
        extra = tmpdir / "extra.json"
        extra.write_text(json.dumps(overrides), encoding="utf-8")
        env = {
            "ANSIBLE_LOCAL_TEMP": tmp,
            "ANSIBLE_HOME": tmp,
            "ANSIBLE_NOCOLOR": "1",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }
        proc = subprocess.run(
            ["ansible-playbook", "-i", "localhost,", str(playbook), "-e", f"@{extra}"],
            capture_output=True,
            text=True,
            env=env,
            cwd=tmp,
        )
    return proc.returncode, proc.stdout + proc.stderr


@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook is not installed")
class ObjectStorageRegistryAssertionTests(unittest.TestCase):
    def run_os(self, **overrides):
        return run_assertions(OBJECT_STORAGE_TASKS, OBJECT_STORAGE_DEFAULTS, overrides)

    def test_default_logicals_pass(self):
        rc, out = self.run_os()
        self.assertEqual(rc, 0, out)

    def test_documented_subset_passes(self):
        rc, out = self.run_os(object_storage_logicals=["app_backups"])
        self.assertEqual(rc, 0, out)

    def test_unknown_logical_fails_and_names_the_missing_entry(self):
        rc, out = self.run_os(object_storage_logicals=["app_backups", "media_archive"])
        self.assertNotEqual(rc, 0, out)
        self.assertIn("media_archive", out)
        self.assertIn("logical_name", out)

    def test_dash_alias_of_a_registered_logical_fails(self):
        rc, out = self.run_os(object_storage_logicals=["app-backups"])
        self.assertNotEqual(rc, 0, out)
        self.assertIn("app-backups", out)

    def test_duplicate_logical_fails(self):
        rc, out = self.run_os(object_storage_logicals=["app_backups", "app_backups"])
        self.assertNotEqual(rc, 0, out)
        self.assertIn("each listed once", out)

    def test_empty_logicals_fails_with_a_clear_message(self):
        rc, out = self.run_os(object_storage_logicals=[])
        self.assertNotEqual(rc, 0, out)
        self.assertIn("at least one logical", out)

    def test_removed_ttl_override_fails_with_migration_message(self):
        rc, out = self.run_os(openbao_approle_secret_id_ttl="720h")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("openbao_approle_secret_id_ttl is no longer read", out)
        self.assertIn("openbao-approle-lifecycle-registry.json", out)


@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook is not installed")
class OpenBaoRegistryAssertionTests(unittest.TestCase):
    def run_bao(self, **overrides):
        return run_assertions(OPENBAO_TASKS, OPENBAO_DEFAULTS, overrides)

    def test_defaults_pass(self):
        rc, out = self.run_bao()
        self.assertEqual(rc, 0, out)

    def test_each_removed_ttl_override_fails(self):
        for name in (
            "openbao_eso_approle_secret_id_ttl",
            "openbao_born_inventory_approle_secret_id_ttl",
        ):
            with self.subTest(name=name):
                rc, out = self.run_bao(**{name: "1h"})
                self.assertNotEqual(rc, 0, out)
                self.assertIn("no longer read", out)
                self.assertIn("openbao-approle-lifecycle-registry.json", out)

    def test_renamed_eso_role_reports_both_values_and_the_entry(self):
        rc, out = self.run_bao(openbao_eso_role_name="eso-custom")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("eso-custom", out)
        self.assertIn("runtime_targets.external-secrets.approle_name", out)
        self.assertIn("is external-secrets.", out)

    def test_renamed_born_inventory_role_reports_both_values_and_the_entry(self):
        rc, out = self.run_bao(openbao_born_inventory_role_name="born-custom")
        self.assertNotEqual(rc, 0, out)
        self.assertIn("born-custom", out)
        self.assertIn("dispositions.born-inventory-netbox-reader.approle_name", out)
        self.assertIn("is born-inventory-netbox-reader.", out)


if __name__ == "__main__":
    unittest.main()
