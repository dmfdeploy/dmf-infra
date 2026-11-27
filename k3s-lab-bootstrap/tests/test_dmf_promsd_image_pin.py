"""The dmf-promsd image is pinned by its OCI index digest, set once in vars/dmf-promsd.yml.

Static checks: the file holds a semver tag and a well-formed sha256 digest and nothing
else; it is the only place either is defined; playbooks 630 and 160 load it; 630 copies
the image by that digest (not by the tag) and mirrors the whole index; the role
references the image by tag and digest and defines neither itself.

Executable checks, run with ansible-playbook on localhost: the play's `vars_files` and
`vars`, after the role defaults, render the value 630 copies from and the image the
deployment names. They render the values; they do not run the tasks, so they do not
prove what Zot or the cluster do with them. Nothing contacts a registry or a cluster."""
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
PIN_FILE = ROOT / "vars" / "dmf-promsd.yml"
ROLE = ROOT / "roles/stack/operator/dmf-promsd"
PLAY_630 = ROOT / "playbooks/630-zot-seed-platform.yml"
PLAY_160 = ROOT / "playbooks/vertical-monitoring/160-promsd.yml"
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
SCANNED = {".yml", ".yaml", ".ini", ".j2", ".json"}
SKIPPED_DIRS = {".git", ".ansible", "node_modules", "__pycache__"}


def _pin():
    return yaml.safe_load(PIN_FILE.read_text(encoding="utf-8"))


def _play(path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))[0]


def _deployment_image():
    tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text(encoding="utf-8"))
    deploy = next(t for t in tasks if t.get("name") == "Deploy dmf-promsd")
    container = deploy["kubernetes.core.k8s"]["definition"]["spec"]["template"]["spec"]["containers"][0]
    return container["image"]


def _seed_entry():
    play = _play(PLAY_630)
    return play, next(i for i in play["vars"]["zot_seed_images"] if i["name"] == "dmf-promsd")


def _ansible_playbook():
    beside_python = pathlib.Path(sys.executable).parent / "ansible-playbook"
    if beside_python.exists():
        return str(beside_python)
    found = shutil.which("ansible-playbook")
    if found is None:
        raise unittest.SkipTest("ansible-playbook is not installed")
    return found


def _render(vars_files, play_vars, expressions):
    """Render each expression in a play with these vars_files and vars; return the values."""
    with tempfile.TemporaryDirectory() as directory:
        work = pathlib.Path(directory)
        play = {
            "name": "render the image values",
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars_files": vars_files,
            "tasks": [{"ansible.builtin.copy": {"content": json.dumps(expressions), "dest": str(work / "out.json")}}],
        }
        if play_vars:
            play["vars"] = play_vars
        (work / "playbook.yml").write_text(yaml.safe_dump([play], sort_keys=False), encoding="utf-8")
        (work / "ansible.cfg").write_text("[defaults]\n", encoding="utf-8")
        env = dict(os.environ, ANSIBLE_HOME=str(work / "home"), ANSIBLE_LOCAL_TEMP=str(work / "tmp"),
                   ANSIBLE_NOCOLOR="1", ANSIBLE_CONFIG=str(work / "ansible.cfg"))
        result = subprocess.run(
            [_ansible_playbook(), "-i", "localhost,", "-e", json.dumps({"ansible_python_interpreter": sys.executable}),
             str(work / "playbook.yml")],
            cwd=work, env=env, capture_output=True, text=True)
        if result.returncode != 0:
            raise AssertionError("ansible-playbook failed\n" + result.stdout + result.stderr)
        return json.loads((work / "out.json").read_text(encoding="utf-8"))


class PinFileTest(unittest.TestCase):
    def test_the_file_holds_a_tag_and_a_digest_only(self):
        data = _pin()
        self.assertEqual(sorted(data), ["dmf_promsd_image_digest", "dmf_promsd_image_tag"])
        self.assertRegex(data["dmf_promsd_image_tag"], r"^[0-9]+\.[0-9]+\.[0-9]+$")

    def test_the_digest_is_a_sha256_index_digest(self):
        self.assertRegex(str(_pin().get("dmf_promsd_image_digest", "")), DIGEST)

    def test_the_pin_is_defined_in_one_file(self):
        key = re.compile(r"^\s*dmf_promsd_image_(tag|digest)\s*:", re.MULTILINE)
        definers = sorted(str(p.relative_to(ROOT)) for p in ROOT.rglob("*")
                          if p.is_file() and p.suffix in SCANNED
                          and not SKIPPED_DIRS & set(p.relative_to(ROOT).parts)
                          and key.search(p.read_text(encoding="utf-8", errors="replace")))
        self.assertEqual(definers, ["vars/dmf-promsd.yml"])

    def test_each_consumer_play_loads_the_pin_file(self):
        self.assertIn("{{ playbook_dir }}/../vars/dmf-promsd.yml", _play(PLAY_630)["vars_files"])
        self.assertIn("{{ playbook_dir }}/../../vars/dmf-promsd.yml", _play(PLAY_160)["vars_files"])


class SeedTest(unittest.TestCase):
    def test_630_copies_the_whole_index_by_the_pinned_digest(self):
        _, entry = _seed_entry()
        self.assertEqual(entry.get("source_digest"), "{{ dmf_promsd_image_digest }}")
        self.assertIs(entry.get("all_platforms"), True)
        self.assertEqual(entry["tag"], "{{ dmf_promsd_image_tag }}")

    def test_630_renders_the_pinned_digest_as_the_copy_source(self):
        play, entry = _seed_entry()
        files = [f.replace("{{ playbook_dir }}", str(PLAY_630.parent)) for f in play["vars_files"]]
        got = _render(files, play["vars"], {"digest": entry["source_digest"], "tag": entry["tag"]})
        self.assertEqual(got, {"digest": _pin()["dmf_promsd_image_digest"], "tag": _pin()["dmf_promsd_image_tag"]})
        self.assertRegex(got["digest"], DIGEST)


class DeploymentTest(unittest.TestCase):
    def test_the_role_defines_neither_tag_nor_digest(self):
        defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text(encoding="utf-8"))
        self.assertNotIn("dmf_promsd_image_tag", defaults)
        self.assertNotIn("dmf_promsd_image_digest", defaults)

    def test_the_deployment_names_the_image_by_tag_and_digest(self):
        self.assertEqual(
            _deployment_image(),
            "{{ dmf_promsd_image_repository }}:{{ dmf_promsd_image_tag }}@{{ dmf_promsd_image_digest }}")

    def test_the_rendered_deployment_image_carries_the_digest(self):
        play = _play(PLAY_160)
        files = [str(ROLE / "defaults/main.yml")] + [
            f.replace("{{ playbook_dir }}", str(PLAY_160.parent)) for f in play["vars_files"]]
        got = _render(files, {"cert_manager_cluster_domain": "lab.test"},
                      {"image": _deployment_image()})["image"]
        pin = _pin()
        self.assertEqual(
            got,
            f"registry.lab.test/dmf-promsd:{pin['dmf_promsd_image_tag']}@{pin['dmf_promsd_image_digest']}")


if __name__ == "__main__":
    unittest.main()
