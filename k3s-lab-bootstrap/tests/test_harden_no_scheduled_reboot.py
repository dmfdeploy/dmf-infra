"""The harden role's unattended-upgrades template turns automatic reboot off.

This checks the role's template text only. It does not cover a reboot started
by anything else, a node installed before the template changed, or another APT
configuration file that overrides the template.

The check removes APT comments (`//` or `#` to the end of the line, and
`/* ... */` blocks), then reads every `Automatic-Reboot` assignment in the
rest of the text, wherever it sits on a line. The template has no string that
contains `//`, so the simple comment removal is safe for it.
"""

import re
import unittest
from pathlib import Path

TEMPLATE = Path(__file__).resolve().parents[1] / "roles/base/harden/templates/50unattended-upgrades.j2"

# The name must be followed by whitespace and a quoted value, so
# Automatic-Reboot-Time and Automatic-Reboot-WithUsers never match.
ASSIGNMENT = re.compile(r'(?<![\w:-])Unattended-Upgrade::Automatic-Reboot\s+"([^"]*)"')


def active_text(text):
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"(//|#).*", "", text)


class HardenTemplateTest(unittest.TestCase):
    def test_every_automatic_reboot_assignment_is_false(self):
        values = ASSIGNMENT.findall(active_text(TEMPLATE.read_text()))
        self.assertTrue(values, "the template sets no Automatic-Reboot value")
        self.assertEqual(set(values), {"false"}, values)

    def test_reboot_time_and_with_users_are_not_matched(self):
        text = (
            'Unattended-Upgrade::Automatic-Reboot-Time "02:00";\n'
            'Unattended-Upgrade::Automatic-Reboot-WithUsers "true";\n'
        )
        self.assertEqual(ASSIGNMENT.findall(active_text(text)), [])


if __name__ == "__main__":
    unittest.main()
