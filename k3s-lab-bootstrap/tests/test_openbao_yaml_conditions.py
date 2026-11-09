"""A condition written as an unquoted YAML plain scalar that holds a colon and a space
parses as a mapping, and Ansible then refuses the task. This test parses every YAML
file under k3s-lab-bootstrap and fails on a that, when, failed_when or changed_when
entry that is a mapping."""

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONDITION_KEYS = {"that", "when", "failed_when", "changed_when"}


def mappings_used_as_conditions(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key in CONDITION_KEYS:
                for entry in value if isinstance(value, list) else [value]:
                    if isinstance(entry, dict):
                        yield key, entry
            yield from mappings_used_as_conditions(value)
    elif isinstance(node, list):
        for value in node:
            yield from mappings_used_as_conditions(value)


class ConditionQuotingTests(unittest.TestCase):
    def test_a_condition_with_a_colon_is_not_parsed_as_a_mapping(self):
        found = []
        for path in sorted(ROOT.rglob("*.yml")):
            for document in yaml.safe_load_all(path.read_text(encoding="utf-8")):
                for key, entry in mappings_used_as_conditions(document):
                    found.append(f"{path.relative_to(ROOT)}: {key}: {entry}")
        self.assertEqual(found, [], "quote these conditions")

    def test_the_check_sees_an_unquoted_condition(self):
        document = yaml.safe_load("- ansible.builtin.assert:\n    that:\n      - item.stderr is search('Code: 403')\n")
        self.assertEqual(len(list(mappings_used_as_conditions(document))), 1)


if __name__ == "__main__":
    unittest.main()
