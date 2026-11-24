"""The execution environment's ansible-core version is set in one file,
requirements-ansible-core.txt. scripts/build.sh checks it and renders it into a
copy of the spec, so the checked-in spec keeps a placeholder."""
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
PIN_FILE = ROOT / "requirements-ansible-core.txt"
EE = ROOT / "ee"
SPEC = EE / "execution-environment.yml"
BUILD = EE / "scripts" / "build.sh"
PLACEHOLDER = "__ANSIBLE_CORE_PIN__"
PIN = re.compile(r"^ansible-core==[0-9]+\.[0-9]+\.[0-9]+$")


def _run(*args):
    return subprocess.run(["bash", str(BUILD), *args], capture_output=True, text=True)


def _pin_file(directory, content):
    path = pathlib.Path(directory) / "pin.txt"
    path.write_text(content, encoding="utf-8")
    return path


def _package_pip(spec_text):
    match = re.search(r"ansible_core:\s*\n(?:\s*#.*\n)*\s*package_pip:\s*(\S+)", spec_text)
    return match.group(1) if match else None


class PinFileTest(unittest.TestCase):
    def test_holds_exactly_one_valid_line(self):
        lines = PIN_FILE.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], PIN.pattern)

    def test_checked_in_spec_holds_the_placeholder(self):
        text = SPEC.read_text(encoding="utf-8")
        self.assertEqual(text.count(PLACEHOLDER), 1)
        self.assertEqual(_package_pip(text), PLACEHOLDER)
        self.assertNotRegex(text, r"ansible-core==")


class BuildScriptPinTest(unittest.TestCase):
    def test_accepts_the_checked_in_pin(self):
        result = _run("--check-pin", str(PIN_FILE))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), PIN_FILE.read_text().strip())

    def test_rejects_malformed_pins_and_extra_lines(self):
        cases = {
            "range": "ansible-core>=2.21\n",
            "no patch": "ansible-core==2.21\n",
            "suffix": "ansible-core==2.21.6rc1\n",
            "other package": "ansible==14.6.0\n",
            "two lines": "ansible-core==2.21.6\nansible-core==2.21.5\n",
            "comment line": "# core\nansible-core==2.21.6\n",
            "empty": "",
        }
        for name, content in cases.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                result = _run("--check-pin", str(_pin_file(tmp, content)))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("ERROR", result.stderr)

    def test_rejects_a_missing_pin_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertNotEqual(_run("--check-pin", f"{tmp}/none.txt").returncode, 0)


class VersionTest(unittest.TestCase):
    def test_publish_default_tag_is_the_build_default(self):
        result = _run("--print-version")
        self.assertEqual(result.returncode, 0, result.stderr)
        version = result.stdout.strip()
        self.assertRegex(version, r"^[0-9]+\.[0-9]+\.[0-9]+$")
        publish = (EE / "scripts" / "publish-to-ghcr.sh").read_text(encoding="utf-8")
        self.assertIn('build.sh" --print-version', publish)
        self.assertNotRegex(publish, r"IMAGE_TAG:-[0-9]")


class BuildRefusalTest(unittest.TestCase):
    """build.sh refuses before it looks for Docker or ansible-builder."""

    def _build(self, script, env_extra=None):
        env = {k: v for k, v in os.environ.items() if k not in ("IMAGE_VERSION", "LOCAL_TAG")}
        env.update(env_extra or {})
        return subprocess.run(["bash", str(script)], capture_output=True, text=True, env=env)

    def test_refuses_an_image_version_or_local_tag_from_the_environment(self):
        for name, value in {"IMAGE_VERSION": "9.9.9", "LOCAL_TAG": "example.invalid/x:1"}.items():
            with self.subTest(name):
                result = self._build(BUILD, {name: value})
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"{name} is set in the environment", result.stderr)
                self.assertNotIn("=== Building", result.stdout)

    def test_print_version_ignores_the_environment(self):
        default = _run("--print-version").stdout.strip()
        env = {**os.environ, "IMAGE_VERSION": "9.9.9"}
        result = subprocess.run(
            ["bash", str(BUILD), "--print-version"], capture_output=True, text=True, env=env
        )
        self.assertEqual(result.stdout.strip(), default)

    def test_refuses_uncommitted_changes_under_ee_or_in_the_pin_file(self):
        git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
        edits = {
            "ee file": lambda root: (root / "k3s-lab-bootstrap/ee/bindep.txt").write_text(
                (root / "k3s-lab-bootstrap/ee/bindep.txt").read_text() + "# x\n"
            ),
            "untracked ee file": lambda root: (root / "k3s-lab-bootstrap/ee/new.txt").write_text("x"),
            "pin file": lambda root: (root / "k3s-lab-bootstrap/requirements-ansible-core.txt").write_text(
                "ansible-core==2.21.7\n"
            ),
        }
        for name, edit in edits.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                root = pathlib.Path(tmp)
                (root / "k3s-lab-bootstrap").mkdir()
                shutil.copytree(EE, root / "k3s-lab-bootstrap/ee", ignore=shutil.ignore_patterns("context"))
                shutil.copy(PIN_FILE, root / "k3s-lab-bootstrap")
                subprocess.run([*git, "init", "-q"], cwd=root, check=True)
                subprocess.run([*git, "add", "."], cwd=root, check=True)
                subprocess.run([*git, "commit", "-q", "-m", "x"], cwd=root, check=True)
                edit(root)
                result = self._build(root / "k3s-lab-bootstrap/ee/scripts/build.sh")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("uncommitted changes", result.stderr)


class RenderTest(unittest.TestCase):
    def _render(self, tmp, pin_content, spec_text=None):
        ee = pathlib.Path(tmp) / "ee"
        ee.mkdir()
        for name in ("requirements.yml", "requirements.txt", "bindep.txt"):
            (ee / name).write_text((EE / name).read_text(encoding="utf-8"), encoding="utf-8")
        (ee / "execution-environment.yml").write_text(
            SPEC.read_text(encoding="utf-8") if spec_text is None else spec_text, encoding="utf-8")
        dest = pathlib.Path(tmp) / "out"
        result = _run("--render", str(_pin_file(tmp, pin_content)), str(ee), str(dest))
        return result, ee, dest

    def test_only_the_pin_file_changes_the_rendered_value(self):
        rendered = {}
        for version in ("2.21.6", "2.21.5"):
            with tempfile.TemporaryDirectory() as tmp:
                result, ee, dest = self._render(tmp, f"ansible-core=={version}\n")
                self.assertEqual(result.returncode, 0, result.stderr)
                rendered[version] = (dest / "execution-environment.yml").read_text(encoding="utf-8")
                self.assertEqual(_package_pip(rendered[version]), f"ansible-core=={version}")
                self.assertIn(PLACEHOLDER, (ee / "execution-environment.yml").read_text())
                for name in ("requirements.yml", "requirements.txt", "bindep.txt"):
                    self.assertTrue((dest / name).is_file(), name)
        self.assertEqual(rendered["2.21.6"].replace("2.21.6", "X"),
                         rendered["2.21.5"].replace("2.21.5", "X"))

    def test_stages_only_the_files_the_spec_references(self):
        referenced = {"execution-environment.yml"}
        for key in ("galaxy", "python", "system"):
            match = re.search(rf"^  {key}:\s*(\S+)\s*$", SPEC.read_text(encoding="utf-8"), re.M)
            self.assertIsNotNone(match, key)
            referenced.add(match.group(1))
        with tempfile.TemporaryDirectory() as tmp:
            result, ee, dest = self._render(tmp, "ansible-core==2.21.6\n")
            (ee / "stray-untracked.txt").write_text("x", encoding="utf-8")
            (ee / "context").mkdir()
            (ee / "context" / "stale").write_text("x", encoding="utf-8")
            (ee / "scripts").mkdir()
            shutil.rmtree(dest)
            result = _run("--render", str(pathlib.Path(tmp) / "pin.txt"), str(ee), str(dest))
            self.assertEqual(result.returncode, 0, result.stderr)
            staged = {p.name for p in dest.rglob("*")}
            self.assertEqual(staged, referenced)
            self.assertEqual({p.name for p in dest.iterdir()}, referenced)

    def test_rejects_a_missing_input_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, ee, dest = self._render(tmp, "ansible-core==2.21.6\n")
            shutil.rmtree(dest)
            (ee / "bindep.txt").unlink()
            result = _run("--render", str(pathlib.Path(tmp) / "pin.txt"), str(ee), str(dest))
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(dest.exists())

    def test_rejects_a_malformed_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            result, _, dest = self._render(tmp, "ansible-core>=2.21\n")
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(dest.exists())

    def test_rejects_a_spec_without_the_placeholder(self):
        spec = SPEC.read_text(encoding="utf-8")
        for name, text in {
            "filled by hand": spec.replace(PLACEHOLDER, "ansible-core==2.21.6"),
            "removed": spec.replace(PLACEHOLDER, ""),
            "twice": spec + f"\n# {PLACEHOLDER}\n",
        }.items():
            with self.subTest(name), tempfile.TemporaryDirectory() as tmp:
                result, _, dest = self._render(tmp, "ansible-core==2.21.6\n", text)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("exactly once", result.stderr)
                self.assertFalse(dest.exists())


if __name__ == "__main__":
    unittest.main()
