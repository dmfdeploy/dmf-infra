"""Static check: no OpenBao task deletes a Pod with `wait: true`.

A StatefulSet recreates a deleted pod under the same name within seconds. A
`kubernetes.core.k8s` delete with `wait: true` waits for that name to disappear, so
it never sees the pod gone and fails after its timeout, in the middle of a restart
or of the migration. A task deletes the pod with `wait: false` and then waits for a
pod with a new UID, as restart-pod.yml and grow-ha.yml do. The check reads the
OpenBao role's task files as text.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = sorted((ROOT / "roles/stack/operator/openbao/tasks").glob("*.yml"))
TASK_START = re.compile(r"^\s*- name:")


def tasks(text):
    """Split a task file into (first line number, list of lines), one per task."""
    block = []
    start = 0
    for number, line in enumerate(text.splitlines(), 1):
        if TASK_START.match(line):
            if block:
                yield start, block
            block, start = [], number
        if block or TASK_START.match(line):
            block.append(line)
    if block:
        yield start, block


def pod_deletes_that_wait(text):
    """Return the first line number of each task that deletes a Pod and waits."""
    found = []
    for start, lines in tasks(text):
        code = [line.split("#", 1)[0] for line in lines]
        joined = "\n".join(code)
        if (
            re.search(r"^\s*kind:\s*Pod\s*$", joined, re.M)
            and re.search(r"^\s*state:\s*absent\s*$", joined, re.M)
            and re.search(r"^\s*wait:\s*(true|yes)\s*$", joined, re.M | re.I)
        ):
            found.append(start)
    return found


class OpenBaoNoPodDeleteWaitTest(unittest.TestCase):
    def test_files_found(self):
        names = {path.name for path in FILES}
        self.assertIn("grow-ha.yml", names)
        self.assertIn("restart-pod.yml", names)

    def test_the_pod_deletes_exist(self):
        # The check has something to look at: both files delete a Pod.
        for name in ("grow-ha.yml", "restart-pod.yml"):
            text = (ROOT / "roles/stack/operator/openbao/tasks" / name).read_text()
            self.assertTrue(
                any(re.search(r"^\s*kind:\s*Pod\s*$", "\n".join(lines), re.M) and "state: absent" in "\n".join(lines)
                    for _, lines in tasks(text)),
                name,
            )

    def test_no_pod_delete_waits(self):
        found = []
        for path in FILES:
            for number in pod_deletes_that_wait(path.read_text()):
                found.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual(found, [], "a Pod delete with wait: true:\n" + "\n".join(found))

    def test_checker_catches_a_pod_delete_that_waits(self):
        sample = (
            "- name: Delete the pod\n"
            "  kubernetes.core.k8s:\n"
            "    state: absent\n"
            "    kind: Pod\n"
            "    name: openbao-0\n"
            "    wait: true\n"
            "\n"
            "- name: Delete the pod, no wait\n"
            "  kubernetes.core.k8s:\n"
            "    state: absent\n"
            "    kind: Pod\n"
            "    name: openbao-0\n"
            "    wait: false\n"
            "\n"
            "- name: Delete a StatefulSet and wait\n"
            "  kubernetes.core.k8s:\n"
            "    state: absent\n"
            "    kind: StatefulSet\n"
            "    wait: true\n"
        )
        self.assertEqual(pod_deletes_that_wait(sample), [1])


if __name__ == "__main__":
    unittest.main()
