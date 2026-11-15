"""Guard: AWX integration reads OpenBao with its own operator login, never with the
ESO AppRole.

The ESO AppRole SecretID rotates. The copy in the break-glass file is dead after the
first rotation, and a login with it fails. The role already logs in as the operator
(ops-admin) and holds that token for the whole run. Its app-runtime-writer policy
grants read on secret/data/apps/+/runtime, which covers the NetBox and Forgejo runtime
secrets that the role reads.

The checks read the task file as text, so they guard against the common ways of
writing this defect, not every possible one.

Run from the repo root:  python3 -m unittest discover -s \
  k3s-lab-bootstrap/roles/stack/operator/awx-integration/tests -v
"""
import pathlib
import re
import unittest

TASKS = (pathlib.Path(__file__).resolve().parent.parent / "tasks/main.yml").read_text(encoding="utf-8")
CODE = "\n".join(line for line in TASKS.splitlines() if not line.lstrip().startswith("#"))
OPERATOR_STDIN = 'stdin: "{{ _awx_openbao_operator_client_token }}\\n'


def tasks(text):
    """Yield (name, body) for each `- name:` task."""
    chunks = re.split(r"^\s*- name:\s*", text, flags=re.M)[1:]
    for chunk in chunks:
        name, _, body = chunk.partition("\n")
        yield name.strip(), body


class NoEsoApproleLoginTest(unittest.TestCase):
    def test_the_role_does_not_log_in_with_an_approle(self):
        self.assertNotIn("auth/approle/login", CODE)

    def test_the_role_does_not_read_the_eso_approle_from_the_break_glass_file(self):
        self.assertNotRegex(CODE, r"eso_(role|secret)_id")
        self.assertNotRegex(CODE, r"_awx_openbao_eso_")

    def test_no_second_openbao_token_exists(self):
        # The operator token is the only token. A second one would be a second login.
        self.assertNotIn("_awx_openbao_client_token", CODE)
        self.assertNotIn("_awx_openbao_login_raw", CODE)

    def test_the_operator_token_is_not_revoked_before_the_later_writes(self):
        # The reads and the later writes share one token. A revoke between them would
        # break the writes.
        self.assertNotRegex(CODE, r"token\s+revoke")

    def test_every_openbao_read_sends_the_operator_token_on_stdin(self):
        reads = [(name, body) for name, body in tasks(CODE) if "IFS= read -r BAO_TOKEN" in body]
        self.assertGreaterEqual(len(reads), 4, [name for name, _ in reads])
        for name, body in reads:
            with self.subTest(task=name):
                self.assertIn(OPERATOR_STDIN, body)

    def test_the_source_reads_need_the_operator_login(self):
        assertion = next(body for name, body in tasks(CODE) if name == "Assert the operator login is available for AWX source-token reads")
        self.assertIn("awx_integration_persist_to_openbao | bool", assertion)


if __name__ == "__main__":
    unittest.main()
