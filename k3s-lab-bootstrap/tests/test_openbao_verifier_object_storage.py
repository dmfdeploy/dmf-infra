"""The identity verifier checks the object-storage ClusterSecretStores and the
s3-creds-* ExternalSecrets only where object storage is enabled. Environments
without it never run base/object-storage-credentials, so those objects do not
exist there. This test parses the playbook and fails unless every task that
looks up or asserts them carries the enabled condition and a skip notice with
the inverse condition exists."""

import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
PLAYBOOK = ROOT / "playbooks/verify-openbao-identity-model.yml"
ENABLED = "dmf_object_storage_enabled | default(true) | bool"
SKIPPED = f"not ({ENABLED})"
STORE_NAMES = ("openbao-openbao-snapshots", "openbao-app-backups")


def load_tasks():
    plays = yaml.safe_load(PLAYBOOK.read_text(encoding="utf-8"))
    return [task for play in plays for task in play["tasks"]]


def dump(task):
    return yaml.safe_dump(task)


def touches_object_storage(task, registered):
    """True for a task that queries the stores or ExternalSecrets, or that
    loops over what such a query registered."""
    text = dump(task)
    if any(name in text for name in STORE_NAMES) and "k8s_info" in text:
        return True
    if "s3-creds-" in text or "verify_expected_external_secrets" in text:
        return "k8s_info" in text or "assert" in text
    return any(var in str(task.get("loop", "")) for var in registered)


def object_storage_tasks(tasks):
    registered = set()
    found = []
    for task in tasks:
        if touches_object_storage(task, registered):
            found.append(task)
            if "register" in task:
                registered.add(task["register"])
    return found


class VerifierObjectStorageTests(unittest.TestCase):
    def test_the_check_finds_the_four_tasks(self):
        names = [task["name"] for task in object_storage_tasks(load_tasks())]
        self.assertEqual(
            names,
            [
                "Look up ClusterSecretStores",
                "Assert each ClusterSecretStore is Ready",
                "Look up ExternalSecrets in target namespaces",
                "Assert each ExternalSecret is Ready",
            ],
        )

    def test_every_object_storage_task_carries_the_enabled_condition(self):
        missing = [
            task["name"]
            for task in object_storage_tasks(load_tasks())
            if str(task.get("when", "")).strip() != ENABLED
        ]
        self.assertEqual(missing, [], "add the enabled condition to these tasks")

    def test_a_skip_notice_runs_when_object_storage_is_off(self):
        notices = [
            task
            for task in load_tasks()
            if "ansible.builtin.debug" in task and str(task.get("when", "")).strip() == SKIPPED
        ]
        self.assertEqual(len(notices), 1)
        self.assertIn("skip", dump(notices[0]).lower())

    def test_the_check_ignores_the_main_store_tasks(self):
        names = [task["name"] for task in object_storage_tasks(load_tasks())]
        self.assertNotIn("Read the main ClusterSecretStore", names)


if __name__ == "__main__":
    unittest.main()
