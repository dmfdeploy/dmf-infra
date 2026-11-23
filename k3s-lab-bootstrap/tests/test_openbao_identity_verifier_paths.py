"""Every OpenBao path the identity verifier requests is granted by its policy, and no other.

The verifier's playbooks send `bao list PATH` and `bao read PATH` with the token of
`policy-reconciler`, which holds the `identity-verifier` policy and `policy-writer`. OpenBao
matches a stanza against the path of the request, and a list request ends in a slash. A
stanza for `auth/+/role` therefore never matches a list of `auth/kubernetes/role/`.

The test reads the requests from the playbook files, so a new request that the policy does
not grant fails here and not on a live install. Matching follows OpenBao's rules, in
tests/fixtures/fake-bao/acl_match.py, which the fake bao uses too.
"""
import importlib.util
import os
import re
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ROLE = ROOT / "roles/stack/operator/openbao"
POLICY_FILE = Path(os.environ.get("VERIFIER_POLICY_FILE", ROLE / "files/policies/identity-verifier.hcl"))

_spec = importlib.util.spec_from_file_location("acl_match", ROOT / "tests/fixtures/fake-bao/acl_match.py")
acl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(acl)

# A name that stands for one object that OpenBao returned from a list: a role, a user, an ID.
NAME = "some-name"
APPROLE_ROLE = "auth/approle/role"
TASKS = sorted((ROOT / "playbooks").glob("**/*.yml"))


def verifier_files():
    """The playbook files that send a request with the reader's token."""
    return [path for path in TASKS if "_verify_token_identity_reader" in path.read_text()]


def reader_includes(path):
    """The vars of every common/openbao-cli include in a file that passes the reader's token."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            include = node.get("ansible.builtin.include_role")
            variables = node.get("vars") or {}
            if (isinstance(include, dict) and include.get("name") == "common/openbao-cli"
                    and variables.get("openbao_cli_token") == "{{ _verify_token_identity_reader }}"):
                found.append(variables)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(yaml.safe_load(path.read_text()))
    return found


def jinja_path(expr):
    """A stdin path with every `{{ ... }}` replaced by a single segment."""
    return re.sub(r"\{\{.*?\}\}", NAME, expr)


def source_list_paths():
    """The list paths of the identity check: `'list_path': EXPR` in the holders file.

    An expression that uses the mount name `m` applies to the mounts of the type in the
    `info.type == 'TYPE'` branch above it, taken from the mount allowlist.
    """
    text = (ROOT / "playbooks/tasks/verify-identity-holders.yml").read_text()
    mounts = yaml.safe_load((ROOT / "playbooks/vars/identity-verifier.yml").read_text())[
        "verify_identity_allowed_auth_mounts"]
    paths = []
    for found in re.finditer(r"'list_path': ([^}]+?)\}", text):
        expr = found.group(1)
        types = re.findall(r"info\.type == '(\w+)'", text[:found.start()])
        names = [mount.rstrip("/") for mount, kind in mounts.items() if types and kind == types[-1]] \
            if re.search(r"(?<![\w'])m(?![\w'])", expr) else [None]
        for name in names:
            parts = []
            for part in expr.split(" ~ "):
                part = part.strip()
                if re.fullmatch(r"'[^']*'", part):
                    parts.append(part[1:-1])
                elif part == "m" and name is not None:
                    parts.append(name)
                else:
                    raise AssertionError(f"cannot read the list path expression {expr!r}")
            paths.append("".join(parts))
    return paths


def call_requests(calls):
    """The (operation, path expression) pairs of an `openbao_cli_calls` value.

    A literal list of calls is read as yaml. A template builds the list in a loop, so its
    `'args': [...]` words are read from the text, and every `{{ }}`-free string literal
    in a path expression joins with a name for each variable part.
    """
    if isinstance(calls, list):
        return [(call["args"][0], call["args"][2]) for call in calls]
    found = []
    for op, expr in re.findall(r"'args': \['(list|read)', '-format=json', (.*?)\]\}", calls):
        parts = [part.strip() for part in expr.split(" ~ ")]
        found.append((op, "".join(part[1:-1] if re.fullmatch(r"'[^']*'", part) else "{{ " + part + " }}"
                                  for part in parts)))
    return found


def verifier_requests():
    """Every (operation, path) that the playbooks send with the reader's token."""
    requests = set()
    for path in verifier_files():
        for variables in reader_includes(path):
            for op, expr in call_requests(variables["openbao_cli_calls"]):
                if "_verify_src" in expr:
                    continue   # a source's own paths, added below
                requests.add((op, jinja_path(expr)))
    for list_path in source_list_paths():
        requests.add(("list", list_path))
        requests.add(("read", f"{list_path}/{NAME}"))
    return requests


def writer_stanzas():
    """The inline policy-writer policy that tasks/main.yml writes."""
    main = (ROLE / "tasks/main.yml").read_text()
    start = main.index("policy-writer-policy.hcl << 'HCL'")
    return acl.parse_policy(main[start:main.index("\n        HCL", start)])


class MatchingFollowsOpenBao(unittest.TestCase):
    def test_a_list_request_ends_in_a_slash_and_a_read_does_not(self):
        self.assertEqual(acl.request_path("list", "auth/kubernetes/role"), "auth/kubernetes/role/")
        self.assertEqual(acl.request_path("list", "auth/kubernetes/role/"), "auth/kubernetes/role/")
        self.assertEqual(acl.request_path("read", "auth/kubernetes/role/x"), "auth/kubernetes/role/x")

    def test_the_trailing_slash_is_part_of_the_path(self):
        self.assertFalse(acl.matches("auth/+/role", "auth/kubernetes/role/"))
        self.assertTrue(acl.matches("auth/+/role/", "auth/kubernetes/role/"))
        self.assertFalse(acl.matches("auth/+/role/", "auth/kubernetes/role"))

    def test_a_plus_matches_exactly_one_segment(self):
        self.assertTrue(acl.matches("auth/+/role/+", "auth/kubernetes/role/web"))
        self.assertFalse(acl.matches("auth/+/role/+", "auth/kubernetes/role/web/secret-id"))
        self.assertFalse(acl.matches("auth/+/role/+", "auth/role/web"))
        self.assertFalse(acl.matches("auth/+/role/+", "auth/kubernetes/role/"))
        self.assertFalse(acl.matches("auth/+/role/+", "auth//role/web"))

    def test_a_star_is_a_prefix_glob_only_at_the_end(self):
        self.assertTrue(acl.matches("sys/policies/acl/*", "sys/policies/acl/"))
        self.assertTrue(acl.matches("sys/policies/acl/*", "sys/policies/acl/a/b"))
        self.assertFalse(acl.matches("sys/policies/acl/*", "sys/policies/acl"))
        self.assertTrue(acl.matches("auth/+/role/*", "auth/kubernetes/role/web/secret-id"))
        self.assertTrue(acl.matches("auth/*/role", "auth/*/role"))
        self.assertFalse(acl.matches("auth/*/role", "auth/kubernetes/role"))

    def test_an_exact_path_matches_only_itself(self):
        self.assertTrue(acl.matches("sys/auth", "sys/auth"))
        self.assertFalse(acl.matches("sys/auth", "sys/auth/"))
        self.assertFalse(acl.matches("sys/auth", "sys/auth/x"))

    def test_a_capability_must_be_listed_and_deny_wins(self):
        stanzas = [("a/b", ["read"]), ("a/c", ["read", "deny"]), ("a/+/", ["list"])]
        self.assertTrue(acl.allows(stanzas, "read", "a/b"))
        self.assertFalse(acl.allows(stanzas, "update", "a/b"))
        self.assertFalse(acl.allows(stanzas, "read", "a/c"))
        self.assertTrue(acl.allows(stanzas, "list", "a/d"))
        self.assertTrue(acl.allows(stanzas, "list", "a/d/"))
        self.assertFalse(acl.allows(stanzas, "read", "z"))

    def test_the_most_specific_stanza_decides(self):
        stanzas = [("a/*", ["read"]), ("a/+", ["list"]), ("a/b", ["update"])]
        self.assertEqual(acl.capabilities_on(stanzas, "read", "a/b"), ["update"])
        self.assertEqual(acl.capabilities_on(stanzas, "read", "a/c"), ["list"])
        self.assertEqual(acl.capabilities_on(stanzas, "read", "a/c/d"), ["read"])


class TheVerifierRequestsAreFound(unittest.TestCase):
    def test_the_extraction_finds_the_requests_the_playbooks_make(self):
        # Catches an extraction that returns too little, which would let every check below pass.
        requests = verifier_requests()
        expected = {
            ("read", "sys/auth"),
            ("list", "sys/policies/acl"),
            ("read", f"sys/policies/acl/{NAME}"),
            ("list", "auth/userpass/users"), ("read", f"auth/userpass/users/{NAME}"),
            ("list", APPROLE_ROLE), ("read", f"{APPROLE_ROLE}/{NAME}"),
            ("list", "auth/kubernetes/role"), ("read", f"auth/kubernetes/role/{NAME}"),
            ("list", "auth/token/roles"), ("read", f"auth/token/roles/{NAME}"),
            ("list", "identity/entity/id"), ("read", f"identity/entity/id/{NAME}"),
            ("list", "identity/group/id"), ("read", f"identity/group/id/{NAME}"),
        }
        self.assertEqual(requests, expected)

    def test_the_token_goes_in_the_token_variable_and_never_in_an_argument_list(self):
        # The shared role sends the token on stdin. A token word in `args` would reach
        # kubectl's argument list instead.
        includes = [v for path in verifier_files() for v in reader_includes(path)]
        self.assertGreaterEqual(len(includes), 6)
        for variables in includes:
            with self.subTest(operation=variables["openbao_cli_operation"]):
                self.assertNotIn("_verify_token", str(variables["openbao_cli_calls"]))

    def test_the_reader_token_holds_the_two_policies_the_check_uses(self):
        main = (ROLE / "tasks/main.yml").read_text()
        self.assertIn("'token_policies': openbao_policy_writer_policy_name ~ ',' ~ openbao_identity_verifier_policy_name", main)


class ThePolicyGrantsTheRequests(unittest.TestCase):
    verifier = acl.parse_policy(POLICY_FILE.read_text())
    writer = writer_stanzas()

    def decider(self, op, path):
        """The (policy name, stanza path) that grants a request, or None."""
        for name, stanzas in (("identity-verifier", self.verifier), ("policy-writer", self.writer)):
            if acl.allows(stanzas, op, path):
                return name, max((p for p, _ in stanzas if acl.matches(p, acl.request_path(op, path))),
                                 key=acl.specificity)
        return None

    def test_every_request_is_granted(self):
        for op, path in sorted(verifier_requests()):
            with self.subTest(op=op, path=path):
                self.assertIsNotNone(self.decider(op, path), f"no stanza grants `bao {op} {path}`")

    def test_every_stanza_of_the_verifier_policy_is_used_with_exactly_the_capabilities_requested(self):
        # Catches a grant that nothing requests, and a capability beyond the ones requested.
        used = {}
        for op, path in verifier_requests():
            found = self.decider(op, path)
            if found and found[0] == "identity-verifier":
                used.setdefault(found[1], set()).add(op)
        granted = {path: set(caps) for path, caps in self.verifier}
        self.assertEqual(used, granted)

    def test_no_request_matches_two_stanzas_of_the_verifier_policy(self):
        # The most specific stanza decides, so an overlap would make a grant depend on that rule.
        for op, path in sorted(verifier_requests()):
            with self.subTest(op=op, path=path):
                hits = [p for p, _ in self.verifier if acl.matches(p, acl.request_path(op, path))]
                self.assertLessEqual(len(hits), 1, hits)


class ThePolicyGrantsNothingElse(unittest.TestCase):
    verifier = acl.parse_policy(POLICY_FILE.read_text())

    def test_paths_outside_the_reads_and_lists_are_refused(self):
        refused = [
            ("read", f"{APPROLE_ROLE}/"),                         # a list stanza does not grant a read
            ("read", APPROLE_ROLE),
            ("list", f"{APPROLE_ROLE}/{NAME}"),                   # a read stanza does not grant a list
            ("list", f"{APPROLE_ROLE}/{NAME}/"),
            ("read", f"{APPROLE_ROLE}/{NAME}/role-id"),           # a credential below a role
            ("read", f"{APPROLE_ROLE}/{NAME}/secret-id"),
            ("update", f"{APPROLE_ROLE}/{NAME}/secret-id"),
            ("update", f"{APPROLE_ROLE}/{NAME}"),
            ("update", f"auth/userpass/users/{NAME}"),
            ("delete", f"auth/userpass/users/{NAME}"),
            ("read", f"auth/userpass/users/{NAME}/password"),
            ("read", "auth/kubernetes/config"),
            ("update", "auth/kubernetes/config"),
            ("read", "auth/token/lookup-accessor"),
            ("list", "auth/token/accessors"),
            ("read", "sys/policies/acl"),
            ("list", "sys/policies/acl"),
            ("read", f"sys/policies/acl/{NAME}"),
            ("read", "sys/mounts"),
            ("update", "sys/auth"),
            ("read", "secret/data/anything"),
            ("list", "secret/metadata"),
        ]
        for op, path in refused:
            with self.subTest(op=op, path=path):
                self.assertFalse(acl.allows(self.verifier, op, path))

    def test_the_policy_grants_only_read_and_list(self):
        for path, caps in self.verifier:
            with self.subTest(path=path):
                self.assertTrue(set(caps) <= {"read", "list"})
                self.assertEqual(path.endswith("/"), caps == ["list"])


if __name__ == "__main__":
    unittest.main()
