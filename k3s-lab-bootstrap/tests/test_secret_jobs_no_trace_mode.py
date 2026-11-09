"""Static check: a job or script template that handles a secret never turns on
shell trace mode.

Trace mode (`set -x`, `bash -x`, `sh -ex`, `set -o xtrace`) prints every command
after expansion. In a job that reads an object-storage key or an OpenBao token
into a variable, the trace puts that value in the pod log, and the log outlives
the job.

The check reads every template and script file under `roles/*/templates` and
`roles/*/files` that names a secret, and fails for each line that turns trace
mode on. It reads the text before Jinja renders it, so a trace flag cannot hide
behind a conditional. It also requires that the check finds the templates known
to handle a secret, so a change to the discovery rule cannot silently empty the
list.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ROLES = ROOT / "roles"
SCRIPT_SUFFIXES = {".j2", ".sh", ".yml", ".yaml"}

# A file that mentions one of these reads or mounts a credential.
SECRET_HINT = re.compile(
    r"(secretName|secretKeyRef|secret_access_key|access_key_id"
    r"|AWS_SECRET_ACCESS_KEY|BAO_TOKEN|VAULT_TOKEN|PASSWORD|_TOKEN)"
)

# Templates that must stay in the checked set.
REQUIRED = (
    "roles/base/resilience-verify/templates/verify-roundtrip-job.yml.j2",
    "roles/base/openbao-raft-snapshot/templates/openbao-raft-snapshot-cronjob.yml.j2",
    "roles/base/openbao-raft-snapshot/templates/snapshot-script.yml.j2",
)

# A short-flag cluster that contains `x`, such as -x, -ex or -euxo.
FLAG_WITH_X = r"(?<![\w-])-[a-zA-Z]*x[a-zA-Z]*(?![\w-])"

TRACE_FORMS = (
    # set -x, set -ex, set -euxo pipefail
    ("set with x flag", re.compile(r"(?<![\w-])set\s+(?:-[a-zA-Z]+\s+)*" + FLAG_WITH_X)),
    # set -o xtrace, bash -o xtrace
    ("xtrace option", re.compile(r"-o\s+xtrace\b")),
    # bash -x, /bin/sh -ex, #!/bin/bash -x, ["/bin/bash", "-ex", ...]
    ("shell invoked with x flag",
     re.compile(r"(?<![\w-])(?:ba|da|z)?sh\b[\"']?[^\n]*?" + FLAG_WITH_X)),
    # a command list item that is only a flag cluster: `- -ex`
    ("flag list item with x",
     re.compile(r"^\s*-\s+[\"']?-[a-zA-Z]*x[a-zA-Z]*[\"']?\s*$")),
)


def trace_lines(text):
    """Return (line number, form, line) for each line that turns trace mode on."""
    found = []
    for number, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#") and not line.startswith("#!"):
            continue
        for form, pattern in TRACE_FORMS:
            if pattern.search(line):
                found.append((number, form, line.strip()))
                break
    return found


def secret_templates():
    found = []
    for path in sorted(ROLES.rglob("*")):
        relative_parts = path.relative_to(ROLES).parts
        if not ("templates" in relative_parts or "files" in relative_parts):
            continue
        if not path.is_file() or path.suffix not in SCRIPT_SUFFIXES:
            continue
        if SECRET_HINT.search(path.read_text(encoding="utf-8")):
            found.append(path)
    return found


class TraceDetectorTest(unittest.TestCase):
    """The detector flags each way to turn trace mode on and spares the rest."""

    def flagged(self, text):
        return bool(trace_lines(text))

    def test_flags_trace_forms(self):
        for text in (
            "set -x",
            "  set -ex",
            "set -euxo pipefail",
            "set -o xtrace",
            "#!/bin/bash -x",
            "bash -x script.sh",
            'command: ["/bin/bash", "-ex", "/scripts/snapshot.sh"]',
            "- -xeu",
            '- "-ex"',
        ):
            with self.subTest(text=text):
                self.assertTrue(self.flagged(text))

    def test_spares_safe_forms(self):
        for text in (
            "set -euo pipefail",
            "set +x",
            "# never run with set -x",
            "- -euo",
            "- pipefail",
            'command: ["/bin/bash", "/scripts/snapshot.sh"]',
            "- /bin/sh",
            "tar -xzf archive.tgz",
        ):
            with self.subTest(text=text):
                self.assertFalse(self.flagged(text))


class SecretJobsNoTraceTest(unittest.TestCase):
    def test_required_templates_are_checked(self):
        checked = {p.relative_to(ROOT).as_posix() for p in secret_templates()}
        for required in REQUIRED:
            with self.subTest(template=required):
                self.assertIn(required, checked)

    def test_no_trace_mode_in_templates_that_handle_a_secret(self):
        failures = []
        for path in secret_templates():
            relative = path.relative_to(ROOT).as_posix()
            text = path.read_text(encoding="utf-8")
            for number, form, line in trace_lines(text):
                failures.append(f"{relative}:{number}: {form}: {line}")
        self.assertEqual(
            failures, [], "trace mode in a template that handles a secret:\n"
            + "\n".join(failures),
        )


if __name__ == "__main__":
    unittest.main()
