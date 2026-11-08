"""Static check: no OpenBao task passes a share, token or password as a command argument.

This is a known defect class: a command argument shows in the process list
of the node and of the pod. A task passes a secret on stdin, or through the task
`environment:` and a shell variable instead. The check reads the OpenBao role's task
files and the OpenBao playbooks as text and reports every Jinja expression that names
secret material inside an `argv:` list or a `cmd:` string. It also reports a shell
variable that is expanded into a credential argument, such as `password="$P"`: the
shell expands it into the child process's argument list, which any process in the
pod can read. Send the value on stdin instead (`password=-`, or a JSON body).
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = sorted(
    list((ROOT / "roles/stack/operator/openbao/tasks").glob("*.yml"))
    + list((ROOT / "playbooks/vertical-security").glob("10*-openbao*.yml"))
)
JINJA = re.compile(r"\{\{(.*?)\}\}")
# A credential key set to an expanded shell variable: password="$P", secret_id="$S".
SHELL_CREDENTIAL = re.compile(r"\b[a-z_]*(?:password|secret_id|token|key)=\"?\$[A-Za-z_{]", re.I)
SECRET = re.compile(r"unseal_key|root_token|_token|password|secret_id|share_|_share", re.I)
# Names that look secret but carry none.
BENIGN = re.compile(r"openbao_shamir_|breakglass_|_name\b|_path\b|_dir\b|_service\b|_ttl\b|openbao_namespace")
# Known earlier defects of the same class, outside the unseal path and not yet fixed.
# Each entry is an expression that this check tolerates for now. Remove the entry
# when the task is fixed.
# Existing logins that expand the password into the `bao write` arguments, in code
# outside the unseal path. They are the same defect class. Each entry is
# the whole stripped line. Remove it when the task sends the password on stdin.
KNOWN_SHELL = {
    ("main.yml", 'bao write -format=json auth/userpass/login/{{ openbao_policy_reconciler_username }} password="$P"\''),
    ("main.yml", 'bao write -format=json auth/userpass/login/{{ openbao_approle_reconciler_username }} password="$P"\''),
    ("main.yml", 'password="$P"'),
    ("main.yml", 'bao write -format=json auth/userpass/login/{{ openbao_operator_username }} password="$P"\''),
}
KNOWN = {
    # Writes share 3 to the macOS Keychain on the operator workstation with `security -w`.
    "openbao_unseal_keys_b64[2]",
}


def secret_expressions(text):
    return [
        expr.strip()
        for expr in JINJA.findall(text)
        if (SECRET.search(expr) and not BENIGN.search(expr)) or expr.strip() == "unseal-argument"
    ]


def argv_and_cmd_lines(path):
    """Yield (line number, line) for lines inside an argv list or a cmd string.

    A line that holds any Jinja expression and follows `unseal` in an argv list
    is yielded with a marker, so a loop variable such as `{{ item }}`
    that carries a share is caught too.
    """
    lines = path.read_text().splitlines()
    block = []
    block_indent = None

    def flush():
        after_unseal = False
        for number, line in block:
            marker = " {{ unseal-argument }}" if after_unseal and JINJA.search(line) else ""
            after_unseal = after_unseal or bool(re.match(r"-\s*unseal\s*$", line.strip()))
            yield number, line + marker

    for number, line in enumerate(lines, 1):
        stripped = line.strip()
        indent = len(line) - len(line.lstrip())
        if block_indent is not None:
            if stripped and indent <= block_indent:
                yield from flush()
                block, block_indent = [], None
            else:
                block.append((number, line))
                continue
        if re.match(r"(argv|cmd):\s*(>-|\||>)?\s*$", stripped):
            block_indent = indent
        elif re.match(r"(argv|cmd):\s*\S", stripped):
            yield number, line
    yield from flush()


class OpenBaoNoSecretArgvTest(unittest.TestCase):
    def test_files_found(self):
        names = {path.name for path in FILES}
        self.assertIn("main.yml", names)
        self.assertIn("unseal-pod.yml", names)
        self.assertIn("100-openbao.yml", names)

    def test_no_secret_in_argv_or_cmd(self):
        found = []
        for path in FILES:
            for number, line in argv_and_cmd_lines(path):
                for expr in secret_expressions(line):
                    if expr in KNOWN:
                        continue
                    found.append(f"{path.relative_to(ROOT)}:{number}: {{{{ {expr} }}}}")
        self.assertEqual(found, [], "secret material in a command argument:\n" + "\n".join(found))

    def test_no_shell_variable_in_a_credential_argument(self):
        found = []
        for path in FILES:
            for number, line in argv_and_cmd_lines(path):
                if SHELL_CREDENTIAL.search(line) and (path.name, line.strip()) not in KNOWN_SHELL:
                    found.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
        self.assertEqual(found, [], "shell variable expanded into a credential argument:\n" + "\n".join(found))

    def test_data_read_sends_the_password_on_stdin(self):
        text = (ROOT / "roles/stack/operator/openbao/tasks/data-read.yml").read_text()
        self.assertIn("password=-", text)
        self.assertNotIn('password="$', text)

    def test_checker_catches_a_share_in_argv(self):
        sample = ROOT / "tests" / "fixtures" / "openbao-argv-share-sample.yml"
        hits = [
            expr
            for number, line in argv_and_cmd_lines(sample)
            for expr in secret_expressions(line)
        ]
        self.assertGreaterEqual(len(hits), 2, hits)

    def test_checker_catches_a_shell_variable_in_a_credential_argument(self):
        sample = ROOT / "tests" / "fixtures" / "openbao-argv-share-sample.yml"
        hits = [
            line.strip()
            for number, line in argv_and_cmd_lines(sample)
            if SHELL_CREDENTIAL.search(line)
        ]
        self.assertGreaterEqual(len(hits), 3, hits)

    def test_unseal_goes_through_the_api_with_a_json_body_on_stdin(self):
        text = (ROOT / "roles/stack/operator/openbao/tasks/unseal-pod.yml").read_text()
        # The API form: a JSON body with a `key` field on stdin of `bao write`.
        self.assertIn("bao write -format=json sys/unseal -", text)
        self.assertIn('{\\"key\\":\\"%s\\"}', text)
        self.assertIn("OPENBAO_SHARE", text)
        # Not the positional command, which takes `-` as the key itself.
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotRegex(code, r"bao\s+operator\s+unseal")

    def test_no_task_in_the_role_uses_the_positional_unseal_command(self):
        # Comments may name the command. Code may not.
        code_hits = []
        for path in FILES:
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if re.search(r"operator\s+unseal", line) and not line.lstrip().startswith("#"):
                    code_hits.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual(code_hits, [])

    def test_the_guard_takes_no_pod_list(self):
        # A pod list would let a caller narrow the guard to fewer pods than the
        # cluster has. The expected set comes from openbao_replicas alone.
        hits = []
        for path in FILES:
            for number, line in enumerate(path.read_text().splitlines(), 1):
                if "openbao_guard_pods" in line:
                    hits.append(f"{path.relative_to(ROOT)}:{number}")
        self.assertEqual(hits, [])

    def test_the_migration_has_no_automatic_resume(self):
        text = (ROOT / "roles/stack/operator/openbao/tasks/grow-ha.yml").read_text()
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        self.assertNotIn("_resume", code)
        self.assertNotIn("serviceName == openbao_headless_service_name", code)
        # The baseline check sits after the before read and before the first change.
        read = code.index("Read the data before the change")
        baseline = code.index("Assert the before read found at least one existing path")
        first_change = code.index("Delete the StatefulSet and keep its pod and volume")
        self.assertLess(read, baseline)
        self.assertLess(baseline, first_change)
        # The capability preflight is also ahead of the first change.
        self.assertLess(code.index("capabilities-check.yml"), first_change)


if __name__ == "__main__":
    unittest.main()
