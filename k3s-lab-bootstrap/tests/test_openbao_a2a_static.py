"""Static checks for step A.2a: no credential in a command argument, the
issuer policy bounds, the receipt namespace RBAC split, and the 694 switch."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPENBAO_ROLE = ROOT / "roles/stack/operator/openbao"
BORN_ROLE = ROOT / "roles/common/dmf-born-inventory"

# Task files that issue or use credentials on the A.2a path. The legacy
# break-glass file is the default path and moved out of main.yml unchanged.
ARGV_CHECKED = [
    OPENBAO_ROLE / "tasks/setup-issuer.yml",
    OPENBAO_ROLE / "tasks/setup-rotation-scheduler.yml",
    BORN_ROLE / "tasks/openbao-setup-only.yml",
    ROOT / "playbooks/verify-openbao-identity-model.yml",
    ROOT / "playbooks/vertical-orchestration/115-approle-rotation.yml",
    ROOT / "playbooks/vertical-orchestration/110-eso-secret-rotation.yml",
]
LEGACY_ARGV_EXCEPTIONS = {
    (BORN_ROLE / "tasks/openbao-breakglass.yml", "Log into OpenBao with born-inventory AppRole"): (
        "the default path, moved verbatim from main.yml; the receipt-driven switch replaces it"
    ),
}

# A lifetime or a policy list is configuration, not a credential.
CREDENTIAL_WORD = r"(token|secret|password|jwt|share|accessor)(?!_(max_)?ttl|_policies|_length)"
STDIN_PIPE = re.compile(r"""printf\s+'[^']*'(?:\s+"\$\w+")+\s*\|""")
SHELL_VARIABLE = re.compile(r"\$\{?\w*" + CREDENTIAL_WORD + r"\w*", re.I)
JINJA_EXPRESSION = re.compile(r"\{\{[^}]*" + CREDENTIAL_WORD + r"[^}]*\}\}", re.I)
# Lines that end the command part of a task.
TASK_KEYS = re.compile(
    r"^\s+(environment|register|no_log|changed_when|failed_when|when|delegate_to|become|loop|loop_control|vars|block|always):"
)


def tasks(text):
    """Split YAML text into (name, command text) pairs for the tasks that run a
    process. The command text is the
    task's `cmd`/`argv`/`_raw_params` section up to the next task key."""
    chunks = re.split(r"^\s*- name:\s*", text, flags=re.M)[1:]
    for chunk in chunks:
        name, _, body = chunk.partition("\n")
        if not re.search(r"^\s+ansible\.builtin\.(shell|command|raw|script):", body, re.M):
            continue
        command = []
        for line in body.splitlines():
            if TASK_KEYS.match(line):
                break
            command.append(line)
        yield name.strip(), "\n".join(command)


def argv_findings(text):
    found = []
    for name, command in tasks(text):
        command = STDIN_PIPE.sub("", command)
        if SHELL_VARIABLE.search(command) or JINJA_EXPRESSION.search(command):
            found.append(name)
    return found


class NoCredentialInArgvTest(unittest.TestCase):
    def test_checked_files_pass_no_credential_as_an_argument(self):
        for path in ARGV_CHECKED:
            with self.subTest(path=path.name):
                self.assertEqual(argv_findings(path.read_text(encoding="utf-8")), [])

    def test_legacy_exceptions_are_exactly_the_known_ones(self):
        for (path, name), reason in LEGACY_ARGV_EXCEPTIONS.items():
            with self.subTest(task=name):
                self.assertTrue(reason)
                self.assertEqual(argv_findings(path.read_text(encoding="utf-8")), [name])

    def test_detector_flags_each_form_of_credential_argument(self):
        bad = [
            "- name: a\n  ansible.builtin.command:\n    argv:\n      - bao\n      - \"{{ client_token }}\"\n",
            "- name: b\n  ansible.builtin.shell:\n    cmd: bao write x secret_id=$OPENBAO_SECRET_ID\n",
            "- name: c\n  ansible.builtin.shell:\n    cmd: bao login password={{ the_password }}\n",
            "- name: d\n  ansible.builtin.shell:\n    cmd: bao login jwt=$ISSUER_JWT\n",
        ]
        for text in bad:
            with self.subTest(text=text):
                self.assertEqual(len(argv_findings(text)), 1)

    def test_detector_accepts_the_stdin_and_environment_forms(self):
        good = (
            "- name: a\n  ansible.builtin.shell:\n    cmd: >-\n"
            "      printf '%s\\n%s\\n' \"$OPENBAO_TOKEN\" \"$POLICY_HCL\" |\n"
            "      kubectl exec -i pod -- sh -c 'IFS= read -r BAO_TOKEN; export BAO_TOKEN; bao policy write p -'\n"
            "  environment:\n    OPENBAO_TOKEN: \"{{ some_token }}\"\n"
        )
        self.assertEqual(argv_findings(good), [])


def hcl_block(policy, path):
    match = re.search(r'path\s+"' + re.escape(path) + r'"\s*\{(.*?)\n\}', policy, re.S)
    return match.group(1) if match else None


class IssuerPolicyTest(unittest.TestCase):
    POLICY = (OPENBAO_ROLE / "files/policies/setup-issuer.hcl").read_text(encoding="utf-8")
    ROLE = "auth/approle/role/born-inventory-netbox-reader"

    def paths(self):
        return re.findall(r'^path\s+"([^"]+)"', self.POLICY, re.M)

    def test_exact_paths_only(self):
        self.assertEqual(
            self.paths(),
            [
                f"{self.ROLE}/secret-id",
                f"{self.ROLE}/secret-id-accessor/lookup",
                f"{self.ROLE}/secret-id-accessor/destroy",
                "auth/token/revoke-self",
            ],
        )

    def test_no_glob_and_no_read_or_write_capability(self):
        self.assertNotIn("*", re.sub(r"^\s*#.*$", "", self.POLICY, flags=re.M))
        for caps in re.findall(r"capabilities\s*=\s*(\[[^\]]*\])", self.POLICY):
            self.assertEqual(caps, '["update"]')

    def test_create_accepts_only_one_hour_and_one_use(self):
        block = hcl_block(self.POLICY, f"{self.ROLE}/secret-id")
        self.assertIn('required_parameters = ["ttl", "num_uses"]', block)
        allowed = re.search(r"allowed_parameters\s*=\s*\{(.*?)\}", block, re.S).group(1)
        self.assertEqual(re.findall(r'"(\w+)"\s*=\s*(\[[^\]]*\])', allowed), [("ttl", '["1h"]'), ("num_uses", '["1"]')])

    def test_issuer_cannot_reach_other_roles_or_the_custom_endpoint(self):
        for forbidden in ("custom-secret-id", "role-id", "external-secrets", "object-storage", "sys/", "secret/"):
            self.assertNotIn(forbidden, self.POLICY)


class ExecutorRbacTest(unittest.TestCase):
    TEXT = (OPENBAO_ROLE / "tasks/setup-issuer.yml").read_text(encoding="utf-8")

    def test_executor_role_is_get_configmaps_and_create_get_leases(self):
        rules = re.search(r"      rules:\n(.*?)\n\n", self.TEXT, re.S).group(1)
        self.assertEqual(
            re.findall(r'apiGroups: \["([^"]*)"\]\n\s+resources: \["(\w+)"\]\n\s+verbs: \[([^\]]*)\]', rules),
            [("", "configmaps", '"get"'), ("coordination.k8s.io", "leases", '"create", "get"')],
        )

    def test_nothing_in_the_file_grants_the_cms_or_a_configmap_create(self):
        self.assertNotIn("dmf-cms-setup-requests", re.sub(r"^\s*#.*$", "", self.TEXT, flags=re.M))
        self.assertNotRegex(self.TEXT, r'configmaps"\]\n\s+verbs: \[[^\]]*"create"')

    def test_cms_slot_is_named_and_empty(self):
        self.assertIn("# CMS slot (step A.2b", self.TEXT)

    def test_kubernetes_auth_role_is_written_on_fresh_init_only(self):
        task = self.TEXT.split("- name: Write the setup issuer Kubernetes-auth role")[1]
        self.assertIn("openbao_init_raw.changed", task)
        self.assertIn("{{ openbao_root_token }}", task)


class BornInventorySwitchTest(unittest.TestCase):
    MAIN = (BORN_ROLE / "tasks/main.yml").read_text(encoding="utf-8")
    DEFAULTS = (BORN_ROLE / "defaults/main.yml").read_text(encoding="utf-8")
    SETUP = (BORN_ROLE / "tasks/openbao-setup-only.yml").read_text(encoding="utf-8")
    BREAKGLASS = (BORN_ROLE / "tasks/openbao-breakglass.yml").read_text(encoding="utf-8")
    # The tasks that the receipt-driven path replaces, in main.yml before A.2a.
    REPLACED = [
        "Validate break-glass inputs",
        "Check OpenBao break-glass JSON exists on operator host",
        "Assert OpenBao break-glass JSON exists for AppRole login",
        "Load OpenBao break-glass JSON from operator host",
        "Extract OpenBao AppRole credentials for born-inventory reads",
        "Assert OpenBao AppRole credentials are available for born-inventory",
        "Discover OpenBao pod for born-inventory reads",
        "Assert OpenBao pod exists for born-inventory",
        "Set OpenBao pod fact for born-inventory",
        "Log into OpenBao with born-inventory AppRole",
        "Extract OpenBao client token for born-inventory",
        "Read NetBox runtime secret from OpenBao",
        "Log NetBox admin token read result",
        "Parse NetBox admin token from OpenBao",
        "Default NetBox runtime secret for born-inventory",
    ]

    def test_switch_is_off_by_default(self):
        self.assertRegex(self.DEFAULTS, r"(?m)^dmf_born_inventory_receipt_path_enabled: false$")

    def test_main_includes_each_path_under_the_switch(self):
        self.assertRegex(
            self.MAIN,
            r"include_tasks: openbao-breakglass\.yml\n  when: not dmf_born_inventory_receipt_path_enabled \| bool",
        )
        self.assertRegex(
            self.MAIN,
            r"include_tasks: openbao-setup-only\.yml\n  when: dmf_born_inventory_receipt_path_enabled \| bool",
        )

    def test_replaced_tasks_moved_out_of_main_and_into_the_legacy_file(self):
        for name in self.REPLACED:
            with self.subTest(task=name):
                self.assertNotIn(f"- name: {name}\n", self.MAIN)
                self.assertEqual(self.BREAKGLASS.count(f"- name: {name}\n"), 1)

    def test_enabled_path_never_references_the_break_glass_file(self):
        code = re.sub(r"^\s*#.*$", "", self.SETUP, flags=re.M)
        for forbidden in (
            "breakglass",
            "break-glass",
            "secret_id_key",
            "role_id_key",
            "OPENBAO_SECRET_ID",
            "approle/login",
            "lookup('ansible.builtin.file'",
            "lookup('file'",
        ):
            self.assertNotIn(forbidden, code)

    def test_the_shared_validation_in_main_does_not_name_the_break_glass_file(self):
        validate = self.MAIN.split("- name: Validate born-inventory inputs")[1].split("\n- name:")[0]
        self.assertNotIn("breakglass", validate)
        self.assertIn("- name: Validate break-glass inputs", self.BREAKGLASS)

    def test_the_failure_message_does_not_claim_a_clean_state(self):
        failure = self.SETUP.split("- name: Assert the setup-only issuance succeeded")[1].split("- name:")[0]
        self.assertIn("unconfirmed", failure)
        for claim in ("No SecretID", "no SecretID", "is left", "nothing remains"):
            self.assertNotIn(claim, failure)

    def test_role_id_comes_from_the_published_configmap_or_the_variable(self):
        self.assertIn("--role-id-configmap", self.SETUP)
        publish = (OPENBAO_ROLE / "tasks/setup-issuer.yml").read_text(encoding="utf-8")
        self.assertIn("- name: Publish the born-inventory AppRole role id", publish)
        self.assertIn("namespace: \"{{ openbao_setup_requests_namespace }}\"", publish)

    def test_setup_only_file_requires_the_request_id_and_cleans_up(self):
        self.assertIn("dmf_born_inventory_setup_request_id | length > 0", self.SETUP)
        always = self.SETUP.split("  always:")[1]
        self.assertIn("state: absent", always)

    def test_request_id_travels_in_the_environment_not_argv(self):
        run = self.SETUP.split("- name: Run the setup-only issuance")[1].split("- name:")[0]
        self.assertIn("DMF_SETUP_REQUEST_ID: ", run)
        argv = run.split("environment:")[0]
        self.assertNotIn("request_id", argv)


@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook is not installed")
class DirectTokenOnReceiptPathTest(unittest.TestCase):
    """Run the real role through ansible-playbook. The first task of the role
    decides, so no NetBox or OpenBao task can run before it."""

    REFUSAL = "does not accept a direct NetBox admin token"

    def run_role(self, switch, token, request_id):
        with tempfile.TemporaryDirectory() as tmp:
            play = Path(tmp) / "play.yml"
            play.write_text(
                "- hosts: localhost\n  connection: local\n  gather_facts: false\n  tasks:\n"
                "    - ansible.builtin.include_role:\n        name: dmf-born-inventory\n"
                "      vars:\n"
                f"        dmf_born_inventory_receipt_path_enabled: {switch}\n"
                f"        dmf_born_inventory_netbox_admin_token: '{token}'\n"
            )
            env = dict(
                os.environ,
                ANSIBLE_ROLES_PATH=str(ROOT / "roles/common"),
                ANSIBLE_LOCAL_TEMP=tmp,
                ANSIBLE_HOME=tmp,
                ANSIBLE_NOCOLOR="1",
                DMF_SETUP_REQUEST_ID=request_id,
            )
            done = subprocess.run(
                ["ansible-playbook", "-i", "localhost,", str(play)],
                capture_output=True, text=True, env=env, cwd=tmp, check=False,
            )
            return done.returncode, done.stdout + done.stderr

    def test_a_direct_token_fails_before_any_other_task_whatever_the_receipt(self):
        for label, request_id in (
            ("missing", ""),
            ("invalid", "not-a-valid-request-id"),
            ("valid", "0123456789abcdef0123456789abcdef"),
        ):
            with self.subTest(receipt=label):
                rc, out = self.run_role("true", "direct-token-value", request_id)
                self.assertNotEqual(rc, 0, out)
                self.assertIn(self.REFUSAL, out)
                self.assertNotIn("Validate born-inventory inputs", out)
                self.assertNotIn("direct-token-value", out)

    def test_with_the_switch_off_a_direct_token_is_not_refused(self):
        rc, out = self.run_role("false", "direct-token-value", "")
        self.assertNotIn(self.REFUSAL, out)
        self.assertIn("Validate born-inventory inputs", out)


FAKE_K8S = """\
from ansible.module_utils.basic import AnsibleModule
import json, os


def main():
    module = AnsibleModule(argument_spec=dict(
        kubeconfig=dict(type="str"), state=dict(type="str"), definition=dict(type="dict")))
    path = os.environ["FAKE_K8S_STATE"]
    state = json.load(open(path)) if os.path.exists(path) else {"calls": 0, "data": None}
    state["calls"] += 1
    definition = module.params["definition"]
    state["data"] = definition["data"]
    json.dump(state, open(path, "w"))
    module.exit_json(changed=True)


main()
"""


def task_text(text, name):
    """The text of one top-level task, from its `- name:` line to the next
    task or comment block."""
    block = text.split(f"- name: {name}\n")[1]
    return f"- name: {name}\n" + re.split(r"\n(?=- name:|# )", block)[0] + "\n"


@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook is not installed")
class RoleIdPublicationTest(unittest.TestCase):
    """Run the real publication tasks of setup-issuer.yml through
    ansible-playbook, with a fake `kubernetes.core.k8s` that records its call
    and keeps one ConfigMap in a state file."""

    TEXT = (OPENBAO_ROLE / "tasks/setup-issuer.yml").read_text(encoding="utf-8")
    NAMES = [
        "Normalize the born-inventory AppRole role id",
        "Publish the born-inventory AppRole role id",
        "Report a missing born-inventory role id",
    ]

    def run_cases(self, role_id_line, existing):
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            modules = tmpdir / "collections/ansible_collections/kubernetes/core/plugins/modules"
            modules.mkdir(parents=True)
            (modules / "k8s.py").write_text(FAKE_K8S)
            state = tmpdir / "state.json"
            if existing:
                state.write_text(json.dumps({"calls": 0, "data": {"role_id": "old-role-id"}}))
            tasks = "".join(
                "    " + line + "\n" if line.strip() else "\n"
                for name in self.NAMES
                for line in task_text(self.TEXT, name).splitlines()
            )
            play = tmpdir / "play.yml"
            play.write_text(
                "- hosts: localhost\n  connection: local\n  gather_facts: false\n  vars:\n"
                "    openbao_born_inventory_role_id_configmap: born-inventory-approle\n"
                "    openbao_setup_requests_namespace: dmf-setup-requests\n"
                + role_id_line
                + "  tasks:\n" + tasks
            )
            env = dict(
                os.environ,
                ANSIBLE_COLLECTIONS_PATH=str(tmpdir / "collections"),
                ANSIBLE_LOCAL_TEMP=tmp,
                ANSIBLE_HOME=tmp,
                ANSIBLE_NOCOLOR="1",
                FAKE_K8S_STATE=str(state),
            )
            done = subprocess.run(
                ["ansible-playbook", "-i", "localhost,", str(play)],
                capture_output=True, text=True, env=env, cwd=tmp, check=False,
            )
            data = json.loads(state.read_text()) if state.exists() else {"calls": 0, "data": None}
            return done.returncode, done.stdout + done.stderr, data

    def test_publication_never_fails_the_role_and_writes_only_a_real_role_id(self):
        cases = {
            "missing key": ("", None),
            "explicit null": ("    openbao_born_inventory_role_id: null\n", None),
            "empty string": ("    openbao_born_inventory_role_id: ''\n", None),
            "whitespace": ("    openbao_born_inventory_role_id: '  '\n", None),
            "present": ("    openbao_born_inventory_role_id: new-role-id\n", "new-role-id"),
        }
        for label, (line, written) in cases.items():
            for existing in (False, True):
                with self.subTest(case=label, existing_configmap=existing):
                    rc, out, state = self.run_cases(line, existing)
                    self.assertEqual(rc, 0, out)
                    if written is None:
                        self.assertEqual(state["calls"], 0)
                        self.assertEqual(state["data"], {"role_id": "old-role-id"} if existing else None)
                        self.assertIn("dmf_born_inventory_setup_role_id", out)
                    else:
                        self.assertEqual(state["calls"], 1)
                        self.assertEqual(state["data"], {"role_id": written})
                        self.assertNotIn("dmf_born_inventory_setup_role_id", out)

    def test_the_tasks_use_only_the_normalized_fact(self):
        for name in self.NAMES[1:]:
            self.assertNotIn("openbao_born_inventory_role_id |", task_text(self.TEXT, name))
            self.assertNotIn("openbao_born_inventory_role_id }}", task_text(self.TEXT, name))


if __name__ == "__main__":
    unittest.main()
