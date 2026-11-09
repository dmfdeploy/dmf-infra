"""Static checks for the OpenBao Raft snapshot role.

The CronJob runs a script that holds an OpenBao token and the object-storage keys,
so its log must stay clean: no trace mode, and no line that prints a credential.
The task file that this role owns for the policy fails loudly and hides no failure.
The templates render with the role's defaults, as Ansible renders them."""

import re
import unittest
from pathlib import Path

import jinja2
import yaml

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/base/openbao-raft-snapshot"
CREDENTIAL_NAMES = re.compile(r"BAO_TOKEN|ACCESS_KEY|SECRET|token", re.I)


def defaults():
    """The role's defaults, with each plain value as the role sets it and each
    templated value rendered from the others."""
    raw = yaml.safe_load((ROLE / "defaults/main.yml").read_text(encoding="utf-8"))
    env = jinja2.Environment(undefined=jinja2.ChainableUndefined)
    values = dict(raw)
    values["inventory_hostname_short"] = "k3s-test"
    for _ in range(3):
        for key, value in list(values.items()):
            if isinstance(value, str) and "{{" in value and "lookup(" not in value:
                values[key] = env.from_string(value).render(**values)
    return values


def render(template_name):
    text = (ROLE / "templates" / template_name).read_text(encoding="utf-8")
    return jinja2.Environment(undefined=jinja2.StrictUndefined).from_string(text).render(**defaults())


def cronjob():
    return yaml.safe_load(render("openbao-raft-snapshot-cronjob.yml.j2"))


def container():
    return cronjob()["spec"]["jobTemplate"]["spec"]["template"]["spec"]["containers"][0]


def script():
    configmap = yaml.safe_load(render("snapshot-script.yml.j2"))
    return configmap["data"]["snapshot.sh"]


def code_lines(text):
    """The lines of a shell script that are not comments or blank."""
    return [line.strip() for line in text.splitlines() if line.strip() and not line.strip().startswith("#")]


class CronJobTest(unittest.TestCase):
    def test_the_job_runs_the_script_without_trace_mode(self):
        command = container()["command"]
        self.assertEqual(command[0], "/bin/bash")
        for argument in command[1:]:
            with self.subTest(argument=argument):
                self.assertFalse(
                    re.fullmatch(r"-[A-Za-z]*x[A-Za-z]*", argument) or argument in ("-o", "xtrace"),
                    "the command turns on trace mode",
                )
        self.assertEqual(command[-1], "/scripts/snapshot.sh")


class ScriptTest(unittest.TestCase):
    def test_the_script_stops_on_error_unset_variables_and_failed_pipes(self):
        self.assertIn("set -euo pipefail", code_lines(script()))

    def test_the_script_never_turns_on_trace_mode(self):
        for line in code_lines(script()):
            with self.subTest(line=line):
                self.assertNotRegex(line, r"set\s+-[A-Za-z]*x|set\s+.*xtrace|bash\s+-[A-Za-z]*x|BASH_XTRACEFD|PS4=")

    def test_no_line_prints_a_credential(self):
        for line in code_lines(script()):
            if re.match(r"(echo|printf)\b", line):
                with self.subTest(line=line):
                    self.assertNotRegex(line, CREDENTIAL_NAMES)


class FailLoudlyTest(unittest.TestCase):
    def tasks_of(self, name):
        return yaml.safe_load((ROLE / "tasks" / name).read_text(encoding="utf-8"))

    def test_the_policy_task_does_not_hide_a_failure(self):
        for task in self.tasks_of("policy.yml"):
            with self.subTest(task=task["name"]):
                self.assertIsNot(task.get("failed_when"), False)
                self.assertNotIn("ignore_errors", task)

    def test_the_policy_is_written_with_the_policy_reconciler_not_a_root_token(self):
        text = (ROLE / "tasks/policy.yml").read_text(encoding="utf-8")
        self.assertIn("openbao_session_mode: policy-reconciler", text)
        self.assertNotIn("openbao_root_token", text)
        write = next(t for t in self.tasks_of("policy.yml") if t["name"] == "Write the openbao-raft-snapshot policy")
        self.assertIn("_openbao_session_client_token", write["ansible.builtin.shell"]["stdin"])

    def test_the_policy_read_fails_on_anything_but_a_missing_policy(self):
        read = next(t for t in self.tasks_of("policy.yml") if t["name"] == "Read the openbao-raft-snapshot policy")
        self.assertIn("No value found", " ".join(read["failed_when"]))
        self.assertIn("rc != 0", " ".join(read["failed_when"]))


if __name__ == "__main__":
    unittest.main()
