"""Static checks for ESO's main store on Kubernetes auth: the two roles read one
variable, the config step never puts a credential in an argument list, the
ClusterRole grants only TokenReview create, and the verifier covers the write
probe for every standing identity."""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPENBAO = ROOT / "roles/stack/operator/openbao"
ESO = ROOT / "roles/base/external-secrets"
VERIFY = ROOT / "playbooks/verify-openbao-identity-model.yml"


def text(path):
    return path.read_text(encoding="utf-8")


CLI_ROLE = "common/openbao-cli"
CLI_ROLE_TASKS = ROOT / "roles/common/openbao-cli/tasks/main.yml"
# A word that names a token or the reviewer JWT, not a TTL or a policy list.
TOKEN_WORD = re.compile(r"\{\{[^}]*(?:token|jwt)(?!_(?:max_)?ttl|_polic)[^}]*\}\}", re.I)


def load_tasks(path):
    """Return every task of a task file, with the tasks of blocks, rescues and always parts."""
    import yaml

    tasks = []

    def walk(items):
        for item in items:
            tasks.append(item)
            for key in ("block", "rescue", "always"):
                walk(item.get(key, []))

    walk(yaml.safe_load(text(path)))
    return tasks


def is_cli_include(task):
    include = task.get("ansible.builtin.include_role")
    return bool(include) and include.get("name") == CLI_ROLE


def cli_includes(path):
    return [t for t in load_tasks(path) if is_cli_include(t)]


def cli_call_words(task):
    """Return every word of `args` of every call of a shared-role include (the calls are a list here)."""
    calls = task["vars"]["openbao_cli_calls"]
    assert isinstance(calls, list), task["name"]
    return [str(word) for call in calls for word in call["args"]]


def role_command_task():
    return next(t for t in load_tasks(CLI_ROLE_TASKS) if "ansible.builtin.command" in t)


class OneVariableTest(unittest.TestCase):
    def test_openbao_role_reads_the_external_secrets_variable_and_defaults_to_approle(self):
        self.assertIn(
            "openbao_eso_auth_method: \"{{ eso_openbao_auth_method | default('approle') }}\"",
            text(OPENBAO / "defaults/main.yml"),
        )
        self.assertRegex(text(ESO / "defaults/main.yml"), r"(?m)^eso_openbao_auth_method: approle$")

    def test_the_reviewer_identity_has_its_own_variable_that_is_on_by_default(self):
        # With false, the mount stores the pod's default ServiceAccount token, which
        # has no TokenReview grant: every Kubernetes-auth login returns 403.
        self.assertRegex(
            text(OPENBAO / "defaults/main.yml"), r"(?m)^openbao_kubernetes_reviewer_enabled: true$"
        )

    def test_no_install_path_writes_the_stored_token_config(self):
        main = text(OPENBAO / "tasks/main.yml")
        self.assertNotIn("token_reviewer_jwt=", main)
        self.assertNotRegex(main, r"(?s)bao write auth/kubernetes/config[^\n]*\n[^\n]*failed_when")
        self.assertIn("kubernetes-auth-config.yml", main)
        self.assertIn("openbao_kubernetes_reviewer_enabled is false", main)
        # The refusal is unconditional: the install always sets up Kubernetes-auth clients.
        assert_block = re.search(
            r"- name: Refuse a Kubernetes auth setup without the reviewer identity\n(.*?)\n\n", main, re.S
        ).group(1)
        self.assertIn("that: openbao_kubernetes_reviewer_enabled | bool", assert_block)
        self.assertNotIn("when:", assert_block)

    def test_the_external_secrets_role_defaults_the_reviewer_variable_to_on(self):
        self.assertIn("openbao_kubernetes_reviewer_enabled | default(true) | bool", text(ESO / "tasks/main.yml"))

    def test_the_reviewer_files_read_only_the_reviewer_variable(self):
        # The ServiceAccount, the ClusterRole and the StatefulSet follow the reviewer
        # variable alone, so flipping the ESO switch back removes none of them.
        for path in [OPENBAO / "tasks/workload.yml", OPENBAO / "templates/statefulset.yml.j2"]:
            with self.subTest(path=path.name):
                self.assertNotIn("auth_method", text(path))
                self.assertIn("openbao_kubernetes_reviewer_enabled", text(path))

    def test_the_eso_switch_needs_the_reviewer_variable_in_both_roles(self):
        self.assertIn("that: openbao_kubernetes_reviewer_enabled | bool", text(OPENBAO / "tasks/main.yml"))
        self.assertIn(
            "eso_openbao_auth_method != 'kubernetes' or openbao_kubernetes_reviewer_enabled | default(true) | bool",
            text(ESO / "tasks/main.yml"),
        )


class TokenReviewTest(unittest.TestCase):
    def test_cluster_role_grants_only_create_on_tokenreviews(self):
        workload = text(OPENBAO / "tasks/workload.yml")
        rules = re.search(r"kind: ClusterRole\n.*?rules:\n(.*?)\n  when:", workload, re.S).group(1)
        self.assertIn('resources: ["tokenreviews"]', rules)
        self.assertIn('verbs: ["create"]', rules)
        self.assertEqual(rules.count("apiGroups"), 1)

    def test_no_binding_to_auth_delegator(self):
        for path in OPENBAO.rglob("*.yml"):
            with self.subTest(path=path.name):
                self.assertNotRegex(text(path), r"name:\s*[\"']?system:auth-delegator")


class ConfigStepTest(unittest.TestCase):
    config = text(OPENBAO / "tasks/kubernetes-auth-config.yml")

    def test_config_has_the_portable_host_and_pins_no_ca_or_token(self):
        # The config write is one `bao write` whose arguments are the words below. The host is
        # an argument word, and the empty CA and reviewer values are words with nothing after `=`.
        writes = [
            call["args"]
            for task in cli_includes(OPENBAO / "tasks/kubernetes-auth-config.yml")
            for call in task["vars"]["openbao_cli_calls"]
            if call["args"][:2] == ["write", "auth/kubernetes/config"]
        ]
        self.assertEqual(len(writes), 1)
        words = writes[0]
        self.assertIn("kubernetes_host={{ openbao_kubernetes_auth_host }}", words)
        self.assertIn("kubernetes_ca_cert=", words)
        self.assertIn("token_reviewer_jwt=", words)
        self.assertIn("disable_local_ca_jwt=false", words)
        self.assertEqual(sum(w.startswith("kubernetes_host=") for w in words), 1)
        self.assertEqual(
            re.search(r"(?m)^openbao_kubernetes_auth_host: (\S+)$", text(OPENBAO / "defaults/main.yml")).group(1),
            "https://kubernetes.default.svc",
        )

    def test_config_is_never_written_from_the_pod_files(self):
        for path in [OPENBAO / "tasks/kubernetes-auth-config.yml"]:
            self.assertNotIn("serviceaccount/token", text(path))
            self.assertNotIn("KUBERNETES_SERVICE_HOST", text(path))

    def test_token_goes_in_by_standard_input_only(self):
        # The caller hands the token to the shared role as `openbao_cli_token`, and the role
        # sends it on stdin. No argument word of any call holds a token, and no task of these
        # files runs a command of its own.
        for path in [
            OPENBAO / "tasks/kubernetes-auth-config.yml",
            OPENBAO / "tasks/kubernetes-auth-revoke.yml",
        ]:
            body = text(path)
            self.assertNotRegex(body, r"(?m)^\s+environment:")
            includes = cli_includes(path)
            self.assertGreater(len(includes), 0)
            for task in load_tasks(path):
                with self.subTest(path=path.name, task=task["name"]):
                    self.assertNotIn("ansible.builtin.command", task)
                    self.assertNotIn("ansible.builtin.shell", task)
            for task in includes:
                with self.subTest(path=path.name, task=task["name"]):
                    self.assertRegex(task["vars"]["openbao_cli_token"], r"^\{\{ openbao_k8s_\w*token \}\}$")
                    for word in cli_call_words(task):
                        self.assertNotRegex(word, TOKEN_WORD)

    def test_the_shared_role_keeps_the_token_on_stdin_and_out_of_the_log(self):
        command = role_command_task()
        argv = " ".join(command["ansible.builtin.command"]["argv"])
        self.assertIs(command.get("no_log"), True)
        self.assertIn("openbao_cli_token", command["vars"]["openbao_cli_stdin"])
        self.assertNotIn("openbao_cli_token", argv)
        self.assertNotIn("openbao_cli_call", argv)
        self.assertIs(command["vars"]["ansible_pipelining"], True)


class VerifierTest(unittest.TestCase):
    verify = text(VERIFY)

    def test_every_standing_identity_has_a_write_probe(self):
        for identity in ["ops-admin", "policy-reconciler", "approle-reconciler"]:
            with self.subTest(identity=identity):
                self.assertRegex(
                    self.verify,
                    r"- identity: %s\n        path: auth/kubernetes/config\n        expected: deny" % identity,
                )
        self.assertIn("loop: [ops-admin, policy-reconciler, approle-reconciler]", self.verify)

    def test_the_probe_namespace_is_unique_and_deleted_only_when_created(self):
        self.assertNotRegex(self.verify, r"(?<!_)verify_wrong_namespace")
        self.assertIn('_verify_wrong_namespace: "dmf-verify-{{ _verify_run_id }}"', self.verify)
        self.assertRegex(self.verify, r"- create\n\s+- namespace\n")
        self.assertNotRegex(self.verify, r"kind: Namespace")
        self.assertRegex(
            self.verify, r"- delete\n(?:.*\n){1,4}\s+changed_when: true\n\s+when: _verify_wrong_ns_created \| default\(false\) \| bool"
        )

    def test_the_refresh_probe_checks_the_store(self):
        self.assertIn("spec.secretStoreRef.name == verify_eso_store_name", self.verify)
        self.assertNotIn("new data", self.verify)

    def test_the_refresh_probe_compares_the_status_with_the_version_it_computes(self):
        attempt = text(ROOT / "playbooks/tasks/verify-main-store-refresh.yml")
        order = [
            "Wait until the synced resource version matches the current metadata",
            "Assert that no annotation or label could hide this run's marker",
            "Assert that this ESO version builds the synced resource version",
            "Set a force-sync annotation",
            "Wait for the synced resource version of the annotated metadata",
            "Note whether this attempt saw the refresh",
        ]
        positions = [attempt.index(name) for name in order]
        self.assertEqual(positions, sorted(positions))
        # The version is computed once, from the object that the same read returned.
        self.assertEqual(attempt.count("hash('sha3_224')"), 1)
        self.assertIn("_verify_es_status_version == _verify_es_version", attempt)
        self.assertEqual(attempt.count("_verify_es_status_version == _verify_es_version"), 2)
        # The verdict needs this attempt's annotation and a Ready status with the sync reason.
        self.assertIn("== 'verify-' ~ _verify_run_id ~ '-' ~ _verify_attempt", attempt)
        self.assertIn("selectattr('reason', 'equalto', 'SecretSynced')", attempt)
        self.assertIn("selectattr('status', 'equalto', 'True')", attempt)
        # No value recorded before the annotation takes part in the verdict.
        self.assertNotIn("_verify_synced_before", attempt)
        self.assertIn("until: _verify_es_refresh_seen | bool", attempt)
        self.assertIn('_verify_refreshed: "{{ _verify_es_refresh_seen | bool }}"', attempt)
        # The compatibility check runs before the first annotation only.
        self.assertEqual(attempt.count("when: _verify_attempt | int == 1"), 3)
        # Metadata that could hide the marker is refused by the compatibility read and by the
        # read that decides an attempt.
        self.assertEqual(attempt.count("_verify_es_metadata_hides_marker | bool"), 3)
        self.assertIn("until: _verify_es_settled | bool or _verify_es_metadata_hides_marker | bool", attempt)
        self.assertIn("that: not (_verify_es_metadata_hides_marker | bool)", attempt)
        self.assertIn("because its metadata could hide the marker", attempt)
        self.assertIn("force-sync=verify-{{ _verify_run_id }}-{{ _verify_attempt }}", attempt)
        self.assertRegex(attempt, r"probably builds the value\s+differently from v2\.3\.0")
        # The bounds of the waits stay as they were.
        self.assertIn("_verify_refresh_retries | default(6)", attempt)
        self.assertIn("_verify_refresh_delay | default(5)", attempt)

    def test_the_refresh_tasks_read_no_clock_and_compare_no_time(self):
        attempt = text(ROOT / "playbooks/tasks/verify-main-store-refresh.yml")
        tasks = "\n".join(line for line in attempt.splitlines() if not line.lstrip().startswith("#"))
        for clock in ["refreshTime", "ansible.builtin.pipe", "ansible_date_time", "now()", "- date", "date -u", "sleep"]:
            self.assertNotIn(clock, tasks)
        self.assertNotRegex(tasks, r"_verify_refresh_after|_verify_clock")

    def test_an_attempt_after_a_success_does_nothing(self):
        attempt = text(ROOT / "playbooks/tasks/verify-main-store-refresh.yml")
        guard = "  when: not (_verify_refreshed | default(false) | bool)\n"
        self.assertIn(guard, attempt)
        # Every task of the attempt sits inside the guarded block.
        self.assertRegex(attempt, r"(?m)^  block:\n")
        self.assertNotRegex(attempt[attempt.index(guard) + len(guard):], r"(?m)^- name:")
        # The caller's loop item does not carry the guard: Ansible would evaluate it
        # for every item before the first attempt ran.
        self.assertNotIn("when: not (_verify_refreshed", self.verify)

    def test_verifier_has_each_negative_login(self):
        for case in ["wrong ServiceAccount", "wrong namespace", "wrong audience"]:
            self.assertIn(case, self.verify)


class ReviewerGuardProbeStaticTest(unittest.TestCase):
    """The login probe keeps the JWT and the client token off every command line."""

    PROBE = OPENBAO / "tasks/reviewer-login-probe.yml"

    @classmethod
    def setUpClass(cls):
        cls.tasks = load_tasks(cls.PROBE)

    def commands(self):
        return [t for t in self.tasks if "ansible.builtin.command" in t]

    def includes(self):
        return [t for t in self.tasks if is_cli_include(t)]

    def test_no_argument_holds_a_token_or_the_jwt(self):
        for task in self.commands():
            argv = task["ansible.builtin.command"]["argv"]
            with self.subTest(task=task["name"]):
                for word in argv:
                    self.assertNotRegex(word, r"_openbao_reviewer_guard_(jwt|login|client_token|revoke)", "a probe result in an argument")
                    self.assertNotRegex(word, r"\{\{.*stdout", "a command output in an argument")
        for task in self.includes():
            with self.subTest(task=task["name"]):
                for word in cli_call_words(task):
                    self.assertNotRegex(word, r"_openbao_reviewer_guard_|stdout", "a probe result in an argument")

    def test_the_login_and_the_revoke_read_their_secret_from_stdin(self):
        by_name = {t["name"]: t for t in self.includes()}
        login = by_name["Log in to the Kubernetes auth mount inside openbao-0"]
        revoke = by_name["Revoke the client token of the login"]
        # The JWT is the `input` of the login. The client token is the token of the revoke.
        login_call = login["vars"]["openbao_cli_calls"][0]
        self.assertIn("_openbao_reviewer_guard_jwt", login_call["input"])
        self.assertNotIn("openbao_cli_token", login["vars"])
        self.assertEqual(
            revoke["vars"]["openbao_cli_token"], "{{ _openbao_reviewer_guard_client_token }}"
        )
        self.assertNotIn("input", revoke["vars"]["openbao_cli_calls"][0])

    def test_the_jwt_is_never_a_command_line_value(self):
        # The JWT is in the login's `input` and in no `args` word of any call. The login reads
        # it from stdin through `jwt=-`.
        login = next(t for t in self.includes() if t["name"].startswith("Log in to"))
        call = login["vars"]["openbao_cli_calls"][0]
        self.assertIn("jwt=-", call["args"])
        self.assertIn("_openbao_reviewer_guard_jwt", call["input"])
        for task in self.includes():
            for word in cli_call_words(task):
                self.assertNotIn("_guard_jwt", word)
        # The mint is the one command task that prints a JWT. It runs `kubectl create token`
        # and takes no secret as an argument.
        for task in self.commands():
            self.assertNotRegex(" ".join(task["ansible.builtin.command"]["argv"]), r"_guard_jwt")

    def test_every_task_that_holds_a_token_logs_nothing(self):
        # A shared-role include gets its no_log from the role, so the role's command task
        # must have it. A task of the probe that is not an include and holds a token must set
        # it itself.
        self.assertIs(role_command_task().get("no_log"), True)
        for task in self.tasks:
            body = repr(task)
            if any(name in body for name in ("_guard_jwt.stdout", "_guard_client_token", "_guard_login.stdout")) \
                    and "ansible.builtin.fail" not in task and "block" not in task and not is_cli_include(task):
                with self.subTest(task=task["name"]):
                    self.assertIs(task.get("no_log"), True)
        for task in self.commands():
            if task["name"].split()[0] in ("Mint", "Log", "Revoke"):
                with self.subTest(task=task["name"]):
                    self.assertIs(task.get("no_log"), True)

    def test_no_task_prints_a_token_bearing_result(self):
        # The registers hold a token in their stdout. No debug task may print one of them.
        for task in self.tasks:
            if "ansible.builtin.debug" in task:
                with self.subTest(task=task["name"]):
                    self.assertNotRegex(repr(task), r"_openbao_reviewer_guard_(jwt|login|client_token|revoke)")

    def test_no_failure_message_prints_a_token(self):
        for task in self.tasks:
            if "ansible.builtin.fail" in task:
                msg = task["ansible.builtin.fail"]["msg"]
                with self.subTest(task=task["name"]):
                    self.assertNotRegex(msg, r"_guard_jwt\.stdout|_guard_client_token|_guard_login\.stdout")
