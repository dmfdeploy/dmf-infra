"""Every OpenBao command runs through the shared role roles/common/openbao-cli.

The role sends the token, the words of the command and the body on stdin, turns
pipelining on for that one task, and fails closed. A task that runs `kubectl exec ...
bao` by hand skips all of that, so this test fails when one appears outside the role.

A site that cannot use the role goes in ALLOWED below with the reason. Each entry
must still match, so the list cannot hold a file that no longer needs it.

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
    # the pod for a login and a capability read as well as for `status`.
    "playbooks/runbooks/eso-openbao-health-check.yml": (
        "runs from AWX with `k3s kubectl` and sets BAO_ADDR and BAO_CACERT in the pod"
    ),
    # This script runs inside the CronJob container, not through kubectl exec.
    "roles/base/openbao-raft-snapshot/templates/snapshot-script.yml.j2": (
        "runs in the snapshot CronJob container and logs in with its own service account token"
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
        if TOKEN_READ.search(text):
            found.append((str(rel), "reads BAO_TOKEN from stdin by hand"))
            continue
        if path.suffix in {".j2", ".sh"} and BAO_IN_SCRIPT.search(text):
            found.append((str(rel), "runs bao in a script"))
            continue
        for block in TASK_SPLIT.split(text):
            if KUBECTL_EXEC.search(block) and (BAO_WORD.search(block) or BAO_ARGV.search(block)):
                found.append((str(rel), "runs bao through kubectl exec"))
                break
    return found


class SharedTaskTest(unittest.TestCase):
    def test_no_direct_openbao_command_outside_the_shared_role(self):
        outside = [v for v in violations(ROOT) if v[0] not in ALLOWED]
        self.assertEqual(outside, [], "run these through common/openbao-cli: %s" % outside)

    def test_every_allowed_entry_still_matches(self):
        direct = {rel for rel, _ in violations(ROOT)}
        for rel, reason in ALLOWED.items():
            self.assertTrue(reason.strip(), rel)
            self.assertIn(rel, direct, "%s no longer holds a direct command; remove it from ALLOWED" % rel)

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
        tasks = yaml.safe_load((ROOT / SHARED_ROLE / "tasks/main.yml").read_text())
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
