"""Setup-only SecretID path: the receipt gate, the single claim, the bounded
issuance, use, and revocation on every exit path."""

import copy
import sys
import traceback
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from approle_lifecycle import setup_only  # noqa: E402
from openbao_fakes import SECRET, FakeKubernetes, FakeOpenBao  # noqa: E402

REQUEST_ID = "0123456789abcdef0123456789abcdef"
NOW = 1_700_000_000
CONFIG = setup_only.SetupOnlyConfig(
    env_id="env-1",
    approle_name="born-inventory-netbox-reader",
    issuer_namespace="openbao",
    issuer_service_account="setup-issuer",
    issuer_openbao_role="setup-issuer",
    audiences=("openbao",),
)


def receipt(**data_overrides):
    data = {
        "schema_version": "1",
        "request_id": REQUEST_ID,
        "action": setup_only.ACTION,
        "env_id": "env-1",
        "cluster_uid": "cluster-uid-1",
        "actor": "admin-subject",
        "role": "engineer",
        "reason": "survey before a drill",
        "issued_at": str(NOW - 60),
        "expires_at": str(NOW + 600),
    }
    data.update(data_overrides)
    return {
        "name": f"request-{REQUEST_ID}",
        "namespace": setup_only.RECEIPT_NAMESPACE,
        "uid": "receipt-uid-1",
        "immutable": True,
        "data": data,
    }


class Harness:
    def __init__(self):
        self.kube = FakeKubernetes()
        self.bao = FakeOpenBao()
        self.kube.receipts[(setup_only.RECEIPT_NAMESPACE, f"request-{REQUEST_ID}")] = receipt()
        self.clients = setup_only.SetupOnlyClients(self.kube, self.bao)
        self.used = []

    def use(self, secret_id):
        self.used.append(secret_id)

    def run(self, use=None):
        return setup_only.issue_use_revoke(REQUEST_ID, CONFIG, self.clients, use or self.use, lambda: NOW)

    def set_receipt(self, value):
        key = (setup_only.RECEIPT_NAMESPACE, f"request-{REQUEST_ID}")
        if value is None:
            self.kube.receipts.pop(key, None)
        else:
            self.kube.receipts[key] = value


class ReceiptGateTests(unittest.TestCase):
    def assert_fails_before_any_write(self, harness, stage="receipt"):
        with self.assertRaises(setup_only.SetupOnlyError) as caught:
            harness.run()
        self.assertEqual(caught.exception.stage, stage)
        self.assertEqual(harness.bao.calls, [])
        self.assertEqual(harness.kube.token_requests, [])
        self.assertEqual(harness.kube.claims, {})
        self.assertEqual(harness.used, [])

    def test_a_valid_receipt_runs_and_returns_only_non_secret_fields(self):
        harness = Harness()
        result = harness.run()
        self.assertEqual((result.actor, result.role, result.reason), ("admin-subject", "engineer", "survey before a drill"))
        self.assertEqual((result.outcome, result.cleanup), ("success", "revoked"))
        self.assertNotIn(SECRET, repr(result))
        self.assertNotIn("synthetic-accessor", repr(result))

    def test_each_defect_in_the_receipt_fails_closed_before_any_write(self):
        cases = {
            "missing": lambda h: h.set_receipt(None),
            "mutable": lambda h: h.set_receipt({**receipt(), "immutable": False}),
            "no uid": lambda h: h.set_receipt({**receipt(), "uid": ""}),
            "wrong name": lambda h: h.set_receipt({**receipt(), "name": "request-other"}),
            "wrong namespace": lambda h: h.set_receipt({**receipt(), "namespace": "default"}),
            "extra key": lambda h: h.set_receipt({**receipt(), "data": {**receipt()["data"], "playbook": "x"}}),
            "missing key": lambda h: h.set_receipt({**receipt(), "data": {k: v for k, v in receipt()["data"].items() if k != "reason"}}),
            "wrong schema": lambda h: h.set_receipt(receipt(schema_version="2")),
            "wrong action": lambda h: h.set_receipt(receipt(action="deploy")),
            "wrong request id": lambda h: h.set_receipt(receipt(request_id="f" * 32)),
            "other environment": lambda h: h.set_receipt(receipt(env_id="env-2")),
            "other cluster": lambda h: h.set_receipt(receipt(cluster_uid="cluster-uid-2")),
            "empty actor": lambda h: h.set_receipt(receipt(actor="  ")),
            "control in reason": lambda h: h.set_receipt(receipt(reason="a\nb")),
            "control in role": lambda h: h.set_receipt(receipt(role="a\x00b")),
            "future issue": lambda h: h.set_receipt(receipt(issued_at=str(NOW + 5))),
            "expired": lambda h: h.set_receipt(receipt(expires_at=str(NOW))),
            "window too long": lambda h: h.set_receipt(receipt(issued_at=str(NOW - 600), expires_at=str(NOW + 301))),
            "window empty": lambda h: h.set_receipt(receipt(issued_at=str(NOW - 10), expires_at=str(NOW - 10))),
            "times not decimal": lambda h: h.set_receipt(receipt(issued_at="2023-11-14")),
            "non-string value": lambda h: h.set_receipt({**receipt(), "data": {**receipt()["data"], "issued_at": 5}}),
        }
        for name, mutate in cases.items():
            with self.subTest(name):
                harness = Harness()
                mutate(harness)
                self.assert_fails_before_any_write(harness)

    def test_a_bad_request_id_is_refused(self):
        harness = Harness()
        for bad in ("short", REQUEST_ID.upper(), REQUEST_ID + "0", None):
            with self.assertRaises(setup_only.SetupOnlyError):
                setup_only.issue_use_revoke(bad, CONFIG, harness.clients, harness.use, lambda: NOW)
        self.assertEqual(harness.bao.calls, [])

    def test_the_cluster_binding_is_checked_against_the_live_cluster(self):
        harness = Harness()
        harness.kube.cluster_uid = ""
        self.assert_fails_before_any_write(harness)

    def test_a_second_run_with_the_same_request_is_refused_at_the_claim(self):
        harness = Harness()
        harness.run()
        issued_before = len(harness.bao.issued)
        with self.assertRaises(setup_only.SetupOnlyError) as caught:
            harness.run()
        self.assertEqual(caught.exception.stage, "claim")
        self.assertEqual(len(harness.bao.issued), issued_before)
        self.assertEqual(len(harness.used), 1)

    def test_the_claim_carries_the_contract_annotations(self):
        harness = Harness()
        harness.run()
        claim = harness.kube.claims[(setup_only.RECEIPT_NAMESPACE, f"claim-{REQUEST_ID}")]
        self.assertEqual(
            set(claim),
            {"schema_version", "request_id", "action", "receipt_name", "receipt_uid", "claimed_at"},
        )
        self.assertEqual(claim["receipt_uid"], "receipt-uid-1")
        self.assertEqual(claim["claimed_at"], str(NOW))


class IssuanceTests(unittest.TestCase):
    def test_the_secret_id_is_minted_with_only_the_allowed_parameters_and_used_once(self):
        harness = Harness()
        harness.run()
        self.assertEqual(harness.bao.issued, [("born-inventory-netbox-reader", {"ttl": "1h", "num_uses": 1})])
        self.assertEqual(harness.used, [SECRET])
        self.assertEqual(harness.bao.destroyed, [("born-inventory-netbox-reader", "synthetic-accessor")])
        self.assertEqual(harness.bao.revoked_tokens, ["synthetic-client-token"])
        self.assertEqual(harness.kube.token_requests, [("openbao", "setup-issuer", ("openbao",))])

    def test_the_accessor_is_destroyed_when_use_fails(self):
        harness = Harness()

        def failing(secret_id):
            raise RuntimeError("the lookup failed")

        with self.assertRaises(setup_only.SetupOnlyError) as caught:
            harness.run(use=failing)
        self.assertEqual(caught.exception.stage, "use")
        self.assertEqual(len(harness.bao.destroyed), 1)
        self.assertEqual(len(harness.bao.revoked_tokens), 1)

    def test_the_accessor_is_destroyed_on_an_interrupt(self):
        harness = Harness()

        def interrupted(secret_id):
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            harness.run(use=interrupted)
        self.assertEqual(len(harness.bao.destroyed), 1)

    def test_a_response_that_does_not_confirm_the_bounds_is_destroyed_and_never_used(self):
        for response in (
            {"secret_id": SECRET, "secret_id_accessor": "acc", "secret_id_ttl": 0, "secret_id_num_uses": 1},
            {"secret_id": SECRET, "secret_id_accessor": "acc", "secret_id_ttl": 3600, "secret_id_num_uses": 0},
            {"secret_id": SECRET, "secret_id_accessor": "acc", "secret_id_ttl": 7200, "secret_id_num_uses": 1},
        ):
            with self.subTest(response=response):
                harness = Harness()
                harness.bao.issue_response = response
                with self.assertRaises(setup_only.SetupOnlyError) as caught:
                    harness.run()
                self.assertEqual(caught.exception.stage, "issue")
                self.assertEqual(harness.used, [])
                self.assertEqual(harness.bao.destroyed, [("born-inventory-netbox-reader", "acc")])

    def test_a_response_with_no_accessor_is_a_failure_that_names_the_bounded_residue(self):
        harness = Harness()
        harness.bao.issue_response = {"secret_id": SECRET, "secret_id_ttl": 3600, "secret_id_num_uses": 1}
        with self.assertRaises(setup_only.SetupOnlyError) as caught:
            harness.run()
        self.assertEqual(caught.exception.stage, "issue")
        self.assertIn("one use and one hour", str(caught.exception))
        self.assertEqual(harness.used, [])
        self.assertEqual(harness.bao.destroyed, [])
        self.assertNotIn(SECRET, str(caught.exception))

    def test_a_failed_destroy_raises_and_names_the_bounded_residue(self):
        harness = Harness()
        harness.bao.fail_destroy = True
        with self.assertRaises(setup_only.SetupOnlyError) as caught:
            harness.run()
        self.assertEqual(caught.exception.stage, "cleanup")
        self.assertIn("one use and one hour", str(caught.exception))

    def test_a_value_in_the_create_response_never_reaches_a_message_or_traceback(self):
        leaking = {
            "ttl as a string": {"secret_id_ttl": SECRET, "secret_id_num_uses": 1},
            "ttl as a dict": {"secret_id_ttl": {"x": SECRET}, "secret_id_num_uses": 1},
            "uses as a string": {"secret_id_ttl": 3600, "secret_id_num_uses": SECRET},
            "uses as a list": {"secret_id_ttl": 3600, "secret_id_num_uses": [SECRET]},
            "accessor as the secret": {"secret_id_ttl": 3600, "secret_id_num_uses": 1, "secret_id_accessor": SECRET},
            "accessor as the secret with bad bounds": {"secret_id_ttl": SECRET, "secret_id_num_uses": SECRET, "secret_id_accessor": SECRET},
        }
        for name, fields in leaking.items():
            for destroy_fails in (False, True):
                with self.subTest(case=name, destroy_fails=destroy_fails):
                    harness = Harness()
                    harness.bao.issue_response = {"secret_id": SECRET, **fields}
                    harness.bao.fail_destroy = destroy_fails
                    harness.bao.destroy_error = RuntimeError(f"destroy refused {SECRET}") if destroy_fails else None
                    try:
                        harness.run()
                    except setup_only.SetupOnlyError as error:
                        self.assertNotIn(SECRET, self.formatted(error))
                        self.assertNotIn(SECRET, str(error))
                    else:
                        if name != "accessor as the secret":
                            self.fail("the run must fail")

    def test_unconfirmed_bounds_are_reported_without_values(self):
        harness = Harness()
        harness.bao.issue_response = {
            "secret_id": SECRET,
            "secret_id_accessor": "acc",
            "secret_id_ttl": 7200,
            "secret_id_num_uses": 0,
        }
        harness.bao.fail_destroy = True
        with self.assertRaises(setup_only.SetupOnlyError) as caught:
            harness.run()
        message = str(caught.exception)
        self.assertIn("the bounds are unconfirmed", message)
        for value in ("7200", "ttl=", "num_uses"):
            self.assertNotIn(value, message)

    def test_exact_documented_values_confirm_the_bounds_and_nothing_else_does(self):
        for ttl, uses, confirmed in (
            (3600, 1, True),
            ("3600", "1", True),
            (True, 1, False),
            (3600, True, False),
            ("1h", 1, False),
            (3600.0, 1, False),
            (3601, 1, False),
            (3600, 2, False),
            (None, None, False),
        ):
            with self.subTest(ttl=ttl, uses=uses):
                issued = {"secret_id": "x", "secret_id_accessor": "a", "secret_id_ttl": ttl, "secret_id_num_uses": uses}
                self.assertEqual(setup_only._bounds_confirmed(issued), confirmed)

    def test_a_lost_issue_response_is_a_failure_that_says_the_bounds_are_unconfirmed(self):
        harness = Harness()
        harness.bao.issue_error = TimeoutError("the response never arrived")
        with self.assertRaises(setup_only.SetupOnlyError) as caught:
            harness.run()
        self.assertEqual(caught.exception.stage, "issue")
        self.assertIn("unconfirmed", str(caught.exception))
        self.assertNotIn("one use and one hour", str(caught.exception))
        self.assertEqual(harness.used, [])
        self.assertEqual(harness.bao.destroyed, [])

    def test_a_missing_response_with_a_failed_issuer_revocation_does_not_claim_a_bound(self):
        harness = Harness()
        harness.bao.issue_error = TimeoutError("the response never arrived")
        harness.bao.fail_revoke_self = True
        with self.assertRaises(setup_only.SetupOnlyError) as caught:
            harness.run()
        self.assertNotIn("one use and one hour", str(caught.exception))

    def test_a_confirmed_response_with_a_failed_destroy_states_the_bound(self):
        harness = Harness()
        harness.bao.fail_destroy = True
        with self.assertRaises(setup_only.SetupOnlyError) as caught:
            harness.run()
        self.assertIn("confirmed the bounds", str(caught.exception))
        self.assertIn("one use and one hour", str(caught.exception))

    def test_an_interrupt_carrying_the_secret_id_stays_an_interrupt_with_and_without_a_failed_destroy(self):
        for fails in (False, True):
            with self.subTest(destroy_fails=fails):
                harness = Harness()
                harness.bao.fail_destroy = fails

                def interrupted(secret_id):
                    raise KeyboardInterrupt(secret_id)

                with self.assertRaises(KeyboardInterrupt) as caught:
                    harness.run(use=interrupted)
                error = caught.exception
                self.assertEqual(error.args, ())
                self.assertIsNone(error.__cause__)
                self.assertIsNone(error.__context__)
                self.assertNotIn(SECRET, self.formatted(error))
                if fails:
                    self.assertEqual(getattr(error, "__notes__", None), [setup_only.CLEANUP_NOTE])
                    self.assertIn("cleanup failed, so the residue is unconfirmed", self.formatted(error))
                    self.assertEqual(harness.bao.destroyed, [])
                else:
                    self.assertFalse(getattr(error, "__notes__", None))
                    self.assertEqual(len(harness.bao.destroyed), 1)

    def test_system_exit_and_generator_exit_are_replaced_the_same_way(self):
        for exit_class in (SystemExit, GeneratorExit):
            with self.subTest(exit_class=exit_class.__name__):
                harness = Harness()

                def leaving(secret_id):
                    raise exit_class(secret_id)

                with self.assertRaises(exit_class) as caught:
                    harness.run(use=leaving)
                self.assertEqual(caught.exception.args, ())
                self.assertNotIn(SECRET, self.formatted(caught.exception))
                self.assertEqual(len(harness.bao.destroyed), 1)

    def test_an_interrupt_during_destroy_is_replaced_and_the_issuer_token_is_still_revoked(self):
        harness = Harness()
        harness.bao.destroy_error = KeyboardInterrupt(SECRET)
        with self.assertRaises(KeyboardInterrupt) as caught:
            harness.run()
        self.assertEqual(caught.exception.args, ())
        self.assertNotIn(SECRET, self.formatted(caught.exception))
        self.assertEqual(len(harness.bao.revoked_tokens), 1)

    def test_an_interrupt_during_issue_is_replaced(self):
        harness = Harness()
        harness.bao.issue_error = KeyboardInterrupt(SECRET)
        with self.assertRaises(KeyboardInterrupt) as caught:
            harness.run()
        self.assertEqual(caught.exception.args, ())
        self.assertNotIn(SECRET, self.formatted(caught.exception))

    def test_no_failure_path_returns_a_result(self):
        for name, arrange in (
            ("no accessor", lambda h: setattr(h.bao, "issue_response", {"secret_id": SECRET, "secret_id_ttl": 3600, "secret_id_num_uses": 1})),
            ("destroy fails", lambda h: setattr(h.bao, "fail_destroy", True)),
            ("issue fails", lambda h: setattr(h.bao, "issue_error", RuntimeError("x"))),
            ("revoke_self fails", lambda h: setattr(h.bao, "fail_revoke_self", True)),
        ):
            with self.subTest(name):
                harness = Harness()
                arrange(harness)
                with self.assertRaises(setup_only.SetupOnlyError):
                    harness.run()

    def test_a_cleanup_failure_fails_closed_even_when_use_succeeded(self):
        harness = Harness()
        harness.bao.fail_destroy = True
        with self.assertRaises(setup_only.SetupOnlyError) as caught:
            harness.run()
        self.assertEqual(caught.exception.stage, "cleanup")
        self.assertEqual(harness.used, [SECRET])

    def formatted(self, error):
        return "".join(traceback.format_exception(error))

    def assert_clean(self, error, *secrets):
        text = self.formatted(error)
        for value in secrets:
            self.assertNotIn(value, text)
        self.assertIsNone(error.__cause__)

    def test_a_secret_id_in_an_issue_exception_never_reaches_the_traceback(self):
        harness = Harness()
        harness.bao.issue_error = RuntimeError(f"the body was secret_id={SECRET}")
        with self.assertRaises(setup_only.SetupOnlyError) as caught:
            harness.run()
        self.assert_clean(caught.exception, SECRET)
        self.assertIsNone(caught.exception.__context__)

    def test_a_secret_id_in_a_use_exception_never_reaches_the_traceback(self):
        harness = Harness()

        def leaking(secret_id):
            raise RuntimeError(f"could not use {secret_id}")

        with self.assertRaises(setup_only.SetupOnlyError) as caught:
            harness.run(use=leaking)
        self.assert_clean(caught.exception, SECRET)
        self.assertIsNone(caught.exception.__context__)

    def test_a_secret_id_in_a_destroy_exception_never_reaches_the_traceback(self):
        harness = Harness()
        harness.bao.destroy_error = RuntimeError(f"could not destroy for {SECRET}")
        with self.assertRaises(setup_only.SetupOnlyError) as caught:
            harness.run()
        self.assert_clean(caught.exception, SECRET)

    def test_a_secret_id_in_both_a_use_and_a_destroy_exception_never_reaches_the_traceback(self):
        harness = Harness()
        harness.bao.destroy_error = RuntimeError(f"could not destroy for {SECRET}")

        def leaking(secret_id):
            raise RuntimeError(f"could not use {secret_id}")

        with self.assertRaises(setup_only.SetupOnlyError) as caught:
            harness.run(use=leaking)
        text = self.formatted(caught.exception)
        for value in (SECRET, "synthetic-accessor"):
            self.assertNotIn(value, text)
        self.assertEqual(caught.exception.stage, "cleanup")
        # The cause is the module's own fixed-message use error, never the original.
        self.assertEqual(caught.exception.__cause__.stage, "use")
        self.assertIsNone(caught.exception.__cause__.__cause__)
        self.assertIsNone(caught.exception.__cause__.__context__)

    def test_a_token_in_a_login_exception_never_reaches_the_traceback(self):
        harness = Harness()
        harness.bao.fail_login = True
        with self.assertRaises(setup_only.SetupOnlyError) as caught:
            harness.run()
        self.assert_clean(caught.exception, "synthetic-service-account-jwt")
        self.assertIsNone(caught.exception.__context__)

    def test_a_cleanup_failure_is_not_hidden_by_a_use_failure(self):
        harness = Harness()
        harness.bao.fail_destroy = True

        def failing(secret_id):
            raise RuntimeError("the lookup failed")

        with self.assertRaises(setup_only.SetupOnlyError) as caught:
            harness.run(use=failing)
        self.assertEqual(caught.exception.stage, "cleanup")
        self.assertEqual(caught.exception.__cause__.stage, "use")

    def test_a_failed_issuer_token_revocation_fails_closed(self):
        harness = Harness()
        harness.bao.fail_revoke_self = True
        with self.assertRaises(setup_only.SetupOnlyError) as caught:
            harness.run()
        self.assertEqual(caught.exception.stage, "cleanup")
        self.assertEqual(len(harness.bao.destroyed), 1)

    def test_an_issuer_login_failure_mints_nothing(self):
        harness = Harness()
        harness.bao.fail_login = True
        with self.assertRaises(setup_only.SetupOnlyError) as caught:
            harness.run()
        self.assertEqual(caught.exception.stage, "login")
        self.assertEqual(harness.bao.issued, [])
        self.assertNotIn("synthetic-service-account-jwt", str(caught.exception))

    def test_no_message_or_result_carries_the_secret_id(self):
        harness = Harness()
        harness.bao.fail_destroy = True
        try:
            harness.run()
        except setup_only.SetupOnlyError as error:
            self.assertNotIn(SECRET, str(error))
            self.assertNotIn("synthetic-accessor", str(error))

    def test_every_rerun_needs_a_fresh_receipt_and_mints_a_fresh_secret_id(self):
        harness = Harness()
        harness.run()
        second_id = "fedcba9876543210fedcba9876543210"
        second = copy.deepcopy(receipt(request_id=second_id))
        second["name"] = f"request-{second_id}"
        second["uid"] = "receipt-uid-2"
        harness.kube.receipts[(setup_only.RECEIPT_NAMESPACE, second["name"])] = second
        setup_only.issue_use_revoke(second_id, CONFIG, harness.clients, harness.use, lambda: NOW)
        self.assertEqual(len(harness.bao.issued), 2)
        self.assertEqual(len(harness.bao.destroyed), 2)


if __name__ == "__main__":
    unittest.main()
