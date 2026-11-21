"""Check that every alert links to a section that exists in the runbook page.

Each alert in the Prometheus role sets `runbook_url` to the alerts page of
dmf-runbooks plus an anchor. The test reads the rule template as text, finds
every alert rule and requires its `runbook_url` to name a heading of the page,
using GitHub's rules for heading anchors. Watchdog and AWXReplicaDivergence are
the named exceptions. Nothing renders and no task runs.

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
ALERT_LINE = re.compile(r"^\s*- alert:\s*(\S+)\s*$", re.M)
RUNBOOK_LINE = re.compile(r"^\s*runbook_url:\s*\"([^\"]*)\"\s*$", re.M)
BASE_EXPRESSION = "{{ prometheus_alert_runbook_base }}"

# Alerts that do not link to a section of the dmf-runbooks page. The test pins
# the exact expected state of each, so an exception cannot hide drift.
# Watchdog fires all the time and has no runbook, by design.
# AWXReplicaDivergence has its own runbook in the umbrella repository.
AWX_RUNBOOK_EXPRESSION = "{{ prometheus_alert_awx_cr_divergence_runbook_url }}"
AWX_RUNBOOK_URL = (
    "https://github.com/dmfdeploy/dmfdeploy/blob/main/docs/runbooks/awx-operator-restart.md"
)
NO_RUNBOOK = {"Watchdog"}
OTHER_RUNBOOK = {"AWXReplicaDivergence": AWX_RUNBOOK_EXPRESSION}
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


def alert_rules(text):
    """Return (name, runbook_url or None) for every alert rule, in file order.

    A rule starts at its `- alert:` line and ends at the next rule, group or
    `if` or `endif` line. The runbook URL is the `runbook_url` line that follows
    the rule's `annotations:` line. Two rules may share a name, and both count.
    """
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if ALERT_LINE.match(line)]
    rules = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        block = []
        for line in lines[start:end]:
            if block and (line.lstrip().startswith(("{% if", "{% endif", "- name:"))):
                break
            block.append(line)
        body = "\n".join(block)
        name = ALERT_LINE.match(block[0]).group(1)
        urls = RUNBOOK_LINE.findall(body)
        if len(urls) > 1:
            raise AssertionError(f"{name}: more than one runbook_url")
        if urls and "annotations:" not in body.split("runbook_url:")[0]:
            raise AssertionError(f"{name}: runbook_url is not under annotations")
        rules.append((name, urls[0] if urls else None))
    return rules


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

    def test_every_alert_rule_is_parsed(self):
        text = TEMPLATE.read_text()
        rules = alert_rules(text)
        self.assertGreater(len(rules), 0, "no alert rules found")
        self.assertEqual(len(rules), len(ALERT_LINE.findall(text)))

    def test_every_alert_rule_links_to_a_section(self):
        headings = set(heading_anchors(load_page()))
        problems = []
        for name, url in alert_rules(TEMPLATE.read_text()):
            if name in NO_RUNBOOK:
                if url is not None:
                    problems.append(f"{name}: expected no runbook_url, found {url}")
            elif name in OTHER_RUNBOOK:
                if url != OTHER_RUNBOOK[name]:
                    problems.append(f"{name}: expected {OTHER_RUNBOOK[name]}, found {url}")
            elif url is None:
                problems.append(f"{name}: no runbook_url")
            elif not url.startswith(BASE_EXPRESSION + "#"):
                problems.append(f"{name}: runbook_url is not the alerts page plus an anchor: {url}")
            else:
                anchor = url[len(BASE_EXPRESSION) + 1 :]
                if anchor not in headings:
                    problems.append(f"{name}: no heading for anchor {anchor}")
        self.assertEqual(problems, [])

    def test_awx_exception_url_is_the_umbrella_runbook(self):
        match = re.search(
            r'^prometheus_alert_awx_cr_divergence_runbook_url:\s*"([^"]+)"',
            DEFAULTS.read_text(),
            re.M,
        )
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), AWX_RUNBOOK_URL)

    def test_rule_parser_reads_alerts_and_skips_jinja_lines(self):
        text = (
            "groups:\n  - name: g\n    rules:\n"
            "      - alert: A\n        annotations:\n"
            '          runbook_url: "{{ prometheus_alert_runbook_base }}#a"\n'
            "{% if x %}\n"
            "      - alert: B\n        annotations:\n          summary: s\n"
            "{% endif %}\n"
        )
        self.assertEqual(
            alert_rules(text), [("A", "{{ prometheus_alert_runbook_base }}#a"), ("B", None)]
        )


if __name__ == "__main__":
    unittest.main()
