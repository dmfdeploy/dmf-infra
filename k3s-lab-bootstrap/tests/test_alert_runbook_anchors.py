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

import yaml

ROLE = Path(__file__).resolve().parents[1] / "roles/base/prometheus"
TEMPLATE = ROLE / "templates/rules/alerts.yaml.j2"
DEFAULTS = ROLE / "defaults/main.yml"
PAGE_URL = "https://raw.githubusercontent.com/dmfdeploy/dmf-runbooks/main/docs/alerts.md"
PAGE_ENV = "DMF_RUNBOOKS_ALERTS_PAGE"

ANCHOR = re.compile(r"prometheus_alert_runbook_base\s*}}#([A-Za-z0-9_-]+)")
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


def load_rules(text):
    """Return the alert rules of the template, parsed as YAML, and the Jinja expressions.

    The template is YAML with a little Jinja. Each `{{ ... }}` expression becomes
    a numbered placeholder. The `if` and `endif` lines around the Alertmanager rules and
    the `raw` tags are dropped, so the rules inside count. Any other `{% %}` tag
    fails the test, because the placeholder approach would then be wrong.
    """
    text = re.sub(r"^[ \t]*\{%-?\s*(if|endif)\b.*%\}[ \t]*\n", "", text, flags=re.M)
    text = re.sub(r"\{%-?\s*(raw|endraw)\s*\+?-?%\}", "", text)
    leftover = re.findall(r"\{%.*?%\}", text)
    if leftover:
        raise AssertionError(f"Jinja blocks the test does not handle: {leftover}")
    expressions = []

    def placeholder(match):
        expressions.append("{{ " + match.group(0)[2:-2].strip() + " }}")
        return f"JINJA{len(expressions) - 1}END"

    text = re.sub(r"\{\{.*?\}\}", placeholder, text)
    document = yaml.safe_load(text)
    rules = []
    for group in document["groups"]:
        for rule in group["rules"]:
            if "alert" in rule:
                rules.append(rule)
    return rules, expressions


def count_alert_declarations(text):
    """Count lines that declare an `alert:` key, with a deliberately loose scan.

    The scan does not share the parser's logic. A line counts when `alert:` is
    its first token, with or without a leading `- `, so a rule that lists the key
    after other keys counts too. A trailing comment does not matter.
    """
    return len(re.findall(r"^\s*(?:-\s+)?[\"']?alert[\"']?\s*:", text, flags=re.M))


def alert_rules(text):
    """Return (name, runbook_url or None) for every alert rule, in file order.

    The URL placeholders keep the `{{ name }}` form of the template, so a rule
    compares equal to what the template says.
    """
    parsed, expressions = load_rules(text)
    rules = []
    for rule in parsed:
        url = (rule.get("annotations") or {}).get("runbook_url")
        if url is not None:
            url = re.sub(r"JINJA(\d+)END", lambda m: expressions[int(m.group(1))], url)
        rules.append((rule["alert"], url))
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
        self.assertEqual(len(rules), count_alert_declarations(text))

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

    def parse(self, rules_yaml):
        return alert_rules("groups:\n  - name: g\n    rules:\n" + rules_yaml)

    def test_rule_parser_reads_alerts_and_keeps_both_alertmanager_branches(self):
        text = (
            "      - alert: A\n        annotations:\n"
            '          runbook_url: "{{ prometheus_alert_runbook_base }}#a"\n'
            "{% if x %}\n"
            "      - alert: B\n        annotations:\n          summary: s\n"
            "{% endif %}\n"
        )
        self.assertEqual(
            self.parse(text), [("A", "{{ prometheus_alert_runbook_base }}#a"), ("B", None)]
        )

    def test_rule_with_trailing_comment_is_found(self):
        text = "      - alert: NewAlert # explanation\n        annotations:\n          summary: s\n"
        self.assertEqual(self.parse(text), [("NewAlert", None)])
        self.assertEqual(count_alert_declarations(text), 1)

    def test_rule_with_quoted_name_is_found(self):
        text = '      - alert: "Quoted"\n        annotations:\n          summary: s\n'
        self.assertEqual(self.parse(text), [("Quoted", None)])

    def test_rule_with_alert_key_not_first_is_found(self):
        text = "      - expr: vector(1)\n        alert: Late\n        annotations:\n          summary: s\n"
        self.assertEqual(self.parse(text), [("Late", None)])
        self.assertEqual(count_alert_declarations(text), 1)

    def test_unhandled_jinja_block_fails(self):
        with self.assertRaises(AssertionError):
            self.parse("{% for x in y %}\n      - alert: A\n{% endfor %}\n")

    def test_loose_count_disagrees_when_the_parser_misses_a_rule(self):
        text = "      - alert: A\n        annotations: {}\n"
        self.assertEqual(count_alert_declarations(text), len(self.parse(text)))
        self.assertNotEqual(count_alert_declarations(text + "      - alert: B\n"), 1)

if __name__ == "__main__":
    unittest.main()
