"""A fresh install of the openbao role, run through playbooks/vertical-security/100-openbao.yml
against the fake cluster (a fake kubectl, a fake bao and a fake kubernetes.core).

The init token that `bao operator init` returns has no lifetime. The role trades it right
after the unseal for a short token, revokes the init token with proof before the first
write, and writes with the short token. These tests read the calls that the fake bao
recorded, token by token, and the files it keeps for each token (fixtures/token_lifecycle.py
judges them). They cover a good run, a rerun, a run with openbao_dispose_root_token set to
false, and the failures and the lost connection that the play headers describe.

The fake kubernetes.core can make only its own tasks unreachable. A lost connection in the
middle of a `kubectl exec` is not modeled.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures"
sys.path.insert(0, str(FIXTURES))
import token_lifecycle  # noqa: E402

INIT = "fake-root-init-1"
SHORT = "fake-root-short-1"
RECONCILER = "fake-reconciler-token"
RAFT_READ = "read -format=json sys/storage/raft/autopilot/state"
LOOKUP = "token lookup -format=json"
MINT = "token create -orphan -policy=root -renewable=false -ttl=15m -format=json"
REVOKE = "token revoke -self"
# The calls of the role that change the server. The logins and the unseal are not here.
WRITES = ("secrets enable", "auth enable", "policy write", "write auth/", "write -format=json -force")
RAFT_READS = ("read -format=json sys/storage/raft/",)
# ansible-playbook exit status when a host is unreachable
UNREACHABLE_STATUS = 4


class Run:
    def __init__(self, returncode, output, state, calls):
        self.returncode = returncode
        self.output = output
        self.state = state
        self.calls = calls

    def writes(self):
        return [(token, args) for token, args in self.calls if args.startswith(WRITES)]

    def by(self, token):
        return [args for who, args in self.calls if who == token and not args.startswith("=")]

    def index(self, call):
        return self.calls.index(call)


def read_calls(state):
    path = state / "bao-calls.log"
    if not path.exists():
        return []
    return [tuple(line.split(" ", 1)) for line in path.read_text(encoding="utf-8").splitlines()]


def play(testcase, replicas=1, env=None, extra=None, state=None, keys=None, seed=None):
    """Run 100-openbao.yml once. With state and keys of an earlier run, the run is a rerun.

    seed, when given, is called with the new state directory before the first run, to add
    objects to the fake cluster."""
    fresh = state is None
    if fresh:
        state = Path(tempfile.mkdtemp(prefix="fresh-init-state-"))
        keys = Path(tempfile.mkdtemp(prefix="fresh-init-keys-"))
        testcase.addCleanup(shutil.rmtree, state, True)
        testcase.addCleanup(shutil.rmtree, keys, True)
        shutil.copytree(FIXTURES / "k8s-seed", state / "k8s")
        (state / "policies").mkdir()
        if seed:
            seed(state)
    skip = len(read_calls(state))
    playbook = shutil.which("ansible-playbook")
    testcase.assertIsNotNone(playbook, "ansible-playbook is not on PATH")
    variables = {
        "openbao_replicas": replicas, "openbao_shamir_shares": 1, "openbao_shamir_threshold": 1,
        "openbao_breakglass_distribution_enabled": False, "openbao_key_path": str(keys / "openbao"),
        "openbao_operator_password": "pw", "openbao_policy_reconciler_password": "pw",
        "openbao_approle_reconciler_password": "pw", "ansible_become": False,
        "openbao_tls_secret_wait_retries": 1, "openbao_tls_secret_wait_delay": 0,
        # One poll and no pause, so a Raft check that fails does so at once.
        "openbao_guard_wait_timeout_seconds": 0,
    }
    variables.update(extra or {})
    environment = {
        **os.environ,
        "PATH": "%s:%s" % (FIXTURES / "fake-kubectl", os.environ["PATH"]),
        "ANSIBLE_COLLECTIONS_PATH": "%s:%s" % (FIXTURES / "fake-collections", Path.home() / ".ansible/collections"),
        "ANSIBLE_ROLES_PATH": "%s:%s" % (FIXTURES / "fake-role-tree", ROOT / "roles"),
        "FAKE_STATE": str(state), "FAKE_FRESH_INIT": "1", "FAKE_SEALED": "openbao-0 openbao-1 openbao-2",
        "FAKE_THRESHOLD": "1", "FAKE_CAPS_JSON": '["create","read","update","delete"]',
        **(env or {}),
    }
    result = subprocess.run(
        [playbook, "-i", "tests/fixtures/inventory-control-local.ini",
         "playbooks/vertical-security/100-openbao.yml", "-e", json.dumps(variables)],
        cwd=ROOT, env=environment, capture_output=True, text=True,
    )
    run = Run(result.returncode, result.stdout + result.stderr, state, read_calls(state)[skip:])
    run.keys = keys
    return run


def problems(run, minted, init_revoked, short_revoked, reads=RAFT_READS):
    return token_lifecycle.problems(str(run.state), INIT, minted, init_revoked, short_revoked, WRITES, reads)


class FreshInstallTest(unittest.TestCase):
    """One good install on three replicas, then a rerun of it."""

    @classmethod
    def setUpClass(cls):
        cls.holder = unittest.TestCase()
        cls.first = play(cls.holder, replicas=3)
        # The rerun adds its calls to the same log, so the install is judged before it.
        cls.first_problems = problems(cls.first, 1, "yes", "yes")
        cls.rerun = play(cls.holder, replicas=3, env={"FAKE_POLICY_WRITER": "1"}, state=cls.first.state, keys=cls.first.keys)

    @classmethod
    def tearDownClass(cls):
        cls.holder.doCleanups()

    def test_the_install_ends_well(self):
        self.assertEqual(self.first.returncode, 0, self.first.output)
        self.assertNotIn("WARNING: the role sent the mint", self.first.output)

    def test_the_init_token_makes_only_the_raft_check_the_trade_and_its_proof(self):
        self.assertEqual(self.first.by(INIT), [RAFT_READ, LOOKUP, MINT, REVOKE, LOOKUP])

    def test_the_tokens_follow_the_lifecycle(self):
        self.assertEqual(self.first_problems, [])

    def test_the_init_token_is_refused_before_the_first_write(self):
        first_write = self.first.index(self.first.writes()[0])
        self.assertLess(self.first.index((INIT, "=revoked")), first_write)
        self.assertLess(self.first.index((INIT, "=refused")), first_write)

    def test_every_write_uses_the_short_token(self):
        self.assertTrue(self.first.writes())
        self.assertEqual({token for token, _ in self.first.writes()}, {SHORT})

    def test_the_short_token_is_revoked_and_refused_after_the_last_write(self):
        last_write = max(self.first.index(call) for call in self.first.writes())
        self.assertGreater(self.first.index((SHORT, "=revoked")), last_write)
        self.assertEqual(self.first.calls[-1], (SHORT, "=refused"))

    def test_a_rerun_trades_nothing_and_writes_with_the_reconciler(self):
        self.assertEqual(self.rerun.returncode, 0, self.rerun.output)
        self.assertTrue(self.rerun.writes())
        self.assertFalse([call for call in self.rerun.calls if call[0].startswith("fake-root")])
        self.assertFalse([call for call in self.rerun.calls if call[1].startswith(("token create", "token revoke"))])
        self.assertEqual({token for token, _ in self.rerun.writes()}, {RECONCILER})


class OneReplicaTest(unittest.TestCase):
    def test_a_one_replica_install_trades_without_the_raft_check(self):
        run = play(self, replicas=1)
        self.assertEqual(run.returncode, 0, run.output)
        self.assertEqual(run.by(INIT), [LOOKUP, MINT, REVOKE, LOOKUP])
        self.assertFalse([call for call in run.calls if call[1] == RAFT_READ])
        self.assertTrue(run.writes())
        self.assertEqual({token for token, _ in run.writes()}, {SHORT})
        self.assertIn((INIT, "=refused"), run.calls)
        self.assertEqual(run.calls[-1], (SHORT, "=refused"))
        self.assertEqual(problems(run, 1, "yes", "yes", reads=()), [])


class FailureTest(unittest.TestCase):
    def test_a_failed_unseal_in_the_rescue_still_ends_with_the_token_states(self):
        run = play(self, env={"FAKE_POLICY_WRITE_FAILS": "1", "FAKE_SEAL_ON_FAIL": "1"})
        self.assertNotEqual(run.returncode, 0, run.output)
        self.assertIn("The server could not be unsealed, so the tokens are not proven revoked", run.output)
        self.assertIn("The init token is revoked", run.output)
        self.assertIn("short-lived write token is still valid until it expires", run.output)
        self.assertIn("bao token revoke -accessor", run.output)
        self.assertFalse((run.state / ("revoked-" + SHORT)).exists())

    def test_a_failure_after_the_trade_revokes_the_short_token_in_the_rescue(self):
        run = play(self, env={"FAKE_POLICY_WRITE_FAILS": "1"})
        self.assertNotEqual(run.returncode, 0, run.output)
        self.assertIn("OpenBao bootstrap failed at", run.output)
        self.assertEqual(problems(run, 1, "yes", "yes"), [])
        failed_write = run.index(run.writes()[-1])
        self.assertGreater(run.index((SHORT, "=revoked")), failed_write)
        self.assertEqual(run.by(INIT).count(REVOKE), 1)
        self.assertEqual(run.calls[-1], (SHORT, "=refused"))

    def revoker_events(self, **kwargs):
        # The fake keeps only the kinds of object that other tests read, so the stopped
        # revoker Deployment is seeded.
        def stopped(state):
            (state / "k8s/Deployment-token-revoker.json").write_text(json.dumps({"spec": {"replicas": 0}}))

        run = play(self, seed=stopped, **kwargs)
        return run, (run.state / "events.log").read_text().splitlines()

    def test_the_rescue_starts_the_revoker_after_it_proves_the_tokens_revoked(self):
        # Catches a rescue that leaves the revoker stopped after a failed install: nothing
        # would then clean up an executor token, and the only sign would be a quiet alert.
        run, lines = self.revoker_events(env={"FAKE_POLICY_WRITE_FAILS": "1"})
        self.assertNotEqual(run.returncode, 0, run.output)
        self.assertEqual(problems(run, 1, "yes", "yes"), [])
        up = lines.index("k8s scale Deployment token-revoker replicas=1")
        self.assertGreater(up, max(i for i, line in enumerate(lines) if line.endswith("token revoke -self")))

    def test_the_rescue_leaves_the_revoker_stopped_when_a_token_is_not_proven_revoked(self):
        # Catches a rescue that resumes the revoker although the short token stays valid.
        run, lines = self.revoker_events(env={"FAKE_POLICY_WRITE_FAILS": "1", "FAKE_REVOKE_FAILS": "short"})
        self.assertNotEqual(run.returncode, 0, run.output)
        self.assertNotIn("k8s scale Deployment token-revoker replicas=1", lines)

    def test_the_rescue_leaves_the_revoker_stopped_when_the_answer_to_the_mint_was_lost(self):
        # The play cannot name that token, so it cannot prove it revoked.
        run, lines = self.revoker_events(env={"FAKE_TOKEN_CREATE_LOST": "1"})
        self.assertNotEqual(run.returncode, 0, run.output)
        self.assertNotIn("k8s scale Deployment token-revoker replicas=1", lines)

    def test_the_rescue_leaves_the_revoker_stopped_when_the_install_keeps_its_init_token(self):
        # With openbao_dispose_root_token false the init token stays live on purpose, and
        # the revoker would revoke it.
        run, lines = self.revoker_events(
            env={"FAKE_POLICY_WRITE_FAILS": "1"}, extra={"openbao_dispose_root_token": False})
        self.assertNotEqual(run.returncode, 0, run.output)
        self.assertNotIn("k8s scale Deployment token-revoker replicas=1", lines)

    def test_a_failed_revoke_of_the_init_token_stops_before_any_write(self):
        run = play(self, env={"FAKE_REVOKE_FAILS": "root"})
        self.assertNotEqual(run.returncode, 0, run.output)
        self.assertEqual(run.writes(), [])
        self.assertIn("still valid, and it has no lifetime", run.output)
        self.assertEqual(problems(run, 1, "no", "yes"), [])

    def test_a_lost_answer_to_the_mint_stops_before_any_write_and_fails_loudly(self):
        run = play(self, env={"FAKE_TOKEN_CREATE_LOST": "1"})
        self.assertNotEqual(run.returncode, 0, run.output)
        self.assertEqual(run.writes(), [])
        self.assertIn("did not read the token from the answer", run.output)
        self.assertIn("bao list auth/token/accessors", run.output)
        self.assertEqual(problems(run, 1, "yes", "no"), [])

    def test_a_failed_raft_check_stops_before_the_trade_and_the_rescue_revokes_the_init_token(self):
        run = play(self, replicas=3, env={"FAKE_AUTOPILOT": "unhealthy"})
        self.assertNotEqual(run.returncode, 0, run.output)
        self.assertEqual(run.writes(), [])
        self.assertNotIn(MINT, run.by(INIT))
        self.assertEqual(problems(run, 0, "yes", "no"), [])

    def test_a_lifetime_outside_the_bound_stops_before_init(self):
        run = play(self, extra={"openbao_k8s_write_token_ttl": "30m"})
        self.assertNotEqual(run.returncode, 0, run.output)
        self.assertIn("Set it to", run.output)
        self.assertFalse([call for call in run.calls if call[1].startswith("operator init")])

    def test_a_lost_connection_after_the_trade_leaves_only_the_short_token(self):
        run = play(self, env={"FAKE_K8S_UNREACHABLE": "ServiceAccount/setup-issuer"})
        self.assertEqual(run.returncode, UNREACHABLE_STATUS, run.output)
        self.assertIn("UNREACHABLE", run.output)
        self.assertTrue(run.writes())
        self.assertEqual(problems(run, 1, "yes", "no"), [])


class KeepTheInitTokenTest(unittest.TestCase):
    def test_a_run_with_openbao_dispose_root_token_false_keeps_todays_behavior(self):
        run = play(self, replicas=3, extra={"openbao_dispose_root_token": False})
        self.assertEqual(run.returncode, 0, run.output)
        self.assertTrue(run.writes())
        self.assertEqual({token for token, _ in run.writes()}, {INIT})
        self.assertEqual([args for args in run.by(INIT) if args.startswith("token ")], [])
        self.assertFalse((run.state / ("revoked-" + INIT)).exists())
        self.assertFalse(list((run.state / "tokens").glob("*.json")))
        last_write = max(run.index(call) for call in run.writes())
        self.assertGreater(run.index((INIT, RAFT_READ)), last_write)


if __name__ == "__main__":
    unittest.main()
