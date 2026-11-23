"""Static checks for the OpenBao 2.7 pin and for authenticated root token generation.

OpenBao 2.6 and later send `bao operator generate-root` only to the authenticated
sys/generate-root-token endpoints, so every caller needs a token. These checks read
the repository as text. They guard four properties:

- the server and the snapshot job run one release, pinned by tag and digest;
- the listener reloads a renewed certificate by itself;
- a narrow policy covers the two endpoints, and only the approval executor's role
  carries it (ops-admin never does);
- no playbook, role or script in this repository calls `bao operator generate-root`:
  the approval executor is the only caller, in a later slice.
"""
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
OPENBAO_ROLE = ROOT / "roles/stack/operator/openbao"
SNAPSHOT_ROLE = ROOT / "roles/base/openbao-raft-snapshot"
POLICY = OPENBAO_ROLE / "files/policies/generate-root.hcl"

PINNED = re.compile(r"^docker\.io/openbao/openbao:2\.7\.\d+@sha256:[0-9a-f]{64}$")
# A line that runs the command, as opposed to one that only names the procedure.
# It covers the shell words, a block list item, and the flow list of an `args` call.
INVOCATION = re.compile(r"operator generate-root|^\s*- generate-root\s*$|operator['\"]?\s*,\s*['\"]?generate-root")
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
# The tasks call `bao` through the shared command role, which takes the words after `bao`
# as an `args` list. The leading plain words of such a list are read here. A list that
# starts with a template is not a subcommand call that can be checked.
ARGS_LIST = re.compile(
    r"""\bargs['"]?\s*:\s*\[\s*((?:['"]?[A-Za-z-][\w=-]*['"]?\s*,\s*)*['"]?[A-Za-z-][\w=-]*['"]?)"""
)
# The smallest number of `args` calls that the scan must find in this repository. A lower
# number means that the scan no longer reads the shape the tasks use.
MINIMUM_ARGS_CALLS = 40


def subcommand(words):
    """Return (group, subcommand) for the words after `bao`, or None."""
    positional = [w for w in words if not w.startswith("-")]
    if len(positional) >= 2 and positional[0] in REAL_SUBCOMMANDS:
        return positional[0], positional[1]
    return None


def walk_args(node):
    """Yield each `args` list of plain strings in a parsed YAML document."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "args" and isinstance(value, list) and all(isinstance(w, str) for w in value):
                yield [w for w in value]
            else:
                yield from walk_args(value)
    elif isinstance(node, list):
        for value in node:
            yield from walk_args(value)


def args_calls():
    """Return the set of (file, group, subcommand) for every `args` call to `bao`.

    A task file is read twice. Its YAML gives the lists in any layout. Its text gives the
    lists that sit inside a Jinja string, which YAML does not parse."""
    found = set()
    for path in repository_files():
        relative = str(path.relative_to(ROOT))
        word_lists = [m.group(1) for _, line in code_lines(path) for m in ARGS_LIST.finditer(line)]
        word_lists = [[w.strip("'\" ") for w in text.split(",")] for text in word_lists]
        if path.suffix in {".yml", ".yaml"}:
            try:
                word_lists += list(walk_args(yaml.safe_load(path.read_text())))
            except yaml.YAMLError:
                pass
        for words in word_lists:
            call = subcommand(words)
            if call:
                found.add((relative, *call))
    return found


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

    def test_every_args_call_in_the_repository_names_a_subcommand_of_the_cli(self) -> None:
        # A subcommand chosen by a template is checked by each quoted word in it.
        invented = [
            f"{relative}: bao {group} {sub}"
            for relative, group, sub in sorted(args_calls())
            for word in (re.findall(r"'([a-z][a-z-]*)'", sub) if "{{" in sub else [sub])
            if word not in REAL_SUBCOMMANDS[group]
        ]

        self.assertEqual(invented, [])

    def test_the_args_scan_finds_the_calls_and_does_not_pass_vacuously(self) -> None:
        # Catches a scan that reads nothing, which would let every invented subcommand
        # through: it must find many calls, in more than one group and in a task file.
        calls = args_calls()
        self.assertGreaterEqual(len(calls), MINIMUM_ARGS_CALLS)
        self.assertTrue({"policy", "token", "auth", "secrets"} <= {group for _, group, _ in calls})
        self.assertIn(("policy", "write"), {(group, sub) for _, group, sub in calls})
        self.assertTrue(any(relative.endswith("openbao/tasks/main.yml") for relative, _, _ in calls))

    def test_the_check_would_catch_token_lookup_self(self) -> None:
        self.assertEqual(BAO_CALL.findall("bao token lookup-self -format=json"), [("token", "lookup-self")])
        # The same invented call in an `args` list, as a flow list in YAML and inside a
        # Jinja string, and with a leading flag.
        self.assertEqual(subcommand(["token", "lookup-self"]), ("token", "lookup-self"))
        self.assertEqual(subcommand(["-format=json", "token", "lookup-self"]), ("token", "lookup-self"))
        for line in (
            "- args: [token, lookup-self, -format=json]",
            "{%- set _ = out.append({'item': li, 'args': ['token', 'lookup-self']}) -%}",
        ):
            words = [w.strip("'\" ") for w in ARGS_LIST.search(line).group(1).split(",")]
            self.assertEqual(subcommand(words), ("token", "lookup-self"), line)
        self.assertEqual(list(walk_args(yaml.safe_load("a:\n  - args:\n      - token\n      - lookup-self\n"))),
                         [["token", "lookup-self"]])


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
        read = next(t for t in tasks if t.get("vars") is None and "_openbao_policy_text_generate_root" in str(t.get("ansible.builtin.set_fact", "")))

        # The call writes the policy by its configured name, reads the policy text from
        # stdin (the `-` word and `input`), and the text comes from the policy file.
        self.assertEqual(write["ansible.builtin.include_role"]["name"], "common/openbao-cli")
        call, = write["vars"]["openbao_cli_calls"]
        self.assertEqual(call["args"], ["policy", "write", "{{ openbao_generate_root_policy_name }}", "-"])
        self.assertIn("_openbao_policy_text_generate_root", call["input"])
        self.assertIn("files/policies/generate-root.hcl", str(read["ansible.builtin.set_fact"]))
        self.assertEqual(write["when"], "openbao_is_initialized | bool")

    def test_the_install_never_attaches_the_policy_to_ops_admin(self) -> None:
        main = (OPENBAO_ROLE / "tasks/main.yml").read_text()

        upsert = main.index("- name: Upsert operator userpass user")
        end = main.index("- name:", upsert + 1)
        self.assertNotIn("generate_root", main[upsert:end])
        self.assertNotIn("generate-root", main[upsert:end])

    def test_only_the_executor_role_carries_the_policy(self) -> None:
        defaults = yaml.safe_load((OPENBAO_ROLE / "defaults/main.yml").read_text())
        holders = sorted(
            key
            for key, value in defaults.items()
            if key.endswith("_k8s_auth_role")
            and isinstance(value, dict)
            and "{{ openbao_generate_root_policy_name }}" in str(value.get("policy"))
        )

        self.assertEqual(holders, ["openbao_executor_k8s_auth_role"])


class CallerTests(unittest.TestCase):
    def callers(self):
        return [
            path
            for path in repository_files()
            if any(INVOCATION.search(line) for _, line in code_lines(path))
        ]

    def test_no_file_of_this_repository_calls_generate_root(self) -> None:
        self.assertEqual([str(p.relative_to(ROOT)) for p in self.callers()], [])

    def test_the_caller_scan_matches_each_spelling_of_the_call(self) -> None:
        for line in (
            "bao operator generate-root -init",
            "    - generate-root",
            "- args: [operator, generate-root, -init]",
            "'args': ['operator', 'generate-root']",
        ):
            self.assertTrue(INVOCATION.search(line), line)

    def test_the_ops_admin_rotation_path_stays_retired(self) -> None:
        # The rotation of the ops-admin password is an entry of the approval catalogue,
        # run by the executor. The old play minted a root token as ops-admin.
        self.assertFalse((ROOT / "playbooks/vertical-orchestration/120-ops-admin-rotation.yml").exists())
        offenders = [
            f"{path.relative_to(ROOT)}:{number}"
            for path in repository_files()
            for number, line in enumerate(path.read_text().splitlines(), 1)
            if re.search(r"120-ops-admin-rotation|ops_admin_rotation_", line)
        ]

        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
