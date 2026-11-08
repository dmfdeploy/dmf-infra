"""Static check: no task passes a share, token or password as a command argument.

This is a known defect class: a command argument shows in the process list
of the node and of the pod. A task passes a secret on stdin instead. The check reads the OpenBao role's task
files and the OpenBao playbooks as text and reports every Jinja expression that names
secret material inside an `argv:` list or a `cmd:` string. It also reports a shell
variable that is expanded into a credential argument, such as `password="$P"`: the
shell expands it into the child process's argument list, which any process in the
pod can read. Send the value on stdin instead (`password=-`, or a JSON body).

A task-level `environment:` entry that holds a secret is the same defect. Ansible turns
`environment:` into an assignment in the remote shell command, so the value shows in
the process list on the node. The module's `stdin:` parameter carries a secret without
that. The environment check below fails on such an entry in the files that are already
fixed, and it pins the entries that remain elsewhere, so a new one cannot appear. It
reads both the block form and the flow form of `environment:`, and it tests the key name
and the value template. A pin names the file, the task and the key.

Two more checks cover every task, in every file, that carries a secret on `stdin:`.
The task must set `no_log: true`, because Ansible records module arguments, `stdin:`
included. The command must not expand a value that it read from stdin into the
arguments of another program. It sends the value on stdin to that program, as a JSON
body.

These checks read task files as text with patterns, so they guard against the common
ways of writing these defects, not every possible one. A secret under an unusual
variable name, a key written in quotes, or a template split across lines can pass them.
Review a new secret-handling task by reading it, and add its form here when a pattern
misses it.
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


# Key names that carry a secret in a task `environment:`.
ENV_SECRET_KEY = re.compile(r"token|secret|password|jwt|share|bearer|role_id|key|payload", re.I)
TASK_START = re.compile(r"^(\s*)- name:\s*(.*)$")
# Commands that the shell runs itself, so an expanded variable in their arguments stays out of
# every process list.
SHELL_BUILTINS = {"printf", "echo", "export", "read", "unset", "test", "[", ":", "exit", "return"}


def iter_tasks(text):
    """Yield (task name, task lines) for each `- name:` task in a YAML file."""
    lines = text.splitlines()
    starts = [i for i, line in enumerate(lines) if TASK_START.match(line)] + [len(lines)]
    for begin, end in zip(starts, starts[1:]):
        yield TASK_START.match(lines[begin]).group(2).strip(), lines[begin:end]


def indent_of(line):
    return len(line) - len(line.lstrip())


def value_with_continuation(lines, index, rest):
    """Return the value that starts after a key on lines[index], with its deeper lines."""
    parts = [rest]
    for line in lines[index + 1:]:
        if line.strip() and indent_of(line) <= indent_of(lines[index]):
            break
        parts.append(line.strip())
    return "\n".join(parts)


def split_top_level(text):
    """Split a flow mapping body on commas that are outside quotes and brackets."""
    parts, depth, quote, current = [], 0, None, ""
    for char in text:
        if quote:
            if char == quote:
                quote = None
        elif char in "'\"":
            quote = char
        elif char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(current)
            current = ""
            continue
        current += char
    parts.append(current)
    return parts


def environment_entries(task):
    """Return (key, value) for every entry of the task `environment:`.

    A value-only `environment:` (a template for the whole mapping) has the key `*`.
    """
    entries = []
    for index, line in enumerate(task):
        match = re.match(r"^\s+environment:\s*(.*)$", line)
        if not match:
            continue
        rest = match.group(1).strip()
        if rest.startswith("{"):
            flow = rest
            more = index
            while flow.count("{") > flow.count("}") and more + 1 < len(task):
                more += 1
                flow += " " + task[more].strip()
            for part in split_top_level(flow.strip()[1:-1]):
                pair = re.match(r"^\s*['\"]?([A-Za-z_]\w*)['\"]?\s*:\s*(.*)$", part, re.S)
                if pair:
                    entries.append((pair.group(1), pair.group(2)))
        elif rest == "":
            children = [
                n for n in range(index + 1, len(task))
                if task[n].strip() and indent_of(task[n]) > indent_of(line)
                and all(not task[m].strip() or indent_of(task[m]) > indent_of(line) for m in range(index + 1, n))
            ]
            if not children:
                continue
            entry_indent = indent_of(task[children[0]])
            for n in children:
                pair = re.match(r"^\s+([A-Za-z_]\w*):\s*(.*)$", task[n])
                if pair and indent_of(task[n]) == entry_indent:
                    entries.append((pair.group(1), value_with_continuation(task, n, pair.group(2))))
        else:
            entries.append(("*", value_with_continuation(task, index, rest)))
    return entries


def is_secret_environment_entry(key, value):
    """A secret by key name, or by a template in the value that names secret material."""
    if key != "*" and ENV_SECRET_KEY.search(key):
        return True
    return any(
        SECRET.search(expr) and not BENIGN.search(expr)
        for expr in JINJA.findall(value)
    )


def environment_findings(text):
    """Return "task::key" for every secret-bearing entry of a task `environment:`."""
    return [
        f"{name}::{key}"
        for name, task in iter_tasks(text)
        for key, value in environment_entries(task)
        if is_secret_environment_entry(key, value)
    ]


def stdin_value(task):
    for index, line in enumerate(task):
        match = re.match(r"^\s+stdin:\s*(.*)$", line)
        if match:
            return value_with_continuation(task, index, match.group(1))
    return None


def carries_a_secret_on_stdin(task):
    value = stdin_value(task)
    return value is not None and any(
        SECRET.search(expr) and not BENIGN.search(expr) for expr in JINJA.findall(value)
    )


def has_no_log(task):
    return any(re.match(r"^\s+no_log:\s*true\s*$", line) for line in task)


def secret_stdin_without_no_log(text):
    """Return the names of tasks that carry a secret on stdin and do not set `no_log: true`."""
    return [
        name for name, task in iter_tasks(text)
        if carries_a_secret_on_stdin(task) and not has_no_log(task)
    ]


def command_text(task):
    """Return the text of the task's `cmd:` string or `argv:` list, with `stdin:` left out."""
    for index, line in enumerate(task):
        match = re.match(r"^\s+(cmd|argv):\s*(.*)$", line)
        if match:
            return value_with_continuation(task, index, match.group(2).lstrip(">-| "))
    return ""


def first_command_word(statement):
    for word in statement.split():
        word = re.sub(r"^\w+=\$\(", "", word).lstrip("$(\"'{")
        if re.match(r"^\w+=", word):
            continue
        return word
    return ""


def secret_in_child_arguments(text):
    """Return "task: statement" for a secret in another program's arguments.

    The check covers tasks that carry a secret on stdin. A shell variable that the command
    reads or captures must appear only in the arguments of a shell builtin, and no template
    that names secret material may sit in the command itself.
    """
    found = []
    for name, task in iter_tasks(text):
        if not carries_a_secret_on_stdin(task):
            continue
        command = command_text(task)
        variables = set(re.findall(r"\bread\s+-r\s+(\w+)", command)) | set(re.findall(r"\b(\w+)=\$\(", command))
        for statement in re.split(r"[;\n|&]+", command):
            statement = statement.strip()
            if not statement:
                continue
            expanded = set(re.findall(r"\$\{?(\w+)", statement))
            if expanded & variables and first_command_word(statement) not in SHELL_BUILTINS:
                found.append(f"{name}: {statement}")
        for expr in secret_expressions(command):
            found.append(f"{name}: {{{{ {expr} }}}}")
    return found


# The OpenBao family: the OpenBao role's tasks, the OpenBao playbooks, the identity verifier
# and the two rotation playbooks. This list is the same in every branch.
OPENBAO_FAMILY_FILES = sorted(
    list((ROOT / "roles/stack/operator/openbao/tasks").glob("*.yml"))
    + list((ROOT / "playbooks/vertical-security").glob("10*-openbao*.yml"))
    + [
        ROOT / "playbooks/verify-openbao-identity-model.yml",
        ROOT / "playbooks/vertical-orchestration/110-eso-secret-rotation.yml",
        ROOT / "playbooks/vertical-orchestration/120-ops-admin-rotation.yml",
    ]
)

# Files whose tasks pass every secret on stdin, except the permanent entries below. The
# change that converts the OpenBao family lists it in the first name; the change that converts
# every other file lists them in the second. Once both are in, both names hold their files.
ENV_CLEAN_OPENBAO_FAMILY = []

ENV_CLEAN_OTHER = [
    path
    for path in sorted(ROOT.rglob("*.yml"))
    if path.relative_to(ROOT).parts[0] != "tests" and path not in OPENBAO_FAMILY_FILES
]

ENV_CLEAN = ENV_CLEAN_OPENBAO_FAMILY + ENV_CLEAN_OTHER


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


# Entries that stay for good, as "path::task name". Both are different defects and each has its
# own fix.
PERMANENT_ENVIRONMENT = {
    # Cloud credentials for a local `docker run -e NAME`; docker reads them from the
    # environment of the process, so stdin does not replace the entry.
    'roles/base/resilience-verify/tasks/verify-host-audit.yml::Audit verify — synthetic round-trip + Object Lock delete refusal (containerised)::AWS_ACCESS_KEY_ID',
    'roles/base/resilience-verify/tasks/verify-host-audit.yml::Audit verify — synthetic round-trip + Object Lock delete refusal (containerised)::AWS_SECRET_ACCESS_KEY',
    # The AppRole login that the receipt-driven path replaces; it expands both ids into the
    # command (see tests/test_openbao_a2a_static.py).
    'roles/common/dmf-born-inventory/tasks/openbao-breakglass.yml::Log into OpenBao with born-inventory AppRole::OPENBAO_ROLE_ID',
    'roles/common/dmf-born-inventory/tasks/openbao-breakglass.yml::Log into OpenBao with born-inventory AppRole::OPENBAO_SECRET_ID',
}

# Entries that a pending change converts, as "path::task name": the OpenBao family in one name,
# every other file in the other. Each branch pins what it leaves as it is on the main branch, so
# the suite passes on its own. Once both changes are in, both sets are empty and the two names go.
PENDING_ENVIRONMENT_OPENBAO_FAMILY = {
    'playbooks/verify-openbao-identity-model.yml::Log in as the setup issuer::ISSUER_JWT',
    'playbooks/verify-openbao-identity-model.yml::Probe capability matrix::OPENBAO_TOKEN',
    'playbooks/vertical-orchestration/110-eso-secret-rotation.yml::Generate new ESO AppRole secret_id::ESO_ROTATION_BAO_TOKEN',
    'playbooks/vertical-orchestration/110-eso-secret-rotation.yml::Log into OpenBao with operator userpass::OPENBAO_PASSWORD',
    'playbooks/vertical-orchestration/120-ops-admin-rotation.yml::Decode temporary root token::OPS_ADMIN_ENCODED_ROOT_TOKEN',
    'playbooks/vertical-orchestration/120-ops-admin-rotation.yml::Revoke temporary root token::OPS_ADMIN_ROOT_TOKEN',
    'playbooks/vertical-orchestration/120-ops-admin-rotation.yml::Submit Shamir shares to generate the temporary root token::OPS_ADMIN_SHARE',
    'playbooks/vertical-orchestration/120-ops-admin-rotation.yml::Update ops-admin password in OpenBao::OPS_ADMIN_NEW_PASSWORD',
    'playbooks/vertical-orchestration/120-ops-admin-rotation.yml::Update ops-admin password in OpenBao::OPS_ADMIN_ROOT_TOKEN',
    'playbooks/vertical-security/100-openbao.yml::Revoke fresh-init root token after failed bootstrap::OPENBAO_TOKEN',
    "roles/stack/operator/openbao/tasks/capabilities-check.yml::Read the token's capabilities on the Raft paths::OPENBAO_TOKEN",
    'roles/stack/operator/openbao/tasks/data-read.yml::Log in as ops-admin for the data read::OPENBAO_PASSWORD',
    'roles/stack/operator/openbao/tasks/data-read.yml::Read the metadata of each path::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/grow-ha.yml::Read the Raft configuration::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/ha-guard-collect.yml::Read the Raft state from Autopilot::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Apply app-admin-reader policy::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Apply app-admin-writer policy::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Apply app-runtime-writer policy::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Apply approle-reconciler-writer policy::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Apply born-inventory runtime-reader policy::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Apply eso-reader policy::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Apply policy-writer policy::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Check ops-admin capabilities on Authentik bootstrap-passkey data path::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Check ops-admin capabilities on Authentik bootstrap-passkey metadata path::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Check ops-admin capabilities on Authentik breakglass data path::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Check ops-admin capabilities on Authentik breakglass metadata path::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Check ops-admin capabilities on Zot admin data path::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Check ops-admin capabilities on Zot admin metadata path::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Configure kubernetes auth backend::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Create ESO AppRole::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Create born-inventory AppRole::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Enable PKI secrets engine::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Enable approle auth method::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Enable kubernetes auth method::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Enable kv-v2 secrets engine::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Enable userpass auth method::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Generate ESO AppRole secret_id::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Generate born-inventory AppRole secret_id::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Log in as ops-admin for capability verification::OPENBAO_PASSWORD',
    'roles/stack/operator/openbao/tasks/main.yml::Log in as policy-reconciler (rerun path)::OPENBAO_POLICY_RECONCILER_PASSWORD',
    'roles/stack/operator/openbao/tasks/main.yml::Read ESO AppRole role_id::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Read born-inventory AppRole role_id::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Revoke root token::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Upsert approle-reconciler userpass user::OPENBAO_APPROLE_RECONCILER_PASSWORD',
    'roles/stack/operator/openbao/tasks/main.yml::Upsert approle-reconciler userpass user::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Upsert operator userpass user::OPENBAO_OPERATOR_PASSWORD',
    'roles/stack/operator/openbao/tasks/main.yml::Upsert operator userpass user::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Upsert policy-reconciler userpass user::OPENBAO_POLICY_RECONCILER_PASSWORD',
    'roles/stack/operator/openbao/tasks/main.yml::Upsert policy-reconciler userpass user::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/main.yml::Verify approle-reconciler login on rerun path::OPENBAO_APPROLE_RECONCILER_PASSWORD',
    'roles/stack/operator/openbao/tasks/setup-issuer.yml::Write the setup issuer Kubernetes-auth role::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/setup-issuer.yml::Write the setup issuer policy::OPENBAO_TOKEN',
    'roles/stack/operator/openbao/tasks/unseal-pod.yml::Unseal the pod with the threshold of shares::OPENBAO_SHARE',
}

PENDING_ENVIRONMENT_OTHER = set()

PINNED_ENVIRONMENT = PERMANENT_ENVIRONMENT | PENDING_ENVIRONMENT_OPENBAO_FAMILY | PENDING_ENVIRONMENT_OTHER


class OpenBaoNoSecretEnvironmentTest(unittest.TestCase):
    def test_clean_files_have_no_secret_in_environment(self):
        found = []
        for path in ENV_CLEAN:
            for name in environment_findings(path.read_text()):
                entry = f"{path.relative_to(ROOT)}::{name}"
                if entry not in PERMANENT_ENVIRONMENT:
                    found.append(entry)
        self.assertEqual(found, [], "secret in a task environment:\n" + "\n".join(found))

    def test_every_entry_is_exactly_the_pinned_ones(self):
        found = set()
        for path in sorted(ROOT.rglob("*.yml")):
            relative = path.relative_to(ROOT)
            if relative.parts[0] == "tests":
                continue
            for name in environment_findings(path.read_text()):
                found.add(f"{relative}::{name}")
        self.assertEqual(found, PINNED_ENVIRONMENT)

    def test_detector_flags_each_form_of_secret_environment(self):
        bad = [
            "- name: a\n  ansible.builtin.shell:\n    cmd: x\n  environment:\n    OPENBAO_TOKEN: \"{{ t }}\"\n",
            "- name: b\n  ansible.builtin.shell:\n    cmd: x\n  environment:\n    BAO_ADDR: y\n    SOME_PASSWORD: \"{{ p }}\"\n  no_log: true\n",
            "- name: c\n  ansible.builtin.shell:\n    cmd: x\n  environment:\n    OPENBAO_TOKEN: >-\n      {{ t }}\n",
            # Flow form on one line.
            "- name: d\n  ansible.builtin.shell:\n    cmd: x\n  environment: {API_TOKEN: \"{{ api_token }}\"}\n",
            # Flow form over several lines.
            "- name: e\n  ansible.builtin.shell:\n    cmd: x\n  environment: {\n    BAO_ADDR: y,\n    API_TOKEN: \"{{ api_token }}\"\n  }\n",
            # A key that names nothing secret, with a value that does.
            "- name: f\n  ansible.builtin.shell:\n    cmd: x\n  environment:\n    VALUE: \"{{ api_token }}\"\n",
            "- name: g\n  ansible.builtin.shell:\n    cmd: x\n  environment: {VALUE: \"{{ api_token }}\"}\n",
            # A template for the whole mapping.
            "- name: h\n  ansible.builtin.shell:\n    cmd: x\n  environment: \"{{ {'A': admin_password} }}\"\n",
        ]
        for text in bad:
            with self.subTest(text=text):
                self.assertEqual(len(environment_findings(text)), 1)

    def test_detector_accepts_stdin_and_plain_environment(self):
        good = [
            "- name: a\n  ansible.builtin.shell:\n    cmd: x\n    stdin: \"{{ t }}\\n\"\n"
            "  environment:\n    BAO_ADDR: https://127.0.0.1:8200\n",
            "- name: b\n  ansible.builtin.shell:\n    cmd: x\n  environment: {BAO_ADDR: \"{{ openbao_addr }}\", PATH: /usr/bin}\n",
        ]
        for text in good:
            with self.subTest(text=text):
                self.assertEqual(environment_findings(text), [])

    def test_a_new_secret_entry_in_a_pinned_task_is_not_pinned(self):
        # A pinned task that gains a second secret entry reports the new key as well, so the
        # exact-set comparison against the pins fails.
        task = (
            "- name: Log into OpenBao with born-inventory AppRole\n"
            "  ansible.builtin.shell:\n    cmd: x\n"
            "  environment:\n    OPENBAO_ROLE_ID: \"{{ r }}\"\n    OPENBAO_SECRET_ID: \"{{ s }}\"\n"
            "    EXTRA_VALUE: \"{{ admin_password }}\"\n"
        )
        entries = {f"roles/common/dmf-born-inventory/tasks/openbao-breakglass.yml::{e}" for e in environment_findings(task)}
        self.assertEqual(
            sorted(entries - PINNED_ENVIRONMENT),
            ["roles/common/dmf-born-inventory/tasks/openbao-breakglass.yml::Log into OpenBao with born-inventory AppRole::EXTRA_VALUE"],
        )


# One converted task as it is written, for the checks below.
CONVERTED_TASK = (
    "- name: Read the secret\n"
    "  ansible.builtin.shell:\n"
    "    cmd: >-\n"
    "      kubectl exec -i pod -- sh -c '\n"
    "      IFS= read -r BAO_TOKEN;\n"
    "      export BAO_TOKEN;\n"
    "      bao kv get -format=json secret/x'\n"
    "    stdin: \"{{ _client_token }}\\n\"\n"
    "  no_log: true\n"
)


class SecretOnStdinTest(unittest.TestCase):
    def test_every_task_with_a_secret_on_stdin_sets_no_log(self):
        found = []
        for path in sorted(ROOT.rglob("*.yml")):
            if path.relative_to(ROOT).parts[0] == "tests":
                continue
            for name in secret_stdin_without_no_log(path.read_text()):
                found.append(f"{path.relative_to(ROOT)}::{name}")
        self.assertEqual(found, [], "secret on stdin without no_log:\n" + "\n".join(found))

    def test_a_secret_on_stdin_without_no_log_fails(self):
        task = CONVERTED_TASK.replace("  no_log: true\n", "")
        self.assertEqual(secret_stdin_without_no_log(task), ["Read the secret"])
        self.assertEqual(secret_stdin_without_no_log(CONVERTED_TASK), [])

    def test_a_stdin_without_a_secret_needs_no_no_log(self):
        task = CONVERTED_TASK.replace("_client_token", "_pod_name").replace("  no_log: true\n", "")
        self.assertEqual(secret_stdin_without_no_log(task), [])

    def test_no_secret_reaches_a_child_process_argument(self):
        found = []
        for path in sorted(ROOT.rglob("*.yml")):
            if path.relative_to(ROOT).parts[0] == "tests":
                continue
            for entry in secret_in_child_arguments(path.read_text()):
                found.append(f"{path.relative_to(ROOT)}::{entry}")
        self.assertEqual(found, [], "secret in a child process argument:\n" + "\n".join(found))

    def test_the_converted_task_passes(self):
        self.assertEqual(secret_in_child_arguments(CONVERTED_TASK), [])

    def test_child_argument_check_flags_each_form(self):
        bad = {
            "bao write with a credential key": CONVERTED_TASK.replace(
                "IFS= read -r BAO_TOKEN;", "IFS= read -r BAO_TOKEN;\n      IFS= read -r SECRET_ID;"
            ).replace("bao kv get -format=json secret/x", 'bao write auth/approle/login secret_id="$SECRET_ID"'),
            "curl with a user and password": CONVERTED_TASK.replace(
                "IFS= read -r BAO_TOKEN;", "IFS= read -r BAO_TOKEN;\n      IFS= read -r PASS;"
            ).replace("bao kv get -format=json secret/x", 'curl -u admin:"$PASS" https://x'),
            "a command substitution that runs another program": CONVERTED_TASK.replace(
                "IFS= read -r BAO_TOKEN;", "IFS= read -r BAO_TOKEN;\n      IFS= read -r B64;"
            ).replace("bao kv get -format=json secret/x", 'KEY=$(printf %s "$B64" | base64 -d); bao kv put secret/x key="$KEY"'),
            "a template that names a secret in the command": CONVERTED_TASK.replace(
                "bao kv get -format=json secret/x", "bao kv put secret/x password={{ admin_password }}"
            ),
        }
        for label, text in bad.items():
            with self.subTest(label=label):
                self.assertTrue(secret_in_child_arguments(text), label)

    def test_a_secret_in_the_arguments_of_a_builtin_is_allowed(self):
        task = CONVERTED_TASK.replace(
            "IFS= read -r BAO_TOKEN;", "IFS= read -r BAO_TOKEN;\n      BODY=$(cat);"
        ).replace("bao kv get -format=json secret/x", 'printf %s "$BODY" | bao kv put secret/x -')
        self.assertEqual(secret_in_child_arguments(task), [])


if __name__ == "__main__":
    unittest.main()
