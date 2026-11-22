"""Static check: an HTTP call that sends a secret verifies the server certificate.

A `uri` or `get_url` task that carries a token, password, bearer, cookie or
other secret must not turn certificate verification off. Turning it off lets
anyone on the path read the secret and answer in the server's place.

The check parses every task file under `roles/` and `playbooks/` and fails for
each task that:

* is a `uri` or `get_url` call,
* names a secret in its arguments (a secret-like variable, an `Authorization`
  or `Cookie` header, `url_password`), and
* sets `validate_certs` to `false`, or to an expression that can evaluate to
  `false` (`default(false)`, or a role variable whose default is `false`),

unless the task is on ALLOWED below with a reason. It also fails for a role
default that sets a `*validate_certs` or `*ssl_verify` variable to `false`.
"""

import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCANNED_DIRS = ("roles", "playbooks")

HTTP_MODULES = {
    "uri", "get_url", "ansible.builtin.uri", "ansible.builtin.get_url",
}
SECRET_ARG = re.compile(
    r"(password|passwd|(?<![a-z])token|secret|api_key|private_key|bearer"
    r"|authorization|cookie|(?<![a-z])jwt)",
    re.I,
)
# A variable name that turns certificate verification on or off.
VERIFY_SWITCH = re.compile(r"(validate_certs|ssl_verify|verify_ssl)$", re.I)
OFF = re.compile(r"^\s*(false|no|off|0)\s*$", re.I)
DEFAULT_OFF = re.compile(r"default\(\s*(false|no|0)\s*\)", re.I)

# Calls that cannot verify today: {(file relative to ROOT, task name): reason}.
# Add an entry only when the endpoint cannot present a certificate the node
# trusts, and say why. Do not widen trust to make a call pass.
ALLOWED = {}


def _walk_tasks(node):
    """Yield every task dict in a loaded YAML document, blocks included."""
    if isinstance(node, list):
        for item in node:
            yield from _walk_tasks(item)
    elif isinstance(node, dict):
        if any(key in node for key in HTTP_MODULES):
            yield node
        for key in ("block", "rescue", "always", "tasks", "pre_tasks",
                    "post_tasks", "handlers"):
            if key in node:
                yield from _walk_tasks(node[key])


def _verification_off(value):
    if isinstance(value, bool):
        return value is False
    text = str(value)
    return bool(OFF.match(text) or DEFAULT_OFF.search(text))


def _off_variables(root):
    """Role-default variables that switch verification off: {name: file}."""
    off = {}
    for path in sorted(root.glob("roles/**/defaults/*.yml")):
        data = yaml.safe_load(path.read_text()) or {}
        for name, value in data.items():
            if VERIFY_SWITCH.search(name) and _verification_off(value):
                off[name] = path
    return off


def find_violations(root=ROOT):
    """Return a sorted list of 'file: task' strings that break the rule."""
    problems = []
    off_vars = _off_variables(root)
    for name, path in off_vars.items():
        if (str(path.relative_to(root)), name) not in ALLOWED:
            problems.append(f"{path.relative_to(root)}: default {name} is false")
    for scanned in SCANNED_DIRS:
        for path in sorted((root / scanned).rglob("*.yml")):
            if "/tests/" in str(path) or "/defaults/" in str(path):
                continue
            try:
                docs = list(yaml.safe_load_all(path.read_text()))
            except yaml.YAMLError:
                continue
            rel = str(path.relative_to(root))
            for doc in docs:
                for task in _walk_tasks(doc):
                    module = next(k for k in HTTP_MODULES if k in task)
                    args = task[module]
                    if not isinstance(args, dict):
                        continue
                    if "validate_certs" not in args:
                        continue
                    value = str(args["validate_certs"])
                    off = _verification_off(args["validate_certs"]) or any(
                        re.search(r"\b" + re.escape(v) + r"\b", value)
                        for v in off_vars
                    )
                    if not off:
                        continue
                    sends_secret = SECRET_ARG.search(
                        yaml.safe_dump({k: v for k, v in args.items()
                                        if k != "validate_certs"})
                    )
                    if not sends_secret:
                        continue
                    label = str(task.get("name", "<unnamed>"))
                    if (rel, label) not in ALLOWED:
                        problems.append(f"{rel}: {label}")
    return sorted(problems)


class VerifiedCertsOnSecretCalls(unittest.TestCase):
    def test_no_secret_call_skips_verification(self):
        self.assertEqual(
            find_violations(),
            [],
            "A call that sends a secret must verify the certificate. Fix the "
            "call, or add it to ALLOWED with the reason it cannot verify.",
        )

    def test_allowlist_entries_name_a_real_call(self):
        # A stale entry would hide a future violation under the same name.
        names = set()
        for scanned in SCANNED_DIRS:
            for path in (ROOT / scanned).rglob("*.yml"):
                names.add(str(path.relative_to(ROOT)))
        for rel, _name in ALLOWED:
            self.assertIn(rel, names, f"{rel} is on ALLOWED but does not exist")


class PlantedCases(unittest.TestCase):
    """The rule fires on a bad call and stays quiet on a good one."""

    def _run(self, task_yaml, defaults_yaml=None):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "playbooks").mkdir()
            (root / "roles").mkdir()
            (root / "playbooks" / "p.yml").write_text(task_yaml)
            if defaults_yaml:
                d = root / "roles" / "r" / "defaults"
                d.mkdir(parents=True)
                (d / "main.yml").write_text(defaults_yaml)
            return find_violations(root)

    def test_literal_false_with_token_fails(self):
        out = self._run(
            "- hosts: all\n  tasks:\n    - name: call\n      ansible.builtin.uri:\n"
            "        url: https://x\n        headers: {Authorization: 'Bearer {{ tok }}'}\n"
            "        validate_certs: false\n"
        )
        self.assertEqual(out, ["playbooks/p.yml: call"])

    def test_default_false_with_password_fails(self):
        out = self._run(
            "- hosts: all\n  tasks:\n    - name: call\n      uri:\n"
            "        url: https://x\n        url_password: '{{ pw }}'\n"
            "        validate_certs: \"{{ v | default(false) }}\"\n"
        )
        self.assertEqual(out, ["playbooks/p.yml: call"])

    def test_variable_defaulting_false_fails(self):
        out = self._run(
            "- hosts: all\n  tasks:\n    - name: call\n      uri:\n"
            "        url: https://x\n        headers: {X-Auth-Token: '{{ t }}'}\n"
            "        validate_certs: \"{{ r_validate_certs }}\"\n",
            "r_validate_certs: false\n",
        )
        self.assertIn("playbooks/p.yml: call", out)
        self.assertIn("roles/r/defaults/main.yml: default r_validate_certs is false", out)

    def test_verified_call_passes(self):
        out = self._run(
            "- hosts: all\n  tasks:\n    - name: call\n      uri:\n"
            "        url: https://x\n        headers: {Authorization: 'Bearer {{ t }}'}\n"
            "        validate_certs: true\n"
        )
        self.assertEqual(out, [])

    def test_call_without_a_secret_passes(self):
        out = self._run(
            "- hosts: all\n  tasks:\n    - name: probe\n      uri:\n"
            "        url: https://x\n        validate_certs: false\n"
        )
        self.assertEqual(out, [])


if __name__ == "__main__":
    unittest.main()
