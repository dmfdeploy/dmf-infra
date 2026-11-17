"""Render the tasks that once passed a secret as an argument and read the argument list.

For each task, the test loads the task file with Ansible's own loader, renders the command
with a marker value for every secret, and checks that the marker is in no argument and is
on the module's `stdin:`. A reader can run the same render by hand to see the same
argument list. The tasks never run.

Run these tests with Ansible's Python, because they import `ansible`.
"""
import base64
import shlex
import unittest
from pathlib import Path

from ansible.parsing.dataloader import DataLoader
from ansible.template import Templar, trust_as_template

ROOT = Path(__file__).resolve().parents[1]
COMMAND_MODULES = ("ansible.builtin.command", "ansible.builtin.shell")


def tasks_named(node, name):
    """Return every task with this name, at any depth of plays and blocks."""
    found = []
    if isinstance(node, dict):
        if node.get("name") == name and any(module in node for module in COMMAND_MODULES):
            found.append(node)
        for value in node.values():
            found.extend(tasks_named(value, name))
    elif isinstance(node, list):
        for item in node:
            found.extend(tasks_named(item, name))
    return found


def load_task(relative_path, name):
    data = DataLoader().load_from_file(str(ROOT / relative_path))
    found = tasks_named(data, name)
    if len(found) != 1:
        raise AssertionError(f"{relative_path}: {len(found)} tasks named {name!r}")
    return found[0]


def trusted(node):
    """Mark every string as a template, as Ansible does for the text of a task file."""
    if isinstance(node, str):
        return trust_as_template(node)
    if isinstance(node, dict):
        return {key: trusted(value) for key, value in node.items()}
    if isinstance(node, list):
        return [trusted(item) for item in node]
    return node


def render(task, variables):
    """Return (the argument list, the stdin text, the whole rendered module) of a command task."""
    templar = Templar(loader=DataLoader(), variables=variables)
    module = next(key for key in COMMAND_MODULES if key in task)
    args = templar.template(trusted(task[module]))
    if isinstance(args, str):
        args = {"cmd": args}
    if "argv" in args:
        argv = [str(item) for item in args["argv"]]
    else:
        argv = shlex.split(args["cmd"])
    return argv, args.get("stdin", ""), args


class SecretsNotInArgvTest(unittest.TestCase):
    def check(self, path, name, variables, secrets, no_log=True):
        task = load_task(path, name)
        argv, stdin, args = render(task, variables)
        arguments = "\n".join(argv)
        for secret in secrets:
            encoded = base64.b64encode(secret.encode()).decode()
            self.assertNotIn(secret, arguments, f"{name}: the secret is an argument")
            self.assertNotIn(encoded, arguments, f"{name}: the encoded secret is an argument")
            self.assertNotIn(secret, str(args.get("cmd", "")), f"{name}: the secret is in the command text")
            self.assertTrue(secret in stdin or encoded in stdin, f"{name}: the secret is not on stdin")
        if no_log:
            self.assertIs(task.get("no_log"), True, f"{name}: no_log is not set")
        return argv, stdin

    def test_awx_admin_password(self):
        marker = "AWX-SECRET-marker-1"
        argv, stdin = self.check(
            "roles/stack/operator/awx/tasks/main.yml",
            "Sync AWX admin password to desired secret",
            {"awx_namespace": "awx", "awx_instance_name": "awx", "awx_admin_user": "admin",
             "awx_admin_password": marker},
            [marker],
        )
        self.assertNotIn("--password", argv)
        self.assertIn("-i", argv)
        self.assertIn("UpdatePassword", stdin)

    def test_librenms_admin_user(self):
        marker = "LNMS-SECRET-marker-2"
        argv, stdin = self.check(
            "roles/modules/infra-monitoring/librenms/tasks/main.yml",
            "Create LibreNMS admin user if missing",
            {"librenms_namespace": "librenms", "librenms_admin_username": "admin",
             "librenms_admin_email": "admin@example.org", "librenms_admin_password_effective": marker},
            [marker],
        )
        self.assertNotIn("-p", " ".join(argv).split())
        self.assertIn("-i", argv)
        self.assertIn("user:add", stdin)

    def test_librenms_database_password(self):
        marker = "DB-SECRET-marker-3"
        argv, stdin = self.check(
            "roles/modules/infra-monitoring/librenms/tasks/main.yml",
            "Check LibreNMS user count",
            {"librenms_namespace": "librenms", "librenms_mysql_password": marker},
            [marker],
        )
        self.assertFalse([word for word in argv if word.startswith("-p")], argv)
        self.assertIn("--defaults-extra-file=/dev/stdin", argv)

    def test_authentik_break_glass_password(self):
        marker = "AK-SECRET-marker-4"
        argv, stdin = self.check(
            "roles/stack/operator/authentik/tasks/main.yml",
            "Create or update Authentik break-glass local user",
            {"authentik_namespace": "authentik", "authentik_release_name": "authentik",
             "authentik_breakglass_username": "break-glass", "authentik_breakglass_email": "bg@example.org",
             "authentik_breakglass_password": marker, "authentik_breakglass_display_name": "Break Glass",
             "authentik_breakglass_user_path": "users", "authentik_breakglass_user_type": "internal",
             "authentik_breakglass_group_name": "g", "authentik_breakglass_admin_group_name": "a",
             "authentik_breakglass_is_active": True},
            [marker],
        )
        self.assertFalse([word for word in argv if word.startswith("AUTHENTIK_BREAKGLASS_PASSWORD=")])
        self.assertIn("-i", argv)

    def test_cms_authentik_service_account_token(self):
        marker = "SA-TOKEN-marker-5"
        task = load_task("playbooks/696-cms-authentik-api.yml", "Create Authentik API token for DMF Console")
        argv, _, args = render(task, {
            "authentik_namespace": "authentik", "authentik_release_name": "authentik",
            "_cms_authentik_exec_sa_token": marker,
            "_cms_authentik_exec_kubeconfig": {"path": "/tmp/authentik-exec-kubeconfig.x"},
            "cms_authentik_api_token_user": "akadmin", "cms_authentik_api_token_name": "dmf-cms",
        })
        self.assertNotIn(marker, "\n".join(argv))
        self.assertNotIn("--token", argv)
        self.assertEqual(argv[argv.index("--kubeconfig") + 1], "/tmp/authentik-exec-kubeconfig.x")
        self.assertIs(task.get("no_log"), True)

    def test_cms_authentik_kubeconfig_file_is_owner_only_and_removed(self):
        marker = "SA-TOKEN-marker-6"
        data = DataLoader().load_from_file(str(ROOT / "playbooks/696-cms-authentik-api.yml"))
        blocks = [task for play in data for task in play["tasks"] if "block" in task]
        self.assertEqual(len(blocks), 1)
        block = blocks[0]
        write = next(t for t in block["block"] if "ansible.builtin.copy" in t)
        copy = write["ansible.builtin.copy"]
        self.assertEqual(copy["mode"], "0600")
        self.assertIs(write.get("no_log"), True)
        templar = Templar(loader=DataLoader(), variables={
            "_cms_authentik_exec_kubeconfig": {"path": "/tmp/x"},
            "_cms_authentik_exec_sa_token": marker,
            "_cms_authentik_cluster": {"server": "https://127.0.0.1:6443"},
            **trusted(write["vars"]),
        })
        self.assertIn(marker, templar.template(trust_as_template(copy["content"])))
        # The block removes the file whatever happens in it.
        remove = block["always"][0]["ansible.builtin.file"]
        self.assertEqual(remove["state"], "absent")
        self.assertEqual(remove["path"], copy["dest"])

    def test_k3s_join_tokens(self):
        for name, variables, marker in (
            ("Install k3s on first node (init cluster)", {"k3s_token": "K3S-marker-7"}, "K3S-marker-7"),
            ("Join node to cluster", {
                "groups": {"k3s_control": ["cp1"]},
                "hostvars": {"cp1": {"k3s_node_token": "K3S-marker-8"}},
            }, "K3S-marker-8"),
        ):
            with self.subTest(task=name):
                self.check(
                    "playbooks/300-k3s.yml", name,
                    {"k3s_version": "v1", "k3s_server_args": "--flag", "k3s_server_url": "https://cp1:6443",
                     **variables},
                    [marker], no_log=True,
                )

    def test_cms_runtime_secret(self):
        variables = {
            "cms_secret_key_effective": "CMS-KEY-marker-9",
            "cms_oidc_client_secret_effective": "CMS-OIDC-marker-10",
            "cms_namespace": "dmf-cms", "cms_runtime_secret_name": "dmf-cms-runtime",
            "cms_runtime_secret_session_key": "sessionKey", "cms_runtime_secret_oidc_key": "oidcKey",
            "_cms_secret_key_b64": {"stdout": "Q01TLU1BUktFUi0xMQ=="},
            "_cms_oidc_secret_b64": {"stdout": "T0lEQy1NQVJLRVItMTI="},
        }
        path = "roles/stack/operator/cms/tasks/main.yml"
        # The old command printed the value with `printf '%s'`, with no newline, so the encoded
        # value stays the same only if the module adds no newline to stdin.
        for name, marker in (
            ("Encode new DMF Console runtime Secret values as base64", "CMS-KEY-marker-9"),
            ("Encode DMF Console OIDC client secret as base64", "CMS-OIDC-marker-10"),
        ):
            self.check(path, name, variables, [marker])
            _, stdin, args = render(load_task(path, name), variables)
            self.assertEqual(stdin, marker)
            self.assertIs(args["stdin_add_newline"], False)
        self.check(
            path, "Update DMF Console runtime Secret (preserve existing keys like authentikApiToken)",
            variables, ["Q01TLU1BUktFUi0xMQ==", "T0lEQy1NQVJLRVItMTI="],
        )


if __name__ == "__main__":
    unittest.main()
