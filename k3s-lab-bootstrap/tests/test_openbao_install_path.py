"""The install path of the openbao role: every step that needs root runs on the installer's
own tokens, the installer revokes them, and no later path to a root token stays outside the
approval executor.

The runs use the fresh-install harness of test_openbao_fresh_init_token.py (a fake kubectl, a
fake bao and a fake kubernetes.core) on 100-openbao.yml. The wiring tests read the task files as text and render one expression for real.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
import test_openbao_fresh_init_token as fresh  # noqa: E402

ROLE = ROOT / "roles/stack/operator/openbao"
REVOKE_EVENT = "bao openbao-0 token revoke -self"
MINT_EVENT = "bao openbao-0 token create -orphan -policy=root"


def events(run):
    return (run.state / "events.log").read_text().splitlines()


def first(lines, prefix):
    return next(i for i, line in enumerate(lines) if line.startswith(prefix))


def count_before(lines, prefix, limit):
    return len([line for line in lines[:limit] if line.startswith(prefix)])


class FreshInstallPathTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.holder = unittest.TestCase()
        cls.run_ = fresh.play(cls.holder, replicas=1)
        cls.events = events(cls.run_)

    @classmethod
    def tearDownClass(cls):
        cls.holder.doCleanups()

    def test_the_install_ends_well(self):
        self.assertEqual(self.run_.returncode, 0, self.run_.output)

    def test_only_the_executor_role_carries_generate_root_after_the_install(self):
        roles = {
            path.name[len("role-"):-len(".json")]: json.loads(path.read_text())["token_policies"]
            for path in (self.run_.state / "k8s-auth").glob("role-*.json")
        }

        self.assertEqual(sorted(name for name, policies in roles.items() if "generate-root" in policies), ["approval-executor"])

    def test_the_ops_admin_write_of_the_install_attaches_generate_root(self):
        # Renders the real expression of the task, so a policy list built another way is
        # judged by its value and not by its text.
        tasks = yaml.safe_load((ROLE / "tasks/main.yml").read_text())
        upsert = next(t for t in tasks if t.get("name") == "Upsert operator userpass user")
        stdin = upsert["ansible.builtin.shell"]["stdin"]
        work = Path(tempfile.mkdtemp(prefix="install-path-render-"))
        self.addCleanup(__import__("shutil").rmtree, work, True)
        playbook = work / "render.yml"
        playbook.write_text(yaml.safe_dump([{
            "hosts": "localhost", "connection": "local", "gather_facts": False,
            "vars_files": [str(ROLE / "defaults/main.yml")],
            "vars": {"openbao_operator_password": "pw", "_openbao_write_token": "t"},
            "tasks": [{"ansible.builtin.copy": {"dest": str(work / "stdin.txt"), "content": stdin}}],
        }]))
        subprocess.run(["ansible-playbook", str(playbook)], check=True, capture_output=True, cwd=ROOT)

        body = json.loads((work / "stdin.txt").read_text().split("\n", 1)[1])

        self.assertEqual(body["token_policies"], "app-admin-writer,app-runtime-writer,generate-root")

    def test_the_role_revokes_the_init_token_and_the_short_token_and_no_other(self):
        self.assertEqual(count_before(self.events, REVOKE_EVENT, len(self.events)), 2)

    def test_the_tokens_of_the_install_follow_the_lifecycle(self):
        self.assertEqual(fresh.problems(self.run_, 1, "yes", "yes"), [])


SEED_SCRIPT = """#!/bin/sh
# argv: OUT_DIR EXIT_STATUS. Records the token from stdin and the arguments, and marks the
# event log, so a test can order the seed against the revokes of the install.
IFS= read -r token
printf '%s\\n' "$token" > "$1/token"
printf '%s\\n' "$*" > "$1/argv"
echo "seed-ran" >> "$FAKE_STATE/events.log"
echo "seed says hello" >&2
exit "$2"
"""


class SeedStepTest(unittest.TestCase):
    def install(self, status="0", command=True):
        out = Path(tempfile.mkdtemp(prefix="install-seed-"))
        self.addCleanup(__import__("shutil").rmtree, out, True)
        script = out / "seed.sh"
        script.write_text(SEED_SCRIPT)
        script.chmod(0o755)
        holder = unittest.TestCase()
        self.addCleanup(holder.doCleanups)
        extra = {"openbao_install_seed_command": [str(script), str(out), status] if command else []}
        run = fresh.play(holder, replicas=1, extra=extra)
        return run, out

    def test_the_seed_runs_with_the_live_short_token_after_the_init_token_is_gone(self):
        run, out = self.install()
        lines = events(run)
        seed = first(lines, "seed-ran")

        self.assertEqual(run.returncode, 0, run.output)
        self.assertEqual((out / "token").read_text().strip(), fresh.SHORT)
        # The init token is revoked before the seed, the short token after it.
        self.assertEqual(count_before(lines, REVOKE_EVENT, seed), 1)
        self.assertEqual(count_before(lines, REVOKE_EVENT, len(lines)), 2)

    def test_the_token_reaches_the_seed_on_stdin_and_in_no_argument(self):
        run, out = self.install()

        self.assertEqual(run.returncode, 0, run.output)
        self.assertNotIn(fresh.SHORT, (out / "argv").read_text())
        self.assertNotIn(fresh.SHORT, run.output)
        self.assertIn("seed says hello", run.output)

    def test_a_failed_seed_ends_the_install_and_the_rescue_revokes_the_short_token(self):
        run, _ = self.install(status="3")

        self.assertNotEqual(run.returncode, 0)
        self.assertIn("The seed step ended with status 3", run.output)
        self.assertIn("may be partial", " ".join(run.output.split()))
        self.assertIn("Rebuild the environment", " ".join(run.output.split()))
        # The rescue of 100-openbao.yml revokes the short token and proves it refused.
        self.assertEqual(fresh.problems(run, 1, "yes", "yes"), [])

    def test_a_fresh_install_without_a_seed_command_stops_before_init_makes_a_token(self):
        # Catches an install that ends green with nothing seeded: the role revokes its tokens at
        # the end, so nothing could seed later.
        run, out = self.install(command=False)

        self.assertNotEqual(run.returncode, 0)
        self.assertIn("run-playbook.sh", run.output)
        self.assertIn("seed nothing", " ".join(run.output.split()))
        log = run.state / "events.log"
        self.assertNotIn("operator init", log.read_text() if log.exists() else "")
        # Only calls that hold no token ran: init, which makes the first token, did not.
        self.assertEqual([call for call in run.calls if call[0] != "none"], [])
        self.assertFalse((out / "token").exists())

    def test_a_rerun_makes_no_seed_because_it_holds_no_token(self):
        out = Path(tempfile.mkdtemp(prefix="install-seed-"))
        self.addCleanup(__import__("shutil").rmtree, out, True)
        script = out / "seed.sh"
        script.write_text(SEED_SCRIPT)
        script.chmod(0o755)
        holder = unittest.TestCase()
        self.addCleanup(holder.doCleanups)
        extra = {"openbao_install_seed_command": [str(script), str(out), "0"]}
        first_run = fresh.play(holder, replicas=1, extra=extra)
        (out / "token").unlink()
        rerun = fresh.play(holder, replicas=1, extra=extra, env={"FAKE_POLICY_WRITER": "1"}, state=first_run.state, keys=first_run.keys)

        self.assertEqual(rerun.returncode, 0, rerun.output)
        self.assertFalse((out / "token").exists())


class NoCallerOutsideTheInstallTest(unittest.TestCase):
    def test_the_bootstrap_wrappers_import_no_rotation_play(self):
        for name in ("bootstrap-provision-pre-seed.yml", "bootstrap-sandbox-provision-pre-seed.yml"):
            with self.subTest(wrapper=name):
                imports = [step["import_playbook"] for step in yaml.safe_load((ROOT / name).read_text()) if "import_playbook" in step]

                self.assertEqual([i for i in imports if "rotation" in i and "eso" not in i and "approle" not in i], [])
                self.assertIn("playbooks/vertical-security/100-openbao.yml", imports)

    def test_the_installed_environment_play_stays_retired(self):
        # An installed environment is rebuilt, and no play takes a root token from the
        # caller. 106 and the task files that only it used must not come back. Neither does
        # the switch that kept the init token: a fresh install always disposes of it.
        for name in ("playbooks/vertical-security/106-openbao-kubernetes-auth.yml",
                     "roles/stack/operator/openbao/tasks/kubernetes-auth-existing.yml",
                     "roles/stack/operator/openbao/tasks/kubernetes-auth-standing-roles.yml"):
            with self.subTest(file=name):
                self.assertFalse((ROOT / name).exists())
        offenders = [
            str(path.relative_to(ROOT))
            for path in ROOT.rglob("*")
            if path.is_file() and path.suffix in {".yml", ".yaml", ".j2", ".md", ".py", ".sh"}
            and "test_openbao_install_path.py" != path.name
            and any(word in path.read_text(errors="replace")
                    for word in ("106-openbao-kubernetes-auth", "OPENBAO_TEMP_ROOT_TOKEN", "openbao_k8s_temp_root_token",
                                 "openbao_dispose_root_token"))
        ]

        self.assertEqual(offenders, [])

    def test_the_install_playbook_and_its_wrappers_name_no_retired_play(self):
        offenders = [
            str(path.relative_to(ROOT))
            for path in ROOT.rglob("*.yml")
            if "tests" not in path.relative_to(ROOT).parts and "120-ops-admin-rotation" in path.read_text()
        ]

        self.assertEqual(offenders, [])


def operator_messages(node):
    """Yield (key, text) for every message a task shows the operator."""
    if isinstance(node, dict):
        for key, value in node.items():
            if key in ("fail_msg", "success_msg", "msg") and isinstance(value, str):
                yield key, value
            elif key in ("msg", "fail_msg") and isinstance(value, list):
                yield key, " ".join(str(item) for item in value)
            else:
                yield from operator_messages(value)
    elif isinstance(node, list):
        for item in node:
            yield from operator_messages(item)


class NoRetiredRepairPathInMessagesTest(unittest.TestCase):
    """After the install no seed command can run, so no message may send the operator to one."""

    RETIRED = ("seed-bao", "upgrade-in-place")

    def test_no_task_message_names_a_retired_seed_path(self):
        files = sorted(
            [*ROOT.glob("*.yml"), *ROOT.glob("playbooks/**/*.yml"), *ROOT.glob("roles/**/*.yml")]
        )
        self.assertTrue(files)
        hits = []
        for path in files:
            for doc in yaml.safe_load_all(path.read_text()):
                for key, text in operator_messages(doc):
                    for word in self.RETIRED:
                        if word in text:
                            hits.append(f"{path.relative_to(ROOT)}: {key} names {word}")
        self.assertEqual(hits, [])


if __name__ == "__main__":
    unittest.main()
