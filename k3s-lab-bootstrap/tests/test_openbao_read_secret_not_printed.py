"""The playbooks that read a token from OpenBao never print its value.

A `debug` message, a `fail_msg` or a `success_msg` in these playbooks must not
template a variable that holds a read secret. The message may show whether the value
is empty, its length, the exit code, the stderr text and the HTTP status. `no_log` on a
task hides its output, but a message that holds the value would still reach a log
where `no_log` is off or `ANSIBLE_DEBUG` is on, so the value must stay out of it.

Run from k3s-lab-bootstrap:
    python3 -m unittest discover -s tests -p 'test_openbao_read_secret_not_printed.py' -v
"""
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PLAYBOOKS = [
    "playbooks/696-cms-authentik-api.yml",
    "playbooks/697-cms-awx-token.yml",
    "playbooks/698-cms-netbox-forgejo-tokens.yml",
    "playbooks/awx-autoscale-secrets.yml",
]
MESSAGE_KEYS = ("msg", "fail_msg", "success_msg")
SECRET_NAME = re.compile(r"token|password|passwd|secret|bearer|jwt|openbao_verify|openbao_check|openbao_raw|openbao_write", re.I)
# Names that look like a secret but hold a state, a name or a place.
BENIGN_NAME = re.compile(
    r"_(valid|exists|provisioned|recovered|recovered_from_openbao|user|name|path|ttl|url|label|id|key_name|source|test)\b"
    r"|token_user|token_exists|bearer_key|secret_name|secret_path|remote_path|remote_property",
    re.I,
)
STRING_LITERAL = re.compile(r"'[^']*'|\"[^\"]*\"")
EXPRESSION = re.compile(r"\{\{(.*?)\}\}", re.S)
IDENTIFIER = re.compile(r"(?<![\w.'\"])([A-Za-z_][A-Za-z0-9_]*)((?:\.[A-Za-z_][A-Za-z0-9_]*)*)")
SAFE_ATTRIBUTES = {"rc", "stderr", "status", "changed", "skipped", "failed", "msg"}


def tasks(items):
    for item in items or []:
        if not isinstance(item, dict):
            continue
        yield item
        for key in ("block", "rescue", "always"):
            yield from tasks(item.get(key))


def messages(task):
    for module in ("ansible.builtin.debug", "ansible.builtin.fail"):
        for key in MESSAGE_KEYS:
            value = (task.get(module) or {}).get(key)
            if isinstance(value, str):
                yield value
    assertion = task.get("ansible.builtin.assert") or {}
    for key in MESSAGE_KEYS:
        if isinstance(assertion.get(key), str):
            yield assertion[key]


def printed_secret_names(message):
    """Names of secret-holding variables that a message templates by value."""
    found = []
    for expression in EXPRESSION.findall(message):
        expression = STRING_LITERAL.sub("''", expression)
        if re.search(r"\|\s*length\b|\bis\s+(not\s+)?defined\b", expression):
            continue
        for name, attributes in IDENTIFIER.findall(expression):
            if not SECRET_NAME.search(name) or BENIGN_NAME.search(name):
                continue
            first = attributes.lstrip(".").split(".")[0] if attributes else ""
            if first in SAFE_ATTRIBUTES:
                continue
            found.append(name + attributes)
    return found


def violations(root):
    found = []
    for rel in PLAYBOOKS:
        plays = yaml.safe_load((root / rel).read_text())
        for play in plays:
            for task in tasks(play.get("tasks", []) + play.get("pre_tasks", []) + play.get("post_tasks", [])):
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
            plays = yaml.safe_load((ROOT / rel).read_text())
            counts[rel] = sum(
                1
                for play in plays
                for task in tasks(play.get("tasks", []) + play.get("pre_tasks", []))
                for _ in messages(task)
            )
        self.assertTrue(all(counts.values()), counts)

    def test_the_scan_flags_the_old_awx_debug_in_a_temporary_copy(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            for rel in PLAYBOOKS:
                (tree / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(ROOT / rel, tree / rel)
            target = tree / "playbooks/697-cms-awx-token.yml"
            text = target.read_text()
            safe = "              stdout empty: {{ _cms_awx_openbao_verify.stdout | length == 0 }}\n"
            self.assertIn(safe, text)
            target.write_text(
                text.replace(safe, "              stdout: '{{ _cms_awx_openbao_verify.stdout }}'\n")
            )
            self.assertEqual(
                [name for _, _, name in violations(tree)], ["_cms_awx_openbao_verify.stdout"]
            )

    def test_the_scan_flags_a_planted_token_in_each_kind_of_message(self):
        for kind, task in {
            "debug": {"ansible.builtin.debug": {"msg": "got {{ _x_svc_token }}"}},
            "fail_msg": {"ansible.builtin.fail": {"msg": "bad {{ some_password }}"}},
            "assert": {"ansible.builtin.assert": {"that": ["true"], "fail_msg": "no {{ _a_openbao_verify.stdout | trim }}"}},
        }.items():
            names = [n for m in messages(task) for n in printed_secret_names(m)]
            self.assertEqual(len(names), 1, kind)

    def test_the_scan_allows_length_status_and_exit_code(self):
        for message in (
            "empty {{ _cms_awx_openbao_verify.stdout | length == 0 }}",
            "rc {{ _cms_awx_openbao_verify.rc }} {{ _cms_awx_openbao_verify.stderr }}",
            "status {{ _cms_awx_existing_token_test.status }}",
            "has {{ _cms_awx_openbao_token | length > 0 }}",
            "valid {{ _cms_awx_token_valid }} user {{ _cms_awx_recovered_token_user }}",
        ):
            self.assertEqual(printed_secret_names(message), [], message)


if __name__ == "__main__":
    unittest.main()
