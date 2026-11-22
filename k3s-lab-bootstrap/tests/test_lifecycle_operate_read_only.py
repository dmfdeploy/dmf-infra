"""Check that no inclusion in lifecycle-operate.yml reaches a 6xx-* or vertical-security/1xx-* playbook, any role, or a task file outside playbooks/lifecycle/tasks.

Operate is the steady state of a running facility, so its playbooks only read.

The test walks everything the entry file reaches, as data. Nothing renders and
no task runs. It follows these forms, with or without a collection prefix such
as ansible.builtin:

- import_playbook
- include_tasks and import_tasks
- include_role and import_role, and the roles list of a play
- the dependencies in a role's meta/main.yml

It fails when an inclusion reaches:

- a playbook named 6xx-* or vertical-security/1xx-*;
- any role, unless the caller allows it (none is allowed today);
- a task file outside playbooks/lifecycle/tasks;
- a target it cannot resolve: a templated path or role name, or the
  unsupported include and include_playbook.

It does not detect a deploy playbook that has another name, or a mutating task
written inline in an Operate playbook. Review covers both. The test guards
against an accidental regression, not against someone who evades it on purpose.
"""
import re
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
ENTRY = "lifecycle-operate.yml"
ALLOWED_ROLES = frozenset()
ALLOWED_TASK_DIRS = ("playbooks/lifecycle/tasks",)
DEPLOY = (
    re.compile(r"(^|/)6\d\d-[^/]*\.yml$"),
    re.compile(r"(^|/)vertical-security/1\d\d-[^/]*\.yml$"),
)
PLAYBOOK = ("import_playbook",)
TASKS = ("include_tasks", "import_tasks")
ROLE = ("include_role", "import_role")
UNSUPPORTED = ("include", "include_playbook")


def short(key):
    """Drop a collection prefix: ansible.builtin.include_tasks -> include_tasks."""
    return key.rsplit(".", 1)[-1] if isinstance(key, str) else key


def target(value):
    """The path or name an inclusion points at, from a string or a dict."""
    if isinstance(value, dict):
        value = value.get("file") or value.get("name") or value.get("role")
    return value if isinstance(value, str) else None


def walk(node):
    """Yield every (short key, value) pair in a parsed YAML tree."""
    if isinstance(node, dict):
        for key, value in node.items():
            yield short(key), value
            yield from walk(value)
    elif isinstance(node, list):
        for item in node:
            yield from walk(item)


def find_role(root, name):
    direct = root / "roles" / name
    if direct.is_dir():
        return direct
    hits = sorted(p for p in (root / "roles").rglob(Path(name).name) if p.is_dir() and p.as_posix().endswith(name))
    return hits[0] if hits else None


def find_file(root, base, name):
    for folder in (base, root, base / "tasks"):
        if (folder / name).is_file():
            return (folder / name).resolve()
    return None


def violations(entry, root=ROOT, allowed_roles=ALLOWED_ROLES, task_dirs=ALLOWED_TASK_DIRS):
    """Return a sorted list of reasons the entry file reaches deploy code."""
    root = root.resolve()
    found, seen = set(), set()

    def rel(path):
        try:
            return path.relative_to(root).as_posix()
        except ValueError:
            return path.as_posix()

    def visit_role(name, via):
        folder = find_role(root, name)
        if folder is None:
            found.add(f"{via}: role {name} not found")
            return
        role = rel(folder)[len("roles/"):]
        if role not in allowed_roles and name not in allowed_roles:
            found.add(f"{via}: reaches role {role}")
        for sub in (folder / "meta" / "main.yml", folder / "tasks" / "main.yml"):
            if sub.is_file():
                visit(sub, sub.name == "main.yml" and sub.parent.name == "meta")

    def visit(path, is_meta=False):
        path = path.resolve()
        if path in seen:
            return
        seen.add(path)
        data = yaml.safe_load(path.read_text()) or {}
        via = rel(path)
        if is_meta:
            for dep in data.get("dependencies") or []:
                name = target(dep)
                if name:
                    visit_role(name, via)
            return
        for key, value in walk(data):
            if key in UNSUPPORTED:
                found.add(f"{via}: unsupported inclusion {key}")
            elif key == "roles" and isinstance(value, list):
                for item in value:
                    name = target(item)
                    if not name or "{{" in name:
                        found.add(f"{via}: cannot resolve roles entry {item!r}")
                    else:
                        visit_role(name, via)
            elif key in PLAYBOOK + TASKS + ROLE:
                name = target(value)
                if not name or "{{" in name:
                    found.add(f"{via}: cannot resolve {key} {value!r}")
                elif key in ROLE:
                    visit_role(name, via)
                else:
                    sub = find_file(root, path.parent, name)
                    if sub is None:
                        found.add(f"{via}: cannot find {key} {name}")
                    elif key in PLAYBOOK:
                        if any(rx.search(rel(sub)) for rx in DEPLOY):
                            found.add(f"{via}: imports deploy play {rel(sub)}")
                        visit(sub)
                    else:
                        if not any(rel(sub).startswith(d + "/") for d in task_dirs):
                            found.add(f"{via}: includes task file {rel(sub)} outside {', '.join(task_dirs)}")
                        visit(sub)

    visit(Path(entry))
    return sorted(found)


def write(base, name, text):
    path = base / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


class LifecycleOperateReadOnlyTest(unittest.TestCase):
    def check_planted(self, files, expect, **kwargs):
        """Plant files in a temp tree whose entry is entry.yml and expect one finding."""
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            for name, text in files.items():
                write(base, name, text)
            got = violations(base / "entry.yml", root=base, **kwargs)
            self.assertTrue(any(expect in g for g in got), f"expected {expect!r} in {got}")

    def test_operate_reaches_no_deploy_code(self):
        self.assertEqual(violations(ROOT / ENTRY), [])

    def test_operate_still_imports_its_probes_and_drift_check(self):
        text = (ROOT / ENTRY).read_text()
        for name in ("operate-health-probes.yml", "operate-catalog-drift.yml"):
            self.assertIn(name, text)

    def test_planted_nested_deploy_import_is_caught(self):
        self.check_planted(
            {
                "entry.yml": "- import_playbook: playbooks/lifecycle/probes.yml\n",
                "playbooks/lifecycle/probes.yml": "- import_playbook: ../650-dmf-cms.yml\n",
                "playbooks/650-dmf-cms.yml": "- hosts: localhost\n  tasks: []\n",
            },
            "imports deploy play playbooks/650-dmf-cms.yml",
        )

    def test_planted_security_deploy_import_is_caught(self):
        self.check_planted(
            {
                "entry.yml": "- import_playbook: vertical-security/191-zot-oidc.yml\n",
                "vertical-security/191-zot-oidc.yml": "- hosts: localhost\n  tasks: []\n",
            },
            "imports deploy play vertical-security/191-zot-oidc.yml",
        )

    def test_planted_qualified_import_playbook_is_caught(self):
        self.check_planted(
            {
                "entry.yml": "- ansible.builtin.import_playbook: playbooks/694-born-inventory.yml\n",
                "playbooks/694-born-inventory.yml": "- hosts: localhost\n  tasks: []\n",
            },
            "imports deploy play playbooks/694-born-inventory.yml",
        )

    def test_planted_include_tasks_outside_allowed_dir_is_caught(self):
        self.check_planted(
            {
                "entry.yml": "- hosts: localhost\n  tasks:\n    - ansible.builtin.include_tasks: playbooks/tasks/deploy.yml\n",
                "playbooks/tasks/deploy.yml": "- ansible.builtin.debug: {msg: x}\n",
            },
            "outside playbooks/lifecycle/tasks",
        )

    def test_planted_import_tasks_in_block_is_caught(self):
        self.check_planted(
            {
                "entry.yml": "- hosts: localhost\n  tasks:\n    - block:\n        - import_tasks: other/deploy.yml\n",
                "other/deploy.yml": "- ansible.builtin.debug: {msg: x}\n",
            },
            "outside playbooks/lifecycle/tasks",
        )

    def test_task_file_that_includes_a_role_is_followed(self):
        self.check_planted(
            {
                "entry.yml": "- hosts: localhost\n  tasks:\n    - include_tasks: playbooks/lifecycle/tasks/t.yml\n",
                "playbooks/lifecycle/tasks/t.yml": "- ansible.builtin.include_role: {name: stack/app}\n",
                "roles/stack/app/tasks/main.yml": "- ansible.builtin.debug: {msg: x}\n",
            },
            "reaches role stack/app",
        )

    def test_planted_include_role_is_caught(self):
        self.check_planted(
            {
                "entry.yml": "- hosts: localhost\n  tasks:\n    - ansible.builtin.include_role:\n        name: app\n",
                "roles/app/tasks/main.yml": "- ansible.builtin.debug: {msg: x}\n",
            },
            "reaches role app",
        )

    def test_planted_import_role_is_caught(self):
        self.check_planted(
            {
                "entry.yml": "- hosts: localhost\n  tasks:\n    - import_role: {name: app}\n",
                "roles/app/tasks/main.yml": "- ansible.builtin.debug: {msg: x}\n",
            },
            "reaches role app",
        )

    def test_planted_play_roles_list_is_caught(self):
        self.check_planted(
            {
                "entry.yml": "- hosts: localhost\n  roles:\n    - app\n    - role: other\n",
                "roles/app/tasks/main.yml": "- ansible.builtin.debug: {msg: x}\n",
                "roles/other/tasks/main.yml": "- ansible.builtin.debug: {msg: x}\n",
            },
            "reaches role other",
        )

    def test_planted_role_dependency_is_caught(self):
        self.check_planted(
            {
                "entry.yml": "- hosts: localhost\n  roles: [probe]\n",
                "roles/probe/tasks/main.yml": "- ansible.builtin.debug: {msg: x}\n",
                "roles/probe/meta/main.yml": "dependencies:\n  - role: deployer\n",
                "roles/deployer/tasks/main.yml": "- ansible.builtin.debug: {msg: x}\n",
            },
            "reaches role deployer",
            allowed_roles=frozenset({"probe"}),
        )

    def test_planted_role_nested_import_is_followed(self):
        self.check_planted(
            {
                "entry.yml": "- hosts: localhost\n  roles: [probe]\n",
                "roles/probe/tasks/main.yml": "- import_role: {name: deployer}\n",
                "roles/deployer/tasks/main.yml": "- ansible.builtin.debug: {msg: x}\n",
            },
            "reaches role deployer",
            allowed_roles=frozenset({"probe"}),
        )

    def test_templated_roles_entry_fails_closed(self):
        self.check_planted(
            {"entry.yml": "- hosts: localhost\n  roles:\n    - '{{ which_role }}'\n"},
            "cannot resolve roles entry",
        )
        self.check_planted(
            {"entry.yml": "- hosts: localhost\n  roles:\n    - role: '{{ which_role }}'\n"},
            "cannot resolve roles entry",
        )

    def test_templated_and_unsupported_inclusions_fail_closed(self):
        self.check_planted(
            {"entry.yml": "- hosts: localhost\n  tasks:\n    - include_tasks: '{{ which }}.yml'\n"},
            "cannot resolve include_tasks",
        )
        self.check_planted(
            {"entry.yml": "- include_playbook: playbooks/650-dmf-cms.yml\n"},
            "unsupported inclusion include_playbook",
        )


if __name__ == "__main__":
    unittest.main()
