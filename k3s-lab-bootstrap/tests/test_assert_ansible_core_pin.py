"""CI installs ansible-core from requirements-ansible-core.txt and runs
scripts/assert-ansible-core-pin.py to check what was installed. These tests
prove the check fails for a core that is not the pin, so a CI job that
installs the wrong version cannot pass it."""
import importlib.util
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "assert-ansible-core-pin.py"
PIN = "ansible-core==2.21.6\n"
GOOD_CLI = "ansible [core 2.21.6]"

_spec = importlib.util.spec_from_file_location("assert_ansible_core_pin", SCRIPT)
assert_pin = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(assert_pin)


class CheckTest(unittest.TestCase):
    def test_the_pinned_version_passes(self):
        self.assertEqual(assert_pin.check(PIN, "2.21.6", GOOD_CLI), [])

    def test_a_different_pip_version_fails(self):
        # The mutation proof: a core that is not the pin must be refused.
        problems = assert_pin.check(PIN, "2.21.5", GOOD_CLI)
        self.assertEqual(len(problems), 1)
        self.assertIn("2.21.5", problems[0])
        self.assertIn("2.21.6", problems[0])

    def test_a_different_cli_version_fails(self):
        problems = assert_pin.check(PIN, "2.21.6", "ansible [core 2.15.13]")
        self.assertEqual(len(problems), 1)
        self.assertIn("2.15.13", problems[0])

    def test_both_wrong_reports_both(self):
        self.assertEqual(len(assert_pin.check(PIN, "2.15.13", "ansible [core 2.15.13]")), 2)

    def test_a_missing_install_fails(self):
        self.assertEqual(len(assert_pin.check(PIN, None, "")), 2)

    def test_the_metapackage_version_line_does_not_pass(self):
        # `ansible --version` names the core in brackets, never the metapackage.
        self.assertEqual(len(assert_pin.check(PIN, "2.21.6", "ansible 14.6.0")), 1)

    def test_a_bad_pin_file_fails(self):
        for text in ("ansible-core>=2.21\n", "ansible==14.6.0\n", PIN + PIN, ""):
            with self.subTest(text=text):
                self.assertEqual(len(assert_pin.check(text, "2.21.6", GOOD_CLI)), 1)


class ScriptTest(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run([sys.executable, str(SCRIPT), *args], capture_output=True, text=True)

    def test_the_installed_core_is_the_checked_in_pin(self):
        result = self._run()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_pin_that_is_not_installed_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            pin = pathlib.Path(directory) / "pin.txt"
            pin.write_text("ansible-core==2.0.0\n", encoding="utf-8")
            result = self._run("--pin-file", str(pin))
        self.assertEqual(result.returncode, 1)
        self.assertIn("2.0.0", result.stderr)


if __name__ == "__main__":
    unittest.main()
