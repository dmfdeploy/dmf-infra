"""Static checks of the Ansible wiring around the rotation engine: the
OpenBao request duration, the scheduler identity tasks, the rotation playbook,
the thin ESO caller and the identity verifier. Each check reads the files as
text, like the other static checks of the openbao role."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from approle_lifecycle import registry, rotation, scheduler_policy  # noqa: E402
from test_openbao_scheduler_policy import parse_policy, permitted  # noqa: E402

OPENBAO_ROLE = ROOT / "roles/stack/operator/openbao"
MAIN = (OPENBAO_ROLE / "tasks/main.yml").read_text(encoding="utf-8")
DEFAULTS = (OPENBAO_ROLE / "defaults/main.yml").read_text(encoding="utf-8")
SCHEDULER = (OPENBAO_ROLE / "tasks/setup-rotation-scheduler.yml").read_text(encoding="utf-8")
ROTATION = (ROOT / "playbooks/vertical-orchestration/115-approle-rotation.yml").read_text(encoding="utf-8")
ESO = (ROOT / "playbooks/vertical-orchestration/110-eso-secret-rotation.yml").read_text(encoding="utf-8")
LEGACY = (ROOT / "tasks/eso-secret-rotation-legacy.yml").read_text(encoding="utf-8")
VERIFY = (ROOT / "playbooks/verify-openbao-identity-model.yml").read_text(encoding="utf-8")
REGISTRY = registry.load(ROOT / "vars/openbao-approle-lifecycle-registry.json")
POLICY = parse_policy(scheduler_policy.render_policy(REGISTRY))


def default(name):
    return re.search(rf"(?m)^{name}: (\S+)$", DEFAULTS).group(1)


def code(text):
    return re.sub(r"(?m)^\s*#.*$", "", text)


def task(text, name):
    """The text of one task, from its `- name:` line to the next one."""
    return re.split(r"(?m)^\s*- name: ", text.split(f"- name: {name}\n")[1])[0]


class RequestDurationTests(unittest.TestCase):
    SETTING = '"{{ openbao_max_request_duration_seconds }}s"'

    @staticmethod
    def listener_body(text):
        """The text of the first listener stanza, up to the line that holds
        only its closing brace."""
        return re.search(r'listener "tcp" \{\n(.*?)\n[ ]*\}\n', text, re.S).group(1)

    TEMPLATE = OPENBAO_ROLE / "templates/config.hcl.j2"

    def test_the_live_config_is_rendered_from_the_template(self):
        workload = (OPENBAO_ROLE / "tasks/workload.yml").read_text(encoding="utf-8")
        self.assertIn("name: openbao-config", workload)
        self.assertIn("lookup('ansible.builtin.template', 'config.hcl.j2')", workload)

    def test_the_template_sets_the_limit_in_the_listener_and_as_the_server_default(self):
        template = self.TEMPLATE.read_text(encoding="utf-8")
        listener = self.listener_body(template)
        self.assertIn(f"max_request_duration = {self.SETTING}", listener)
        outside = template.replace(listener, "")
        self.assertIn(f"default_max_request_duration = {self.SETTING}", outside)
        self.assertNotRegex(outside, r"(?m)^\s+max_request_duration")

    def test_the_rendered_live_config_passes_the_preflight(self):
        from approle_lifecycle.rotation_preflight import check_openbao_request_duration
        config = re.sub(r"(?m)^\s*\{%.*%\}\s*$\n?", "", self.TEMPLATE.read_text(encoding="utf-8"))
        config = config.replace("{{ openbao_max_request_duration_seconds }}", default("openbao_max_request_duration_seconds"))
        config = re.sub(r"\{\{[^}]*\}\}", "x", config)
        self.assertLessEqual(check_openbao_request_duration(config, rotation.MAX_REQUEST_SECONDS), rotation.MAX_REQUEST_SECONDS)

    def test_the_default_is_within_the_engines_request_timeout(self):
        seconds = int(default("openbao_max_request_duration_seconds"))
        self.assertTrue(0 < seconds <= rotation.MAX_REQUEST_SECONDS)


class SchedulerIdentityTests(unittest.TestCase):
    def test_the_role_includes_the_tasks_after_the_setup_issuer(self):
        issuer = MAIN.index("include_tasks: setup-issuer.yml")
        scheduler = MAIN.index("include_tasks: setup-rotation-scheduler.yml")
        self.assertLess(issuer, scheduler)

    def test_the_policy_file_is_the_one_the_task_loads_and_the_names_agree(self):
        write = task(SCHEDULER, "Write the rotation scheduler policy")
        self.assertIn("files/policies/rotation-scheduler.hcl", write)
        self.assertTrue((OPENBAO_ROLE / "files/policies/rotation-scheduler.hcl").is_file())
        self.assertIn("bao policy write {{ openbao_rotation_scheduler_policy_name }} -", write)
        names = {default(n) for n in (
            "openbao_rotation_scheduler_service_account", "openbao_rotation_scheduler_k8s_role",
            "openbao_rotation_scheduler_policy_name",
        )}
        self.assertEqual(names, {rotation.SCHEDULER_ROLE})

    def test_the_policy_reaches_openbao_on_stdin_and_the_task_logs_nothing(self):
        body = task(SCHEDULER, "Write the rotation scheduler policy")
        self.assertIn("no_log: true", body)
        self.assertRegex(body, r"(?m)^\s+stdin: \"\{\{ \w+ \}\}\\n")
        self.assertNotRegex(body, r"(?m)^\s+environment:")
        role = (OPENBAO_ROLE / "tasks/kubernetes-auth-role.yml").read_text(encoding="utf-8")
        self.assertIn("no_log: true", role)
        self.assertRegex(role, r"(?m)^\s+stdin: \"\{\{ openbao_k8s_auth_token \}\}\\n\{\{ \{")
        self.assertNotRegex(role, r"(?m)^\s+environment:")

    def test_the_kubernetes_auth_role_is_written_on_a_fresh_install_with_the_shared_body(self):
        body = task(SCHEDULER, "Write the rotation scheduler Kubernetes-auth role")
        self.assertIn("openbao_init_raw.changed", body)
        self.assertIn("{{ openbao_root_token }}", body)
        self.assertIn("kubernetes-auth-role.yml", body)
        self.assertIn('openbao_k8s_auth_role: "{{ openbao_rotation_scheduler_k8s_auth_role }}"', body)
        mapping = re.search(r"(?ms)^openbao_rotation_scheduler_k8s_auth_role:\n(.*?)^\S", DEFAULTS + "\nend").group(1)
        for line in (
            'name: "{{ openbao_rotation_scheduler_k8s_role }}"',
            'service_account: "{{ openbao_rotation_scheduler_service_account }}"',
            'namespace: "{{ openbao_namespace }}"',
            'policy: "{{ openbao_rotation_scheduler_policy_name }}"',
        ):
            self.assertIn(line, mapping)
        self.assertNotIn("generate-root", code(SCHEDULER))

    def test_the_identity_rbac_is_rendered_from_the_registry_and_applied(self):
        render = task(SCHEDULER, "Render the rotation scheduler ServiceAccount and Lease RBAC")
        self.assertIn("approle_lifecycle.scheduler_policy", render)
        self.assertIn("openbao-approle-lifecycle-registry.json", render)
        self.assertRegex(render, r"- --scope\n\s+- identity")
        self.assertIn("kubernetes.core.k8s", task(SCHEDULER, "Apply the rotation scheduler ServiceAccount and Lease RBAC"))

    def test_the_scheduler_and_the_issuer_have_separate_identities(self):
        for name in ("service_account", "k8s_role", "policy_name"):
            self.assertNotEqual(default(f"openbao_rotation_scheduler_{name}"), default(f"openbao_setup_issuer_{name}"))


class RotationPlaybookTests(unittest.TestCase):
    def names(self):
        return re.findall(r"(?m)^        ?- name: (.+)$|^    - name: (.+)$", ROTATION)

    def position(self, name):
        return ROTATION.index(f"- name: {name}\n")

    def test_the_context_is_asserted_before_anything_runs(self):
        first = ROTATION.index("- name:", ROTATION.index("  tasks:"))
        self.assertEqual(first, self.position("Assert the run context is present"))
        body = task(ROTATION, "Assert the run context is present")
        for variable in ("approle_rotation_target", "approle_rotation_actor", "approle_rotation_reason"):
            self.assertIn(variable, body)

    def test_the_checks_run_and_are_asserted_before_the_engine_runs(self):
        order = [
            "Check the assumptions of the rotation engine",
            "Assert the assumptions of the rotation engine hold",
            "Run the rotation engine",
        ]
        self.assertEqual([self.position(n) for n in order], sorted(self.position(n) for n in order))
        self.assertIn("--preflight-only", task(ROTATION, order[0]))
        self.assertNotIn("--preflight-only", task(ROTATION, order[2]))
        self.assertIn("_approle_rotation_preflight.rc == 0", task(ROTATION, order[1]))
        self.assertIn("_approle_rotation_preflight.stderr", task(ROTATION, order[1]))

    def test_the_run_reports_each_target_and_fails_on_a_failed_target(self):
        self.assertIn("_approle_rotation_run.stdout_lines", task(ROTATION, "Report the outcome of each target"))
        self.assertIn("_approle_rotation_run.rc == 0", task(ROTATION, "Assert that no target failed"))
        self.assertIn("failed_when: false", task(ROTATION, "Run the rotation engine"))

    def test_the_command_logs_in_as_the_scheduler_and_acts_as_it(self):
        self.assertIn("--act-as-service-account", ROTATION)
        for flag in (
            "--service-account", "--openbao-role", "--audience", "--lease-namespace", "--openbao-config-map",
            "--openbao-pod-selector",
        ):
            self.assertIn(flag, ROTATION)

    def test_the_run_is_retried_under_the_same_request_id_and_cleans_up(self):
        self.assertIn("approle_rotation_request_id | default(", ROTATION)
        always = ROTATION.split("      always:")[1]
        self.assertIn("state: absent", always)

    def test_the_code_directory_is_private_and_holds_only_code_and_the_registry(self):
        self.assertIn("ansible.builtin.tempfile", ROTATION)
        copy = task(ROTATION, "Copy the rotation code and the registry to the control node")
        self.assertIn('mode: "0600"', copy)
        self.assertIn('directory_mode: "0700"', copy)

    def test_the_playbook_does_not_read_the_break_glass_file(self):
        for word in ("breakglass", "break-glass", "ops_admin", "userpass", "openbao_root_token"):
            self.assertNotIn(word, code(ROTATION))


# The task list that playbook 110 ran before the switch existed, with its four
# leading spaces removed, as it is in the legacy file. The two OpenBao tasks send
# their secrets on stdin; the rest is unchanged.
LEGACY_BODY_SHA256 = "eef72edf3f224d6b2cf583ede5bcf5cdc93ce12de38628453ae296f0f650f9a4"
LEGACY_HEADER_END = "Everything below this comment is the old task list.\n"
SWITCH_ON = "approle_rotation_engine_enabled | default(false) | bool"


class EsoCallerTests(unittest.TestCase):
    def test_the_legacy_file_is_the_old_body_apart_from_its_header_comment(self):
        header, marker, body = LEGACY.partition(LEGACY_HEADER_END)
        self.assertTrue(marker)
        self.assertTrue(all(line.startswith("#") or line == "---" for line in header.splitlines()))
        self.assertEqual(hashlib.sha256(body.encode()).hexdigest(), LEGACY_BODY_SHA256)

    def test_the_switch_off_runs_the_legacy_tasks_and_the_switch_on_calls_the_rotation_playbook(self):
        body = code(ESO)
        self.assertIn("import_playbook: 115-approle-rotation.yml", body)
        self.assertIn("approle_rotation_target: external-secrets", body)
        self.assertIn(f"when: {SWITCH_ON}", ESO)
        include = task(ESO, "Run the earlier ESO rotation procedure")
        self.assertIn("eso-secret-rotation-legacy.yml", include)
        self.assertIn(f"when: not ({SWITCH_ON})", include)
        self.assertIn(f"when: not ({SWITCH_ON})", task(ESO, "Wait for the cluster to be ready"))
        self.assertIn("name: base/cluster-ready", task(ESO, "Wait for the cluster to be ready"))

    def test_the_default_is_off_so_the_old_procedure_runs(self):
        self.assertNotRegex(DEFAULTS, r"approle_rotation_engine_enabled")
        self.assertIn("default(false)", ESO)
        self.assertNotIn("default(true)", ESO)

    def test_the_legacy_play_keeps_its_hosts_and_variables(self):
        for line in (
            "  hosts: k3s_control[0]",
            "  become: true",
            "    eso_rotation_openbao_namespace: \"{{ openbao_namespace | default('openbao') }}\"",
            "    eso_rotation_openbao_role_name: \"{{ openbao_eso_role_name | default('external-secrets') }}\"",
            "    eso_rotation_approle_secret_name: \"{{ eso_approle_secret_name | default('openbao-approle') }}\"",
            "    eso_rotation_breakglass_file: \"{{ eso_openbao_breakglass_file | default('') }}\"",
            "    eso_rotation_external_secrets_namespace: \"{{ eso_namespace | default('external-secrets') }}\"",
        ):
            self.assertIn(line + "\n", ESO)

    def test_the_bootstrap_playbooks_still_import_the_file_in_the_same_place(self):
        for name in ("bootstrap-provision-pre-seed.yml", "bootstrap-sandbox-provision-pre-seed.yml"):
            with self.subTest(playbook=name):
                text = (ROOT / name).read_text(encoding="utf-8")
                self.assertEqual(text.count("playbooks/vertical-orchestration/110-eso-secret-rotation.yml"), 1)


class VerifierTests(unittest.TestCase):
    PROBES = re.findall(
        r"- identity: rotation-scheduler\n\s+path: (\S+)\n\s+expected: (\w+)\n", VERIFY
    )

    def test_the_scheduler_logs_in_with_a_token_request_and_the_token_is_probed(self):
        self.assertIn("- name: Request a JWT for the rotation scheduler ServiceAccount", VERIFY)
        self.assertIn("- name: Log in as the rotation scheduler", VERIFY)
        self.assertIn("'rotation-scheduler': _verify_token_rotation_scheduler", VERIFY)
        login = task(VERIFY, "Log in as the rotation scheduler")
        self.assertIn("jwt=-", login)
        self.assertIn('stdin: "{{ _verify_scheduler_jwt.stdout | trim }}"', login)
        self.assertIn("stdin_add_newline: false", login)
        self.assertNotRegex(login, r"(?m)^\s+environment:")

    def test_the_negative_probes_cover_every_required_class(self):
        denied = {path for path, expected in self.PROBES if expected == "deny"}
        self.assertTrue(any(p.startswith("secret/data/apps/") for p in denied), "an application secret read")
        self.assertTrue(any(p.startswith("sys/policies/acl/") for p in denied), "a policy write")
        self.assertTrue(
            any(re.fullmatch(r"auth/approle/role/(?!external-secrets/)[^/]+/secret-id", p) for p in denied),
            "an unrelated AppRole's secret-id endpoint",
        )
        self.assertTrue({"sys/auth", "sys/mounts"} <= denied, "sys/*")

    def test_every_probe_expectation_agrees_with_the_rendered_policy(self):
        self.assertTrue(self.PROBES)
        for path, expected in self.PROBES:
            with self.subTest(path=path):
                if expected == "deny":
                    self.assertNotIn(path, POLICY)
                else:
                    self.assertEqual(POLICY[path]["capabilities"], [expected])

    def test_the_assertion_accepts_read(self):
        self.assertIn("(item.0.expected == 'read' and item.1 == ['read'])", VERIFY)

    def test_the_login_and_probes_run_only_when_the_scheduler_exists_or_the_switch_is_on(self):
        self.assertIn("failed_when: false", task(VERIFY, "Request a JWT for the rotation scheduler ServiceAccount"))
        login = task(VERIFY, "Log in as the rotation scheduler")
        self.assertIn("failed_when: false", login)
        self.assertIn("when: _verify_scheduler_jwt.rc == 0", login)
        ready = task(VERIFY, "Work out whether the rotation scheduler is set up")
        self.assertIn("approle_rotation_engine_enabled | default(false) | bool", ready)
        self.assertIn("'could not be found'", ready)
        self.assertIn("'invalid role name'", ready)
        self.assertIn("' not found'", ready)
        self.assertNotIn("(NotFound)", ready)
        self.assertIn("'serviceaccounts'", ready)
        self.assertNotIn("serviceaccounts", task(VERIFY, "Work out whether the token request failed for another reason"))
        guard = task(VERIFY, "Assert the rotation scheduler can log in when it is expected to")
        self.assertIn("_verify_scheduler_ready | bool or (_verify_scheduler_not_set_up | bool and not (_verify_scheduler_switch | bool))", guard)
        self.assertIn("is not set up yet", task(VERIFY, "Say that the rotation scheduler is not set up yet") + "is not set up yet")
        self.assertIn("rejectattr('identity', 'equalto', 'rotation-scheduler')", task(VERIFY, "Choose the capability probes to run"))
        self.assertIn("else ''", task(VERIFY, "Capture rotation-scheduler token"))

    def test_every_probe_loop_and_the_summary_use_the_chosen_probes(self):
        self.assertNotIn("loop: \"{{ verify_probes", code(VERIFY))
        self.assertNotRegex(code(VERIFY), r"for p in verify_probes|verify_probes \| length")
        self.assertIn('loop: "{{ _verify_active_probes }}"', VERIFY)
        self.assertIn("loop: \"{{ _verify_active_probes | zip(_verify_probe_caps_list) | list }}\"", VERIFY)

    def test_the_verifier_defaults_match_the_role_defaults(self):
        for name in ("service_account", "k8s_role"):
            self.assertIn(
                f"openbao_rotation_scheduler_{name} | default('{default('openbao_rotation_scheduler_' + name)}')", VERIFY
            )
        self.assertIn(f"openbao_rotation_scheduler_audience | default('{default('openbao_rotation_scheduler_audience')}')", VERIFY)


SCHEDULER_TASKS = [
    "Work out whether the rotation scheduler is set up",
    "Work out whether the token request failed for another reason",
    "Assert the token request for the rotation scheduler did not fail",
    "Work out whether the rotation scheduler is missing",
    "Assert the rotation scheduler can log in when it is expected to",
    "Say that the rotation scheduler is not set up yet",
]
# What `kubectl create token` prints for a ServiceAccount that does not exist.
SA_MISSING = 'error: failed to create token: serviceaccounts "approle-rotation-scheduler" not found'


@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook is not installed")
class SchedulerDecisionTests(unittest.TestCase):
    """Run the verifier's own decision tasks through ansible-playbook with
    each kind of token request and login result."""

    def run_case(self, jwt_rc, jwt_err, login, switch):
        """`login` is (rc, stderr), or None when the login task was skipped."""
        login_value = {"skipped": True} if login is None else {"rc": login[0], "stderr": login[1], "stdout": ""}
        tasks = "".join(
            f"    - name: {name}\n" + task(VERIFY, name) for name in SCHEDULER_TASKS
        )
        play = (
            "- hosts: localhost\n  connection: local\n  gather_facts: false\n  vars:\n"
            "    verify_rotation_scheduler_service_account: approle-rotation-scheduler\n"
            "    verify_rotation_scheduler_k8s_role: approle-rotation-scheduler\n"
            f"    approle_rotation_engine_enabled: {str(switch).lower()}\n"
            f"    _verify_scheduler_jwt: {json.dumps({'rc': jwt_rc, 'stderr': jwt_err, 'stdout': ''})}\n"
            f"    _verify_scheduler_login: {json.dumps(login_value)}\n"
            "  tasks:\n" + tasks
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "play.yml")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(play)
            env = dict(os.environ, ANSIBLE_LOCAL_TEMP=tmp, ANSIBLE_HOME=tmp, ANSIBLE_NOCOLOR="1")
            done = subprocess.run(
                ["ansible-playbook", "-i", "localhost,", path], capture_output=True, text=True, env=env, cwd=tmp, check=False
            )
        return done.returncode, done.stdout + done.stderr

    def assert_runs(self, *case, skipped_message):
        rc, out = self.run_case(*case)
        self.assertEqual(rc, 0, out)
        self.assertEqual("does not exist yet" in out, skipped_message, out)

    def assert_fails(self, *case, mentions):
        rc, out = self.run_case(*case)
        self.assertNotEqual(rc, 0, out)
        self.assertIn(mentions, out)

    def test_a_working_scheduler_runs_the_probes(self):
        for switch in (False, True):
            self.assert_runs(0, "", (0, ""), switch, skipped_message=False)

    def test_a_missing_service_account_skips_with_the_switch_off_and_fails_with_it_on(self):
        self.assert_runs(1, SA_MISSING, None, False, skipped_message=True)
        self.assert_fails(1, SA_MISSING, None, True, mentions="could not log in")

    def test_both_missing_role_answers_skip_with_the_switch_off_and_fail_with_it_on(self):
        for error in (
            'Error writing data to auth/kubernetes/login: Error making API request.\n* invalid role name "x"',
            'Error making API request.\n* role "x" could not be found',
        ):
            with self.subTest(error=error):
                self.assert_runs(0, "", (2, error), False, skipped_message=True)
                self.assert_fails(0, "", (2, error), True, mentions="could not log in")

    def test_any_other_token_request_failure_fails_whatever_the_switch_says(self):
        for error in (
            'Error from server (Forbidden): serviceaccounts "x" is forbidden: cannot create resource "serviceaccounts/token"',
            "Unable to connect to the server: net/http: request canceled (Client.Timeout exceeded)",
            "Error from server (InternalError): Internal error occurred",
            'Error from server (NotFound): namespaces "openbao" not found',
        ):
            for switch in (False, True):
                with self.subTest(error=error[:40], switch=switch):
                    self.assert_fails(1, error, None, switch, mentions="failed for a reason other than")
                    self.assert_fails(1, error, None, switch, mentions=error[:30])

    def test_any_other_login_failure_fails_whatever_the_switch_says(self):
        for switch in (False, True):
            with self.subTest(switch=switch):
                self.assert_fails(0, "", (2, "Error making API request.\n* permission denied"), switch, mentions="could not log in")


class PolicyAgainstProbesTests(unittest.TestCase):
    def test_the_permitted_helper_agrees_with_the_verifier_for_the_allowed_probes(self):
        for path, expected in VerifierTests.PROBES:
            if expected != "deny":
                params = {"secret_id": "c", "ttl": f"{registry.ttl_seconds('720h')}s", "num_uses": 0} if path.endswith("custom-secret-id") else None
                self.assertTrue(permitted(POLICY, path, expected, params), path)


if __name__ == "__main__":
    unittest.main()
