"""No task loops over the registered result of a `no_log` task unless it is `no_log` too.

A task that registers a `no_log` result hides the result from its own output, but the
registered variable still holds everything the call returned, such as a client secret
or a token. A later task that loops over that variable prints each item at `-v` and
above, and Ansible shows the item in the result of every iteration. `loop_control.label`
changes only the item header. To check a value, `set_fact` a small summary with
`no_log: true`, then loop over the summary.

The test finds every task under `playbooks/` and `roles/` that has these three
properties:

- It loops with `loop`, `with_items`, `with_list` or another `with_*` keyword.
- The loop expression names a variable that a `no_log` task registered.
- The task is not `no_log` itself, directly or through its block or play.

The scan follows registered variables, and the names given to the `common/openbao-cli`
role as `openbao_cli_register`, only. A `no_log` `set_fact` that reduces a result
to the fields a check needs, such as a name and a count, is the sanctioned way to loop
over the outcome of a lookup, so the scan does not follow it. Keep secrets out of such a
summary.

Run from k3s-lab-bootstrap:
    python3 -m unittest discover -s tests -p 'test_no_log_result_not_looped.py' -v
"""
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SCAN_DIRS = ("playbooks", "roles")
SCOPE_KEYS = ("block", "rescue", "always")
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def is_no_log(value):
    """True when a `no_log` value is the literal true, or a template we cannot read."""
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"true", "yes"} or "{{" in value
    return False


def walk(items, inherited=False):
    """Yield (task, effective no_log) for every task, descending into blocks."""
    for item in items or []:
        if not isinstance(item, dict):
            continue
        effective = inherited or is_no_log(item.get("no_log"))
        if any(key in item for key in SCOPE_KEYS):
            for key in SCOPE_KEYS:
                yield from walk(item.get(key), effective)
        else:
            yield item, effective


def file_tasks(path):
    """Every (task, effective no_log) of a playbook, a tasks file or a role file."""
    try:
        document = yaml.safe_load(path.read_text())
    except yaml.YAMLError:
        return []
    if not isinstance(document, list):
        return []
    out = []
    for item in document:
        if isinstance(item, dict) and "hosts" in item:
            play = is_no_log(item.get("no_log"))
            for key in ("pre_tasks", "tasks", "post_tasks", "handlers"):
                out += list(walk(item.get(key), play))
        else:
            out += list(walk([item]))
    return out


def loop_expression(task):
    """The text of a task's loop, from `loop` or any `with_*` keyword."""
    parts = []
    for key, value in task.items():
        if key == "loop" or key.startswith("with_"):
            parts.append(yaml.safe_dump(value))
    return "\n".join(parts)


def identifiers(text):
    return set(IDENTIFIER.findall(text))


def secret_variables(tasks):
    """Names registered by a task that is no_log."""
    names = {task["register"] for task, hidden in tasks if hidden and isinstance(task.get("register"), str)}
    # The shared `common/openbao-cli` role sets the fact named by `openbao_cli_register`
    # from its own no_log tasks, so that name holds a hidden result as well.
    for task, _ in tasks:
        for module in ("ansible.builtin.include_role", "include_role", "ansible.builtin.import_role", "import_role"):
            name = ((task.get("vars") or {}).get("openbao_cli_register")) if module in task else None
            if isinstance(name, str) and "{{" not in name:
                names.add(name)
    return names


def violations(root):
    """(file, task name, variable) for every non-no_log loop over a secret variable."""
    per_file = {}
    for directory in SCAN_DIRS:
        for path in sorted((root / directory).rglob("*.yml")):
            tasks = file_tasks(path)
            if tasks:
                per_file[path.relative_to(root).as_posix()] = tasks
    # A registered name that two files share by `include_tasks` counts across the tree.
    everything = [pair for tasks in per_file.values() for pair in tasks]
    secret = secret_variables(everything)
    found = []
    for rel, tasks in per_file.items():
        for task, hidden in tasks:
            if hidden:
                continue
            for name in sorted(identifiers(loop_expression(task)) & secret):
                found.append((rel, task.get("name"), name))
    return found


def violations_in(tasks_yaml):
    """The same scan over an inline playbook, for the planted cases."""
    tasks = file_tasks_from_text(tasks_yaml)
    secret = secret_variables(tasks)
    return [
        (task.get("name"), name)
        for task, hidden in tasks
        if not hidden
        for name in sorted(identifiers(loop_expression(task)) & secret)
    ]


def file_tasks_from_text(text):
    document = yaml.safe_load(text)
    out = []
    for item in document:
        if isinstance(item, dict) and "hosts" in item:
            out += list(walk(item.get("tasks"), is_no_log(item.get("no_log"))))
    return out


LEAK = """
- hosts: all
  tasks:
    - name: Look up
      ansible.builtin.uri: {url: "https://x/api"}
      register: _lookup
      no_log: true
    - name: Assert every lookup
      ansible.builtin.assert: {that: ["item.json.results | length > 0"]}
      loop: "{{ _lookup.results }}"
"""

LEAK_THROUGH_BLOCK_AND_WITH = """
- hosts: all
  tasks:
    - block:
        - name: Look up
          ansible.builtin.uri: {url: "https://x/api"}
          register: _lookup
      no_log: true
    - name: Print each
      ansible.builtin.debug: {var: item}
      with_items: "{{ _lookup.results }}"
"""

SAFE_NO_LOG_LOOP = """
- hosts: all
  tasks:
    - name: Look up
      ansible.builtin.uri: {url: "https://x/api"}
      register: _lookup
      no_log: true
    - name: Assert every lookup
      ansible.builtin.assert: {that: ["item.json.results | length > 0"]}
      loop: "{{ _lookup.results }}"
      no_log: true
"""

SAFE_VISIBLE_SOURCE = """
- hosts: all
  tasks:
    - name: List
      ansible.builtin.command: {argv: [ls]}
      register: _listing
    - name: Print each
      ansible.builtin.debug: {var: item}
      loop: "{{ _listing.stdout_lines }}"
"""

SAFE_SUMMARY = """
- hosts: all
  tasks:
    - name: Look up
      ansible.builtin.uri: {url: "https://x/api"}
      register: _lookup
      no_log: true
    - name: Reduce
      ansible.builtin.set_fact:
        _summary: "{{ _lookup.results | map(attribute='status') | list }}"
      no_log: true
    - name: Print each
      ansible.builtin.debug: {var: item}
      loop: "{{ _summary | from_json }}"
"""


LEAK_FROM_ROLE = """
- hosts: all
  tasks:
    - name: Call
      ansible.builtin.include_role: {name: common/openbao-cli}
      vars: {openbao_cli_register: _logins}
    - name: Assert each
      ansible.builtin.assert: {that: ["item.rc != 0"]}
      loop: "{{ _logins.results }}"
"""


class NoLogResultNotLoopedTest(unittest.TestCase):
    def test_no_task_loops_over_a_no_log_result_without_no_log(self):
        self.assertEqual(violations(ROOT), [])

    def test_the_scan_reads_the_tree_and_finds_no_log_registers(self):
        tasks = [pair for d in SCAN_DIRS for p in (ROOT / d).rglob("*.yml") for pair in file_tasks(p)]
        self.assertGreater(len(tasks), 100)
        self.assertIn("_verify_oauth_providers", secret_variables(tasks))

    def test_the_scan_flags_a_loop_over_a_no_log_result(self):
        self.assertEqual(violations_in(LEAK), [("Assert every lookup", "_lookup")])

    def test_the_scan_flags_no_log_set_on_a_block_and_a_with_keyword(self):
        self.assertEqual(violations_in(LEAK_THROUGH_BLOCK_AND_WITH), [("Print each", "_lookup")])

    def test_the_scan_flags_a_loop_over_a_result_the_openbao_cli_role_sets(self):
        self.assertEqual(violations_in(LEAK_FROM_ROLE), [("Assert each", "_logins")])

    def test_the_scan_allows_the_safe_forms(self):
        for name, text in (
            ("no_log loop", SAFE_NO_LOG_LOOP),
            ("visible source", SAFE_VISIBLE_SOURCE),
        ):
            self.assertEqual(violations_in(text), [], name)

    def test_the_scan_does_not_follow_a_reduced_summary_fact(self):
        self.assertEqual(violations_in(SAFE_SUMMARY), [])


if __name__ == "__main__":
    unittest.main()
