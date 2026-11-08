"""The checks before a rotation sweep, and the command line that runs them.

Each refusal is tested alone: the other inputs stay valid, so a test fails
only when its own check is missing. The command line tests use a fake of the
process boundary and prove that a refusal comes before any write, and that the
report holds no credential."""

import json
import subprocess
import time
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from approle_lifecycle import registry, rotation, rotation_cli  # noqa: E402
from approle_lifecycle.rotation_preflight import (  # noqa: E402
    PreflightError, apiserver_request_timeout, check_apiserver_timeout, check_client_timeouts, check_clock,
    check_k3s_files_current, check_openbao_request_duration, check_running_config, parse_duration_seconds,
    refuse_unbounded,
)

REGISTRY_PATH = ROOT / "vars/openbao-approle-lifecycle-registry.json"
BOUND = rotation.MAX_REQUEST_SECONDS
MARGIN = rotation.CLOCK_SKEW_MARGIN_SECONDS
RUN = 900  # the Lease duration, in seconds
JWT = "synthetic-jwt-value"
TOKEN = "synthetic-client-token-value"
GOOD_CONFIG = (
    'storage "raft" {\n  path = "/x"\n}\n\n'
    'listener "tcp" {\n  address = "0.0.0.0:8200"\n  max_request_duration = "60s"\n}\n\n'
    'default_max_request_duration = "60s"\n'
)
GOOD_K3S = "ExecStart=/usr/local/bin/k3s server --kube-apiserver-arg=audit-log-maxage=7\n"
CONFIG_TIME = 1_767_323_000
CONFIG_TIME_TEXT = "2026-01-02T02:43:20Z"
STARTED_TEXT = "2026-01-02T02:50:00Z"
BEFORE_TEXT = "2026-01-02T02:40:00Z"
# A synchronized clock: reference id, name, stratum, reference time, offset,
# last offset, rms offset, frequency, residual frequency, skew, root delay,
# root dispersion, update interval, leap status.
GOOD_TRACKING = "A9FEA97B,169.254.169.123,4,1767323045.25,0.000012345,0.000011,0.000020,1.5,0.001,0.02,0.0008,0.0004,64.0,Normal"


def tracking(**changes):
    fields = GOOD_TRACKING.split(",")
    for index, value in changes.items():
        fields[int(index[1:])] = str(value)
    return ",".join(fields)


class Client:
    def __init__(self, request_timeout):
        self.request_timeout = request_timeout


def listener(body="", name="tcp"):
    return f'listener "{name}" {{\n  address = "0.0.0.0:8200"\n{body}}}\n'


def setting(value, key="max_request_duration", indent="  "):
    return f'{indent}{key} = "{value}"\n'


class RequestDurationTests(unittest.TestCase):
    def refused(self, config):
        with self.assertRaises(PreflightError) as caught:
            check_openbao_request_duration(config, BOUND)
        return str(caught.exception)

    def test_the_rendered_scopes_pass(self):
        self.assertEqual(check_openbao_request_duration(GOOD_CONFIG, BOUND), 60)

    def test_a_listener_limit_within_the_timeout_passes_whatever_the_default(self):
        for text in ("60s", "59s", "1m", "500ms", "0m59s"):
            with self.subTest(text=text):
                config = listener(setting(text)) + setting("90s", "default_max_request_duration", "")
                check_openbao_request_duration(config, BOUND)

    def test_the_server_default_covers_a_listener_without_its_own_limit(self):
        check_openbao_request_duration(listener() + setting("60s", "default_max_request_duration", ""), BOUND)
        message = self.refused(listener() + setting("2m", "default_max_request_duration", ""))
        self.assertIn("the server default", message)

    def test_a_config_with_no_limit_anywhere_uses_openbaos_default_and_is_refused(self):
        message = self.refused(listener())
        self.assertIn("90 seconds", message)
        self.assertIn("openbao_max_request_duration_seconds", message)

    def test_a_limit_at_the_wrong_scope_does_not_count(self):
        # max_request_duration outside every listener is not a setting of OpenBao
        self.refused(listener() + setting("60s", "max_request_duration", ""))
        # default_max_request_duration inside a listener is not the server default either
        self.refused(listener(setting("60s", "default_max_request_duration")))

    def test_a_listener_override_longer_than_the_timeout_is_refused_even_with_a_short_default(self):
        config = listener(setting("90s")) + setting("60s", "default_max_request_duration", "")
        self.assertIn("its own max_request_duration", self.refused(config))

    def test_every_listener_is_checked(self):
        config = listener(setting("60s")) + listener(setting("61s"), "tcp") + setting("60s", "default_max_request_duration", "")
        self.assertIn("listener 2", self.refused(config))
        config = listener(setting("60s")) + listener("", "tcp") + setting("60s", "default_max_request_duration", "")
        check_openbao_request_duration(config, BOUND)

    def test_nested_braces_do_not_end_a_listener_early(self):
        body = '  telemetry {\n    x = "1"\n  }\n' + setting("120s")
        self.assertIn("listener 1", self.refused(listener(body) + setting("60s", "default_max_request_duration", "")))

    def test_a_config_without_a_listener_or_with_an_open_stanza_is_refused(self):
        self.refused('storage "raft" {}\n' + setting("60s", "default_max_request_duration", ""))
        self.refused('listener "tcp" {\n  address = "x"\n')

    def test_a_malformed_value_is_refused(self):
        for text in ("soon", "", "60", "-5s", "60 s"):
            with self.subTest(text=text):
                self.refused(listener(setting(text)))
                self.refused(listener() + setting(text, "default_max_request_duration", ""))

    def test_commented_out_settings_do_not_count(self):
        self.refused(listener('  # max_request_duration = "60s"\n  // max_request_duration = "60s"\n'))

    def test_durations_parse_like_go(self):
        self.assertEqual(parse_duration_seconds("1m30s"), 90)
        self.assertEqual(parse_duration_seconds("1h"), 3600)
        with self.assertRaises(ValueError):
            parse_duration_seconds("1x")


def pod(name="openbao-0", **containers):
    return {"name": name, "containers": [{"name": c, "started_at": s} for c, s in containers.items()]}


class RunningConfigTests(unittest.TestCase):
    def refused(self, times, pods):
        with self.assertRaises(PreflightError) as caught:
            check_running_config(times, pods, "openbao-config")
        return str(caught.exception)

    def test_containers_that_started_after_the_last_update_pass(self):
        check_running_config([CONFIG_TIME - 100, CONFIG_TIME], [pod(openbao=CONFIG_TIME + 1)], "openbao-config")

    def test_a_container_started_before_or_at_the_last_update_is_refused(self):
        for started in (CONFIG_TIME - 1, CONFIG_TIME):
            with self.subTest(started=started):
                message = self.refused([CONFIG_TIME], [pod(openbao=started)])
                self.assertIn("Restart the OpenBao pods", message)

    def test_the_newest_update_counts_not_the_first(self):
        self.refused([CONFIG_TIME - 500, CONFIG_TIME + 500], [pod(openbao=CONFIG_TIME + 100)])

    def test_every_container_of_every_pod_is_checked(self):
        late = CONFIG_TIME + 10
        self.refused([CONFIG_TIME], [pod("openbao-0", openbao=late), pod("openbao-1", openbao=CONFIG_TIME - 5)])
        message = self.refused([CONFIG_TIME], [pod("openbao-0", openbao=late, sidecar=CONFIG_TIME - 5)])
        self.assertIn("sidecar", message)

    def test_a_container_that_is_not_running_or_a_pod_without_containers_is_refused(self):
        self.refused([CONFIG_TIME], [pod(openbao=None)])
        self.refused([CONFIG_TIME], [pod()])

    def test_no_pod_or_no_update_time_is_refused(self):
        self.refused([CONFIG_TIME], [])
        self.assertIn("records no update time", self.refused([], [pod(openbao=CONFIG_TIME + 1)]))


class ApiServerTimeoutTests(unittest.TestCase):
    FLAG_2M = "ExecStart=/usr/local/bin/k3s server --kube-apiserver-arg=request-timeout=2m\n"

    def test_no_override_means_the_documented_default(self):
        self.assertEqual(apiserver_request_timeout(["write-kubeconfig-mode: '0644'\n"], [GOOD_K3S]), 60)
        check_apiserver_timeout([], [GOOD_K3S], BOUND)

    def test_the_flag_form_is_read(self):
        self.assertEqual(apiserver_request_timeout([], [self.FLAG_2M]), 120)
        with self.assertRaises(PreflightError) as caught:
            check_apiserver_timeout([], [self.FLAG_2M], BOUND)
        self.assertIn("Remove the request-timeout override", str(caught.exception))

    def test_the_config_file_forms_are_read_when_the_command_line_has_no_kube_apiserver_arg(self):
        for text in (
            'kube-apiserver-arg:\n  - "request-timeout=2m"\n',
            "kube-apiserver-arg:\n- request-timeout=2m\n- audit-log-maxage=7\n",
            'kube-apiserver-arg: ["audit-log-maxage=7", "request-timeout=2m"]\n',
            "kube-apiserver-arg: request-timeout=2m\n",
        ):
            with self.subTest(text=text):
                self.assertEqual(apiserver_request_timeout([text], ["ExecStart=/usr/local/bin/k3s server\n"]), 120)

    def test_other_keys_and_other_arguments_do_not_count(self):
        for config, unit in (
            ('kubelet-arg:\n  - "request-timeout=2m"\n', GOOD_K3S),
            ('kube-apiserver-arg:\n  - "audit-log-maxage=7"\nkubelet-arg:\n  - "request-timeout=2m"\n', ""),
            ("", "ExecStart=/usr/local/bin/k3s server --kubelet-arg=request-timeout=2m\n"),
        ):
            with self.subTest(config=config, unit=unit):
                self.assertEqual(apiserver_request_timeout([config], [unit]), 60)

    def test_a_command_line_flag_wins_over_a_lower_config_file_value(self):
        # The case that a check of the last value found would pass: the file says 30s, the flag says 2m.
        config = 'kube-apiserver-arg: ["request-timeout=30s"]\n'
        self.assertEqual(apiserver_request_timeout([config], [self.FLAG_2M]), 120)
        with self.assertRaises(PreflightError):
            check_apiserver_timeout([config], [self.FLAG_2M], BOUND)

    def test_any_command_line_kube_apiserver_arg_replaces_the_files_list(self):
        # k3s replaces the whole list of a repeatable flag, so the file's 2m never applies.
        config = 'kube-apiserver-arg: ["request-timeout=2m"]\n'
        self.assertEqual(apiserver_request_timeout([config], [GOOD_K3S]), 60)
        check_apiserver_timeout([config], [GOOD_K3S], BOUND)

    def test_the_last_value_within_each_source_wins(self):
        self.assertEqual(apiserver_request_timeout([], ["--kube-apiserver-arg=request-timeout=5m\n", "--kube-apiserver-arg=request-timeout=30s\n"]), 30)
        configs = ["kube-apiserver-arg:\n  - request-timeout=5m\n", "kube-apiserver-arg:\n  - request-timeout=30s\n"]
        self.assertEqual(apiserver_request_timeout(configs, []), 30)
        self.assertEqual(apiserver_request_timeout(configs[::-1], []), 300)

    def test_a_malformed_override_is_refused(self):
        with self.assertRaises(PreflightError):
            apiserver_request_timeout([], ["--kube-apiserver-arg=request-timeout=soon\n"])


class K3sFilesCurrentTests(unittest.TestCase):
    def test_files_older_than_the_start_pass(self):
        check_k3s_files_current([("/a", 100.0), ("/b", 200.0)], 200)

    def test_a_file_changed_after_the_start_is_refused_and_named(self):
        with self.assertRaises(PreflightError) as caught:
            check_k3s_files_current([("/a", 100.0), ("/etc/rancher/k3s/config.yaml", 201.0)], 200)
        self.assertIn("/etc/rancher/k3s/config.yaml", str(caught.exception))
        self.assertIn("Restart k3s", str(caught.exception))

    def test_a_server_that_is_not_running_is_refused(self):
        with self.assertRaises(PreflightError):
            check_k3s_files_current([("/a", 1.0)], None)


class ClientTimeoutTests(unittest.TestCase):
    def test_clients_within_the_bound_pass(self):
        check_client_timeouts({"kubernetes": Client(BOUND), "openbao": Client(1)}, BOUND)

    def test_a_longer_or_missing_bound_is_refused_naming_the_client(self):
        for declared in (BOUND + 1, None, 0, True, 1.5):
            with self.subTest(declared=declared):
                with self.assertRaises(PreflightError) as caught:
                    check_client_timeouts({"kubernetes": Client(BOUND), "openbao": Client(declared)}, BOUND)
                self.assertIn("openbao", str(caught.exception))


class RefuseUnboundedTests(unittest.TestCase):
    def run_with(self, argv, timeout):
        seen = []
        runner = refuse_unbounded(lambda a, s, t: seen.append(a) or (0, "", ""), BOUND)
        try:
            runner(argv, "", timeout)
        finally:
            self.seen = seen

    def test_a_bounded_call_runs(self):
        self.run_with(["kubectl", f"--request-timeout={BOUND}s", "get", "x"], BOUND)
        self.assertEqual(len(self.seen), 1)

    def test_a_call_without_a_process_timeout_is_refused_and_not_run(self):
        for timeout in (None, BOUND + 1):
            with self.subTest(timeout=timeout):
                with self.assertRaises(PreflightError):
                    self.run_with(["kubectl", f"--request-timeout={BOUND}s", "get", "x"], timeout)
                self.assertEqual(self.seen, [])

    def test_a_kubectl_call_without_a_command_line_bound_is_refused_and_not_run(self):
        for argv in (
            ["kubectl", "get", "x"],
            ["kubectl", f"--request-timeout={BOUND + 1}s", "get", "x"],
            ["kubectl", "--request-timeout=1s", "--request-timeout=2s", "get", "x"],
        ):
            with self.subTest(argv=argv):
                with self.assertRaises(PreflightError):
                    self.run_with(argv, BOUND)
                self.assertEqual(self.seen, [])

    def test_a_call_to_another_program_needs_only_the_process_timeout(self):
        self.run_with(["chronyc", "-c", "tracking"], BOUND)
        self.assertEqual(len(self.seen), 1)


class ClockTests(unittest.TestCase):
    def test_a_synchronized_clock_passes(self):
        self.assertLess(check_clock(GOOD_TRACKING, MARGIN, RUN), MARGIN / 2)

    def test_an_unsynchronized_clock_is_refused(self):
        for changes in (
            {"f0": "00000000"}, {"f2": 0}, {"f13": "Not synchronised"}, {"f13": "Insert second"},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(PreflightError) as caught:
                    check_clock(tracking(**changes), MARGIN, RUN)
                self.assertIn("not synchronized", str(caught.exception))
                self.assertIn("chronyc tracking", str(caught.exception))

    def test_an_estimated_error_over_half_the_margin_is_refused_in_either_direction(self):
        for changes in (
            {"f4": 16.0}, {"f4": -16.0}, {"f4": 14.0, "f11": 2.0}, {"f10": 40.0}, {"f11": 31.0}, {"f4": 30.0},
        ):
            with self.subTest(changes=changes):
                with self.assertRaises(PreflightError) as caught:
                    check_clock(tracking(**changes), MARGIN, RUN)
                self.assertIn("half of the clock skew margin", str(caught.exception))

    def test_an_estimated_error_up_to_half_the_margin_passes(self):
        for changes in ({"f4": 14.0}, {"f4": -14.0}, {"f4": 7.0, "f11": 6.0, "f10": 2.0}):
            with self.subTest(changes=changes):
                self.assertLessEqual(check_clock(tracking(**changes), MARGIN, RUN), MARGIN / 2)

    def test_the_drift_allowance_is_the_run_length_times_500_parts_per_million(self):
        steady = check_clock(tracking(f4=0, f10=0, f11=0), MARGIN, 0)
        self.assertAlmostEqual(check_clock(tracking(f4=0, f10=0, f11=0), MARGIN, RUN) - steady, RUN * 500e-6)

    def test_an_error_that_passes_for_a_moment_is_refused_when_drift_can_push_it_over(self):
        near = {"f4": 14.8, "f10": 0, "f11": 0}
        check_clock(tracking(**near), MARGIN, 0)
        with self.assertRaises(PreflightError) as caught:
            check_clock(tracking(**near), MARGIN, RUN)
        self.assertIn("drift", str(caught.exception))

    def test_a_longer_run_reserves_more(self):
        fields = {"f4": 14.0, "f10": 0, "f11": 0}
        check_clock(tracking(**fields), MARGIN, RUN)
        with self.assertRaises(PreflightError):
            check_clock(tracking(**fields), MARGIN, 3 * 3600)

    def test_two_runners_that_each_pass_differ_by_at_most_the_margin(self):
        worst = check_clock(tracking(f4=MARGIN / 2 - 1.0, f10=0, f11=0), MARGIN, RUN)
        self.assertLessEqual(2 * worst, MARGIN)

    def test_unreadable_output_is_refused(self):
        for text in ("", "a,b", GOOD_TRACKING + ",extra", tracking(f4="soon")):
            with self.subTest(text=text):
                with self.assertRaises(PreflightError):
                    check_clock(text, MARGIN, RUN)


class ScriptedProcesses:
    """Stands in for the process boundary of the command line. A response is
    chosen by the first predicate that matches the call."""

    def __init__(self, config=GOOD_CONFIG, chrony=(0, GOOD_TRACKING + "\n", ""), config_time=CONFIG_TIME_TEXT,
                 pod_state=None, k3s_start=None):
        self.calls = []
        self.k3s_start = (0, f"ActiveEnterTimestamp=@{int(time.time()) + 3600}\n", "") if k3s_start is None else k3s_start
        self.config = config
        self.config_time = config_time
        self.pod_state = {"running": {"startedAt": STARTED_TEXT}} if pod_state is None else pod_state
        self.chrony = chrony
        self.rules = []

    def __call__(self, argv, stdin_text="", timeout=None):
        self.calls.append((list(argv), stdin_text, timeout))
        for matches, response in self.rules:
            if matches(argv):
                return response(argv, stdin_text) if callable(response) else response
        if argv[0] == "systemctl":
            if isinstance(self.k3s_start, BaseException):
                raise self.k3s_start
            return self.k3s_start
        if argv[0] == "chronyc":
            if isinstance(self.chrony, BaseException):
                raise self.chrony
            return self.chrony
        if "configmap" in argv:
            return 0, json.dumps({
                "metadata": {"managedFields": [{"time": self.config_time}]},
                "data": {"config.hcl": self.config},
            }), ""
        if "pods" in argv:
            return 0, json.dumps({"items": [{
                "metadata": {"name": "openbao-0"},
                "status": {"containerStatuses": [{
                    "name": "openbao", "state": self.pod_state,
                }]},
            }]}), ""
        if "token" in argv:
            return 0, JWT + "\n", ""
        if "exec" in argv:
            return 0, json.dumps({"auth": {"client_token": TOKEN}}), ""
        return 1, "", "Error from server (NotFound): not scripted"

    def writes(self):
        """Calls that log in to OpenBao, mint a token or change an object."""
        found = []
        for argv, _stdin, _timeout in self.calls:
            words = " ".join(argv)
            if any(word in words for word in (" exec ", " create ", " replace ", " patch ", " delete ")):
                found.append(argv)
        return found


K3S_FILE = []


def k3s_file(text=GOOD_K3S):
    import tempfile
    handle = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False)
    handle.write(text)
    handle.close()
    K3S_FILE.append(handle.name)
    return handle.name


NO_CONFIG = "/nonexistent/k3s/config.yaml"


def arguments(*extra, k3s=None, config=None):
    return [
        "--registry", str(REGISTRY_PATH), "--target=external-secrets", "--actor=tester",
        "--request-id=run-1", "--reason=a test", "--openbao-namespace=openbao", "--openbao-pod=openbao-0",
        "--openbao-config-map=openbao-config", "--openbao-pod-selector=app.kubernetes.io/name=openbao",
        f"--k3s-unit-path={k3s or k3s_file()}", f"--k3s-config-path={config or NO_CONFIG}", "--service-account=sa", "--openbao-role=role",
        "--audience=openbao", "--lease-namespace=openbao", *extra,
    ]


def run_cli(processes, *extra, k3s=None, config=None):
    import contextlib
    import io
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status = rotation_cli.main(arguments(*extra, k3s=k3s, config=config), runner=processes, now=lambda: 1_700_000_000)
    return status, out.getvalue(), err.getvalue()


class CommandLineRefusalTests(unittest.TestCase):
    def assert_refused_before_any_write(self, processes, *extra, mentions):
        status, out, err = run_cli(processes, *extra)
        self.assertEqual(status, rotation_cli.EXIT_REFUSED, err)
        self.assertEqual(out, "")
        self.assertIn(mentions, err)
        self.assertEqual(processes.writes(), [])

    def test_a_config_without_a_limit_is_refused(self):
        self.assert_refused_before_any_write(
            ScriptedProcesses(config=listener()), mentions="90 seconds"
        )

    def test_a_config_with_a_longer_listener_limit_is_refused(self):
        self.assert_refused_before_any_write(
            ScriptedProcesses(config=listener(setting("90s"))), mentions="longer than the request timeout"
        )

    def test_a_pod_that_started_before_the_config_changed_is_refused(self):
        self.assert_refused_before_any_write(
            ScriptedProcesses(config_time=STARTED_TEXT, pod_state={"running": {"startedAt": BEFORE_TEXT}}),
            mentions="Restart the OpenBao pods",
        )

    def test_a_pod_that_is_not_running_is_refused(self):
        self.assert_refused_before_any_write(
            ScriptedProcesses(pod_state={"waiting": {"reason": "CrashLoopBackOff"}}), mentions="Restart the OpenBao pods"
        )

    def test_an_api_server_override_over_the_timeout_is_refused(self):
        path = k3s_file("ExecStart=k3s server --kube-apiserver-arg=request-timeout=3m\n")
        status, out, err = run_cli(ScriptedProcesses(), k3s=path)
        self.assertEqual((status, out), (rotation_cli.EXIT_REFUSED, ""))
        self.assertIn("API server", err)

    def test_the_command_line_wins_over_the_config_file_in_the_whole_command(self):
        config = k3s_file('kube-apiserver-arg: ["request-timeout=30s"]\n')
        unit = k3s_file("ExecStart=k3s server --kube-apiserver-arg=request-timeout=3m\n")
        status, _out, err = run_cli(ScriptedProcesses(), k3s=unit, config=config)
        self.assertEqual(status, rotation_cli.EXIT_REFUSED)
        self.assertIn("API server", err)

    def test_the_lease_duration_sets_the_drift_the_clock_check_reserves(self):
        status, _out, _err = run_cli(ScriptedProcesses(), "--preflight-only")
        self.assertEqual(status, rotation_cli.EXIT_OK)
        status, out, err = run_cli(ScriptedProcesses(), "--preflight-only", "--lease-duration=100000")
        self.assertEqual((status, out), (rotation_cli.EXIT_REFUSED, ""))
        self.assertIn("drift", err)

    def test_a_k3s_file_changed_after_k3s_started_is_refused(self):
        processes = ScriptedProcesses(k3s_start=(0, "ActiveEnterTimestamp=@1000\n", ""))
        self.assert_refused_before_any_write(processes, mentions="changed after k3s last started")

    def test_a_k3s_that_is_not_running_or_unreadable_by_systemctl_is_refused(self):
        for response, mention in (
            ((0, "ActiveEnterTimestamp=\n", ""), "k3s is not running"),
            ((1, "", "Failed"), "systemctl did not report"),
            ((0, "ActiveEnterTimestamp=Sat 2026-01-02 02:50:00 UTC\n", ""), "not a Unix time"),
            (FileNotFoundError("systemctl"), "systemctl did not report"),
        ):
            with self.subTest(mention=mention):
                self.assert_refused_before_any_write(ScriptedProcesses(k3s_start=response), mentions=mention)

    def test_an_unreadable_or_missing_k3s_unit_is_refused(self):
        import os
        import tempfile
        processes = ScriptedProcesses()
        status, _out, err = run_cli(processes, k3s=os.path.join(tempfile.gettempdir(), "no-such-k3s-unit"))
        self.assertEqual(status, rotation_cli.EXIT_REFUSED)
        self.assertIn("no k3s unit file", err)
        self.assertEqual(processes.writes(), [])
        blocked = k3s_file()
        os.chmod(blocked, 0)
        try:
            if os.access(blocked, os.R_OK):
                self.skipTest("the test runs with a privilege that ignores file modes")
            status, _out, err = run_cli(processes, k3s=blocked)
            self.assertEqual(status, rotation_cli.EXIT_REFUSED)
            self.assertIn("could not be read", err)
        finally:
            os.chmod(blocked, 0o600)

    def test_an_unreadable_config_map_is_refused(self):
        processes = ScriptedProcesses()
        processes.rules.append((lambda argv: "configmap" in argv, (1, "", "Error from server (NotFound): gone")))
        self.assert_refused_before_any_write(processes, mentions="openbao role has run")

    def test_an_unsynchronized_clock_is_refused(self):
        self.assert_refused_before_any_write(
            ScriptedProcesses(chrony=(0, tracking(f13="Not synchronised"), "")), mentions="not synchronized"
        )

    def test_a_clock_outside_the_margin_is_refused(self):
        self.assert_refused_before_any_write(
            ScriptedProcesses(chrony=(0, tracking(f4=45.0), "")), mentions="clock skew margin"
        )

    def test_a_missing_or_failing_chronyc_is_refused(self):
        self.assert_refused_before_any_write(
            ScriptedProcesses(chrony=(1, "", "chronyc: command failed")), mentions="chronyc did not report"
        )
        self.assert_refused_before_any_write(
            ScriptedProcesses(chrony=FileNotFoundError("chronyc")), mentions="chronyc did not report"
        )
        self.assert_refused_before_any_write(
            ScriptedProcesses(chrony=subprocess.TimeoutExpired(["chronyc"], BOUND)), mentions="chronyc did not report"
        )

    def test_a_timeout_option_over_the_engine_bound_is_refused_by_the_wrapper(self):
        status, _out, err = run_cli(ScriptedProcesses(), "--request-timeout=0")
        self.assertEqual(status, rotation_cli.EXIT_REFUSED)
        self.assertIn("positive", err)

    def test_an_unreadable_registry_is_refused_after_the_checks_and_before_any_write(self):
        processes = ScriptedProcesses()
        args = arguments()
        args[1] = str(ROOT / "vars/does-not-exist.json")
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            status = rotation_cli.main(args, runner=processes)
        self.assertEqual(status, rotation_cli.EXIT_REFUSED)
        self.assertEqual(processes.writes(), [])

    def test_preflight_only_runs_the_checks_and_logs_in_nowhere(self):
        processes = ScriptedProcesses()
        status, out, _err = run_cli(processes, "--preflight-only")
        self.assertEqual((status, out.strip()), (rotation_cli.EXIT_OK, "preflight ok"))
        self.assertEqual(processes.writes(), [])

    def test_every_process_call_has_the_timeout_and_every_kubectl_call_the_flag(self):
        processes = ScriptedProcesses()
        run_cli(processes, "--preflight-only")
        self.assertTrue(processes.calls)
        for argv, _stdin, timeout in processes.calls:
            self.assertEqual(timeout, BOUND)
            if argv[0] == "kubectl":
                self.assertIn(f"--request-timeout={BOUND}s", argv)


def result(target, outcome, state="", reason="", transaction="", cause=""):
    return rotation.TargetResult(target, outcome, state, reason, transaction, cause)


class ReportFormatTests(unittest.TestCase):
    def test_one_line_per_target_then_a_summary(self):
        report = rotation_cli.format_report([
            result("a", "rotated", "active", "", "tx-1"),
            result("b", "not_due", "active"),
            result("c", "failed", "delivered-pending-readiness", "readiness_failed", "tx-3", ""),
            result("d", "failed", "issued-pending-delivery", "delivery_failed", "tx-4", "credential_expiring"),
        ])
        self.assertEqual(report.splitlines(), [
            "target=a outcome=rotated state=active reason_code=- transaction_id=tx-1 cause=-",
            "target=b outcome=not_due state=active reason_code=- transaction_id=- cause=-",
            "target=c outcome=failed state=delivered-pending-readiness reason_code=readiness_failed transaction_id=tx-3 cause=-",
            "target=d outcome=failed state=issued-pending-delivery reason_code=delivery_failed transaction_id=tx-4 cause=credential_expiring",
            "summary rotated=1 not_due=1 failed=2",
        ])

    def test_an_empty_sweep_still_has_a_summary(self):
        self.assertEqual(rotation_cli.format_report([]), "summary rotated=0 not_due=0 failed=0")


class CommandLineRunTests(unittest.TestCase):
    """The whole command with the engine replaced, so the test sees the
    session handling, the exit status and the report."""

    def setUp(self):
        self.calls = []
        self.original = (rotation.rotate_target, rotation.rotate_all)

    def tearDown(self):
        rotation.rotate_target, rotation.rotate_all = self.original

    def replace(self, results):
        def fake_target(registry_data, target, context, clients, config, now):
            self.calls.append(("target", target, context, config))
            return results[0]

        def fake_all(registry_data, context, clients, config, now):
            self.calls.append(("all", context, config))
            return rotation.SweepResult(tuple(results))

        rotation.rotate_target, rotation.rotate_all = fake_target, fake_all

    def test_the_session_is_opened_for_the_run_and_revoked_after_it(self):
        self.replace([result("external-secrets", "rotated", "active", "", "tx-1")])
        processes = ScriptedProcesses()
        status, out, err = run_cli(processes)
        self.assertEqual(status, rotation_cli.EXIT_OK, err)
        words = [" ".join(argv) for argv, _s, _t in processes.calls]
        login = next(i for i, w in enumerate(words) if "auth/kubernetes/login" in w)
        revoke = next(i for i, w in enumerate(words) if "token revoke -self" in w)
        self.assertLess(login, revoke)
        self.assertEqual(self.calls[0][0], "target")
        self.assertIn("target=external-secrets outcome=rotated", out)

    def test_the_session_is_revoked_when_the_run_raises(self):
        def boom(*args):
            raise rotation.RotationError("lease_conflict", "another run holds the Lease")

        rotation.rotate_target = boom
        processes = ScriptedProcesses()
        status, out, err = run_cli(processes)
        self.assertEqual(status, rotation_cli.EXIT_RUN_FAILED)
        self.assertEqual(out, "")
        self.assertIn("reason_code=lease_conflict", err)
        self.assertTrue(any("token revoke -self" in " ".join(argv) for argv, _s, _t in processes.calls))

    def test_a_failed_target_gives_exit_status_one_and_is_reported(self):
        self.replace([result("external-secrets", "failed", "delivered-pending-readiness", "readiness_failed", "tx-1")])
        status, out, _err = run_cli(ScriptedProcesses())
        self.assertEqual(status, rotation_cli.EXIT_TARGET_FAILED)
        self.assertIn("reason_code=readiness_failed", out)

    def test_a_sweep_runs_every_target_and_the_context_reaches_the_engine(self):
        self.replace([result("a", "not_due"), result("b", "rotated", "active", "", "tx-2")])
        args = [a for a in arguments() if not a.startswith("--target")] + ["--all"]
        import contextlib
        import io
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            status = rotation_cli.main(args, runner=ScriptedProcesses(), now=lambda: 1_700_000_000)
        self.assertEqual(status, rotation_cli.EXIT_OK)
        kind, context, config = self.calls[0]
        self.assertEqual(kind, "all")
        self.assertEqual((context.actor, context.role, context.request_id, context.reason),
                         ("tester", rotation.SCHEDULER_ROLE, "run-1", "a test"))
        self.assertEqual(config.request_timeout, BOUND)
        self.assertEqual(out.getvalue().splitlines()[-1], "summary rotated=1 not_due=1 failed=0")

    def test_no_credential_is_in_any_argv_or_in_the_output(self):
        self.replace([result("external-secrets", "rotated", "active", "", "tx-1")])
        processes = ScriptedProcesses()
        _status, out, err = run_cli(processes)
        for argv, _stdin, _timeout in processes.calls:
            for word in argv:
                self.assertNotIn(JWT, word)
                self.assertNotIn(TOKEN, word)
        for text in (out, err):
            self.assertNotIn(JWT, text)
            self.assertNotIn(TOKEN, text)

    def test_the_run_context_is_validated_by_the_engine_before_any_write(self):
        rotation.rotate_target, rotation.rotate_all = self.original
        processes = ScriptedProcesses()
        status, _out, err = run_cli(processes, "--reason=")
        self.assertEqual(status, rotation_cli.EXIT_REFUSED)
        self.assertIn("invalid_state", err)
        self.assertEqual(processes.writes(), [])


def tearDownModule():
    import os
    for path in K3S_FILE:
        try:
            os.chmod(path, 0o600)
            os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    unittest.main()
