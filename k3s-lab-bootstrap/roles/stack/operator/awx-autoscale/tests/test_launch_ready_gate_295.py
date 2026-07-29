"""Discriminating tests for #295: AWX launch-ready gate (not just ping-200).

Issue #295: reproduced live against AWX 24.6.1 — a real deploy dispatched the
instant readyReplicas >= 1 + ping-200 went green hit a bare Django Server Error
(500) on the very next authenticated call (job template lookup). ping-200 alone
is insufficient; must also check:
  - instance_group capacity > 0 (not just ping-200)
  - authenticated read succeeds via a strengthened best-effort gate

Tests verify: ping-200 + capacity-0 or job-templates-503 are NOT launch-ready.
Tests also verify that regression between snapshots (after stabilization delay)
correctly re-arms the gate and prevents false-ready returns.

NOTE: The helper uses awx-svc token, NOT console identity. Console-side
bounded transient retry is the required companion fix for #295.

Run:
  python3 -m pytest tests/test_launch_ready_gate_295.py -v
"""
import importlib.util
import itertools
import json
import os
import time
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

import pytest

import fake_k8s
from fake_k8s import drained_cluster, pod

# The helper reads these at import time (no defaults); set test values first.
os.environ.setdefault("AWX_AUTOSCALE_NAMESPACE", fake_k8s.NAMESPACE)
os.environ.setdefault("AWX_AUTOSCALE_AWX_API_URL", "http://awx.test")
os.environ.setdefault("AWX_AUTOSCALE_WAKE_POLL_INTERVAL", "1")  # speed up polls (still int)
os.environ.setdefault("AWX_AUTOSCALE_STABILIZATION_DELAY", "0.01")  # speed up stabilization

_HELPER = Path(__file__).resolve().parents[1] / "files" / "awx_autoscale_helper.py"
_spec = importlib.util.spec_from_file_location("awx_autoscale_helper", _HELPER)
helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helper)


def _ping_response(capacity):
    """Mock ping response with instance_group capacity."""
    m = mock.MagicMock()
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    m.status = 200
    m.read.return_value = json.dumps({
        "instances": [{"node": "awx-task-1", "node_type": "control", "capacity": capacity}],
        "instance_groups": [
            {"name": "controlplane", "capacity": capacity, "instances": ["awx-task-1"]},
            {"name": "default", "capacity": 0, "instances": []},
        ]
    }).encode()
    return m


def _ping_response_groups(groups):
    """Mock ping response with an explicit instance_groups list."""
    m = mock.MagicMock()
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    m.status = 200
    m.read.return_value = json.dumps({
        "instances": [{"node": "awx-task-1", "node_type": "control", "capacity": 20}],
        "instance_groups": groups,
    }).encode()
    return m


def _auth_body_response(raw_body, status=200):
    """Mock job_templates response carrying an exact raw body."""
    m = mock.MagicMock()
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    m.status = status
    m.read.return_value = raw_body if isinstance(raw_body, bytes) else raw_body.encode()
    return m


def _job_templates_response(status=200):
    """Mock job_templates response."""
    m = mock.MagicMock()
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    m.status = status
    if status == 200:
        m.read.return_value = json.dumps({"count": 5, "results": []}).encode()
    else:
        # Empty 500 (matches the real prod error from #295)
        m.read.return_value = b""
    return m


def _http_error(code):
    """Raise HTTPError with the given code."""
    exc = HTTPError("url", code, "msg", {}, None)
    exc.fp = None
    raise exc


@mock.patch.object(helper, "_read_secret", return_value="tok")
@mock.patch.object(helper, "_k8s_request")
@mock.patch.object(helper, "urlopen")
def test_ping_200_alone_not_launch_ready_no_capacity(urlopen_mock, k8s_mock, _):
    """Ping-200 + no registered capacity → NOT launch-ready (#295).

    After sleep→wake, ping can return 200 while instance_groups show capacity 0.
    Reproduced live: first few seconds after pod is Running, ping succeeds but
    the execution instance hasn't sent its first heartbeat yet.
    """
    # Both Deployments ready
    k8s_mock.side_effect = drained_cluster()

    # Ping returns 200 but instance_group capacity is 0
    urlopen_mock.return_value = _ping_response(capacity=0)

    # Should NOT be ready, despite ping-200
    result = helper.wait_awx_ready(timeout=0.5)
    assert result is False, \
        "ping-200 + capacity-0 should NOT be launch-ready (instance not yet heartbeat)"


@mock.patch.object(helper, "_read_secret", return_value="tok")
@mock.patch.object(helper, "_k8s_request")
@mock.patch.object(helper, "urlopen")
def test_ping_200_capacity_ok_but_auth_fails(urlopen_mock, k8s_mock, _):
    """Ping-200 + capacity>0 + authenticated read fails → NOT launch-ready (#295).

    Real scenario: awx-web pod's pod-level readiness succeeds and ping-200 works,
    but the Django backend still 500s on ORM/RBAC-backed endpoints. The console's
    post-wake job-template lookup would hit this and error the operation.
    """
    # Both Deployments ready
    k8s_mock.side_effect = drained_cluster()

    # Sequence of urlopen calls:
    # 1. ping returns 200 + capacity > 0
    # 2. job_templates returns 500 (empty body, matching prod error)
    # 3. (retry) job_templates still returns 500
    ping_ok = _ping_response(capacity=20)
    job_fail = _job_templates_response(status=500)

    urlopen_mock.side_effect = [ping_ok, job_fail, job_fail, job_fail]

    # Should NOT be ready despite ping-200 + capacity
    result = helper.wait_awx_ready(timeout=0.5)
    assert result is False, \
        "ping-200 + capacity>0 + job_templates-500 should NOT be launch-ready (#295)"


@mock.patch.object(helper, "_read_secret", return_value="tok")
@mock.patch.object(helper, "_k8s_request")
@mock.patch.object(helper, "urlopen")
def test_all_signals_green_becomes_ready(urlopen_mock, k8s_mock, _):
    """All four signals green (+ stabilization pass) → launch-ready.

    Positive case: pods ready, ping-200 + capacity, and authenticated reads
    all succeed. Verifies the fix accepts launch-ready state correctly.
    """
    # Both Deployments ready
    k8s_mock.side_effect = drained_cluster()

    # All urlopen calls succeed
    ping_ok = _ping_response(capacity=20)
    job_ok = _job_templates_response(status=200)

    # EXACTLY four AWX calls: two complete snapshots of (ping, job_templates).
    # No spare response — a fifth entry would hide a sequencing error.
    urlopen_mock.side_effect = [
        ping_ok, job_ok,  # snapshot 1: ping + job_templates green
        ping_ok, job_ok,  # confirmation snapshot after STABILIZATION_DELAY
    ]

    # Should be ready
    result = helper.wait_awx_ready(timeout=5.0)
    assert result is True, \
        "All four signals green should result in launch-ready"
    assert urlopen_mock.call_count == 4, \
        f"two complete snapshots consume exactly four AWX calls; got {urlopen_mock.call_count}"


@mock.patch.object(helper, "_read_secret", return_value="tok")
@mock.patch.object(helper, "_k8s_request")
@mock.patch.object(helper, "urlopen")
def test_transient_auth_failure_re_arms_gate(urlopen_mock, k8s_mock, _):
    """Transient authenticated read failure re-arms the gate (not forgiven forever).

    After ping + capacity are green, a transient 500 on job_templates should
    NOT latch an "authenticated-ready" flag forever; the gate must re-check
    every poll until it succeeds.
    """
    # Both Deployments ready
    k8s_mock.side_effect = drained_cluster()

    ping_ok = _ping_response(capacity=20)
    job_fail = _job_templates_response(status=500)
    job_ok = _job_templates_response(status=200)

    # Use a custom side_effect function to provide responses based on call count.
    # auth_ok_at controls when the job_templates endpoint starts returning 200.
    call_count = [0]
    auth_ok_at = 3  # After this many auth check calls, start returning 200

    def urlopen_side_effect(req, **kwargs):
        call_count[0] += 1
        # Check the request to determine what to return
        req_str = str(req.full_url) if hasattr(req, 'full_url') else str(req)
        if "ping" in req_str:
            return ping_ok
        elif "job_templates" in req_str:
            if call_count[0] >= auth_ok_at:
                return job_ok
            else:
                return job_fail
        # Shouldn't reach here in normal test
        return job_ok

    urlopen_mock.side_effect = urlopen_side_effect

    result = helper.wait_awx_ready(timeout=5.0)
    assert result is True, \
        "Transient auth failure should re-arm gate, eventual success should proceed"


@mock.patch.object(helper, "_read_secret", return_value="tok")
@mock.patch.object(helper, "_k8s_request")
@mock.patch.object(helper, "urlopen")
def test_body_read_reset_during_auth(urlopen_mock, k8s_mock, _):
    """Auth returns 200 header but body read() raises URLError → NOT ready.

    Regression test for #295: the prior implementation returned True on status==200
    without actually reading the body. This test ensures body-read errors are caught.
    """
    k8s_mock.side_effect = drained_cluster()

    ping_ok = _ping_response(capacity=20)

    # Auth returns 200 but read() fails
    job_reset = mock.MagicMock()
    job_reset.__enter__.return_value = job_reset
    job_reset.__exit__.return_value = False
    job_reset.status = 200
    job_reset.read.side_effect = URLError("connection reset by peer")

    urlopen_mock.side_effect = [ping_ok, job_reset, ping_ok, job_reset]

    result = helper.wait_awx_ready(timeout=0.5)
    assert result is False, \
        "Body-read error should prevent ready return"


@mock.patch.object(helper, "_read_secret", return_value="tok")
@mock.patch.object(helper, "_k8s_request")
@mock.patch.object(helper, "urlopen")
def test_invalid_json_in_auth_response(urlopen_mock, k8s_mock, _):
    """Auth returns 200 but body is invalid JSON → NOT ready."""
    k8s_mock.side_effect = drained_cluster()

    ping_ok = _ping_response(capacity=20)

    # Auth returns 200 but body is not valid JSON
    job_invalid = mock.MagicMock()
    job_invalid.__enter__.return_value = job_invalid
    job_invalid.__exit__.return_value = False
    job_invalid.status = 200
    job_invalid.read.return_value = b"not valid json"

    urlopen_mock.side_effect = [ping_ok, job_invalid, ping_ok, job_invalid]

    result = helper.wait_awx_ready(timeout=0.5)
    assert result is False, \
        "Invalid JSON should prevent ready return"


@mock.patch.object(helper, "_read_secret", return_value="tok")
@mock.patch.object(helper, "_k8s_request")
@mock.patch.object(helper, "urlopen")
def test_missing_count_field_in_auth(urlopen_mock, k8s_mock, _):
    """Auth returns 200 + valid JSON but missing 'count' field → NOT ready."""
    k8s_mock.side_effect = drained_cluster()

    ping_ok = _ping_response(capacity=20)

    # Auth returns 200 + JSON but lacks 'count' field
    job_invalid = mock.MagicMock()
    job_invalid.__enter__.return_value = job_invalid
    job_invalid.__exit__.return_value = False
    job_invalid.status = 200
    job_invalid.read.return_value = json.dumps({"results": []}).encode()

    urlopen_mock.side_effect = [ping_ok, job_invalid, ping_ok, job_invalid]

    result = helper.wait_awx_ready(timeout=0.5)
    assert result is False, \
        "Missing 'count' field should prevent ready return"


@mock.patch.object(helper, "_read_secret", return_value="tok")
@mock.patch.object(helper, "_k8s_request")
@mock.patch.object(helper, "urlopen")
def test_regression_task_readiness_between_snapshots(urlopen_mock, k8s_mock, _):
    """First snapshot all green, then task readiness regresses → NOT ready.

    Regression test: the first poll is fully ready, but after the stabilization
    delay the task deployment no longer has a Ready pod. Must NOT return true.
    """
    ping_ok = _ping_response(capacity=20)
    job_ok = _job_templates_response(status=200)

    cluster = drained_cluster()

    def regress_task(cl, method, path, n):
        # Snapshot 1 reads web then task; from the confirmation snapshot on,
        # the task pod is no longer Ready.
        if cl.deployment_get_count("task") >= 2:
            cl.task.pods = [pod("awx-task-restarting", ready=False)]

    cluster.on_call = regress_task
    k8s_mock.side_effect = cluster
    urlopen_mock.side_effect = itertools.cycle([ping_ok, job_ok])

    result = helper.wait_awx_ready(timeout=5.0)
    assert result is False, \
        "Task regression between snapshots should prevent ready return"


@mock.patch.object(helper, "_read_secret", return_value="tok")
@mock.patch.object(helper, "_k8s_request")
@mock.patch.object(helper, "urlopen")
def test_regression_controlplane_capacity_between_snapshots(urlopen_mock, k8s_mock, _):
    """First snapshot all green, then controlplane capacity drops to 0 → NOT ready."""
    k8s_mock.side_effect = drained_cluster()

    ping_ok = _ping_response(capacity=20)
    ping_fail = _ping_response(capacity=0)  # capacity drops post-stabilization
    job_ok = _job_templates_response(status=200)

    # Sequence: ping ok → after delay, ping shows 0 capacity — and STAYS
    # regressed (capacity never recovers). api_ok's short-circuit means
    # auth is never re-probed once capacity is 0, so only "ping" values are
    # consumed from here on; repeat ping_fail forever so the poll loop can
    # run all the way to the timeout deadline without exhausting the mock.
    urlopen_mock.side_effect = itertools.chain(
        [ping_ok, job_ok, ping_fail, job_ok], itertools.repeat(ping_fail)
    )

    result = helper.wait_awx_ready(timeout=5.0)
    assert result is False, \
        "Controlplane capacity regression between snapshots should prevent ready return"


@mock.patch.object(helper, "_read_secret", return_value="tok")
@mock.patch.object(helper, "_k8s_request")
@mock.patch.object(helper, "urlopen")
def test_regression_auth_fails_between_snapshots(urlopen_mock, k8s_mock, _):
    """First snapshot all green, then auth returns 500 → NOT ready."""
    k8s_mock.side_effect = drained_cluster()

    ping_ok = _ping_response(capacity=20)
    job_ok = _job_templates_response(status=200)
    job_fail = _job_templates_response(status=500)

    # Sequence: all green → after delay, auth fails with 500 — and STAYS
    # failing (ping keeps succeeding, so api_ok stays True and auth is
    # re-probed every poll; repeat the ping/job_fail pair forever so the
    # poll loop can run all the way to the timeout deadline without
    # exhausting the mock).
    urlopen_mock.side_effect = itertools.chain(
        [ping_ok, job_ok, ping_ok, job_fail], itertools.cycle([ping_ok, job_fail])
    )

    result = helper.wait_awx_ready(timeout=5.0)
    assert result is False, \
        "Auth regression between snapshots should prevent ready return"


@mock.patch.object(helper, "_read_secret", return_value="tok")
@mock.patch.object(helper, "_k8s_request")
@mock.patch.object(helper, "urlopen")
def test_regression_web_readiness_between_snapshots(urlopen_mock, k8s_mock, _):
    """First snapshot all green, then WEB readiness regresses → NOT ready.

    Symmetric counterpart to the task-readiness regression test: the
    confirmation snapshot must re-read BOTH Deployments, not just the task
    one. An implementation that latched web_ready from the first snapshot
    would return True here.
    """
    ping_ok = _ping_response(capacity=20)
    job_ok = _job_templates_response(status=200)

    cluster = drained_cluster()

    def regress_web(cl, method, path, n):
        if cl.deployment_get_count("web") >= 2:
            cl.web.pods = [pod("awx-web-restarting", ready=False)]

    cluster.on_call = regress_web
    k8s_mock.side_effect = cluster
    # web_ready False short-circuits ping/auth, so no further AWX calls are
    # consumed after the first snapshot; repeat anyway so the poll loop can
    # run to the deadline without exhausting the mock.
    urlopen_mock.side_effect = itertools.cycle([ping_ok, job_ok])

    result = helper.wait_awx_ready(timeout=5.0)
    assert result is False, \
        "Web regression between snapshots should prevent ready return"


# ---------------------------------------------------------------------------
# Request contract: exact path, identity, headers, call order (#295 re-gate)
# ---------------------------------------------------------------------------

@mock.patch.object(helper, "_read_secret", return_value="tok")
@mock.patch.object(helper, "_k8s_request")
@mock.patch.object(helper, "urlopen")
def test_ready_path_issues_exact_awx_request_sequence(urlopen_mock, k8s_mock, read_secret_mock):
    """Two complete snapshots issue exactly four AWX requests, in order.

    Pins the probe contract the gate claims to exercise:
      - unauthenticated ping at /api/v2/ping/;
      - authenticated read at /api/v2/job_templates/?page_size=1;
      - Authorization built from the token at the awx-svc mount path;
      - ping, auth, ping, auth — four calls, no more, no fewer.
    """
    k8s_mock.side_effect = drained_cluster()
    urlopen_mock.side_effect = [
        _ping_response(capacity=20), _job_templates_response(status=200),
        _ping_response(capacity=20), _job_templates_response(status=200),
    ]

    assert helper.wait_awx_ready(timeout=5.0) is True, "all-green path should be ready"

    requests = [call.args[0] for call in urlopen_mock.call_args_list]
    ping_url = f"{helper.AWX_API_URL}/api/v2/ping/"
    auth_url = f"{helper.AWX_API_URL}/api/v2/job_templates/?page_size=1"
    assert [r.full_url for r in requests] == [ping_url, auth_url, ping_url, auth_url], \
        f"exact URL sequence for two snapshots; got {[r.full_url for r in requests]}"

    for req in requests:
        assert req.get_method() == "GET", "readiness probes must be safe reads"

    # Authenticated reads carry the awx-svc bearer token; ping does not.
    for req in (requests[1], requests[3]):
        assert req.get_header("Authorization") == "Bearer tok", \
            f"auth probe must send the awx-svc bearer token; got {req.header_items()}"
        assert req.get_header("Accept") == "application/json"
    for req in (requests[0], requests[2]):
        assert req.get_header("Authorization") is None, \
            "ping is the unauthenticated probe — it must not carry a token"

    # The token comes from the awx-svc mount, once per authenticated probe.
    assert helper.AWX_TOKEN_PATH == "/etc/awx-autoscale/secrets/awx-svc-token", \
        "helper identity is awx-svc (NOT the console's dmf-cms-svc)"
    assert read_secret_mock.call_args_list == [mock.call(helper.AWX_TOKEN_PATH)] * 2, \
        f"token read from the awx-svc mount per auth probe; got {read_secret_mock.call_args_list}"


# ---------------------------------------------------------------------------
# Named-controlplane capacity predicate (#295 re-gate)
# ---------------------------------------------------------------------------

@mock.patch.object(helper, "urlopen")
def test_unrelated_group_capacity_does_not_satisfy_ping(urlopen_mock):
    """Unrelated group capacity > 0 while controlplane == 0 → NOT ready.

    This is the discriminator against an `any(group.capacity > 0)`
    implementation: an execution-node pool that has already heartbeat must
    not stand in for the control plane.
    """
    urlopen_mock.return_value = _ping_response_groups([
        {"name": "controlplane", "capacity": 0, "instances": []},
        {"name": "execution-pool", "capacity": 40, "instances": ["exec-1"]},
        {"name": "default", "capacity": 12, "instances": ["exec-1"]},
    ])
    assert helper._check_awx_api_ping() is False, \
        "capacity on an unrelated instance_group must not satisfy the control-plane gate"


@mock.patch.object(helper, "_read_secret", return_value="tok")
@mock.patch.object(helper, "_k8s_request")
@mock.patch.object(helper, "urlopen")
def test_unrelated_group_capacity_snapshot_not_ready(urlopen_mock, k8s_mock, _):
    """Same discriminator at snapshot level: pods up, unrelated group hot,
    controlplane still at zero → snapshot is not ready and auth is never
    probed."""
    k8s_mock.side_effect = drained_cluster()
    urlopen_mock.return_value = _ping_response_groups([
        {"name": "controlplane", "capacity": 0, "instances": []},
        {"name": "execution-pool", "capacity": 40, "instances": ["exec-1"]},
    ])

    snapshot = helper.check_launch_ready_snapshot()
    assert snapshot["api_ok"] is False, f"controlplane at zero is not api_ok; got {snapshot}"
    assert snapshot["all_ready"] is False, "snapshot must not be ready"


@mock.patch.object(helper, "urlopen")
def test_controlplane_capacity_satisfies_ping_when_other_groups_zero(urlopen_mock):
    """controlplane > 0 with every other group at zero → ready.

    Positive counterpart: the gate must not require the unrelated pools to
    be hot before it lets a wake through.
    """
    urlopen_mock.return_value = _ping_response_groups([
        {"name": "default", "capacity": 0, "instances": []},
        {"name": "controlplane", "capacity": 20, "instances": ["awx-task-1"]},
        {"name": "container-group", "capacity": 0, "instances": []},
    ])
    assert helper._check_awx_api_ping() is True, \
        "positive named controlplane capacity is the ready signal"


@mock.patch.object(helper, "urlopen")
def test_missing_controlplane_group_not_ready(urlopen_mock):
    """No controlplane group registered at all → NOT ready."""
    urlopen_mock.return_value = _ping_response_groups([
        {"name": "default", "capacity": 40, "instances": ["exec-1"]},
    ])
    assert helper._check_awx_api_ping() is False, \
        "an absent controlplane group is not a ready signal"


@pytest.mark.parametrize("groups", [
    pytest.param([{"name": "controlplane", "capacity": True}], id="capacity-true"),
    pytest.param([{"name": "controlplane", "capacity": "20"}], id="capacity-string"),
    pytest.param([{"name": "controlplane", "capacity": None}], id="capacity-null"),
    pytest.param([{"name": "controlplane", "capacity": -1}], id="capacity-negative"),
    pytest.param([{"name": "controlplane"}], id="capacity-missing"),
    pytest.param(["controlplane"], id="group-not-an-object"),
    pytest.param({}, id="instance-groups-not-a-list"),
])
@mock.patch.object(helper, "urlopen")
def test_malformed_ping_capacity_is_not_ready(urlopen_mock, groups):
    """Malformed/typed-wrong capacity yields a plain False snapshot.

    `isinstance(True, int)` is true in Python, so `"capacity": true` would
    otherwise read as capacity 1 and pass the gate. Malformed shapes must
    also produce False rather than an escaping AttributeError.
    """
    urlopen_mock.return_value = _ping_response_groups(groups)
    assert helper._check_awx_api_ping() is False, \
        f"malformed capacity/groups must not be ready: {groups!r}"


# ---------------------------------------------------------------------------
# Authenticated collection schema — typed validation (#295 re-gate, A-B1)
# ---------------------------------------------------------------------------

_INVALID_AUTH_BODIES = [
    pytest.param('{"count": null, "results": []}', id="count-null"),
    pytest.param('{"count": "5", "results": []}', id="count-string"),
    pytest.param('{"count": "five", "results": {}}', id="count-string-results-object"),
    pytest.param('{"count": true, "results": []}', id="count-bool"),
    pytest.param('{"count": -1, "results": []}', id="count-negative"),
    pytest.param('{"count": 1.5, "results": []}', id="count-float"),
    pytest.param('{"count": 0, "results": null}', id="results-null"),
    pytest.param('{"count": null, "results": null}', id="both-null"),
    pytest.param('{"count": 0, "results": {}}', id="results-object"),
    pytest.param('{"count": 0, "results": "[]"}', id="results-string"),
    pytest.param('{"count": 0}', id="results-missing"),
    pytest.param('{"results": []}', id="count-missing"),
    pytest.param('{"count": 1, "results": ["not-an-object"]}', id="result-item-not-an-object"),
    pytest.param('[]', id="top-level-list"),
    pytest.param('"a string"', id="top-level-string"),
    pytest.param('null', id="top-level-null"),
    pytest.param('', id="empty-body"),
]


@pytest.mark.parametrize("raw_body", _INVALID_AUTH_BODIES)
@mock.patch.object(helper, "_read_secret", return_value="tok")
@mock.patch.object(helper, "urlopen")
def test_auth_rejects_non_collection_body(urlopen_mock, _, raw_body):
    """HTTP 200 with a body that is not an AWX paginated collection → NOT ready.

    Key presence is not the contract. Before this gate, direct probes
    accepted `{"count": null, "results": null}` and
    `{"count": "five", "results": {}}` as authenticated-ready — neither can
    support the console's lookup semantics.
    """
    urlopen_mock.return_value = _auth_body_response(raw_body)
    assert helper._check_awx_authenticated_ready() is False, \
        f"non-collection body must not be authenticated-ready: {raw_body!r}"


@pytest.mark.parametrize("raw_body", [
    pytest.param('{"count": 0, "results": []}', id="valid-empty"),
    pytest.param('{"count": 2, "results": [{"id": 1, "name": "a"}, {"id": 2, "name": "b"}]}',
                 id="valid-non-empty"),
    pytest.param('{"count": 1, "next": null, "previous": null, "results": [{"id": 7}]}',
                 id="valid-full-pagination"),
])
@mock.patch.object(helper, "_read_secret", return_value="tok")
@mock.patch.object(helper, "urlopen")
def test_auth_accepts_valid_collection_body(urlopen_mock, _, raw_body):
    """A well-formed AWX paginated collection (empty or not) IS ready."""
    urlopen_mock.return_value = _auth_body_response(raw_body)
    assert helper._check_awx_authenticated_ready() is True, \
        f"valid collection must be authenticated-ready: {raw_body!r}"


@mock.patch.object(helper, "_read_secret", return_value="tok")
@mock.patch.object(helper, "_k8s_request")
@mock.patch.object(helper, "urlopen")
def test_typed_schema_failure_keeps_gate_closed_end_to_end(urlopen_mock, k8s_mock, _):
    """A persistently null-typed collection never opens the gate.

    End-to-end counterpart of the direct schema tests: pods up, ping green,
    auth 200 — but the body is `{"count": null, "results": null}` forever.
    wait_awx_ready must time out rather than declare AWX launch-ready.
    """
    k8s_mock.side_effect = drained_cluster()
    urlopen_mock.side_effect = itertools.cycle([
        _ping_response(capacity=20),
        _auth_body_response('{"count": null, "results": null}'),
    ])

    result = helper.wait_awx_ready(timeout=2.0)
    assert result is False, \
        "null-typed collection must never satisfy the authenticated gate"
