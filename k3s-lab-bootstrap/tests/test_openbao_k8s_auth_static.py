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


class OneVariableTest(unittest.TestCase):
    def test_openbao_role_reads_the_external_secrets_variable_and_defaults_to_approle(self):
        self.assertIn(
            "openbao_eso_auth_method: \"{{ eso_openbao_auth_method | default('approle') }}\"",
            text(OPENBAO / "defaults/main.yml"),
        )
        self.assertRegex(text(ESO / "defaults/main.yml"), r"(?m)^eso_openbao_auth_method: approle$")

    def test_the_reviewer_identity_has_its_own_variable_that_is_off_by_default(self):
        self.assertRegex(
            text(OPENBAO / "defaults/main.yml"), r"(?m)^openbao_kubernetes_reviewer_enabled: false$"
        )

    def test_the_reviewer_files_read_only_the_reviewer_variable(self):
        # The ServiceAccount, the ClusterRole and the StatefulSet follow the reviewer
        # variable alone, so flipping the ESO switch back removes none of them.
        for path in [OPENBAO / "tasks/workload.yml", OPENBAO / "templates/statefulset.yml.j2"]:
            with self.subTest(path=path.name):
                self.assertNotIn("auth_method", text(path))
                self.assertIn("openbao_kubernetes_reviewer_enabled", text(path))

    def test_the_eso_switch_needs_the_reviewer_variable_in_both_roles(self):
        self.assertIn(
            "openbao_eso_auth_method != 'kubernetes' or openbao_kubernetes_reviewer_enabled | bool",
            text(OPENBAO / "tasks/main.yml"),
        )
        self.assertIn(
            "eso_openbao_auth_method != 'kubernetes' or openbao_kubernetes_reviewer_enabled | default(false) | bool",
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
        self.assertIn("kubernetes_host={{ openbao_kubernetes_auth_host | quote }}", self.config)
        self.assertIn('kubernetes_ca_cert=""', self.config)
        self.assertIn('token_reviewer_jwt=""', self.config)
        self.assertIn("disable_local_ca_jwt=false", self.config)
        self.assertEqual(
            re.search(r"(?m)^openbao_kubernetes_auth_host: (\S+)$", text(OPENBAO / "defaults/main.yml")).group(1),
            "https://kubernetes.default.svc",
        )

    def test_config_is_never_written_from_the_pod_files(self):
        for path in [OPENBAO / "tasks/kubernetes-auth-config.yml", OPENBAO / "tasks/kubernetes-auth-existing.yml"]:
            self.assertNotIn("serviceaccount/token", text(path))
            self.assertNotIn("KUBERNETES_SERVICE_HOST", text(path))

    def test_token_goes_in_by_standard_input_only(self):
        for path in [OPENBAO / "tasks/kubernetes-auth-config.yml", OPENBAO / "tasks/kubernetes-auth-existing.yml"]:
            body = text(path)
            self.assertNotRegex(body, r"(?m)^\s+environment:")
            commands = re.findall(r"cmd: >-\n(.*?)\n\s+stdin: ", body, re.S)
            self.assertGreater(len(commands), 0)
            for command in commands:
                self.assertIn("IFS= read -r BAO_TOKEN", command)
                self.assertNotRegex(command, r"\{\{[^}]*token(?!_(max_)?ttl|_polic)[^}]*\}\}")


class InstalledPathTest(unittest.TestCase):
    existing = text(OPENBAO / "tasks/kubernetes-auth-existing.yml")
    playbook = text(ROOT / "playbooks/vertical-security/106-openbao-kubernetes-auth.yml")

    def test_token_is_revoked_in_an_always_section(self):
        self.assertRegex(self.existing, r"(?s)  always:\n.*Revoke the temporary root token.*bao token revoke -self")

    def test_the_pod_lookup_runs_inside_the_block_that_owns_the_token(self):
        block = self.existing.index("  block:\n")
        always = self.existing.index("  always:\n")
        self.assertLess(block, self.existing.index("Resolve the pod that runs the commands"))
        self.assertLess(self.existing.index("Resolve the pod that runs the commands"), always)

    def test_the_always_section_fails_loudly_when_the_pod_is_unknown(self):
        always = self.existing[self.existing.index("  always:\n"):]
        self.assertLess(always.index("Fail loudly when the pod is unknown"), always.index("Revoke the temporary root token"))
        self.assertIn("The temporary root token was NOT revoked", always)
        self.assertIn("bao token revoke -accessor", always)
        # The revoke tasks need the pod, so they wait for it.
        for name in ["Revoke the temporary root token", "Prove the server refuses the revoked token"]:
            task = always[always.index(name):]
            self.assertIn("_openbao_k8s_pod_known | default(false) | bool", task.split("\n\n")[0])

    def test_readiness_runs_inside_the_block(self):
        block = self.existing.index("  block:\n")
        always = self.existing.index("  always:\n")
        self.assertLess(block, self.existing.index("Wait until the cluster is ready"))
        self.assertLess(self.existing.index("Wait until the cluster is ready"), always)
        self.assertIn("openbao_k8s_readiness_role: base/cluster-ready", text(OPENBAO / "defaults/main.yml"))

    def test_the_play_runs_nothing_before_the_role_that_owns_the_token(self):
        self.assertNotIn("roles:", self.playbook)
        self.assertNotIn("cluster-ready", re.sub(r"(?m)^\s*#.*$", "", self.playbook))

    def test_the_header_says_what_to_do_when_the_revoke_fails(self):
        self.assertIn("bao token revoke -accessor", self.playbook)
        self.assertIn("bao token revoke -accessor", self.existing)

    def test_the_record_is_written_only_when_something_changed(self):
        record = self.existing[self.existing.index("Record what was written"):self.existing.index("  always:")]
        self.assertIn("when: _openbao_k8s_changed | bool", record)

    def test_play_never_reads_the_break_glass_file(self):
        self.assertNotIn("breakglass", self.existing.lower().replace("break-glass", ""))
        self.assertNotIn("openbao_key_path", self.existing)


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


if __name__ == "__main__":
    unittest.main()
