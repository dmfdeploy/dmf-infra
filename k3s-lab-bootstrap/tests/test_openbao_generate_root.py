"""Static checks for the OpenBao 2.7 pin and for authenticated root token generation.

OpenBao 2.6 and later send `bao operator generate-root` only to the authenticated
sys/generate-root-token endpoints, so every caller needs a token. These checks read
the repository as text. They guard four properties:

- the server and the snapshot job run one release, pinned by tag and digest;
- the listener reloads a renewed certificate by itself;
- ops-admin carries a narrow policy for the two endpoints;
- every caller of generate-root logs in as ops-admin, passes the login token on
  stdin and revokes it in an `always` section.
"""
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OPENBAO_ROLE = ROOT / "roles/stack/operator/openbao"
SNAPSHOT_ROLE = ROOT / "roles/base/openbao-raft-snapshot"
ROTATION_PLAY = ROOT / "playbooks/vertical-orchestration/120-ops-admin-rotation.yml"
POLICY = OPENBAO_ROLE / "files/policies/generate-root.hcl"

PINNED = re.compile(r"^docker\.io/openbao/openbao:2\.7\.\d+@sha256:[0-9a-f]{64}$")
# A line that runs the command, as opposed to one that only names the procedure.
INVOCATION = re.compile(r"operator generate-root|^\s*- generate-root\s*$")
SCANNED_SUFFIXES = {".yml", ".yaml", ".j2", ".sh", ".hcl"}


def default(role: Path, name: str) -> str:
    return yaml.safe_load((role / "defaults/main.yml").read_text())[name]


def code_lines(path: Path):
    """Yield (number, line) for each line that is not a whole-line comment."""
    for number, line in enumerate(path.read_text().splitlines(), 1):
        if not line.lstrip().startswith("#"):
            yield number, line


def repository_files():
    for path in sorted(ROOT.rglob("*")):
        relative = path.relative_to(ROOT)
        if path.is_file() and path.suffix in SCANNED_SUFFIXES and relative.parts[0] != "tests":
            yield path


def parse_policy(text: str) -> dict:
    """Return {path: set(capabilities)} for the path stanzas of an HCL policy."""
    stanzas = {}
    for match in re.finditer(r'path\s+"([^"]+)"\s*\{(.*?)\}', text, re.S):
        capabilities = re.search(r"capabilities\s*=\s*\[(.*?)\]", match.group(2), re.S)
        stanzas[match.group(1)] = set(re.findall(r'"([^"]+)"', capabilities.group(1)))
    return stanzas


# The subcommands of the groups below in the command registry of OpenBao v2.5.2
# (command/commands.go) and v2.7.2 (internal/command/commands.go). The CLI has no
# `token lookup-self`: the call to the endpoint auth/token/lookup-self is `token lookup`
# with no argument.
REAL_SUBCOMMANDS = {
    "token": {"create", "capabilities", "lookup", "renew", "revoke"},
    "operator": {"diagnose", "generate-root", "init", "key-status", "members", "migrate", "raft", "rekey",
                 "rotate", "rotate-keys", "seal", "step-down", "unseal", "validate-config"},
    "policy": {"delete", "fmt", "list", "read", "write"},
    "auth": {"tune", "disable", "enable", "help", "list", "move"},
    "secrets": {"disable", "enable", "list", "move", "tune"},
    "kv": {"put", "patch", "rollback", "get", "delete", "list", "destroy", "undelete", "enable-versioning", "metadata", "scan"},
    "audit": {"disable", "enable", "list"},
    "lease": {"renew", "lookup", "revoke"},
}
BAO_CALL = re.compile(r"\bbao\s+(?:-[\w=-]+\s+)*(" + "|".join(REAL_SUBCOMMANDS) + r")\s+([a-z][a-z-]*)")


class CliRegistryTests(unittest.TestCase):
    def test_every_bao_subcommand_in_the_repository_exists_in_the_cli(self) -> None:
        invented = [
            f"{path.relative_to(ROOT)}:{number}: bao {group} {sub}"
            for path in repository_files()
            for number, line in code_lines(path)
            for group, sub in BAO_CALL.findall(line)
            if sub not in REAL_SUBCOMMANDS[group]
        ]

        self.assertEqual(invented, [])

    def test_the_check_would_catch_token_lookup_self(self) -> None:
        self.assertEqual(BAO_CALL.findall("bao token lookup-self -format=json"), [("token", "lookup-self")])


class ImagePinTests(unittest.TestCase):
    def test_server_and_snapshot_image_are_one_digest_pinned_release(self) -> None:
        server = default(OPENBAO_ROLE, "openbao_image")
        snapshot = default(SNAPSHOT_ROLE, "openbao_raft_snapshot_image")

        self.assertRegex(server, PINNED)
        self.assertEqual(server, snapshot)

    def test_no_latest_tag_remains_for_openbao(self) -> None:
        offenders = [
            f"{path.relative_to(ROOT)}:{number}"
            for path in repository_files()
            for number, line in code_lines(path)
            if re.search(r"openbao/openbao:latest", line)
        ]

        self.assertEqual(offenders, [])


class ListenerTests(unittest.TestCase):
    def test_listener_reloads_a_renewed_certificate_by_itself(self) -> None:
        config = (OPENBAO_ROLE / "templates/config.hcl.j2").read_text()
        listener = re.search(r'listener "tcp" \{(.*?)\n\}', config, re.S).group(1)

        self.assertRegex(listener, r"(?m)^\s*tls_auto_reload\s*=\s*true\s*$")

    def test_legacy_unauthenticated_endpoints_stay_off(self) -> None:
        offenders = [
            f"{path.relative_to(ROOT)}:{number}"
            for path in repository_files()
            for number, line in code_lines(path)
            if re.search(r"disable_unauthed_generate_root_endpoints\s*=\s*false", line)
        ]

        self.assertEqual(offenders, [])


class PolicyTests(unittest.TestCase):
    def test_policy_grants_the_generate_root_token_paths_and_nothing_broader(self) -> None:
        stanzas = parse_policy("\n".join(line for _, line in code_lines(POLICY)))

        self.assertEqual(
            stanzas,
            {
                "sys/generate-root-token/attempt": {"read", "update", "delete"},
                "sys/generate-root-token/update": {"update"},
            },
        )

    def test_role_writes_the_policy_on_every_run(self) -> None:
        tasks = yaml.safe_load((OPENBAO_ROLE / "tasks/main.yml").read_text())
        write = next(t for t in tasks if t.get("name") == "Write the generate-root policy")

        self.assertIn("bao policy write {{ openbao_generate_root_policy_name }} -", write["ansible.builtin.shell"]["cmd"])
        self.assertIn("files/policies/generate-root.hcl", write["ansible.builtin.shell"]["stdin"])
        self.assertEqual(write["when"], "openbao_is_initialized | bool")

    def test_ops_admin_carries_the_policy(self) -> None:
        main = (OPENBAO_ROLE / "tasks/main.yml").read_text()
        rotation = yaml.safe_load(ROTATION_PLAY.read_text())[0]

        upsert = main.index("- name: Upsert operator userpass user")
        self.assertIn("openbao_generate_root_policy_name", main[upsert:upsert + 1200])
        self.assertIn(
            "{{ openbao_generate_root_policy_name | default('generate-root') }}",
            rotation["vars"]["ops_admin_rotation_attach_policy"],
        )


class CallerTests(unittest.TestCase):
    def callers(self):
        return [
            path
            for path in repository_files()
            if any(INVOCATION.search(line) for _, line in code_lines(path))
        ]

    def test_the_rotation_play_is_the_only_caller(self) -> None:
        self.assertEqual([p.relative_to(ROOT) for p in self.callers()], [ROTATION_PLAY.relative_to(ROOT)])

    def test_every_caller_logs_in_as_ops_admin_and_revokes_the_login_token(self) -> None:
        for path in self.callers():
            with self.subTest(path=str(path.relative_to(ROOT))):
                play = yaml.safe_load(path.read_text())[0]
                block = next(t for t in play["tasks"] if "block" in t)
                names = [t["name"] for t in block["block"]]

                self.assertEqual(names[0], "Log in as ops-admin for root token generation")
                login = block["block"][0]["ansible.builtin.shell"]
                self.assertIn("auth/userpass/login/", login["cmd"])
                self.assertIn("password=-", login["cmd"])
                tokens = next(t for t in block["always"] if t["name"] == "Revoke the tokens")
                for task, token in [
                    (tokens["block"][0], "root"),
                    (tokens["always"][0], "login"),
                ]:
                    shell = task["ansible.builtin.shell"]
                    self.assertIn("bao token revoke -self", shell["cmd"])
                    self.assertEqual(shell["stdin"], "{{ _ops_admin_rotation_" + token + "_token }}\n")
                    self.assertEqual(task["when"], "_ops_admin_rotation_" + token + "_token is defined")

    def test_every_caller_runs_in_the_active_pod(self) -> None:
        for path in self.callers():
            text = path.read_text()
            with self.subTest(path=str(path.relative_to(ROOT))):
                self.assertIn("tasks_from: find-active-pod.yml", text)
                self.assertNotIn("resources[0].metadata.name", text)

    def test_the_otp_is_passed_on_stdin_and_never_in_an_argument(self) -> None:
        for path in self.callers():
            for number, line in code_lines(path):
                if "-otp=" in line:
                    with self.subTest(path=str(path.relative_to(ROOT)), line=number):
                        self.assertIn('-otp="$OTP"', line)
            play = yaml.safe_load(path.read_text())[0]
            script = play["vars"]["ops_admin_rotation_generate_root_otp_script"]
            self.assertIn("IFS= read -r OTP", script)
            block = next(t for t in play["tasks"] if "block" in t)["block"]
            for task in block:
                text = str(task.get("ansible.builtin.shell", task.get("ansible.builtin.command", "")))
                if "_otp_script" in text:
                    with self.subTest(task=task["name"]):
                        module = next(v for k, v in task.items() if k.startswith("ansible.builtin."))
                        self.assertIn("_rotation_otp }}", module["stdin"])
                        self.assertNotIn("_rotation_otp", module.get("cmd", "") + " ".join(map(str, module.get("argv", []))))

    def test_every_generate_root_task_takes_the_token_on_stdin(self) -> None:
        for path in self.callers():
            play = yaml.safe_load(path.read_text())[0]
            script = play["vars"]["ops_admin_rotation_generate_root_script"]
            self.assertIn("IFS= read -r BAO_TOKEN", script)
            self.assertIn('exec bao operator generate-root "$@"', script)
            self.assertNotIn("{{", script)

            block = next(t for t in play["tasks"] if "block" in t)["block"]
            users = [t for t in block if "ops_admin_rotation_generate_root_" in str(t)]
            self.assertEqual(len(users), 6)
            for task in users:
                with self.subTest(task=task["name"]):
                    module = next(v for k, v in task.items() if k.startswith("ansible.builtin."))
                    self.assertTrue(task["no_log"])
                    self.assertTrue(module["stdin"].startswith("{{ _ops_admin_rotation_login_token }}\n"))
                    self.assertNotIn("environment", task)
                    command = module.get("cmd", "") + " ".join(map(str, module.get("argv", [])))
                    self.assertNotIn("_login_token", command)


if __name__ == "__main__":
    unittest.main()
