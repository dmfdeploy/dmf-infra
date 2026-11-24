"""The AWX execution environment's image tag is set once, as awx_ee_image_tag
in vars/awx-ee.yml. Playbooks 050, 630 and 693 load that file with vars_files,
and `-e awx_ee_image_tag=<tag>` overrides it.

Static checks: the old names are gone from every inventory, group_vars,
host_vars, play, role and template; the new name is defined in one file only;
each consumer play loads the file; the awx-integration role builds the image
reference once (`awx_ee_catalog_ref`) and each of its three task sites uses that
variable and nothing else; the ansible-runner role builds `ansible_runner_image`
once from the tag.

Executable checks, run with ansible-playbook on localhost: for each play, a
derived play loads the real play's `vars_files` and `vars` after the defaults of
the role the play runs (role defaults rank below `vars_files`), and renders the
value the consumer receives. For 693 that is the real expression at each of the
three task sites. The checks run with and without `-e awx_ee_image_tag=...`.
They render the values; they do not run the role's tasks, so they do not prove
what AWX or Zot do with the value. Nothing contacts a cluster or a registry."""
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
TAG_FILE = ROOT / "vars" / "awx-ee.yml"
OLD_NAMES = ("awx_ee_catalog_tag", "ansible_runner_image_tag")
OVERRIDE = "9.9.9-test"
SCANNED = {".yml", ".yaml", ".ini", ".j2", ".json", ".cfg"}
SKIPPED_DIRS = {".git", ".ansible", "node_modules", "__pycache__"}
THIS_FILE = pathlib.Path(__file__).resolve()


def _files():
    for path in ROOT.rglob("*"):
        if (path.is_file() and path.suffix in SCANNED and path != THIS_FILE
                and not SKIPPED_DIRS & set(path.relative_to(ROOT).parts)):
            yield path


def _default_tag():
    return yaml.safe_load(TAG_FILE.read_text(encoding="utf-8"))["awx_ee_image_tag"]


def _play(playbook):
    return yaml.safe_load((PLAYBOOKS / playbook).read_text(encoding="utf-8"))[0]


def _real_vars_files(play):
    """The play's own vars_files, with {{ playbook_dir }} pointed at the real directory."""
    return [entry.replace("{{ playbook_dir }}", str(PLAYBOOKS)) for entry in play.get("vars_files", [])]


class SingleDefinitionTest(unittest.TestCase):
    def test_the_tag_file_holds_one_semver_value(self):
        data = yaml.safe_load(TAG_FILE.read_text(encoding="utf-8"))
        self.assertEqual(list(data), ["awx_ee_image_tag"])
        self.assertRegex(data["awx_ee_image_tag"], r"^[0-9]+\.[0-9]+\.[0-9]+$")

    def test_the_old_variable_names_are_set_nowhere(self):
        hits = [f"{path.relative_to(ROOT)} names {name}"
                for path in _files() for name in OLD_NAMES
                if name in path.read_text(encoding="utf-8", errors="replace")]
        self.assertEqual(hits, [])

    def test_the_new_variable_is_defined_in_one_file(self):
        key = re.compile(r"^\s*awx_ee_image_tag\s*:", re.MULTILINE)
        definers = [str(path.relative_to(ROOT)) for path in _files()
                    if path.suffix in {".yml", ".yaml", ".ini", ".json"}
                    and key.search(path.read_text(encoding="utf-8", errors="replace"))]
        self.assertEqual(definers, ["vars/awx-ee.yml"])

    def test_each_consumer_play_loads_the_tag_file(self):
        for playbook in ("050-ansible-runner.yml", "630-zot-seed-platform.yml", "693-awx-integration.yml"):
            with self.subTest(playbook=playbook):
                files = _play(playbook).get("vars_files", [])
                self.assertIn("{{ playbook_dir }}/../vars/awx-ee.yml", files)


class ConsumerSitesTest(unittest.TestCase):
    def test_awx_integration_builds_the_reference_once_and_uses_it_at_every_site(self):
        sites = _awx_integration_sites()
        self.assertEqual(len(sites), 3, sites)
        for where, line in sites:
            with self.subTest(site=where):
                self.assertEqual(line, f"image: {AWX_REF}")

    def test_awx_integration_defaults_define_the_reference_from_the_tag(self):
        defaults = yaml.safe_load((AWX_INTEGRATION / "defaults/main.yml").read_text(encoding="utf-8"))
        self.assertEqual(defaults["awx_ee_catalog_ref"], "{{ awx_ee_catalog_image }}:{{ awx_ee_image_tag }}")

    def test_ansible_runner_builds_its_image_from_the_tag(self):
        defaults = yaml.safe_load(
            (ROOT / "roles/stack/operator/ansible-runner/defaults/main.yml").read_text(encoding="utf-8"))
        self.assertEqual(defaults["ansible_runner_image"],
                         "registry.dmf.example.com/dmf/awx-ee:{{ awx_ee_image_tag }}")


def _ansible_playbook():
    beside_python = pathlib.Path(sys.executable).parent / "ansible-playbook"
    if beside_python.exists():
        return str(beside_python)
    found = shutil.which("ansible-playbook")
    if found is None:
        raise unittest.SkipTest("ansible-playbook is not installed")
    return found


def _resolve(vars_files, play_vars, expression, override):
    """Render `expression` in a play with these vars_files and vars, as ansible-playbook does."""
    with tempfile.TemporaryDirectory() as directory:
        work = pathlib.Path(directory)
        out = work / "value.txt"
        play = {
            "name": "resolve the image value",
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars_files": vars_files,
            "tasks": [{"ansible.builtin.copy": {"content": expression, "dest": str(out)}}],
        }
        if play_vars:
            play["vars"] = play_vars
        playbook = work / "playbook.yml"
        playbook.write_text(yaml.safe_dump([play], sort_keys=False), encoding="utf-8")
        extra = {"ansible_python_interpreter": sys.executable}
        if override is not None:
            extra["awx_ee_image_tag"] = override
        env = dict(os.environ, ANSIBLE_HOME=str(work / "home"), ANSIBLE_LOCAL_TEMP=str(work / "tmp"),
                   ANSIBLE_NOCOLOR="1", ANSIBLE_CONFIG=str(work / "ansible.cfg"))
        (work / "ansible.cfg").write_text("[defaults]\n", encoding="utf-8")
        result = subprocess.run(
            [_ansible_playbook(), "-i", "localhost,", "-e", json.dumps(extra), str(playbook)],
            cwd=work, env=env, capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise AssertionError("ansible-playbook failed\n" + result.stdout + result.stderr)
        return out.read_text(encoding="utf-8")


AWX_INTEGRATION = ROOT / "roles/stack/operator/awx-integration"
AWX_REF = '"{{ awx_ee_catalog_ref }}"'
# Any text in a task that builds the EE image reference: a variable that carries
# it, or a literal repository and tag.
EE_IMAGE_TEXT = re.compile(r"awx_ee_(image_tag|catalog_image|catalog_ref)|awx-ee:[0-9A-Za-z]")


def _awx_integration_sites():
    """The task lines of the awx-integration role that build the EE image, as written."""
    sites = []
    for path in sorted((AWX_INTEGRATION / "tasks").rglob("*.yml")):
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if EE_IMAGE_TEXT.search(line):
                sites.append((f"{path.relative_to(ROOT)}:{number}", line.strip()))
    return sites


def _case_630():
    play = _play("630-zot-seed-platform.yml")
    entry = next(item for item in play["vars"]["zot_seed_images"] if item["name"] == "awx-ee")
    return _real_vars_files(play), play["vars"], entry["tag"], lambda tag: tag


def _case_693(expression):
    play = _play("693-awx-integration.yml")
    defaults = str(AWX_INTEGRATION / "defaults/main.yml")
    # Role defaults sit below the play's own vars_files, so they come first.
    return ([defaults, *_real_vars_files(play)], None, expression,
            lambda tag: f"zot.zot.svc.cluster.local:5000/dmf/awx-ee:{tag}")


def _case_050():
    play = _play("050-ansible-runner.yml")
    defaults = ROOT / "roles/stack/operator/ansible-runner/defaults/main.yml"
    expression = yaml.safe_load(defaults.read_text(encoding="utf-8"))["ansible_runner_image"]
    return ([str(defaults), *_real_vars_files(play)], None, expression,
            lambda tag: f"registry.dmf.example.com/dmf/awx-ee:{tag}")


class PlaysResolveTheTagTest(unittest.TestCase):
    def _check(self, case):
        vars_files, play_vars, expression, expected = case
        self.assertEqual(_resolve(vars_files, play_vars, expression, None), expected(_default_tag()))
        self.assertEqual(_resolve(vars_files, play_vars, expression, OVERRIDE), expected(OVERRIDE))

    def test_630_seeds_zot_with_the_tag(self):
        self._check(_case_630())

    def test_693_registers_the_ee_with_the_tag_at_every_site(self):
        for where, line in _awx_integration_sites():
            with self.subTest(site=where):
                self._check(_case_693(yaml.safe_load(line)["image"]))

    def test_050_names_the_runner_image_with_the_tag(self):
        self._check(_case_050())


if __name__ == "__main__":
    unittest.main()
