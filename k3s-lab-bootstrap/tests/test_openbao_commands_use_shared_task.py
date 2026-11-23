"""Every OpenBao command runs through the shared role roles/common/openbao-cli.

The role sends the token, the words of the command and the body on stdin, turns
pipelining on for that one task, and fails closed. A task that runs `kubectl exec ...
bao` by hand skips all of that, so this test fails when one appears outside the role.

A site that cannot use the role goes in ALLOWED below with the reason. An entry for a
YAML file names the tasks it covers, so a new direct call in that file is caught. Each
entry must still match, so the list cannot hold a site that no longer needs it.

Three kinds of site are found: a task that runs `bao` through `kubectl exec`, a task
that reads the token by hand, and a command, shell or raw task whose command line
starts `bao` (alone, after `;`, `&&`, `|` or `--`, or as the first argument of `argv`).
Task names, comments and messages are not command lines and are not read.

Run from k3s-lab-bootstrap:
    python3 -m unittest discover -s tests -p 'test_openbao_commands_use_shared_task.py' -v
"""
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
SHARED_ROLE = Path("roles/common/openbao-cli")

# Paths relative to k3s-lab-bootstrap -> why the site cannot use the shared role.
ALLOWED = {
    # This runbook runs from AWX with `k3s kubectl`, and it sets BAO_ADDR and BAO_CACERT in
    # the pod for a login and a capability read as well as for `status`. The entry names the
    # three tasks, so a new direct call in the file is caught.
    "playbooks/runbooks/eso-openbao-health-check.yml": (
        "runs from AWX with `k3s kubectl` and sets BAO_ADDR and BAO_CACERT in the pod",
        {
            "Read OpenBao health via API",
            "Log in as ops-admin for policy verification",
            "Check ops-admin capabilities on Zot admin path",
        },
    ),
    # This script runs inside the CronJob container, not through kubectl exec.
    "roles/base/openbao-raft-snapshot/templates/snapshot-script.yml.j2": (
        "runs in the snapshot CronJob container and logs in with its own service account token",
        None,
    ),
}

SCANNED_SUFFIXES = {".yml", ".yaml", ".j2", ".sh"}
SKIPPED_TOP_DIRS = {"tests", "docs", "charts"}
TASK_SPLIT = re.compile(r"^\s*- name:", re.M)
KUBECTL_EXEC = re.compile(r"kubectl\b[^\n]*\bexec\b|\bexec\b[^\n]*\bkubectl\b|^\s*-\s*exec\s*$", re.M)
BAO_WORD = re.compile(r"(?:^|[\s;|&('\"=])bao(?:\s+-\S+)*\s+[a-z]")
BAO_ARGV = re.compile(r"^\s*-\s*bao\s*$", re.M)
TOKEN_READ = re.compile(r"IFS=\s*read\s+-r\s+BAO_TOKEN")
BAO_IN_SCRIPT = re.compile(
    r"(?:^|\$\(|[\s;|&])bao\s+(?:-\S+\s+)*(?:login|operator|read|write|list|delete|kv|status|token|policy|auth|secrets|lease)\b"
)


def code_only(text):
    """The text without whole-line comments."""
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


COMMAND_MODULES = ("command", "shell", "raw", "script")
# `bao` in command position: at the start, after a separator, after `--`, after an env
# assignment, `exec` or `sudo`.
BAO_COMMAND = re.compile(
    r"(?:^|[;&|('\"`\n]|\$\(|(?<=\s)--(?=\s))\s*(?:[A-Za-z_]\w*=\S*\s+)*(?:(?:exec|sudo)\s+)*bao(?:\s+\S|\s*$)"
)


def module_key(task):
    for key in task:
        if not isinstance(key, str):
            continue
        if key.split(".")[-1] in COMMAND_MODULES and key.split(".")[0] in COMMAND_MODULES + ("ansible",):
            return key
    return None


def command_lines(task):
    """The command strings of a command, shell or raw task: the free-form text, `cmd`, `argv`."""
    value = task[module_key(task)]
    lines, words = [], []
    if isinstance(value, str):
        lines.append(value)
    elif isinstance(value, dict):
        for key in ("cmd", "_raw_params"):
            if isinstance(value.get(key), str):
                lines.append(value[key])
        if isinstance(value.get("argv"), list):
            words = [str(w) for w in value["argv"]]
    return lines, words


def direct_bao_task(task):
    lines, words = command_lines(task)
    if any(BAO_COMMAND.search(line) for line in lines):
        return True
    if any(BAO_COMMAND.search(word) for word in words if word.strip() != "bao" and len(word) > 3):
        return True
    return any(w == "bao" and (i == 0 or words[i - 1] in ("--", "exec", "sudo")) for i, w in enumerate(words))


def yaml_tasks(node):
    if isinstance(node, list):
        for item in node:
            yield from yaml_tasks(item)
    elif isinstance(node, dict):
        if module_key(node):
            yield node
        for value in node.values():
            if isinstance(value, (list, dict)):
                yield from yaml_tasks(value)


def direct_task_names(path):
    """Names of the tasks in a YAML file that run `bao` as a command."""
    try:
        documents = list(yaml.safe_load_all(path.read_text(errors="replace")))
    except yaml.YAMLError:
        return []
    return [str(t.get("name", "(unnamed)")) for d in documents for t in yaml_tasks(d) if direct_bao_task(t)]


def violations(root):
    """Return (relative path, reason) for each direct OpenBao command under root."""
    found = []
    for path in sorted(root.rglob("*")):
        rel = path.relative_to(root)
        if (
            not path.is_file()
            or path.suffix not in SCANNED_SUFFIXES
            or rel.parts[0] in SKIPPED_TOP_DIRS
            or SHARED_ROLE in rel.parents
        ):
            continue
        text = code_only(path.read_text(errors="replace"))
        if path.suffix in {".yml", ".yaml"}:
            for name in direct_task_names(path):
                found.append((str(rel), "runs bao as a command in the task: " + name))
            for block in TASK_SPLIT.split(text):
                name = block.split("\n", 1)[0].strip().strip("\"'")
                if TOKEN_READ.search(block):
                    found.append((str(rel), "reads BAO_TOKEN from stdin by hand in the task: " + name))
                if KUBECTL_EXEC.search(block) and (BAO_WORD.search(block) or BAO_ARGV.search(block)):
                    found.append((str(rel), "runs bao through kubectl exec in the task: " + name))
            continue
        if TOKEN_READ.search(text):
            found.append((str(rel), "reads BAO_TOKEN from stdin by hand"))
            continue
        if BAO_IN_SCRIPT.search(text):
            found.append((str(rel), "runs bao in a script"))
    return found


def allowed(found):
    """Drop the findings that ALLOWED covers; return the rest."""
    rest = []
    for rel, reason in found:
        entry = ALLOWED.get(rel)
        if entry is not None:
            tasks_covered = entry[1]
            if tasks_covered is None or any(reason.endswith(": " + name) for name in tasks_covered):
                continue
        rest.append((rel, reason))
    return rest


class SharedTaskTest(unittest.TestCase):
    def test_no_direct_openbao_command_outside_the_shared_role(self):
        outside = allowed(violations(ROOT))
        self.assertEqual(outside, [], "run these through common/openbao-cli: %s" % outside)

    def test_every_allowed_entry_still_matches(self):
        found = violations(ROOT)
        for rel, (reason, covered) in ALLOWED.items():
            self.assertTrue(reason.strip(), rel)
            mine = [r for f, r in found if f == rel]
            self.assertTrue(mine, "%s no longer holds a direct command; remove it from ALLOWED" % rel)
            for name in covered or ():
                self.assertTrue(
                    any(r.endswith(": " + name) for r in mine),
                    "%s: the task %r no longer runs bao directly; remove it from ALLOWED" % (rel, name),
                )

    def test_a_new_direct_call_in_an_allowed_file_is_caught(self):
        rel = "playbooks/runbooks/eso-openbao-health-check.yml"
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            (tree / rel).parent.mkdir(parents=True)
            text = (ROOT / rel).read_text()
            (tree / rel).write_text(
                text.rstrip("\n")
                + "\n\n    - name: A new direct call\n      ansible.builtin.command:\n        argv: [bao, status]\n"
            )
            outside = allowed(violations(tree))
            self.assertEqual(outside, [(rel, "runs bao as a command in the task: A new direct call")])

    def test_the_scan_catches_a_planted_command(self):
        planted = {
            "kubectl exec with sh -c": (
                "- name: Read\n"
                "  ansible.builtin.shell:\n"
                "    cmd: >-\n"
                "      kubectl -n openbao exec -i openbao-0 -- sh -c 'bao read x'\n"
            ),
            "kubectl exec with argv": (
                "- name: Status\n"
                "  ansible.builtin.command:\n"
                "    argv:\n"
                "      - kubectl\n"
                "      - exec\n"
                "      - openbao-0\n"
                "      - --\n"
                "      - bao\n"
                "      - status\n"
            ),
            "token read by hand": (
                "- name: Write\n"
                "  ansible.builtin.command:\n"
                "    argv: [sh, -c, 'IFS= read -r BAO_TOKEN; export BAO_TOKEN; bao kv put a -']\n"
            ),
        }
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            shutil.copytree(ROOT / SHARED_ROLE, tree / SHARED_ROLE)
            (tree / "roles/x/tasks").mkdir(parents=True)
            for i, (name, text) in enumerate(planted.items()):
                (tree / "roles/x/tasks" / ("case%d.yml" % i)).write_text(text)
            (tree / "roles/x/templates").mkdir()
            (tree / "roles/x/templates/job.sh.j2").write_text('TOKEN="$(bao login -method=kubernetes)"\n')
            got = {rel for rel, _ in violations(tree)}
            expected = {"roles/x/tasks/case%d.yml" % i for i in range(len(planted))} | {"roles/x/templates/job.sh.j2"}
            self.assertEqual(got, expected)

    def test_the_scan_catches_a_plain_direct_command_and_ignores_text(self):
        flagged = {
            "free-form command": "- name: A\n  ansible.builtin.command: bao status\n",
            "free-form shell": "- name: B\n  ansible.builtin.shell: bao kv get secret/foo\n",
            "short module name": "- name: C\n  command: bao status -format=json\n",
            "cmd": "- name: D\n  ansible.builtin.shell:\n    cmd: echo start && bao read x\n",
            "folded cmd": "- name: E\n  ansible.builtin.shell:\n    cmd: >-\n      cd /tmp;\n      bao list sys/policies/acl\n",
            "argv": "- name: F\n  ansible.builtin.command:\n    argv: [bao, status]\n",
            "argv list": "- name: G\n  ansible.builtin.command:\n    argv:\n      - bao\n      - kv\n      - get\n",
            "sh -c": "- name: H\n  ansible.builtin.command:\n    argv: [sh, -c, 'cd /x && bao read y']\n",
            "env prefix": "- name: I\n  ansible.builtin.shell: BAO_ADDR=https://x bao status\n",
            "after --": "- name: J\n  ansible.builtin.command:\n    argv: [k3s, kubectl, exec, p, --, bao, status]\n",
            "in a block": "- name: K\n  block:\n    - name: L\n      ansible.builtin.raw: bao status\n",
            "pipe": "- name: M\n  ansible.builtin.shell: echo x | bao write y -\n",
        }
        clean = {
            "name": "- name: Run bao status by hand\n  ansible.builtin.debug:\n    msg: ok\n",
            "message": "- name: N\n  ansible.builtin.fail:\n    msg: Run bao status and bao kv get by hand.\n",
            "assert message": "- name: O\n  ansible.builtin.assert:\n    that: [true]\n    fail_msg: bao read failed\n",
            "comment": "# bao status\n- name: P\n  ansible.builtin.command: echo done\n",
            "echo": "- name: Q\n  ansible.builtin.command: echo bao\n",
            "argv echo": "- name: R\n  ansible.builtin.command:\n    argv: [echo, bao, status]\n",
            "similar word": "- name: S\n  ansible.builtin.command: baobab --version\n",
            "flag text": "- name: T\n  ansible.builtin.command:\n    argv: [kubectl, apply, --description=the bao server]\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            (tree / "roles/x/tasks").mkdir(parents=True)
            for i, text in enumerate(list(flagged.values()) + list(clean.values())):
                (tree / "roles/x/tasks" / ("case%02d.yml" % i)).write_text(text)
            got = {rel for rel, _ in violations(tree)}
            names = list(flagged) + list(clean)
            for i, name in enumerate(names):
                path = "roles/x/tasks/case%02d.yml" % i
                self.assertEqual(path in got, name in flagged, name)

    def test_the_scan_passes_comments_and_the_shared_role(self):
        with tempfile.TemporaryDirectory() as tmp:
            tree = Path(tmp)
            shutil.copytree(ROOT / SHARED_ROLE, tree / SHARED_ROLE)
            (tree / "roles/x/tasks").mkdir(parents=True)
            (tree / "roles/x/tasks/note.yml").write_text(
                "# kubectl exec openbao-0 -- bao read x\n"
                "- name: Note\n"
                "  ansible.builtin.debug:\n"
                "    msg: Run bao status by hand.\n"
            )
            self.assertEqual(violations(tree), [])

    def test_the_shared_role_turns_pipelining_on_for_its_one_command(self):
        tasks = yaml.safe_load((ROOT / SHARED_ROLE / "tasks/step.yml").read_text())
        runs = [t for t in tasks if "ansible.builtin.command" in t]
        self.assertEqual(len(runs), 1, "the role must have exactly one task that runs a command")
        run = runs[0]
        self.assertIs(run["vars"]["ansible_pipelining"], True)
        self.assertIs(run["no_log"], True)
        self.assertIs(run["ansible.builtin.command"]["stdin_add_newline"], False)
        argv = run["ansible.builtin.command"]["argv"]
        program = argv[-1]
        self.assertNotIn("{{", program, "no value may be part of the program text")
        templated = [word for word in argv[:-1] if "{{" in word]
        self.assertEqual(
            templated,
            ["{{ openbao_cli_namespace | default('openbao') }}", "{{ openbao_cli_pod }}"],
            "only the namespace and the pod may be templated in the argument list",
        )


if __name__ == "__main__":
    unittest.main()
