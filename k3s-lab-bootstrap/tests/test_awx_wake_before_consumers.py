"""Check that each AWX consumer in the configure playbooks starts on a woken AWX.

The idle reaper puts AWX to sleep after a quiet period, and only changes made
in AWX count as activity. A consumer that runs for a long time without changing
AWX can leave AWX asleep before the next consumer starts. So when a configure
playbook puts AWX to sleep, the import directly before each consumer must wake
it.

The test reads each playbook's import list as data. Nothing renders and no task
runs.
"""
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIGURE_PLAYBOOKS = ("bootstrap-sandbox-configure.yml", "bootstrap-configure.yml")
CONSUMERS = (
    "playbooks/693-awx-integration.yml",
    "playbooks/697-cms-awx-token.yml",
    "playbooks/699-cms-smoke-test.yml",
)
WAKE = "playbooks/awx-wake.yml"
SLEEP = "playbooks/awx-sleep.yml"


def imports(name):
    plays = yaml.safe_load((ROOT / name).read_text())
    return [p for p in plays if "import_playbook" in p]


class AwxWakeBeforeConsumersTest(unittest.TestCase):
    def test_each_consumer_directly_follows_a_wake(self):
        for name in CONFIGURE_PLAYBOOKS:
            entries = imports(name)
            paths = [e["import_playbook"] for e in entries]
            if SLEEP not in paths:
                # AWX is never put to sleep before the consumers, so it stays awake.
                continue
            checked = 0
            for i, path in enumerate(paths):
                if path not in CONSUMERS:
                    continue
                checked += 1
                prev = entries[i - 1] if i else {}
                with self.subTest(playbook=name, consumer=path):
                    self.assertEqual(prev.get("import_playbook"), WAKE, "no wake directly before the consumer")
                    self.assertEqual(prev.get("vars", {}).get("awx_presence_state"), "awake")
            self.assertGreater(checked, 0, f"{name} sleeps AWX but imports no consumer")

    def test_each_wake_carries_every_tag_of_the_consumer_it_precedes(self):
        # A run selected by a consumer's tag must also run that consumer's wake.
        for name in CONFIGURE_PLAYBOOKS:
            entries = imports(name)
            paths = [e["import_playbook"] for e in entries]
            if SLEEP not in paths:
                continue
            for i, path in enumerate(paths):
                if path not in CONSUMERS or not i:
                    continue
                missing = set(entries[i].get("tags", [])) - set(entries[i - 1].get("tags", []))
                with self.subTest(playbook=name, consumer=path):
                    self.assertFalse(missing, f"the wake lacks the consumer's tags: {sorted(missing)}")

    def test_the_sandbox_playbook_sleeps_awx_and_runs_all_three_consumers(self):
        paths = [e["import_playbook"] for e in imports("bootstrap-sandbox-configure.yml")]
        self.assertIn(SLEEP, paths)
        for consumer in CONSUMERS:
            self.assertIn(consumer, paths)


if __name__ == "__main__":
    unittest.main()
