"""Check that every alert links to a section that exists in the runbook page.

Each alert in the Prometheus role sets `runbook_url` to the alerts page of
dmf-runbooks plus an anchor. The test reads the rule template as text, collects
the anchors and checks each against the headings of the page, using GitHub's
rules for heading anchors. Nothing renders and no task runs.

The page comes from the main branch of dmf-runbooks over HTTPS. To run offline,
point DMF_RUNBOOKS_ALERTS_PAGE at a local copy of docs/alerts.md.

The anchor rules cover ASCII headings. A repeated heading gets a numeric suffix
on GitHub, which the test does not model: it reports the repeat as an error.
"""
import os
import re
import unittest
import urllib.request
from collections import Counter
from pathlib import Path

ROLE = Path(__file__).resolve().parents[1] / "roles/base/prometheus"
TEMPLATE = ROLE / "templates/rules/alerts.yaml.j2"
DEFAULTS = ROLE / "defaults/main.yml"
PAGE_URL = "https://raw.githubusercontent.com/dmfdeploy/dmf-runbooks/main/docs/alerts.md"
PAGE_ENV = "DMF_RUNBOOKS_ALERTS_PAGE"

ANCHOR = re.compile(r"prometheus_alert_runbook_base\s*}}#([A-Za-z0-9_-]+)")
HEADING = re.compile(r"^#{1,6}[ \t]+(.*?)[ \t]*#*[ \t]*$")


def slug(heading):
    """Return the GitHub anchor of a heading: lowercase, spaces to hyphens, punctuation dropped."""
    lowered = heading.lower()
    kept = re.sub(r"[^a-z0-9 _-]", "", lowered)
    return kept.replace(" ", "-")


def heading_anchors(markdown):
    """Return the anchor of every heading outside fenced code blocks, in order."""
    anchors = []
    fenced = False
    for line in markdown.splitlines():
        if line.startswith(("```", "~~~")):
            fenced = not fenced
            continue
        match = None if fenced else HEADING.match(line)
        if match:
            anchors.append(slug(match.group(1)))
    return anchors


def template_anchors(text):
    """Return the sorted set of runbook anchors the rule template uses."""
    return sorted(set(ANCHOR.findall(text)))


def load_page():
    path = os.environ.get(PAGE_ENV)
    if path:
        return Path(path).read_text(encoding="utf-8")
    with urllib.request.urlopen(PAGE_URL, timeout=30) as response:
        return response.read().decode("utf-8")


class SlugTest(unittest.TestCase):
    def test_lowercases_and_joins_words_with_hyphens(self):
        self.assertEqual(slug("Title Case Heading"), "title-case-heading")

    def test_drops_backticks(self):
        self.assertEqual(slug("`HostMemoryPressure`"), "hostmemorypressure")

    def test_keeps_hyphens_and_underscores(self):
        self.assertEqual(slug("openbao-token-revoker_x"), "openbao-token-revoker_x")

    def test_drops_punctuation(self):
        self.assertEqual(slug("What it is: a test (2024)?"), "what-it-is-a-test-2024")

    def test_headings_in_fenced_code_do_not_count(self):
        page = "# One\n```bash\n# not a heading\n```\n## Two\n"
        self.assertEqual(heading_anchors(page), ["one", "two"])

    def test_closing_hashes_are_not_part_of_the_heading(self):
        self.assertEqual(heading_anchors("## Two ##\n"), ["two"])


class AlertRunbookAnchorsTest(unittest.TestCase):
    def test_runbook_base_is_the_alerts_page_of_dmf_runbooks(self):
        match = re.search(
            r'^prometheus_alert_runbook_base:\s*"([^"]+)"', DEFAULTS.read_text(), re.M
        )
        self.assertIsNotNone(match, "prometheus_alert_runbook_base not found")
        self.assertEqual(
            match.group(1),
            "https://github.com/dmfdeploy/dmf-runbooks/blob/main/docs/alerts.md",
        )

    def test_every_alert_anchor_has_a_heading(self):
        wanted = template_anchors(TEMPLATE.read_text())
        self.assertTrue(wanted, "no runbook anchors found in the rule template")
        headings = heading_anchors(load_page())
        repeats = sorted(a for a, n in Counter(headings).items() if n > 1)
        self.assertEqual(repeats, [], "headings with the same anchor")
        self.assertEqual(
            sorted(set(wanted) - set(headings)),
            [],
            "alert anchors without a heading in the dmf-runbooks alerts page",
        )


if __name__ == "__main__":
    unittest.main()
