"""A name the `hosts` plugin does not answer must reach a `forward` to the
node's resolvers in the base-domain server block of the coredns-custom
ConfigMap (rendered by 321-local-ca-trust). Without it, CoreDNS answers
SERVFAIL for such names."""
import pathlib
import re
import unittest

import yaml

PLAYBOOK = pathlib.Path(__file__).resolve().parent.parent / "playbooks" / "321-local-ca-trust.yml"
HEADER = re.compile(r"^\{\{ _base_domain \}\}:53 \{\s*$")
TEMPLATE_EXPR = re.compile(r"\{\{.*?\}\}")


def _server_value():
    plays = yaml.safe_load(PLAYBOOK.read_text())
    for task in plays[0]["tasks"]:
        definition = task.get("kubernetes.core.k8s", {}).get("definition", {})
        if definition.get("metadata", {}).get("name") == "coredns-custom":
            return definition["data"]["dmf-local.server"]
    raise AssertionError("no coredns-custom task in the playbook")


def _delta(line):
    """Net brace depth change of a line, ignoring Jinja `{{ ... }}` expressions."""
    line = TEMPLATE_EXPR.sub("", line)
    return line.count("{") - line.count("}")


def _base_domain_block(value):
    """Return the lines between the base-domain server block's braces."""
    lines = value.splitlines()
    start = next((i for i, ln in enumerate(lines) if HEADER.match(ln)), None)
    if start is None:
        raise AssertionError("no `{{ _base_domain }}:53 {` server block")
    depth = 1
    for i in range(start + 1, len(lines)):
        depth += _delta(lines[i])
        if depth == 0:
            return lines[start + 1:i]
    raise AssertionError("base-domain server block is not closed")


def _depths(lines):
    """Brace depth before each line, relative to the inside of the server block.

    The list has one extra entry: the depth after the last line.
    """
    depth, before = 1, []
    for ln in lines:
        before.append(depth)
        depth += _delta(ln)
    before.append(depth)
    return before


def _hosts_block(lines):
    """Return (start, end) indexes of the `hosts {` block at server-block depth."""
    depths = _depths(lines)
    start = next(
        (i for i, ln in enumerate(lines)
         if depths[i] == 1 and re.match(r"\s*hosts\s*\{\s*$", ln)),
        None,
    )
    if start is None:
        raise AssertionError("no hosts block directly inside the server block")
    for i in range(start + 1, len(lines) + 1):
        if depths[i] == 1:
            return start, i - 1
    raise AssertionError("hosts block is not closed")


class CorednsCustomZoneTest(unittest.TestCase):
    def setUp(self):
        self.value = _server_value()
        self.lines = _base_domain_block(self.value)

    def test_hosts_block_falls_through(self):
        start, end = _hosts_block(self.lines)
        body = self.lines[start + 1:end + 1]
        self.assertTrue(any(ln.strip() == "fallthrough" for ln in body))

    def test_forward_to_node_resolvers_follows_hosts(self):
        _, end = _hosts_block(self.lines)
        depths = _depths(self.lines)
        # Depth 1: directly inside the server block, outside `hosts`.
        forwards = [
            ln.split() for i, ln in enumerate(self.lines)
            if i > end and depths[i] == 1 and ln.split()[:1] == ["forward"]
        ]
        self.assertIn(["forward", ".", "/etc/resolv.conf"], forwards)

    def test_braces_balance(self):
        stripped = TEMPLATE_EXPR.sub("", self.value)
        self.assertEqual(stripped.count("{"), stripped.count("}"))


if __name__ == "__main__":
    unittest.main()
