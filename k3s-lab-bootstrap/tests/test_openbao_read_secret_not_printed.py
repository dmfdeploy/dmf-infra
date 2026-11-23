"""The playbooks that read a token from OpenBao never print its value.

A `debug` message, a `fail_msg` or a `success_msg` in these playbooks must not
template a variable that holds a read secret, or the text of an error that may echo
one. The message may show whether the value is empty, its length, the exit code, the
HTTP status and whether a flag is set. `no_log` on a task hides its output, but a
message that holds the value would still reach a log where `no_log` is off or
`ANSIBLE_DEBUG` is on, so the value must stay out of it.

The test parses each message with Jinja. A `length` filter or an `is defined` test
makes only the expression it applies to safe, so `{{ secret ~ (other | length) }}`
still shows `secret`.

Run from k3s-lab-bootstrap:
    python3 -m unittest discover -s tests -p 'test_openbao_read_secret_not_printed.py' -v
"""
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import jinja2
import jinja2.nodes as nodes
import yaml

ROOT = Path(__file__).resolve().parents[1]
PLAYBOOKS = [
    "playbooks/696-cms-authentik-api.yml",
    "playbooks/697-cms-awx-token.yml",
    "playbooks/tasks/cms-awx-token-readback.yml",
    "playbooks/698-cms-netbox-forgejo-tokens.yml",
    "playbooks/awx-autoscale-secrets.yml",
]
MESSAGE_KEYS = ("msg", "fail_msg", "success_msg")
# A name that may hold a secret, or the output of a call that handled one.
SECRET_NAME = re.compile(
    r"token|password|passwd|secret|bearer|jwt|openbao_verify|openbao_check|openbao_raw|openbao_write",
    re.I,
)
# A name that holds a state, a name or a place. `secret_id` is not one of them, and neither
# is any name that ends in `_id` alone.
BENIGN_NAME = re.compile(
    r"_(valid|exists|provisioned|recovered|recovered_from_openbao|user|name|path|ttl|url|label|key_name|source|test)$"
    r"|token_user|bearer_key|secret_name|secret_path|remote_path|remote_property",
    re.I,
)
# Attributes and filters that show no part of the value. `stderr` and `stdout` are not here:
# an error can echo a credential.
SAFE_ATTRIBUTES = {"rc", "status", "changed", "skipped", "failed"}
SAFE_FILTERS = {"length", "count"}
SAFE_TESTS = {"defined", "undefined", "none"}
ENVIRONMENT = jinja2.Environment()


def tasks(items):
    for item in items or []:
        if not isinstance(item, dict):
            continue
        yield item
        for key in ("block", "rescue", "always"):
            yield from tasks(item.get(key))


def messages(task):
    for module in ("ansible.builtin.debug", "ansible.builtin.fail", "ansible.builtin.assert"):
        for key in MESSAGE_KEYS:
            value = (task.get(module) or {}).get(key)
            if isinstance(value, str):
                yield value


def printed_secret_names(message):
    """Names of secret-holding variables that a message templates by value."""
    try:
        tree = ENVIRONMENT.parse(message)
    except jinja2.TemplateSyntaxError as error:
        return ["(the message does not parse: %s)" % error.message]
    found = []

    def visit(node, safe):
        if isinstance(node, nodes.Name):
            if not safe and SECRET_NAME.search(node.name) and not BENIGN_NAME.search(node.name):
                found.append(node.name)
            return
        if (
            isinstance(node, nodes.Getattr)
            and not safe
            and node.attr not in SAFE_ATTRIBUTES
            and SECRET_NAME.search(node.attr)
            and not BENIGN_NAME.search(node.attr)
        ):
            found.append(node.attr)
        if isinstance(node, nodes.Filter) and node.name in SAFE_FILTERS:
            safe = True
        elif isinstance(node, nodes.Test) and node.name in SAFE_TESTS:
            safe = True
        elif isinstance(node, nodes.Getattr) and node.attr in SAFE_ATTRIBUTES:
            safe = True
        elif (
            isinstance(node, nodes.Getitem)
            and isinstance(node.arg, nodes.Const)
            and node.arg.value in SAFE_ATTRIBUTES
        ):
            safe = True
        for child in node.iter_child_nodes():
            visit(child, safe)

    visit(tree, False)
    return found


def file_tasks(path):
    """Every task of a playbook, or of a tasks file."""
    document = yaml.safe_load(path.read_text())
    out = []
    for item in document:
        if isinstance(item, dict) and "hosts" in item:
            out += list(tasks(item.get("tasks", []) + item.get("pre_tasks", []) + item.get("post_tasks", [])))
        else:
            out += list(tasks([item]))
    return out


def violations(root):
    found = []
    for rel in PLAYBOOKS:
        for task in file_tasks(root / rel):
            for message in messages(task):
                for name in printed_secret_names(message):
                    found.append((rel, task.get("name"), name))
    return found


class ReadSecretNotPrintedTest(unittest.TestCase):
    def test_no_message_templates_a_secret_value(self):
        self.assertEqual(violations(ROOT), [])

    def test_the_scan_reads_the_messages_of_every_playbook(self):
        counts = {}
        for rel in PLAYBOOKS:
            counts[rel] = sum(1 for task in file_tasks(ROOT / rel) for _ in messages(task))
        self.assertTrue(all(counts.values()), counts)

    def test_the_scan_flags_the_old_awx_debug_in_a_temporary_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            for rel in PLAYBOOKS:
                (tree / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(ROOT / rel, tree / rel)
            target = tree / "playbooks/tasks/cms-awx-token-readback.yml"
            text = target.read_text()
            safe = "      stdout empty: {{ _cms_awx_openbao_verify.stdout | length == 0 }}\n"
            self.assertIn(safe, text)
            planted = text.replace(safe, "      stdout: '{{ _cms_awx_openbao_verify.stdout }}'\n")
            target.write_text(planted)
            self.assertEqual([n for _, _, n in violations(tree)], ["_cms_awx_openbao_verify"])
            # The old stderr line is flagged too.
            safe_err = "      stderr length: {{ _cms_awx_openbao_verify.stderr | length }}\n"
            self.assertIn(safe_err, text)
            target.write_text(text.replace(safe_err, "      stderr: '{{ _cms_awx_openbao_verify.stderr }}'\n"))
            self.assertEqual([n for _, _, n in violations(tree)], ["_cms_awx_openbao_verify"])

    def test_the_scan_flags_a_planted_token_in_each_kind_of_message(self):
        for kind, task in {
            "debug": {"ansible.builtin.debug": {"msg": "got {{ _x_svc_token }}"}},
            "fail_msg": {"ansible.builtin.fail": {"msg": "bad {{ some_password }}"}},
            "assert": {"ansible.builtin.assert": {"that": ["true"], "fail_msg": "no {{ _a_openbao_verify.stdout | trim }}"}},
        }.items():
            names = [n for m in messages(task) for n in printed_secret_names(m)]
            self.assertEqual(len(names), 1, kind)

    def test_a_length_filter_makes_only_its_own_expression_safe(self):
        for message, expected in (
            ("{{ secret_token ~ (other | length) }}", ["secret_token"]),
            ("{{ (secret_token | length) ~ secret_token }}", ["secret_token"]),
            ("{{ a_password if (b_token | length) > 0 else c_password }}", ["a_password", "c_password"]),
            ("{% if x_token | length > 0 %}{{ x_token }}{% endif %}", ["x_token"]),
            ("{{ secret_token | trim | length }}", []),
            ("{{ _cms_awx_openbao_token | length > 0 }}", []),
            ("{{ x_token is defined }}", []),
        ):
            self.assertEqual(printed_secret_names(message), expected, message)

    def test_secret_id_and_a_secret_attribute_are_not_exempt(self):
        for message, expected in (
            ("id {{ secret_id }}", ["secret_id"]),
            ("id {{ _aab_secret_id }}", ["_aab_secret_id"]),
            ("id {{ item.secret_id }}", ["secret_id"]),
            ("id {{ (item.stdout | from_json).auth.client_token }}", ["client_token"]),
            ("err {{ _cms_awx_openbao_verify.stderr }}", ["_cms_awx_openbao_verify"]),
            ("err {{ _cms_awx_openbao_verify['stderr'] }}", ["_cms_awx_openbao_verify"]),
            ("{{ item.secret_id | length }}", []),
        ):
            self.assertEqual(printed_secret_names(message), expected, message)

    def test_the_scan_allows_the_safe_forms(self):
        for message in (
            "empty {{ _cms_awx_openbao_verify.stdout | length == 0 }}",
            "rc {{ _cms_awx_openbao_verify.rc }} {{ _cms_awx_openbao_verify['rc'] }}",
            "status {{ _cms_awx_existing_token_test.status }}",
            "valid {{ _cms_awx_token_valid }} user {{ _cms_awx_recovered_token_user }}",
            "id {{ _cms_awx_inventory_id }} {{ _cms_awx_svc_user_id }}",
        ):
            self.assertEqual(printed_secret_names(message), [], message)


if __name__ == "__main__":
    unittest.main()
