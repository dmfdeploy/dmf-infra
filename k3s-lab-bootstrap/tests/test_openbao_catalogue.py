"""The catalogue of setup requests and its pinned policies.

files/catalogue.yml lists the setup requests that the approval service offers. Each
entry pins a policy file by SHA-256 digest, lists the change's requests, and states
their number, which becomes the use limit of the scoped token. These tests check the
digests, that each policy grants exactly the listed requests (and the token revoke),
that the password rotation cannot reach the policies of the user, and that the
ConfigMap carries every file unchanged. Run them with the Python of Ansible: the
render test uses its templating.
"""
import hashlib
import re
import unittest
from pathlib import Path

import yaml
from ansible.parsing.dataloader import DataLoader
from ansible.plugins.loader import init_plugin_loader
from ansible.template import Templar, trust_as_template

ROOT = Path(__file__).resolve().parents[1]
ROLE = ROOT / "roles/stack/operator/openbao"
CATALOGUE = ROLE / "files/catalogue.yml"
TEMPLATE = ROLE / "templates/catalogue-configmap.yml.j2"
TASKS = ROLE / "tasks/catalogue.yml"
MAIN = ROLE / "tasks/main.yml"

ROTATION = "ops-admin-password-rotation"
PASSWORD_PATH = "auth/userpass/users/ops-admin/password"
REVOKE_SELF = ("auth/token/revoke-self", "update")
FIELDS = {
    "name", "consequence", "policy_name", "policy_file", "policy_sha256",
    "requests", "request_count", "parameters",
}
STANZA_START = re.compile(r'path\s+"(?P<path>[^"]+)"\s*\{')
CAPABILITIES = re.compile(r"capabilities\s*=\s*\[(?P<caps>[^\]]*)\]")
ALLOWED = re.compile(r"allowed_parameters\s*=\s*\{(?P<body>[^{}]*)\}")
ALLOWED_KEY = re.compile(r'"(?P<key>[^"]+)"\s*=\s*\[(?P<values>[^\]]*)\]')


def catalogue():
    return yaml.safe_load(CATALOGUE.read_text(encoding="utf-8"))


def entry(name):
    return next(e for e in catalogue()["entries"] if e["name"] == name)


def policy_text(name):
    return (ROLE / entry(name)["policy_file"]).read_text(encoding="utf-8")


def stanzas(text):
    """Return a dict per path stanza of an HCL policy: path, capabilities, allowed_parameters.

    allowed_parameters is None when the stanza has none, else a dict of key to values.
    A stanza ends at its matching closing brace, so a nested allowed_parameters block
    stays inside it.
    """
    found = []
    for start in STANZA_START.finditer(text):
        depth, end = 1, start.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(text[end], 0)
            end += 1
        body = text[start.end():end - 1]
        listed = CAPABILITIES.search(body)
        if listed is None:
            raise AssertionError(f"stanza {start.group('path')!r} has no capabilities list")
        allowed = ALLOWED.search(body)
        found.append({
            "path": start.group("path"),
            "capabilities": re.findall(r'"([^"]+)"', listed.group("caps")),
            "allowed": None if allowed is None else {
                key.group("key"): re.findall(r'"([^"]*)"', key.group("values"))
                for key in ALLOWED_KEY.finditer(allowed.group("body"))
            },
        })
    return found


def grants(text):
    return {(s["path"], cap) for s in stanzas(text) for cap in s["capabilities"]}


def documented_requests(name):
    return {(r["path"], r["capability"]) for r in entry(name)["requests"]}


class CatalogueDigests(unittest.TestCase):
    def test_every_digest_matches_the_policy_file(self):
        # Catches a policy edited without its digest, and a digest copied from the wrong file.
        for e in catalogue()["entries"]:
            digest = hashlib.sha256((ROLE / e["policy_file"]).read_bytes()).hexdigest()
            self.assertEqual(e["policy_sha256"], digest, e["name"])

    def test_entries_have_the_documented_fields_and_unique_names(self):
        # Catches a missing field, an extra undocumented one, or a name or policy name used twice.
        data = catalogue()
        self.assertEqual(1, data["schema_version"])
        entries = data["entries"]
        for e in entries:
            self.assertEqual(FIELDS, set(e), e.get("name"))
            self.assertIsInstance(e["request_count"], int)
            self.assertGreater(e["request_count"], 0)
        for key in ("name", "policy_name", "policy_file"):
            values = [e[key] for e in entries]
            self.assertEqual(len(values), len(set(values)), key)

    def test_request_count_equals_the_documented_request_list(self):
        # Catches a count that no longer matches the entry's own request list, which would
        # leave the token with a spare use or too few. The list is the entry's own design:
        # the entry sets a password with one request. It is the only way left to rotate
        # the ops-admin password: the old rotation playbook is gone.
        for e in catalogue()["entries"]:
            pairs = [(r["path"], r["capability"]) for r in e["requests"]]
            self.assertEqual(len(pairs), len(set(pairs)), e["name"])
            self.assertEqual(len(pairs), e["request_count"], e["name"])
        self.assertEqual({(PASSWORD_PATH, "update")}, documented_requests(ROTATION))
        self.assertEqual(1, entry(ROTATION)["request_count"])


class RotationPolicy(unittest.TestCase):
    def setUp(self):
        self.text = policy_text(ROTATION)
        self.stanzas = stanzas(self.text)

    def test_policy_has_no_wildcard_and_no_sys_path(self):
        # Catches a glob such as users/*, a "+" segment, any sys/ path, any capability other
        # than update, and sudo.
        self.assertTrue(self.stanzas)
        for s in self.stanzas:
            self.assertFalse(set(s["path"]) & set("*+"), s["path"])
            self.assertFalse(s["path"].startswith("sys/"), s["path"])
            self.assertEqual(["update"], s["capabilities"], s["path"])

    def test_policy_grants_exactly_the_documented_requests(self):
        # Catches a stanza that grants more than the entry lists (an extra capability or
        # path), a listed request that no stanza grants, and a policy without the token
        # revoke, which the executor needs because the token has no default policy.
        self.assertEqual(documented_requests(ROTATION) | {REVOKE_SELF}, grants(self.text))

    def test_policy_cannot_reach_the_policies_of_the_user(self):
        # Catches a grant on the user path (its update takes token_policies) or on the
        # policies path, either of which lets the token attach a policy to ops-admin.
        for s in self.stanzas:
            self.assertFalse(
                s["path"].startswith("auth/userpass/users/") and s["path"] != PASSWORD_PATH,
                s["path"],
            )
            self.assertNotIn("policies", s["path"])

    def test_password_stanza_limits_the_parameters_to_password(self):
        # Catches a stanza without allowed_parameters (OpenBao then accepts every parameter
        # of the endpoint, password_hash included), one that lists more than password, and
        # one that limits password to fixed values, which no random password could match.
        password = [s for s in self.stanzas if s["path"] == PASSWORD_PATH]
        self.assertEqual(1, len(password))
        self.assertEqual({"password": []}, password[0]["allowed"])

    def test_every_catalogue_policy_grants_revoke_self(self):
        # Catches a catalogue policy that cannot end its own token.
        for e in catalogue()["entries"]:
            self.assertIn(REVOKE_SELF, grants(policy_text(e["name"])), e["name"])


class CatalogueRole(unittest.TestCase):
    def test_role_writes_policies_and_renders_the_configmap(self):
        # Catches a catalogue that the role never applies: the task file must be included
        # from main.yml, write each policy by its stable name, and render the ConfigMap.
        main = MAIN.read_text(encoding="utf-8")
        self.assertIn("include_tasks: catalogue.yml", main)
        tasks = yaml.safe_load(TASKS.read_text(encoding="utf-8"))
        write = next(t for t in tasks if t["name"].startswith("Write the pinned policy"))
        # The role runs the call through the shared OpenBao command role. The call writes
        # the policy by its stable name, reads the policy text from stdin (the `-` word),
        # and gets the token through the token variable. The shared role keeps the token
        # and the input off the command line and out of the log.
        self.assertEqual("common/openbao-cli", write["ansible.builtin.include_role"]["name"])
        call_vars = write["vars"]
        calls = call_vars["openbao_cli_calls"]
        self.assertIn("'args': ['policy', 'write', item.policy_name, '-']", calls)
        self.assertIn("'input': _openbao_catalogue_policy_text[item.policy_name]", calls)
        self.assertIn("_openbao_policy_apply_token", call_vars["openbao_cli_token"])
        self.assertNotIn("token", calls)
        self.assertEqual("openbao_is_initialized | bool", write["when"])
        render = next(t for t in tasks if t["name"] == "Render the catalogue ConfigMap")
        self.assertIn("catalogue-configmap.yml.j2", render["kubernetes.core.k8s"]["definition"])

    def test_configmap_carries_every_file_byte_for_byte(self):
        # Catches an entry or policy file missing from the render, and a render that alters
        # bytes (a trimmed trailing newline, changed indentation or a re-encoded character).
        init_plugin_loader()
        templar = Templar(
            loader=DataLoader(),
            variables={
                "role_path": str(ROLE),
                "openbao_setup_requests_namespace": "dmf-setup-requests",
                "openbao_catalogue_configmap_name": "setup-catalogue",
            },
        )
        text = templar.template(trust_as_template(TEMPLATE.read_text(encoding="utf-8")))
        rendered = text if isinstance(text, dict) else yaml.safe_load(text)
        self.assertEqual("ConfigMap", rendered["kind"])
        self.assertEqual("dmf-setup-requests", rendered["metadata"]["namespace"])
        data = rendered["data"]
        self.assertEqual(CATALOGUE.read_bytes(), data["catalogue.yml"].encode("utf-8"))
        entries = catalogue()["entries"]
        self.assertEqual({"catalogue.yml"} | {Path(e["policy_file"]).name for e in entries}, set(data))
        for e in entries:
            self.assertEqual((ROLE / e["policy_file"]).read_bytes(), data[Path(e["policy_file"]).name].encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
