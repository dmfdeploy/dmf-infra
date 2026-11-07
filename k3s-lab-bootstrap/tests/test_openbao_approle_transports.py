"""Transports for the setup-only path, tested against a fake of the process
boundary. The fake records every argv and stdin, so the tests can prove that no
credential reaches argv."""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from approle_lifecycle import born_inventory_cli, setup_only  # noqa: E402
from approle_lifecycle.transports import (  # noqa: E402
    KubernetesTransport,
    OpenBaoTransport,
    TransportError,
)
from openbao_fakes import SECRET  # noqa: E402

JWT = "synthetic-jwt-value"
TOKEN = "synthetic-client-token-value"
ACCESSOR = "synthetic-accessor-value"
CREDENTIALS = (JWT, TOKEN, ACCESSOR, SECRET)


class FakeRunner:
    """Replaces `run`. `script` maps a predicate on argv to (rc, out, err)."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, argv, stdin_text=""):
        self.calls.append((list(argv), stdin_text))
        return self.responses.pop(0) if self.responses else (0, "", "")

    def assert_no_credential_in_argv(self, test):
        for argv, _stdin in self.calls:
            for word in argv:
                for secret in CREDENTIALS:
                    test.assertNotIn(secret, word)


def configmap(**overrides):
    obj = {
        "metadata": {"name": "request-x", "namespace": "dmf-setup-requests", "uid": "uid-1"},
        "immutable": True,
        "data": {"schema_version": "1"},
    }
    obj.update(overrides)
    return json.dumps(obj)


class KubernetesTransportTest(unittest.TestCase):
    def test_get_receipt_flattens_the_configmap(self):
        runner = FakeRunner((0, configmap(), ""))
        receipt = KubernetesTransport(runner=runner).get_receipt("dmf-setup-requests", "request-x")
        self.assertEqual(
            receipt,
            {
                "name": "request-x",
                "namespace": "dmf-setup-requests",
                "uid": "uid-1",
                "immutable": True,
                "data": {"schema_version": "1"},
            },
        )

    def test_missing_receipt_is_none(self):
        runner = FakeRunner((0, "", ""))
        self.assertIsNone(KubernetesTransport(runner=runner).get_receipt("n", "request-x"))
        self.assertIn("--ignore-not-found", runner.calls[0][0])

    def test_receipt_read_error_raises_fixed_text(self):
        runner = FakeRunner((1, "", "forbidden: body " + SECRET))
        with self.assertRaises(TransportError) as caught:
            KubernetesTransport(runner=runner).get_receipt("n", "request-x")
        self.assertNotIn(SECRET, str(caught.exception))

    def test_malformed_receipt_raises(self):
        for out in ("not json", json.dumps({"metadata": {}}), json.dumps([1])):
            with self.subTest(out=out), self.assertRaises(TransportError):
                KubernetesTransport(runner=FakeRunner((0, out, ""))).get_receipt("n", "request-x")

    def test_impersonation_applies_to_receipt_and_claim_only(self):
        runner = FakeRunner((0, configmap(), ""), (0, "", ""), (0, JWT + "\n", ""), (0, "uid", ""))
        transport = KubernetesTransport(as_user="system:serviceaccount:ns:exec", runner=runner)
        transport.get_receipt("n", "request-x")
        transport.create_claim("n", "claim-x", {"a": "b"})
        transport.request_token("openbao", "setup-issuer", ["openbao"])
        transport.kube_system_uid()
        self.assertEqual(runner.calls[0][0][3:5], ["--as", "system:serviceaccount:ns:exec"])
        self.assertIn("--as", runner.calls[1][0])
        self.assertNotIn("--as", runner.calls[2][0])
        self.assertNotIn("--as", runner.calls[3][0])

    def test_create_claim_sends_a_lease_on_stdin(self):
        runner = FakeRunner((0, "", ""))
        KubernetesTransport(runner=runner).create_claim("dmf-setup-requests", "claim-x", {"request_id": "x"})
        argv, stdin = runner.calls[0]
        self.assertEqual(argv[-3:], ["create", "-f", "-"])
        lease = json.loads(stdin)
        self.assertEqual(lease["kind"], "Lease")
        self.assertEqual(lease["metadata"]["name"], "claim-x")
        self.assertEqual(lease["metadata"]["annotations"], {"request_id": "x"})

    def test_existing_claim_is_a_conflict(self):
        runner = FakeRunner((1, "", 'Error from server (AlreadyExists): leases "claim-x" already exists'))
        with self.assertRaises(setup_only.ClaimConflict):
            KubernetesTransport(runner=runner).create_claim("n", "claim-x", {})

    def test_other_claim_failure_raises_transport_error(self):
        runner = FakeRunner((1, "", "Error from server (Forbidden)"))
        with self.assertRaises(TransportError):
            KubernetesTransport(runner=runner).create_claim("n", "claim-x", {})

    def test_kube_system_uid(self):
        runner = FakeRunner((0, "uid-9\n", ""))
        self.assertEqual(KubernetesTransport(runner=runner).kube_system_uid(), "uid-9")
        for rc, out in ((1, "uid"), (0, "  \n")):
            with self.subTest(rc=rc), self.assertRaises(TransportError):
                KubernetesTransport(runner=FakeRunner((rc, out, ""))).kube_system_uid()

    def test_request_token_returns_the_jwt_and_passes_audiences(self):
        runner = FakeRunner((0, JWT + "\n", ""))
        jwt = KubernetesTransport(runner=runner).request_token("openbao", "setup-issuer", ["a", "b"])
        self.assertEqual(jwt, JWT)
        argv = runner.calls[0][0]
        self.assertEqual(argv.count("--audience"), 2)
        runner.assert_no_credential_in_argv(self)

    def test_configmap_value(self):
        body = json.dumps({"data": {"role_id": "role-id-1"}})
        runner = FakeRunner((0, body, ""))
        value = KubernetesTransport(as_user="u", runner=runner).get_configmap_value("n", "cm", "role_id")
        self.assertEqual(value, "role-id-1")
        self.assertIn("--as", runner.calls[0][0])
        for response in (
            (1, "", ""),
            (0, "not json", ""),
            (0, json.dumps({"data": {}}), ""),
            (0, json.dumps({"data": {"role_id": ""}}), ""),
            (0, json.dumps({"data": {"role_id": "a b; c"}}), ""),
            (0, json.dumps({"data": {"role_id": 5}}), ""),
        ):
            with self.subTest(response=response), self.assertRaises(TransportError):
                KubernetesTransport(runner=FakeRunner(response)).get_configmap_value("n", "cm", "role_id")

    def test_empty_token_request_raises(self):
        with self.assertRaises(TransportError):
            KubernetesTransport(runner=FakeRunner((0, "\n", ""))).request_token("n", "s", ["a"])


def bao(*responses):
    runner = FakeRunner(*responses)
    return OpenBaoTransport("openbao", "openbao-0", runner=runner), runner


def login_response(token=TOKEN):
    return json.dumps({"auth": {"client_token": token}})


def issue_response(**data):
    body = {
        "secret_id": SECRET,
        "secret_id_accessor": ACCESSOR,
        "secret_id_ttl": 3600,
        "secret_id_num_uses": 1,
    }
    body.update(data)
    return json.dumps({"data": body})


class OpenBaoTransportTest(unittest.TestCase):
    def test_kubernetes_login_sends_the_jwt_on_stdin_only(self):
        transport, runner = bao((0, login_response(), ""))
        self.assertEqual(transport.login_kubernetes("setup-issuer", JWT), TOKEN)
        argv, stdin = runner.calls[0]
        self.assertEqual(stdin, JWT)
        self.assertIn("jwt=-", argv[-1])
        self.assertIn("role=setup-issuer", argv[-1])
        runner.assert_no_credential_in_argv(self)

    def test_login_rejects_a_bad_jwt_or_token(self):
        transport, _ = bao()
        for jwt in ("", "  ", None, 5):
            with self.subTest(jwt=jwt), self.assertRaises(TransportError):
                transport.login_kubernetes("setup-issuer", jwt)
        for token in ("", None, 5, ["t"]):
            transport, _ = bao((0, login_response(token), ""))
            with self.subTest(token=token), self.assertRaises(TransportError):
                transport.login_kubernetes("setup-issuer", JWT)

    def test_login_failure_and_bad_json_raise_fixed_text(self):
        for response in ((1, "", "denied " + JWT), (0, "not json", ""), (0, "{}", "")):
            transport, _ = bao(response)
            with self.subTest(response=response), self.assertRaises(TransportError) as caught:
                transport.login_kubernetes("setup-issuer", JWT)
            self.assertNotIn(JWT, str(caught.exception))

    def test_issue_puts_the_token_on_stdin_and_the_bounds_in_argv(self):
        transport, runner = bao((0, issue_response(), ""))
        issued = transport.issue_secret_id(TOKEN, "born-inventory-netbox-reader", {"ttl": "1h", "num_uses": 1})
        self.assertEqual(issued["secret_id"], SECRET)
        self.assertEqual(issued["secret_id_accessor"], ACCESSOR)
        argv, stdin = runner.calls[0]
        self.assertEqual(stdin, TOKEN + "\n")
        self.assertIn("num_uses=1 ttl=1h", argv[-1])
        self.assertIn("auth/approle/role/born-inventory-netbox-reader/secret-id", argv[-1])
        runner.assert_no_credential_in_argv(self)

    def test_issue_rejects_a_bad_accessor(self):
        for bad in ("", "   ", None, 5, ["a"], {"a": 1}, True):
            transport, _ = bao((0, issue_response(**{"secret_id_accessor": bad}), ""))
            with self.subTest(bad=bad), self.assertRaises(TransportError):
                transport.issue_secret_id(TOKEN, "r", {"ttl": "1h", "num_uses": 1})

    def test_a_bad_secret_id_keeps_the_valid_accessor_and_blanks_the_secret(self):
        for bad in ("", "   ", None, 5, ["a"], {"a": 1}, True):
            transport, _ = bao((0, issue_response(**{"secret_id": bad}), ""))
            with self.subTest(bad=bad):
                issued = transport.issue_secret_id(TOKEN, "r", {"ttl": "1h", "num_uses": 1})
                self.assertEqual(issued["secret_id"], "")
                self.assertEqual(issued["secret_id_accessor"], ACCESSOR)

    def test_a_response_with_no_secret_id_key_keeps_the_accessor(self):
        body = json.dumps({"data": {"secret_id_accessor": ACCESSOR}})
        transport, _ = bao((0, body, ""))
        issued = transport.issue_secret_id(TOKEN, "r", {"ttl": "1h", "num_uses": 1})
        self.assertEqual((issued["secret_id"], issued["secret_id_accessor"]), ("", ACCESSOR))

    def test_issue_rejects_a_missing_data_object(self):
        for out in ("not json", "{}", json.dumps({"data": None}), json.dumps({"data": [1]})):
            transport, _ = bao((0, out, ""))
            with self.subTest(out=out), self.assertRaises(TransportError):
                transport.issue_secret_id(TOKEN, "r", {"ttl": "1h", "num_uses": 1})

    def test_issue_passes_the_bounds_through_for_the_library_to_check(self):
        transport, _ = bao((0, issue_response(**{"secret_id_ttl": 86400, "secret_id_num_uses": 0}), ""))
        issued = transport.issue_secret_id(TOKEN, "r", {"ttl": "1h", "num_uses": 1})
        self.assertEqual((issued["secret_id_ttl"], issued["secret_id_num_uses"]), (86400, 0))

    def test_destroy_sends_token_then_accessor_on_stdin(self):
        transport, runner = bao((0, "", ""))
        transport.destroy_secret_id_accessor(TOKEN, "r", ACCESSOR)
        argv, stdin = runner.calls[0]
        self.assertEqual(stdin, f"{TOKEN}\n{ACCESSOR}")
        self.assertIn("secret_id_accessor=-", argv[-1])
        runner.assert_no_credential_in_argv(self)

    def test_destroy_rejects_an_empty_accessor(self):
        transport, runner = bao()
        with self.assertRaises(TransportError):
            transport.destroy_secret_id_accessor(TOKEN, "r", "")
        self.assertEqual(runner.calls, [])

    def test_revoke_self_and_failures(self):
        transport, runner = bao((0, "", ""), (1, "", "x"))
        transport.revoke_self(TOKEN)
        self.assertEqual(runner.calls[0][1], TOKEN + "\n")
        self.assertIn("token revoke -self", runner.calls[0][0][-1])
        with self.assertRaises(TransportError):
            transport.revoke_self(TOKEN)

    def test_names_that_are_not_plain_words_are_refused(self):
        transport, runner = bao()
        for bad in ("a b", "a;b", "$(x)", "a'b", ""):
            with self.subTest(bad=bad), self.assertRaises(TransportError):
                transport.login_kubernetes(bad, JWT)
            with self.subTest(bad=bad), self.assertRaises(TransportError):
                transport.issue_secret_id(TOKEN, bad, {"ttl": "1h"})
        self.assertEqual(runner.calls, [])

    def test_approle_login_sends_the_secret_id_on_stdin(self):
        transport, runner = bao((0, login_response(), ""))
        self.assertEqual(transport.login_approle("role-id-1", SECRET), TOKEN)
        self.assertEqual(runner.calls[0][1], SECRET)
        runner.assert_no_credential_in_argv(self)

    def test_read_field(self):
        body = json.dumps({"data": {"data": {"netbox_admin_token": " tok \n"}}})
        transport, runner = bao((0, body, ""))
        self.assertEqual(transport.read_kv2_field(TOKEN, "secret/data/apps/netbox/runtime", "netbox_admin_token"), "tok")
        self.assertEqual(runner.calls[0][1], TOKEN + "\n")
        for out in ("{}", json.dumps({"data": {"data": {"netbox_admin_token": ""}}})):
            transport, _ = bao((0, out, ""))
            with self.subTest(out=out), self.assertRaises(TransportError):
                transport.read_kv2_field(TOKEN, "secret/data/apps/netbox/runtime", "netbox_admin_token")


class CommandLineTest(unittest.TestCase):
    """The command line wires the transports to the library. The process
    boundary is faked with a script that plays kubectl and bao."""

    REQUEST_ID = "0123456789abcdef0123456789abcdef"
    NOW = 1_700_000_000

    def args(self, role_id="role-id-1"):
        return born_inventory_cli.build_parser().parse_args(
            [
                "--env-id", "env-1", "--approle-name", "born-inventory-netbox-reader",
                "--role-id", role_id, "--role-id-configmap", "born-inventory-approle",
                "--issuer-namespace", "openbao",
                "--issuer-service-account", "setup-issuer", "--issuer-openbao-role", "setup-issuer",
                "--audience", "openbao", "--openbao-namespace", "openbao", "--openbao-pod", "openbao-0",
            ]
        )

    def runner(self, destroy_rc=0, issue_body=None):
        receipt = configmap(
            metadata={"name": f"request-{self.REQUEST_ID}", "namespace": "dmf-setup-requests", "uid": "u"},
            data={
                "schema_version": "1", "request_id": self.REQUEST_ID,
                "action": setup_only.ACTION, "env_id": "env-1", "cluster_uid": "cluster-uid-1",
                "actor": "admin-subject", "role": "engineer", "reason": "survey",
                "issued_at": str(self.NOW - 60), "expires_at": str(self.NOW + 600),
            },
        )

        def respond(argv, stdin_text=""):
            text = " ".join(argv)
            if "born-inventory-approle" in text:
                return 0, json.dumps({"data": {"role_id": "published-role-id"}}), ""
            if "configmap" in text:
                return 0, receipt, ""
            if "kube-system" in text:
                return 0, "cluster-uid-1", ""
            if "create -f -" in text:
                return 0, "", ""
            if "create token" in text:
                return 0, JWT + "\n", ""
            if "auth/kubernetes/login" in text:
                return 0, login_response(), ""
            if "auth/approle/login" in text:
                return 0, login_response("approle-token"), ""
            if "/secret-id-accessor/destroy" in text:
                return destroy_rc, "", ""
            if "/secret-id " in text:
                return 0, issue_body if issue_body is not None else issue_response(), ""
            if "read -format=json" in text:
                return 0, json.dumps({"data": {"data": {"netbox_admin_token": "netbox-token"}}}), ""
            return 0, "", ""

        runner = FakeRunner()
        runner.__class__ = type("Scripted", (FakeRunner,), {
            "__call__": lambda self, argv, stdin_text="": (
                self.calls.append((list(argv), stdin_text)) or respond(argv, stdin_text)
            )
        })
        return runner

    def clients(self, runner):
        return setup_only.SetupOnlyClients(
            kubernetes=KubernetesTransport(runner=runner),
            openbao=OpenBaoTransport("openbao", "openbao-0", runner=runner),
        )

    def test_success_returns_the_token_after_revoking_everything(self):
        runner = self.runner()
        out = born_inventory_cli.run_setup(self.args(), self.REQUEST_ID, self.clients(runner), lambda: self.NOW)
        self.assertEqual(json.loads(out), {"netbox_admin_token": "netbox-token"})
        texts = [" ".join(argv) for argv, _ in runner.calls]
        self.assertTrue(any("secret-id-accessor/destroy" in t for t in texts))
        self.assertEqual(sum("token revoke -self" in t for t in texts), 2)
        runner.assert_no_credential_in_argv(self)

    def test_role_id_is_read_from_the_published_configmap_when_not_given(self):
        runner = self.runner()
        born_inventory_cli.run_setup(self.args(role_id=""), self.REQUEST_ID, self.clients(runner), lambda: self.NOW)
        logins = [argv[-1] for argv, _ in runner.calls if "auth/approle/login" in argv[-1]]
        self.assertEqual(len(logins), 1)
        self.assertIn("role_id=published-role-id", logins[0])

    def test_a_missing_role_id_fails_before_the_claim(self):
        runner = self.runner()
        original = runner.__class__.__call__

        def broken(self_, argv, stdin_text=""):
            if "born-inventory-approle" in " ".join(argv):
                self_.calls.append((list(argv), stdin_text))
                return 1, "", "not found"
            return original(self_, argv, stdin_text)

        runner.__class__ = type("NoRoleId", (runner.__class__,), {"__call__": broken})
        with self.assertRaises(setup_only.SetupOnlyError) as caught:
            born_inventory_cli.run_setup(self.args(role_id=""), self.REQUEST_ID, self.clients(runner), lambda: self.NOW)
        self.assertEqual(caught.exception.stage, "role-id")
        self.assertIn("dmf_born_inventory_setup_role_id", str(caught.exception))
        texts = [" ".join(argv) for argv, _ in runner.calls]
        self.assertFalse(any("create -f -" in t for t in texts))
        self.assertFalse(any("get configmap request-" in t for t in texts))

    def test_a_malformed_secret_id_with_a_valid_accessor_destroys_it_and_never_uses_it(self):
        for label, body in (
            ("missing", json.dumps({"data": {"secret_id_accessor": ACCESSOR, "secret_id_ttl": 3600, "secret_id_num_uses": 1}})),
            ("empty", issue_response(**{"secret_id": ""})),
            ("not a string", issue_response(**{"secret_id": {"x": 1}})),
        ):
            with self.subTest(case=label):
                runner = self.runner(issue_body=body)
                with self.assertRaises(setup_only.SetupOnlyError) as caught:
                    born_inventory_cli.run_setup(self.args(), self.REQUEST_ID, self.clients(runner), lambda: self.NOW)
                self.assertEqual(caught.exception.stage, "issue")
                texts = [" ".join(argv) for argv, _ in runner.calls]
                self.assertEqual(sum("secret-id-accessor/destroy" in t for t in texts), 1)
                self.assertFalse(any("auth/approle/login" in t for t in texts))
                self.assertFalse(any("read -format=json" in t for t in texts))

    def test_failed_destroy_prints_no_token(self):
        runner = self.runner(destroy_rc=1)
        with self.assertRaises(setup_only.SetupOnlyError) as caught:
            born_inventory_cli.run_setup(self.args(), self.REQUEST_ID, self.clients(runner), lambda: self.NOW)
        self.assertEqual(caught.exception.stage, "cleanup")

    def test_main_needs_the_request_id(self):
        rc = born_inventory_cli.main(
            ["--env-id", "e", "--approle-name", "r", "--role-id", "i", "--role-id-configmap", "c", "--issuer-namespace", "n",
             "--issuer-service-account", "s", "--issuer-openbao-role", "o", "--audience", "a",
             "--openbao-namespace", "n", "--openbao-pod", "p"],
            environ={},
        )
        self.assertEqual(rc, 1)


if __name__ == "__main__":
    unittest.main()
