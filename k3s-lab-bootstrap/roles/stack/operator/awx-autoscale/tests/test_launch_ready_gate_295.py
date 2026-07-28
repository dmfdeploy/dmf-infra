"""Discriminating tests for #295: AWX launch-ready gate (not just ping-200).

Issue #295: reproduced live against AWX 24.6.1 — a real deploy dispatched the
instant readyReplicas >= 1 + ping-200 went green hit a bare Django Server Error
(500) on the very next authenticated call (job template lookup). ping-200 alone
is insufficient; must also check:
  - instance_group capacity > 0 (not just ping-200)
  - authenticated read succeeds (job_templates endpoint, the exact code path
    the console's post-wake lookup depends on)

Tests verify: ping-200 + capacity-0 or job-templates-503 are NOT launch-ready.
Run:
  python3 -m pytest tests/test_launch_ready_gate_295.py -v
"""
import importlib.util
import json
import os
import time
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

# The helper reads these at import time (no defaults); set test values first.
os.environ.setdefault("AWX_AUTOSCALE_NAMESPACE", "awx")
os.environ.setdefault("AWX_AUTOSCALE_AWX_API_URL", "http://awx.test")
os.environ.setdefault("AWX_AUTOSCALE_WAKE_POLL_INTERVAL", "1")  # speed up polls (still int)
os.environ.setdefault("AWX_AUTOSCALE_STABILIZATION_DELAY", "0.01")  # speed up stabilization

_HELPER = Path(__file__).resolve().parents[1] / "files" / "awx_autoscale_helper.py"
_spec = importlib.util.spec_from_file_location("awx_autoscale_helper", _HELPER)
helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helper)


def _k8s_deploy_ready(replicas):
    """Mock Kubernetes API response for a Deployment."""
    m = mock.MagicMock()
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    m.read.return_value = json.dumps({
        "status": {"readyReplicas": replicas}
    }).encode()
    return m


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


def _job_templates_response(status=200):
    """Mock job_templates response."""
    m = mock.MagicMock()
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    m.status = status
    if status == 200:
        m.read.return_value = json.dumps({"count": 5}).encode()
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
    k8s_mock.return_value = {"status": {"readyReplicas": 1}}

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
    k8s_mock.return_value = {"status": {"readyReplicas": 1}}

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
    k8s_mock.return_value = {"status": {"readyReplicas": 1}}

    # All urlopen calls succeed
    ping_ok = _ping_response(capacity=20)
    job_ok = _job_templates_response(status=200)

    # Enough calls for: initial poll + stabilization delay + final confirm
    urlopen_mock.side_effect = [
        ping_ok, job_ok,  # first pass: ping + job_templates green
        ping_ok, job_ok,  # stabilization delay: re-check both
        job_ok,           # final confirmation after stabilization
    ]

    # Should be ready
    result = helper.wait_awx_ready(timeout=5.0)
    assert result is True, \
        "All four signals green should result in launch-ready"


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
    k8s_mock.return_value = {"status": {"readyReplicas": 1}}

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
