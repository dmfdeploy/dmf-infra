"""CI installs ee/requirements.yml, the file the AWX execution environment builds
from. requirements.yml serves operators and must list the same collections at
the same versions. Every version is exact, so a new release cannot change what
the image ships or what CI tests."""
import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parent.parent
EXACT = re.compile(r"^\d+\.\d+\.\d+$")


def _collections(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return {c["name"]: str(c.get("version")) for c in data["collections"]}


class CollectionsMatchEeTest(unittest.TestCase):
    def test_requirements_yml_equals_the_ee_file(self):
        self.assertEqual(_collections(ROOT / "requirements.yml"),
                         _collections(ROOT / "ee" / "requirements.yml"))

    def test_collection_versions_are_exact(self):
        for name, version in _collections(ROOT / "ee" / "requirements.yml").items():
            self.assertRegex(version, EXACT, name)

    def test_ee_python_packages_are_exact(self):
        lines = (ROOT / "ee" / "requirements.txt").read_text(encoding="utf-8").splitlines()
        for line in (line.strip() for line in lines):
            if line and not line.startswith("#"):
                self.assertRegex(line, r"^[A-Za-z0-9_.-]+==[0-9][^<>=!~,]*$", line)


if __name__ == "__main__":
    unittest.main()
