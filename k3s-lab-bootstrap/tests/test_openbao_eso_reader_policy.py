"""The eso-reader policy reads KV secrets and grants nothing else.

External Secrets Operator reads application secrets from the KV mount. The policy
that its token carries therefore needs no capability on the PKI mount, and a token
that can issue certificates is a larger grant than ESO uses. The OpenBao role holds
the only copy of the policy HCL, inside the task "Write eso-reader policy HCL in
pod". This test parses that task file as YAML, takes the HCL out of the task's
command, and checks the stanzas.
"""
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TASKS = ROOT / "roles/stack/operator/openbao/tasks/main.yml"
TASK_NAME = "Write eso-reader policy HCL in pod"
HEREDOC = re.compile(r"<< 'HCL'\n(?P<hcl>.*?)\n\s*HCL\s*$", re.S)
STANZA = re.compile(r'path\s+"(?P<path>[^"]+)"\s*\{(?P<body>[^}]*)\}')
CAPABILITIES = re.compile(r"capabilities\s*=\s*\[(?P<caps>[^\]]*)\]")
READ_ONLY = {"read", "list"}
PKI_PATH = re.compile(r"(\{\{\s*openbao_pki_path\s*\}\}|pki)(/|$)")


def eso_reader_hcl():
    tasks = yaml.safe_load(TASKS.read_text(encoding="utf-8"))
    matching = [task for task in tasks if task.get("name") == TASK_NAME]
    if len(matching) != 1:
        raise AssertionError(f"expected one task named {TASK_NAME!r}, found {len(matching)}")
    script = matching[0]["ansible.builtin.command"]["argv"][-1]
    heredoc = HEREDOC.search(script)
    if heredoc is None:
        raise AssertionError("the task has no HCL heredoc")
    return heredoc.group("hcl")


def stanzas():
    """Return (path, capabilities) for every path stanza of the policy."""
    found = []
    for stanza in STANZA.finditer(eso_reader_hcl()):
        listed = CAPABILITIES.search(stanza.group("body"))
        if listed is None:
            raise AssertionError(f"stanza {stanza.group('path')!r} has no capabilities list")
        found.append((stanza.group("path"), re.findall(r'"([^"]+)"', listed.group("caps"))))
    return found


def pki_capabilities():
    """The capabilities that the policy grants on the PKI mount, without repeats."""
    granted = {cap for path, caps in stanzas() if PKI_PATH.match(path) for cap in caps}
    return sorted(granted)


class EsoReaderPolicyTests(unittest.TestCase):
    def test_the_check_finds_the_application_secret_stanzas(self):
        paths = [path for path, _ in stanzas()]
        self.assertIn("secret/data/apps/+/admin", paths)
        self.assertIn("secret/metadata/apps", paths)
        self.assertEqual(len(paths), len(set(paths)), "a path appears in two stanzas")

    def test_no_stanza_names_the_pki_mount(self):
        pki = [path for path, _ in stanzas() if PKI_PATH.match(path)]
        self.assertEqual(pki, [], "eso-reader must grant nothing on the PKI mount")

    def test_no_stanza_names_a_path_outside_the_kv_mount(self):
        outside = [path for path, _ in stanzas() if not path.startswith("secret/")]
        self.assertEqual(outside, [], "eso-reader reads application secrets in the KV mount only")

    def test_every_capability_is_read_or_list(self):
        wider = {path: caps for path, caps in stanzas() if not set(caps) <= READ_ONLY or not caps}
        self.assertEqual(wider, {}, "eso-reader may only read and list")


if __name__ == "__main__":
    unittest.main()
