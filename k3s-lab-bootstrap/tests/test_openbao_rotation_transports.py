"""The rotation engine's real clients, tested against a fake of the process
boundary. The fake records every argv, every stdin and every timeout, so the
tests can prove three things: no credential reaches argv, every call carries
the request timeout, and the clients tell an answered error from a call whose
outcome is unknown. Every credential is synthetic."""

import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from approle_lifecycle import rotation  # noqa: E402
from approle_lifecycle.rotation import (  # noqa: E402
    AlreadyRegistered, ApiError, LeaseConflict, LeaseExists, RotationClients, RotationConfig, StagingExists,
)
from approle_lifecycle.rotation_transports import (  # noqa: E402
    CallTimedOut, RotationKubernetes, RotationOpenBao,
)
from approle_lifecycle.transports import TransportError  # noqa: E402

JWT = "synthetic-jwt-value"
TOKEN = "synthetic-client-token-value"
CANDIDATE = "synthetic-candidate-secret-id"
ACCESSOR = "synthetic-accessor-value"
PAYLOAD = "synthetic-payload-value"
CREDENTIALS = (JWT, TOKEN, CANDIDATE, ACCESSOR, PAYLOAD)
BOUND = 45
API_ERROR = "Error writing data to somewhere: Error making API request.\n\nURL: PUT https://127.0.0.1:8200/v1/x\nCode: 400. Errors:\n"


class FakeRunner:
    """Replaces `run`. `responses` is a list of (rc, stdout, stderr), used in
    order; an empty list answers every call with success and no output."""

    def __init__(self, *responses, inspect=None):
        self.responses = list(responses)
        self.calls = []  # (argv, stdin, timeout)
        self.inspect = inspect

    def __call__(self, argv, stdin_text="", timeout=None):
        self.calls.append((list(argv), stdin_text, timeout))
        if self.inspect is not None:
            self.inspect(argv)
        response = self.responses.pop(0) if self.responses else (0, "", "")
        if isinstance(response, BaseException):
            raise response
        return response

    def assert_no_credential_in_argv(self, test):
        for argv, _stdin, _timeout in self.calls:
            for word in argv:
                for secret in CREDENTIALS:
                    test.assertNotIn(secret, word)


def kube(runner, **kwargs):
    return RotationKubernetes(runner=runner, request_timeout=BOUND, **kwargs)


def bao(runner, token=TOKEN):
    client = RotationOpenBao("openbao", "openbao-0", runner=runner, request_timeout=BOUND)
    client._token = token
    return client


def secret_json(**overrides):
    obj = {
        "metadata": {"name": "s", "namespace": "ns", "resourceVersion": "7", "annotations": {"a": "b"}},
        "data": {"id": "b2xkLXZhbHVl"},
    }
    obj.update(overrides)
    return json.dumps(obj)


CONFIGMAP_JSON = json.dumps({
    "metadata": {"managedFields": [{"time": "2026-01-02T03:00:00Z"}, {"time": "2026-01-02T03:10:00Z"}, {}]},
    "data": {"config.hcl": "x"},
})
PODS_JSON = json.dumps({"items": [{
    "metadata": {"name": "openbao-0"},
    "status": {"containerStatuses": [
        {"name": "openbao", "state": {"running": {"startedAt": "2026-01-02T03:20:00Z"}}},
        {"name": "sidecar", "state": {"waiting": {"reason": "x"}}},
    ]},
}]})
LEASE_JSON = json.dumps({
    "metadata": {"resourceVersion": "12"},
    "spec": {"holderIdentity": "h", "renewTime": "2026-01-02T03:04:05.250000Z", "leaseDurationSeconds": 900},
})


class KubernetesCalls(unittest.TestCase):
    """One entry per client method, each with a response the method accepts."""

    def calls(self):
        return [
            ("get_lease", ("ns", "n"), (0, LEASE_JSON, "")),
            ("create_lease", ("ns", "n", "h", 1_700_000_000, 900), (0, "", "")),
            ("update_lease", ("ns", "n", "h", 1_700_000_000, 900, "3"), (0, "", "")),
            ("delete_lease", ("ns", "n", "3"), (0, "", "")),
            ("get_secret", ("ns", "n"), (0, secret_json(), "")),
            ("patch_secret", ("ns", "n", "id", PAYLOAD, {"t": "1"}, "7"), (0, "", "")),
            ("patch_secret", ("ns", "n", "id", PAYLOAD, {"t": "1"}, None), (0, "", "")),
            ("annotate_secret", ("ns", "n", {"t": "1"}, "7"), (0, "", "")),
            ("get_object", ("ClusterSecretStore", None, "store"), (0, "{}", "")),
            ("get_object", ("ExternalSecret", "ns", "es"), (0, "{}", "")),
            ("list_objects", ("ExternalSecret", "ns"), (0, '{"items": []}', "")),
            ("get_configmap", ("ns", "cm"), (0, CONFIGMAP_JSON, "")),
            ("list_pods", ("ns", "app.kubernetes.io/name=openbao"), (0, PODS_JSON, "")),
            ("request_token", ("ns", "sa", "aud"), (0, JWT + "\n", "")),
        ]

    def test_every_call_carries_the_timeout_in_the_process_and_on_the_command_line(self):
        for method, args, response in self.calls():
            with self.subTest(method=method, args=args[:2]):
                runner = FakeRunner(response)
                getattr(kube(runner), method)(*args)
                self.assertEqual(len(runner.calls), 1)
                argv, _stdin, timeout = runner.calls[0]
                self.assertEqual(timeout, BOUND)
                self.assertIn(f"--request-timeout={BOUND}s", argv)
                self.assertEqual(argv[0], "kubectl")

    def test_every_public_method_is_in_the_table(self):
        listed = {method for method, _args, _response in self.calls()}
        public = {
            name for name in dir(RotationKubernetes)
            if not name.startswith("_") and callable(getattr(RotationKubernetes, name))
        }
        self.assertEqual(public - {"request_timeout"}, listed)

    def test_scoped_calls_run_as_the_service_account_and_the_checks_run_as_the_runner(self):
        scoped = FakeRunner((0, "", ""))
        kube(scoped, as_user="system:serviceaccount:openbao:sa").create_lease("ns", "n", "h", 1, 900)
        self.assertIn("--as", scoped.calls[0][0])
        for method, args in (
            ("request_token", ("ns", "sa", "aud")), ("get_configmap", ("ns", "cm")),
            ("list_pods", ("ns", "app.kubernetes.io/name=openbao")),
        ):
            runner = FakeRunner((0, {"request_token": JWT, "get_configmap": CONFIGMAP_JSON}.get(method, PODS_JSON), ""))
            getattr(kube(runner, as_user="system:serviceaccount:openbao:sa"), method)(*args)
            self.assertNotIn("--as", runner.calls[0][0])

    def test_a_secret_value_reaches_kubectl_only_through_a_private_file(self):
        seen = {}

        def inspect(argv):
            path = argv[-1]
            seen["mode"] = stat.S_IMODE(os.stat(path).st_mode)
            seen["text"] = Path(path).read_text()
            seen["path"] = path

        for resource_version in ("7", None):
            with self.subTest(resource_version=resource_version):
                runner = FakeRunner(inspect=inspect)
                kube(runner).patch_secret("ns", "n", "id", PAYLOAD, {"t": "1"}, resource_version)
                self.assertEqual(seen["mode"], 0o600)
                self.assertIn(PAYLOAD, seen["text"])
                self.assertFalse(os.path.exists(seen["path"]))
                runner.assert_no_credential_in_argv(self)
                self.assertEqual(runner.calls[0][1], "")

    def test_the_private_file_is_removed_when_the_call_fails_or_times_out(self):
        paths = []
        for response in ((1, "", "Error from server (Conflict): changed"), subprocess.TimeoutExpired(["kubectl"], BOUND)):
            runner = FakeRunner(response, inspect=lambda argv: paths.append(argv[-1]))
            with self.assertRaises((ApiError, CallTimedOut)):
                kube(runner).patch_secret("ns", "n", "id", PAYLOAD, {}, "7")
        self.assertEqual(len(paths), 2)
        for path in paths:
            self.assertFalse(os.path.exists(path))

    def test_the_patch_names_the_version_it_expects(self):
        runner = FakeRunner(inspect=lambda argv: runner.__dict__.setdefault("body", json.loads(Path(argv[-1]).read_text())))
        kube(runner).patch_secret("ns", "n", "id", PAYLOAD, {"t": "1"}, "7")
        self.assertEqual(runner.body["metadata"]["resourceVersion"], "7")
        self.assertEqual(runner.body["stringData"], {"id": PAYLOAD})

    def test_the_configmap_read_returns_its_update_times_and_asks_for_them(self):
        runner = FakeRunner((0, CONFIGMAP_JSON, ""))
        found = kube(runner).get_configmap("ns", "cm")
        self.assertIn("--show-managed-fields", runner.calls[0][0])
        self.assertEqual(found["data"], {"config.hcl": "x"})
        self.assertEqual(found["update_times"], [1767322800.0, 1767323400.0])

    def test_the_pod_list_returns_the_start_time_of_each_container(self):
        runner = FakeRunner((0, PODS_JSON, ""))
        found = kube(runner).list_pods("ns", "app.kubernetes.io/name=openbao")
        self.assertIn("--selector=app.kubernetes.io/name=openbao", runner.calls[0][0])
        self.assertEqual(found, [{"name": "openbao-0", "containers": [
            {"name": "openbao", "started_at": 1767324000.0}, {"name": "sidecar", "started_at": None},
        ]}])

    def test_a_malformed_selector_or_response_is_refused(self):
        for selector in ("", "a b=c", "x;y=z", "novalue", "a=b,c=d"):
            with self.subTest(selector=selector):
                with self.assertRaises(TransportError):
                    kube(FakeRunner()).list_pods("ns", selector)
        with self.assertRaises(TransportError):
            kube(FakeRunner((0, '{"items": [{"metadata": {}}]}', ""))).list_pods("ns", "a=b")
        with self.assertRaises(TransportError):
            kube(FakeRunner((0, '{"metadata": {"managedFields": [{"time": "soon"}]}}', ""))).get_configmap("ns", "cm")

    def test_a_missing_version_cannot_annotate(self):
        with self.assertRaises(TransportError):
            kube(FakeRunner()).annotate_secret("ns", "n", {}, None)

    def test_lease_round_trip_uses_unix_seconds_and_micro_time(self):
        lease = kube(FakeRunner((0, LEASE_JSON, ""))).get_lease("ns", "n")
        self.assertEqual(lease["holder"], "h")
        self.assertEqual(lease["duration_seconds"], 900)
        self.assertEqual(lease["resource_version"], "12")
        self.assertEqual(lease["renew_time"], 1767323045.25)
        runner = FakeRunner()
        kube(runner).update_lease("ns", "n", "h", 1767323045, 900, "12")
        body = json.loads(runner.calls[0][1])
        self.assertEqual(body["spec"]["renewTime"], "2026-01-02T03:04:05.000000Z")
        self.assertEqual(body["metadata"]["resourceVersion"], "12")

    def test_lease_delete_names_the_version_it_expects(self):
        runner = FakeRunner()
        kube(runner).delete_lease("ns", "n", "12")
        argv, stdin, _timeout = runner.calls[0]
        self.assertIn("--raw", argv)
        self.assertEqual(json.loads(stdin)["preconditions"], {"resourceVersion": "12"})

    def test_answered_errors_map_to_the_engine_exceptions(self):
        conflict = (1, "", 'Error from server (Conflict): Operation cannot be fulfilled on leases "n"')
        cases = [
            ("create_lease", ("ns", "n", "h", 1, 900), (1, "", 'Error from server (AlreadyExists): leases "n"'), LeaseExists),
            ("update_lease", ("ns", "n", "h", 1, 900, "3"), conflict, LeaseConflict),
            ("delete_lease", ("ns", "n", "3"), conflict, LeaseConflict),
            ("update_lease", ("ns", "n", "h", 1, 900, "3"), (1, "", 'Error from server (NotFound): leases "n"'), LeaseConflict),
            ("create_lease", ("ns", "n", "h", 1, 900), (1, "", "Error from server (Forbidden): no"), ApiError),
            ("patch_secret", ("ns", "n", "id", PAYLOAD, {}, "7"), conflict, ApiError),
        ]
        for method, args, response, expected in cases:
            with self.subTest(method=method, expected=expected.__name__):
                with self.assertRaises(expected) as caught:
                    getattr(kube(FakeRunner(response)), method)(*args)
                self.assertEqual(type(caught.exception), expected)

    def test_a_call_without_an_answer_is_unanswered_and_carries_no_output(self):
        leaky = f"Unable to connect to the server: dial tcp: lookup {TOKEN}"
        for method, args in (("get_lease", ("ns", "n")), ("get_secret", ("ns", "n"))):
            with self.subTest(method=method):
                with self.assertRaises(TransportError) as caught:
                    getattr(kube(FakeRunner((1, "", leaky))), method)(*args)
                self.assertNotIsInstance(caught.exception, rotation.ANSWERED)
                self.assertNotIn(TOKEN, str(caught.exception))

    def test_a_timeout_is_unanswered_with_a_fixed_message(self):
        runner = FakeRunner(subprocess.TimeoutExpired(["kubectl", JWT], BOUND, output=PAYLOAD))
        with self.assertRaises(CallTimedOut) as caught:
            kube(runner).get_lease("ns", "n")
        self.assertNotIsInstance(caught.exception, rotation.ANSWERED)
        self.assertIsNone(caught.exception.__cause__)
        for secret in CREDENTIALS:
            self.assertNotIn(secret, str(caught.exception))

    def test_a_missing_object_reads_as_none(self):
        for method, args in (("get_lease", ("ns", "n")), ("get_secret", ("ns", "n")), ("get_object", ("ExternalSecret", "ns", "x"))):
            with self.subTest(method=method):
                self.assertIsNone(getattr(kube(FakeRunner((0, "", ""))), method)(*args))

    def test_secret_data_is_decoded(self):
        found = kube(FakeRunner((0, secret_json(), ""))).get_secret("ns", "n")
        self.assertEqual(found, {"data": {"id": "old-value"}, "annotations": {"a": "b"}, "resource_version": "7"})

    def test_an_unknown_kind_and_an_unsafe_name_are_refused(self):
        with self.assertRaises(TransportError):
            kube(FakeRunner()).get_object("Pod", "ns", "x")
        with self.assertRaises(TransportError):
            kube(FakeRunner()).get_secret("ns", "bad name; rm")


class OpenBaoCalls(unittest.TestCase):
    def calls(self):
        role = json.dumps({"data": {"secret_id_ttl": 2592000}})
        secret_id = json.dumps({"data": {
            "secret_id_accessor": "acc", "secret_id_ttl": 2592000, "secret_id_num_uses": 0,
            "creation_time": "2026-01-01T00:00:00Z", "expiration_time": "2026-01-31T00:00:00Z",
        }})
        kv = json.dumps({"data": {"data": {"k": "v"}, "metadata": {"version": 3}}})
        return [
            ("read_role", ("external-secrets",), (0, role, "")),
            ("lookup_secret_id", ("external-secrets", CANDIDATE), (0, secret_id, "")),
            ("create_custom_secret_id", ("external-secrets", CANDIDATE, {"ttl": "2592000s", "num_uses": 0}), (0, secret_id, "")),
            ("destroy_secret_id_accessor", ("external-secrets", ACCESSOR), (0, "", "")),
            ("read_kv2", ("secret/data/platform/x",), (0, kv, "")),
            ("write_kv2", ("secret/data/platform/x", {"k": PAYLOAD}, 3), (0, "", "")),
            ("create_staging", ("secret/data/platform/s", {"secret_id": CANDIDATE}), (0, "", "")),
            ("read_staging", ("secret/data/platform/s",), (0, kv, "")),
            ("delete_staging", ("secret/data/platform/s",), (0, "", "")),
            ("revoke_self", (), (0, "", "")),
        ]

    def test_every_call_carries_the_timeout_and_the_token_on_stdin_only(self):
        for method, args, response in self.calls():
            with self.subTest(method=method):
                runner = FakeRunner(response)
                getattr(bao(runner), method)(*args)
                argv, stdin, timeout = runner.calls[0]
                self.assertEqual(timeout, BOUND)
                self.assertIn(f"--request-timeout={BOUND}s", argv)
                self.assertTrue(stdin.startswith(TOKEN + "\n"), method)
                runner.assert_no_credential_in_argv(self)

    def test_the_login_sends_the_jwt_on_stdin_and_stores_the_token(self):
        out = json.dumps({"auth": {"client_token": TOKEN}})
        runner = FakeRunner((0, out, ""))
        client = RotationOpenBao("openbao", "openbao-0", runner=runner, request_timeout=BOUND)
        client.login_kubernetes("approle-rotation-scheduler", JWT)
        argv, stdin, timeout = runner.calls[0]
        self.assertEqual((stdin, timeout), (JWT, BOUND))
        runner.assert_no_credential_in_argv(self)
        self.assertEqual(client._token, TOKEN)

    def test_every_public_method_is_in_the_table(self):
        listed = {method for method, _args, _response in self.calls()} | {"login_kubernetes"}
        public = {
            name for name in dir(RotationOpenBao)
            if not name.startswith("_") and callable(getattr(RotationOpenBao, name))
        }
        self.assertEqual(public, listed)

    def test_a_call_without_a_session_is_refused_before_it_runs(self):
        runner = FakeRunner()
        client = RotationOpenBao("openbao", "openbao-0", runner=runner, request_timeout=BOUND)
        with self.assertRaises(TransportError):
            client.read_kv2("secret/data/x")
        self.assertEqual(runner.calls, [])

    def test_the_candidate_and_the_accessor_are_the_only_stdin_after_the_token(self):
        runner = FakeRunner((0, json.dumps({"data": {"secret_id_accessor": "a"}}), ""))
        bao(runner).create_custom_secret_id("external-secrets", CANDIDATE, {"ttl": "5s", "num_uses": 0})
        argv, stdin, _timeout = runner.calls[0]
        self.assertEqual(stdin, f"{TOKEN}\n{CANDIDATE}")
        self.assertIn("secret_id=-", argv[-1])
        runner = FakeRunner()
        bao(runner).destroy_secret_id_accessor("external-secrets", ACCESSOR)
        self.assertEqual(runner.calls[0][1], f"{TOKEN}\n{ACCESSOR}")

    def test_a_kv_write_sends_a_json_body_with_the_version_condition(self):
        runner = FakeRunner()
        bao(runner).write_kv2("secret/data/platform/x", {"k": PAYLOAD}, 3)
        _argv, stdin, _timeout = runner.calls[0]
        token_line, body = stdin.split("\n", 1)
        self.assertEqual(json.loads(body), {"options": {"cas": 3}, "data": {"k": PAYLOAD}})
        self.assertNotIn("\n", body)

    def test_the_create_response_never_returns_the_secret_id(self):
        response = json.dumps({"data": {
            "secret_id": CANDIDATE, "secret_id_accessor": "a", "secret_id_ttl": 5, "secret_id_num_uses": 0,
        }})
        found = bao(FakeRunner((0, response, ""))).create_custom_secret_id("external-secrets", CANDIDATE, {"ttl": "5s"})
        self.assertEqual(found, {"secret_id_accessor": "a", "secret_id_ttl": 5, "secret_id_num_uses": 0})

    def test_staging_is_deleted_by_its_metadata_path(self):
        runner = FakeRunner()
        bao(runner).delete_staging("secret/data/platform/approle-rotation-staging/external-secrets")
        self.assertIn("secret/metadata/platform/approle-rotation-staging/external-secrets", runner.calls[0][0][-1])
        with self.assertRaises(TransportError):
            bao(FakeRunner()).delete_staging("secret/platform/not-a-data-path")

    def test_answered_errors_map_to_the_engine_exceptions(self):
        exists = (1, "", API_ERROR + "* check-and-set parameter did not match the current version")
        registered = (1, "", API_ERROR + "* secret id already registered")
        self.assertRaises(StagingExists, bao(FakeRunner(exists)).create_staging, "secret/data/x", {})
        self.assertRaises(AlreadyRegistered, bao(FakeRunner(registered)).create_custom_secret_id, "external-secrets", CANDIDATE, {})
        self.assertRaises(ApiError, bao(FakeRunner(exists)).write_kv2, "secret/data/x", {}, 1)
        self.assertRaises(ApiError, bao(FakeRunner((1, "", API_ERROR + "* denied"))).read_role, "external-secrets")

    def test_not_found_reads_as_none_and_a_gone_accessor_is_not_an_error(self):
        missing = (2, "", "No value found at secret/data/x")
        self.assertIsNone(bao(FakeRunner(missing)).read_kv2("secret/data/x"))
        self.assertIsNone(bao(FakeRunner(missing)).read_role("external-secrets"))
        self.assertIsNone(bao(FakeRunner((0, "", ""))).lookup_secret_id("external-secrets", CANDIDATE))
        gone = (1, "", API_ERROR + "* failed to find accessor entry for secret_id_accessor")
        self.assertIsNone(bao(FakeRunner(gone)).destroy_secret_id_accessor("external-secrets", ACCESSOR))

    def test_a_call_without_an_answer_is_unanswered_and_carries_no_output(self):
        leaky = f'Put "https://127.0.0.1:8200": context deadline exceeded for {TOKEN} {CANDIDATE}'
        for response in ((1, "", leaky), subprocess.TimeoutExpired(["kubectl"], BOUND)):
            with self.subTest(response=type(response).__name__):
                with self.assertRaises(TransportError) as caught:
                    bao(FakeRunner(response)).create_custom_secret_id("external-secrets", CANDIDATE, {})
                self.assertNotIsInstance(caught.exception, rotation.ANSWERED)
                for secret in CREDENTIALS:
                    self.assertNotIn(secret, str(caught.exception))

    def test_a_deleted_latest_version_reads_as_none(self):
        deleted = json.dumps({"data": {"data": None, "metadata": {"version": 4}}})
        self.assertIsNone(bao(FakeRunner((0, deleted, ""))).read_kv2("secret/data/x"))

    def test_unsafe_words_never_reach_the_remote_shell(self):
        for call in (
            lambda c: c.read_role("name; echo"),
            lambda c: c.read_kv2("secret/data/x y"),
            lambda c: c.create_custom_secret_id("external-secrets", CANDIDATE, {"ttl": "5s; echo"}),
        ):
            runner = FakeRunner()
            with self.assertRaises(TransportError):
                call(bao(runner))
            self.assertEqual(runner.calls, [])


class EngineAcceptsTheClients(unittest.TestCase):
    def clients(self, timeout):
        runner = FakeRunner()
        return RotationClients(
            kubernetes=RotationKubernetes(runner=runner, request_timeout=timeout),
            openbao=RotationOpenBao("openbao", "openbao-0", runner=runner, request_timeout=timeout),
        )

    def test_the_engine_accepts_clients_within_its_bound(self):
        config = RotationConfig(lease_namespace="openbao")
        rotation.validate_config(config, self.clients(config.request_timeout))

    def test_the_engine_refuses_a_client_with_a_longer_bound(self):
        config = RotationConfig(lease_namespace="openbao")
        with self.assertRaises(rotation.RotationError):
            rotation.validate_config(config, self.clients(config.request_timeout + 1))

    def test_a_client_refuses_a_bad_timeout(self):
        for bad in (0, -1, 1.5, True, "60"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    RotationKubernetes(runner=FakeRunner(), request_timeout=bad)


if __name__ == "__main__":
    unittest.main()
