"""Static check: a task that carries a secret has `no_log: true`.

`ansible.builtin.uri` and `get_url` redact `url_password` themselves, and
`url_username` is a user name, not a secret. A secret placed anywhere else in
a task (a header, a body, a command, a manifest, a debug or assert message, a
fact value) or a secret that a task registers is printed on failure and with
`-v` unless the task, or a block around it, sets `no_log: true`.

The check parses every task file under `roles/` and `playbooks/` and fails
for each task that:

* names a secret variable inside a Jinja expression in its arguments, or
* registers a secret-named variable, or
* reads a Kubernetes Secret with `kubernetes.core.k8s_info`,

and has no `no_log: true`.
"""

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCANNED_DIRS = ("roles", "playbooks")

# A variable name that holds a credential value. Names that end in one of the
# NOT_A_VALUE suffixes hold a path, a name, a flag or a count and are skipped.
SECRET_NAME = re.compile(
    r"(password|passwd|(?<![a-z])token(?!_(name|scopes|ttl|review|validity))"
    r"|secret_id|secret_access_key|api_key|private_key|htpasswd"
    r"|runtime_secret_raw|(?<![a-z])jwt|unseal_key)",
    re.I,
)
NOT_A_VALUE = re.compile(
    r"(_name|_names|_path|_file|_file_effective|_namespace|_ttl|_seconds"
    r"|_scopes|_exists|_reused|_check|_check_cmd|_cmd|_role|_configmap"
    r"|_service|_checksum|_too_long|_credentials|_already_provisioned"
    r"|_user|_url)$",
    re.I,
)

# Task keys that are control flow or metadata, not arguments that get printed.
CONTROL_KEYS = {
    "name", "when", "loop", "with_items", "loop_control", "register", "no_log",
    "vars", "tags", "delegate_to", "become", "become_user", "changed_when",
    "failed_when", "until", "retries", "delay", "environment", "ignore_errors",
    "run_once", "notify", "check_mode", "args", "any_errors_fatal", "async",
    "poll", "connection", "module_defaults",
}
# `no_log` on a dynamic include does not reach the tasks it pulls in, so the
# included files are checked on their own and the include itself is skipped.
INCLUDE_KEYS = (
    "include_role", "import_role", "include_tasks", "import_tasks",
    "ansible.builtin.include_role", "ansible.builtin.import_role",
    "ansible.builtin.include_tasks", "ansible.builtin.import_tasks",
)
# The one deliberate debug switch: secrets stay hidden unless it is on.
CONDITIONAL_NO_LOG = "{{ not awx_integration_debug | bool }}"
PLAY_KEYS = ("pre_tasks", "tasks", "post_tasks", "handlers")

JINJA_OPEN = re.compile(r"\{\{|\{%")
QUOTED = re.compile(r"""'(?:[^'\\]|\\.)*'|"(?:[^"\\]|\\.)*\"""")
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# A digest hides the value.
DIGEST = re.compile(r"[\w.]+\s*\|\s*hash\([^)]*\)")
# A reference that reads only the outcome, not the credential.
OUTCOME_ONLY = re.compile(
    r"^(\.(rc|stderr|failed|skipped|changed|status)\b"
    r"|(\.\w+)?(\s*\|\s*default\([^)]*\))?\s*\|\s*length\b"
    r"|\s+is\s+(not\s+)?(defined|none)\b)"
)
URL_CREDENTIAL_KEY = re.compile(r"^url_(password|username)$")


def is_secret_name(name):
    return bool(SECRET_NAME.search(name)) and not NOT_A_VALUE.search(name)


def jinja_expressions(text):
    """Yield each `{{ ... }}` or `{% ... %}` body, with quoted strings blanked.

    A quoted string can hold `}}` (an ExternalSecret template placeholder, for
    example), so the scan skips over quotes while it looks for the closing mark.
    """
    position = 0
    while True:
        opening = JINJA_OPEN.search(text, position)
        if opening is None:
            return
        closing = "}}" if opening.group(0) == "{{" else "%}"
        index = opening.end()
        body = []
        while index < len(text) and not text.startswith(closing, index):
            quoted = QUOTED.match(text, index)
            if quoted:
                body.append("''")
                index = quoted.end()
            else:
                body.append(text[index])
                index += 1
        yield "".join(body)
        position = index + len(closing)


def secret_references(text):
    """Secret-named variables that a Jinja expression in `text` reads."""
    found = set()
    for expression in jinja_expressions(text):
        expression = DIGEST.sub("", expression)
        for match in IDENT.finditer(expression):
            if expression[: match.start()].endswith("."):
                continue
            if not is_secret_name(match.group(0)):
                continue
            if OUTCOME_ONLY.match(expression[match.end():]):
                continue
            found.add(match.group(0))
    return found


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if URL_CREDENTIAL_KEY.match(str(key)):
                continue
            yield from strings(key)
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def is_include(task):
    return any(key in task for key in INCLUDE_KEYS)


def reads_kubernetes_secret(task):
    module = task.get("kubernetes.core.k8s_info")
    return isinstance(module, dict) and str(module.get("kind", "")).lower() == "secret"


def secret_carriers(task):
    """Why `task` carries a secret, as a list of short reasons."""
    reasons = []
    if is_include(task):
        return reasons
    arguments = {k: v for k, v in task.items() if k not in CONTROL_KEYS}
    names = set()
    for text in strings(arguments):
        names |= secret_references(text)
    if names:
        reasons.append("uses " + ", ".join(sorted(names)))
    register = task.get("register")
    if isinstance(register, str) and is_secret_name(register):
        reasons.append("registers " + register)
    if reads_kubernetes_secret(task):
        reasons.append("reads a Kubernetes Secret")
    return reasons


def covers(no_log):
    return no_log is True or no_log == CONDITIONAL_NO_LOG


def walk(node, covered):
    """Yield (task, covered) for every task under a task list or a play list."""
    if isinstance(node, list):
        for item in node:
            yield from walk(item, covered)
    elif isinstance(node, dict):
        if "hosts" in node:
            play_covered = covered or covers(node.get("no_log"))
            for key in PLAY_KEYS:
                yield from walk(node.get(key), play_covered)
            return
        block_covered = covered or covers(node.get("no_log"))
        if "block" in node:
            for key in ("block", "rescue", "always"):
                yield from walk(node.get(key), block_covered)
            return
        yield node, block_covered


def task_files():
    for directory in SCANNED_DIRS:
        for path in sorted((ROOT / directory).rglob("*.yml")):
            parts = path.relative_to(ROOT).parts
            if directory == "roles" and not {"tasks", "handlers"} & set(parts):
                continue
            yield path


def offending_tasks():
    """Return sorted (relative path, task name, reasons) for each uncovered task."""
    offenders = []
    for path in task_files():
        for document in yaml.safe_load_all(path.read_text()):
            if not isinstance(document, list):
                continue
            for task, covered in walk(document, False):
                if covered:
                    continue
                reasons = secret_carriers(task)
                if reasons:
                    relative = str(path.relative_to(ROOT))
                    offenders.append((relative, str(task.get("name", "(unnamed)")), reasons))
    return offenders


class NoLogSecretsTest(unittest.TestCase):
    def test_every_secret_carrying_task_has_no_log(self):
        offenders = offending_tasks()
        report = "\n".join(f"{path}: {name}  ({'; '.join(reasons)})" for path, name, reasons in offenders)
        self.assertEqual([], offenders, f"{len(offenders)} task(s) carry a secret without no_log: true:\n{report}")

    def test_secret_name_rules(self):
        for name in ("awx_integration_forgejo_token", "forgejo_admin_password", "_cms_awx_svc_token", "zot_htpasswd_combined"):
            self.assertTrue(is_secret_name(name), name)
        for name in ("awx_integration_vault_password_file_effective", "forgejo_admin_token_name", "k3s_token_ttl", "_netbox_sot_admin_nb_token_check"):
            self.assertFalse(is_secret_name(name), name)

    def test_allowed_constructs_are_not_flagged(self):
        self.assertEqual(set(), secret_references("{{ zot_htpasswd_combined | hash('sha256') }}"))
        self.assertEqual(set(), secret_references("{{ _login.rc }} {{ some_token | length }}"))
        self.assertEqual(set(), secret_references("{{ '{{ .secret_access_key }}' }}"))
        self.assertEqual({"netbox_token"}, secret_references("Bearer {{ netbox_token }}"))
        self.assertEqual({"api_token"}, secret_references("{{ api_token | trim }}"))

    def test_url_credentials_are_redacted_by_the_module(self):
        task = {"ansible.builtin.uri": {"url": "https://x", "url_password": "{{ admin_password }}"}}
        self.assertEqual([], secret_carriers(task))
        task["ansible.builtin.uri"]["headers"] = {"Authorization": "Bearer {{ admin_token }}"}
        self.assertEqual(["uses admin_token"], secret_carriers(task))


if __name__ == "__main__":
    unittest.main()
