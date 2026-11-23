"""The identity verifier's policy (files/policies/identity-verifier.hcl).

The verifier reads the configuration of the auth methods and of the identity store, and
writes nothing. These tests read the policy file as text, so a grant that goes past a read
or a list fails here.
"""
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ROLE = ROOT / "roles/stack/operator/openbao"


def role_tasks():
    """Every task of the openbao role's main task file, including the ones in blocks."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            if "name" in node:
                found.append(node)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(yaml.safe_load((ROLE / "tasks/main.yml").read_text()))
    return found


def cli_calls(task):
    """The `args` lists of the shared-role include in a task, or an empty list."""
    include = task.get("ansible.builtin.include_role")
    if not (isinstance(include, dict) and include.get("name") == "common/openbao-cli"):
        return []
    return [call["args"] for call in task["vars"]["openbao_cli_calls"]]


def policy_grants(text):
    """The path blocks of an HCL policy as {path: sorted capabilities}."""
    found = {}
    for path, body in re.findall(r'path "([^"]+)"\s*\{([^}]*)\}', text):
        found[path] = sorted(re.findall(r'"(\w+)"', re.search(r"capabilities\s*=\s*\[([^\]]*)\]", body).group(1)))
    return found


class IdentityVerifierPolicy(unittest.TestCase):
    text = (ROLE / "files/policies/identity-verifier.hcl").read_text()

    def test_the_policy_grants_only_reads_and_lists_of_names_and_policy_lists(self):
        # Catches a write, a sudo, or a path outside the auth and identity configuration. A list
        # stanza ends in a slash because a list request does; test_openbao_identity_verifier_paths
        # checks the stanzas against the paths that the playbooks request.
        self.assertEqual(
            policy_grants(self.text),
            {
                "sys/auth": ["read"],
                "auth/+/users/": ["list"],
                "auth/+/users/+": ["read"],
                "auth/+/role/": ["list"],
                "auth/+/role/+": ["read"],
                "auth/token/roles/": ["list"],
                "auth/token/roles/+": ["read"],
                "identity/entity/id/": ["list"],
                "identity/entity/id/+": ["read"],
                "identity/group/id/": ["list"],
                "identity/group/id/+": ["read"],
            },
        )
        self.assertEqual(len(re.findall(r'(?m)^path "', self.text)), 11)

    def test_no_path_reaches_a_secret_or_a_credential_below_a_role(self):
        # Each read path ends in the single-segment wildcard, so a role's role ID and
        # SecretID paths, one segment deeper, stay out of reach.
        for path in policy_grants(self.text):
            with self.subTest(path=path):
                self.assertFalse(path.startswith("secret/"))
                self.assertNotIn("*", path)

    def test_the_policy_name_is_the_one_the_fake_and_the_role_use(self):
        defaults = yaml.safe_load((ROLE / "defaults/main.yml").read_text())

        self.assertEqual(defaults["openbao_identity_verifier_policy_name"], "identity-verifier")

    def test_the_main_play_writes_this_file_under_that_name(self):
        # The role runs the call, so the policy text is read into a fact by the task before it.
        tasks = {task["name"]: task for task in role_tasks()}
        read = tasks["Read the identity-verifier policy"]
        write = tasks["Write the identity-verifier policy"]
        calls = write["vars"]["openbao_cli_calls"]

        self.assertIn("files/policies/identity-verifier.hcl", read["ansible.builtin.set_fact"]["_openbao_policy_text_identity_verifier"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["args"], ["policy", "write", "{{ openbao_identity_verifier_policy_name }}", "-"])
        self.assertEqual(calls[0]["input"], "{{ _openbao_policy_text_identity_verifier }}\n")

class ExistingVerifierStaysWired(unittest.TestCase):
    def test_the_bootstrap_verify_play_still_imports_the_identity_model_verifier(self):
        # Catches a change that switches off the checks that run on every bootstrap verify:
        # the three logins, the separation probes and the ExternalSecrets stores.
        steps = yaml.safe_load((ROOT / "bootstrap-verify.yml").read_text())
        imports = [step["import_playbook"] for step in steps if "import_playbook" in step]

        self.assertIn("playbooks/verify-openbao-identity-model.yml", imports)

    def test_the_sandbox_verify_play_imports_the_identity_model_verifier(self):
        # Catches a sandbox verify that skips the identity checks. The checks fail on an
        # install that still holds the generate-root grant, and a sandbox install is the
        # one that would otherwise never be asked.
        steps = yaml.safe_load((ROOT / "bootstrap-sandbox-verify.yml").read_text())
        imports = [step["import_playbook"] for step in steps if "import_playbook" in step]

        self.assertIn("playbooks/verify-openbao-identity-model.yml", imports)



class AllowlistHasAnEnumerator(unittest.TestCase):
    def test_every_allowed_auth_type_has_a_source_of_principals(self):
        # Catches an allowed auth type that the holder check never enumerates: its principals
        # could hold generate-root and nothing would look. Adding a type to the allowlist needs
        # a branch in "Work out which sources of principals to read" in the same change.
        allowed = set(yaml.safe_load((ROOT / "playbooks/vars/identity-verifier.yml").read_text())[
            "verify_identity_allowed_auth_mounts"].values())
        holders = (ROOT / "playbooks/tasks/verify-identity-holders.yml").read_text()
        start = holders.index("- name: Work out which sources of principals to read")
        sources = holders[start:holders.index("- name:", start + 1)]

        self.assertTrue(allowed)
        for auth_type in allowed:
            with self.subTest(auth_type=auth_type):
                self.assertIn(f"info.type == '{auth_type}'", sources)

    def test_every_allowed_mount_is_one_that_the_role_enables(self):
        # The allowlist names mounts, so a mount that the role enables at another path, or
        # one that the role no longer enables, must not stay on it. The built-in token mount
        # is not enabled by the role.
        allowed = yaml.safe_load((ROOT / "playbooks/vars/identity-verifier.yml").read_text())[
            "verify_identity_allowed_auth_mounts"]
        enabled = [args[-1] for task in role_tasks() for args in cli_calls(task)
                   if args[:2] == ["auth", "enable"]]

        self.assertGreaterEqual(len(enabled), 3)
        self.assertEqual(allowed["token/"], "token")
        for mount, auth_type in allowed.items():
            if auth_type == "token":
                continue
            with self.subTest(mount=mount):
                self.assertEqual(mount, f"{auth_type}/")
                self.assertIn(auth_type, enabled)


if __name__ == "__main__":
    unittest.main()
