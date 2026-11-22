"""dmf-init refuses a checkout that does not list the install capabilities it
needs, so the marker file must exist, parse and list the seed capability."""
import pathlib
import re
import unittest

MARKER = pathlib.Path(__file__).resolve().parent.parent / "install-capabilities.txt"
TOKEN = re.compile(r"[a-z0-9-]+")


def _capabilities():
    lines = (line.strip() for line in MARKER.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


class InstallCapabilitiesTest(unittest.TestCase):
    def test_file_exists(self):
        self.assertTrue(MARKER.is_file())

    def test_each_line_is_one_token(self):
        for cap in _capabilities():
            self.assertRegex(cap, TOKEN.pattern)
            self.assertIsNotNone(TOKEN.fullmatch(cap), cap)

    def test_lists_openbao_seed_in_install(self):
        self.assertIn("openbao-seed-in-install", _capabilities())


if __name__ == "__main__":
    unittest.main()
