"""The openbao role stops an image change to 2.6 or later before ops-admin holds the
generate-root policy.

The check lives in tasks/image-policy-preflight.yml, which workload.yml runs first, so every
play that applies the StatefulSet is covered: 100-openbao.yml, 106-openbao-kubernetes-auth.yml
(through kubernetes-auth-existing.yml) and the grow-HA play. The tests run workload.yml for
real against the fake cluster and the fake bao of tests/fixtures.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
ROLE = ROOT / "roles/stack/operator/openbao"
FIXTURES = ROOT / "tests/fixtures"
OLD = "openbao/openbao:2.5.2"
NEW = "docker.io/openbao/openbao:2.7.2@sha256:" + "a" * 64
WITH_POLICY = "app-admin-writer,app-runtime-writer,generate-root"


class StaticTests(unittest.TestCase):
    def test_workload_runs_the_check_first(self) -> None:
        tasks = yaml.safe_load((ROLE / "tasks/workload.yml").read_text())

        self.assertEqual(tasks[0]["ansible.builtin.include_tasks"], "image-policy-preflight.yml")

    def test_every_entry_point_that_applies_the_workload_goes_through_workload_yml(self) -> None:
        for name in ("main.yml", "kubernetes-auth-existing.yml", "grow-ha.yml"):
            with self.subTest(task_file=name):
                self.assertIn("ansible.builtin.include_tasks: workload.yml", (ROLE / "tasks" / name).read_text())

    def test_only_workload_yml_renders_the_statefulset(self) -> None:
        users = sorted(
            path.name
            for path in (ROLE / "tasks").glob("*.yml")
            if "lookup('ansible.builtin.template', 'statefulset.yml.j2')" in path.read_text()
        )

        self.assertEqual(users, ["workload.yml"])


@unittest.skipUnless(shutil.which("ansible-playbook"), "ansible-playbook is not on PATH")
class PreflightRunTests(unittest.TestCase):
    def run_workload(self, installed=OLD, target=NEW, env=None, orphan_pod=None, pvc=False, extra_vars=None):
        work = Path(tempfile.mkdtemp(prefix="image-preflight-"))
        self.addCleanup(shutil.rmtree, work, ignore_errors=True)
        state = work / "state"
        shutil.copytree(FIXTURES / "k8s-seed", state / "k8s")
        if installed:
            (state / "k8s/StatefulSet-openbao.json").write_text(json.dumps(
                {"spec": {"template": {"spec": {"containers": [{"name": "openbao", "image": installed}]}}}}))
        if not installed and not orphan_pod:
            # The seed holds a pod for other tests. A new install has none.
            (state / "k8s/Pod-openbao-0.json").unlink()
        if orphan_pod:
            (state / "k8s/Pod-openbao-0.json").write_text(json.dumps(
                {"metadata": {"name": "openbao-0"}, "spec": {"containers": [{"name": "openbao", "image": orphan_pod}]}}))
        if pvc:
            (state / "k8s/PersistentVolumeClaim-data-openbao-0.json").write_text(json.dumps({"metadata": {"name": "data-openbao-0"}}))
        (work / "breakglass.json").write_text(json.dumps({"ops_admin_username": "ops-admin", "ops_admin_password": "pw"}))
        playbook = work / "play.yml"
        playbook.write_text(yaml.safe_dump([{
            "hosts": "localhost", "connection": "local", "gather_facts": False,
            "vars": {
                "openbao_key_path": str(work / "breakglass"),
                "openbao_image": target,
                "openbao_tls_secret_wait_retries": 2,
                "openbao_tls_secret_wait_delay": 0,
                "openbao_preflight_revoke_delay": 0,
                **(extra_vars or {}),
            },
            "tasks": [{"ansible.builtin.include_role": {"name": "stack/operator/openbao", "tasks_from": "workload.yml"}}],
        }]))
        run_env = {
            **os.environ,
            "PATH": f"{FIXTURES / 'fake-kubectl'}:{os.environ['PATH']}",
            "ANSIBLE_COLLECTIONS_PATH": str(FIXTURES / "fake-collections"),
            "ANSIBLE_ROLES_PATH": os.environ.get("TEST_ROLES_PATH", str(ROOT / "roles")),
            "FAKE_STATE": str(state),
            **(env or {}),
        }
        result = subprocess.run(["ansible-playbook", str(playbook)], capture_output=True, text=True, env=run_env, cwd=ROOT)
        return result, state

    def mutations(self, state):
        path = state / "k8s-mutations.log"
        return path.read_text() if path.exists() else ""

    def logged_in(self, state):
        path = state / "bao-argv.log"
        return path.exists() and "auth/userpass/login" in path.read_text()

    def test_an_installed_2_5_2_server_without_the_policy_stops_before_any_workload_change(self) -> None:
        result, state = self.run_workload()

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Attach the generate-root policy first", result.stdout)
        self.assertIn("ops_admin_rotation_attach_only=true", result.stdout)
        self.assertEqual(self.mutations(state), "")
        self.assertTrue((state / "revoked-fake-ops-token").exists())

    def test_with_the_policy_present_the_workload_applies(self) -> None:
        result, state = self.run_workload(env={"FAKE_OPS_POLICIES": WITH_POLICY})

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("present StatefulSet openbao", self.mutations(state))
        self.assertTrue((state / "revoked-fake-ops-token").exists())

    def test_an_unchanged_image_skips_the_check(self) -> None:
        result, state = self.run_workload(target=OLD)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.logged_in(state))

    def test_a_new_install_skips_the_check(self) -> None:
        result, state = self.run_workload(installed=None)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.logged_in(state))

    def test_an_image_older_than_2_6_skips_the_check(self) -> None:
        result, state = self.run_workload(target="openbao/openbao:2.5.3")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.logged_in(state))

    def test_a_server_that_is_not_initialized_skips_the_check(self) -> None:
        result, state = self.run_workload(env={"FAKE_INITIALIZED": "false"})

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.logged_in(state))

    def test_an_orphan_pod_without_a_statefulset_counts_as_installed(self) -> None:
        # An interrupted migration: the StatefulSet is gone, the initialized 2.5.2 pod stays.
        result, state = self.run_workload(installed=None, orphan_pod=OLD)

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Attach the generate-root policy first", result.stdout)
        self.assertEqual(self.mutations(state), "")

    def test_an_orphan_pod_with_the_policy_present_lets_the_workload_apply(self) -> None:
        result, state = self.run_workload(installed=None, orphan_pod=OLD, env={"FAKE_OPS_POLICIES": WITH_POLICY})

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("present StatefulSet openbao", self.mutations(state))

    def test_an_orphan_pod_that_already_runs_the_target_image_skips_the_check(self) -> None:
        result, state = self.run_workload(installed=None, orphan_pod=NEW)

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(self.logged_in(state))

    def test_a_volume_claim_without_a_pod_or_statefulset_counts_as_installed(self) -> None:
        result, state = self.run_workload(installed=None, pvc=True, env={"FAKE_MISSING": "openbao-0"})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Cannot read the OpenBao status", result.stdout)
        self.assertEqual(self.mutations(state), "")

    def test_a_login_revoke_that_keeps_failing_names_the_accessor(self) -> None:
        result, state = self.run_workload(env={"FAKE_OPS_POLICIES": WITH_POLICY, "FAKE_REVOKE_FAILS": "login"})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("may stay valid until it expires", result.stdout)
        self.assertIn("fake-ops-accessor", result.stdout)
        self.assertNotIn("fake-ops-token", result.stdout)
        self.assertEqual((state / "count-revoke-login").read_text().strip(), "3")

    def test_a_sealed_server_stops_the_play_before_any_workload_change(self) -> None:
        result, state = self.run_workload(env={"FAKE_SEALED": "openbao-0"})

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("is sealed", result.stdout)
        self.assertEqual(self.mutations(state), "")


if __name__ == "__main__":
    unittest.main()
