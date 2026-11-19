"""Tests for fixtures/token_lifecycle.py, the checker that the installed-environment
fixture cases use to judge the token record of the fake bao.

Each test writes a call log and token records the way the fake bao does, including the
"TOKEN =revoked" and "TOKEN =refused" outcome lines, then checks that the checker
reports a valid run as clean and names each kind of invalid run.
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "fixtures"))
import token_lifecycle  # noqa: E402

ROOT = "fake-root-1"
SHORT = "fake-root-short-1"
LOOKUP = "token lookup -format=json"
MINT = "token create -orphan -policy=root -renewable=false -ttl=15m -format=json"
REVOKE = "token revoke -self"
WRITE = "write auth/kubernetes/config kubernetes_host=https://example"
HEAD = [(ROOT, LOOKUP), (ROOT, MINT)]
PROOF = [(ROOT, REVOKE), (ROOT, "=revoked"), (ROOT, LOOKUP), (ROOT, "=refused")]
TAIL = [(SHORT, REVOKE), (SHORT, "=revoked"), (SHORT, LOOKUP), (SHORT, "=refused")]
GOOD_CALLS = HEAD + PROOF + [(SHORT, WRITE)] + TAIL
GOOD_RECORD = {"parent": ROOT, "orphan": True, "policies": ["root"], "ttl": 900, "renewable": False}


def store_path(state):
    os.makedirs(os.path.join(state, "tokens"), exist_ok=True)
    return os.path.join(state, "tokens", "%s.json" % SHORT)


class TokenLifecycleCheckerTest(unittest.TestCase):
    def run_checker(self, calls, record=GOOD_RECORD, revoked=(ROOT, SHORT), minted=1):
        with tempfile.TemporaryDirectory() as state:
            with open(os.path.join(state, "bao-calls.log"), "w", encoding="utf-8") as handle:
                handle.write("".join("%s %s\n" % call for call in calls))
            if record is not None:
                with open(store_path(state), "w", encoding="utf-8") as handle:
                    json.dump(record, handle)
            for name in revoked:
                open(os.path.join(state, "revoked-" + name), "w").close()
            return token_lifecycle.problems(state, ROOT, minted, "yes", "yes")

    def assert_names(self, found, text):
        self.assertTrue(any(text in item for item in found), "expected a problem with %r in %r" % (text, found))

    def test_a_valid_run_has_no_problems(self):
        self.assertEqual(self.run_checker(GOOD_CALLS), [])

    def test_a_run_stopped_before_the_policy_check_is_clean(self):
        self.assertEqual(self.run_checker(PROOF, record=None, revoked=(ROOT,), minted=0), [])

    def test_a_renewable_token_is_reported(self):
        self.assert_names(self.run_checker(GOOD_CALLS, dict(GOOD_RECORD, renewable=True)), "is renewable")

    def test_a_record_without_the_renewable_field_is_reported(self):
        record = {key: value for key, value in GOOD_RECORD.items() if key != "renewable"}
        self.assert_names(self.run_checker(GOOD_CALLS, record), "is renewable")

    def test_a_write_before_the_revoke_is_reported(self):
        calls = HEAD + [(SHORT, WRITE)] + PROOF + TAIL
        self.assert_names(self.run_checker(calls), "before the generate-root token was revoked and proved refused")

    def test_a_write_before_the_refused_lookup_is_reported(self):
        calls = HEAD + PROOF[:2] + [(SHORT, WRITE)] + PROOF[2:] + TAIL
        self.assert_names(self.run_checker(calls), "before the generate-root token was revoked and proved refused")

    def test_a_failed_revoke_and_an_accepted_lookup_before_a_write_are_reported(self):
        # The first revoke fails and the server still accepts the token, then a write
        # runs, and only a later cleanup revokes it: the final state looks revoked.
        calls = HEAD + [(ROOT, REVOKE), (ROOT, LOOKUP), (SHORT, WRITE)] + PROOF + TAIL
        self.assert_names(self.run_checker(calls), "before the generate-root token was revoked and proved refused")

    def test_a_mint_after_the_revoke_is_reported(self):
        calls = [(ROOT, LOOKUP)] + PROOF + [(ROOT, MINT), (SHORT, WRITE)] + TAIL
        self.assert_names(self.run_checker(calls), "minted a token after its revoke")

    def test_a_mint_before_the_policy_lookup_is_reported(self):
        calls = [(ROOT, MINT)] + PROOF + [(SHORT, WRITE)] + TAIL
        self.assert_names(self.run_checker(calls), "minted a token before the policy lookup")

    def test_a_second_mint_is_reported(self):
        calls = HEAD + [(ROOT, MINT)] + PROOF + [(SHORT, WRITE)] + TAIL
        self.assert_names(self.run_checker(calls), "minted more than one token")

    def test_a_lookup_after_the_revoke_without_a_new_revoke_is_reported(self):
        calls = HEAD + PROOF + [(ROOT, LOOKUP), (SHORT, WRITE)] + TAIL
        self.assert_names(self.run_checker(calls), "looked up after its revoke without a revoke just before")

    def test_a_write_with_the_generate_root_token_is_reported(self):
        calls = HEAD + PROOF + [(ROOT, WRITE)] + TAIL
        found = self.run_checker(calls)
        self.assert_names(found, "the generate-root token made the call")
        self.assert_names(found, "a write ran with a token that was not minted")

    def test_a_non_orphan_token_is_reported(self):
        self.assert_names(self.run_checker(GOOD_CALLS, dict(GOOD_RECORD, orphan=False)), "is not an orphan")

    def test_a_long_lifetime_is_reported(self):
        self.assert_names(self.run_checker(GOOD_CALLS, dict(GOOD_RECORD, ttl=3600)), "outside 600 to 900")

    def test_the_write_prefixes_decide_which_calls_count_as_writes(self):
        userpass = "write auth/userpass/users/ops-admin/policies -"
        calls = HEAD + [(SHORT, userpass)] + PROOF + TAIL
        with tempfile.TemporaryDirectory() as state:
            with open(os.path.join(state, "bao-calls.log"), "w", encoding="utf-8") as handle:
                handle.write("".join("%s %s\n" % call for call in calls))
            with open(store_path(state), "w", encoding="utf-8") as handle:
                json.dump(GOOD_RECORD, handle)
            for name in (ROOT, SHORT):
                open(os.path.join(state, "revoked-" + name), "w").close()
            default = token_lifecycle.problems(state, ROOT, 1, "yes", "yes")
            named = token_lifecycle.problems(state, ROOT, 1, "yes", "yes", ("write auth/userpass/users/",))
        self.assertEqual(default, [])
        self.assert_names(named, "before the generate-root token was revoked and proved refused")

    def test_a_read_that_the_caller_names_is_allowed_before_the_revoke_only(self):
        read = "read -format=json sys/storage/raft/autopilot/state"
        before = [(ROOT, read)] + GOOD_CALLS
        after = GOOD_CALLS[:-4] + [(ROOT, read)] + TAIL
        with tempfile.TemporaryDirectory() as state:
            with open(store_path(state), "w", encoding="utf-8") as handle:
                json.dump(GOOD_RECORD, handle)
            for name in (ROOT, SHORT):
                open(os.path.join(state, "revoked-" + name), "w").close()

            def check(calls, reads):
                with open(os.path.join(state, "bao-calls.log"), "w", encoding="utf-8") as handle:
                    handle.write("".join("%s %s\n" % call for call in calls))
                return token_lifecycle.problems(state, ROOT, 1, "yes", "yes", root_reads=reads)

            self.assertEqual(check(before, ("read -format=json sys/storage/raft/",)), [])
            self.assert_names(check(before, ()), "the generate-root token made the call: " + read)
            self.assert_names(check(after, ("read -format=json sys/storage/raft/",)), "made a read after its revoke")


if __name__ == "__main__":
    unittest.main()
