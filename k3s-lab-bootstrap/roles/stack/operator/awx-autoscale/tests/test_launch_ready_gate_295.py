"""Discriminating test for #295: AWX launch-ready gate discriminates partially-ready states.

Best-effort gate; console-side bounded transient retry is the required companion fix.
"""
import importlib.util
import json
import os
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

os.environ.setdefault("AWX_AUTOSCALE_NAMESPACE", "awx")
os.environ.setdefault("AWX_AUTOSCALE_AWX_API_URL", "http://awx.test")

_HELPER = Path(__file__).resolve().parents[1] / "files" / "awx_autoscale_helper.py"
_spec = importlib.util.spec_from_file_location("awx_autoscale_helper", _HELPER)
helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helper)


def _mock_deployment(ready=True):
    return {"status": {"readyReplicas": 1 if ready else 0}}


def _mock_ping_response(capacity=20):
    m = mock.MagicMock()
    m.status = 200
    m.read.return_value = json.dumps({
        "instances": [{"node": "awx-task-1", "capacity": capacity}],
        "instance_groups": [{"name": "controlplane", "capacity": capacity, "instances": ["awx-task-1"]}],
    }).encode()
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    return m


def _mock_auth_200():
    m = mock.MagicMock()
    m.status = 200
    m.read.return_value = b'{"count": 0, "results": []}'
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    return m


def test_zero_capacity_not_ready():
    """Snapshot rejects AWX when instance_group capacity is 0 (#295)."""
    with mock.patch.object(helper, "_k8s_request", return_value=_mock_deployment()):
        with mock.patch("urllib.request.urlopen", return_value=_mock_ping_response(capacity=0)):
            snapshot = helper.check_launch_ready_snapshot()
            assert snapshot["api_ok"] is False, "zero-capacity fails ping check"
            assert snapshot["all_ready"] is False, "not ready with zero capacity"


def test_auth_500_not_ready():
    """Snapshot rejects AWX when authenticated read 500s (#295)."""
    def urlopen_side_effect(*args, **kwargs):
        if "job_templates" in str(args[0] if args else ""):
            raise HTTPError("http://awx.test/api/v2/job_templates/", 500, "ISE", {}, None)
        return _mock_ping_response(capacity=20)

    with mock.patch.object(helper, "_k8s_request", return_value=_mock_deployment()):
        with mock.patch("urllib.request.urlopen", side_effect=urlopen_side_effect):
            with mock.patch.object(helper, "_read_secret", return_value="tok"):
                snapshot = helper.check_launch_ready_snapshot()
                assert snapshot["auth_ok"] is False, f"auth 500 fails auth check; got {snapshot}"
                assert snapshot["all_ready"] is False, "not ready on auth error"


def test_all_green_snapshot():
    """Snapshot succeeds when all four signals green."""
    with mock.patch.object(helper, "_k8s_request", return_value=_mock_deployment()):
        with mock.patch("urllib.request.urlopen", return_value=_mock_ping_response(capacity=20)):
            with mock.patch.object(helper, "_read_secret", return_value="tok"):
                snapshot = helper.check_launch_ready_snapshot()
                assert snapshot["web_ready"] is True, "web deployment ready"
                assert snapshot["task_ready"] is True, "task deployment ready"
                assert snapshot["api_ok"] is True, "ping + capacity ok"
                assert snapshot["auth_ok"] is True, "auth ok"
                assert snapshot["all_ready"] is True, "all signals green"


def test_wait_requires_two_snapshots():
    """wait_awx_ready requires two complete green snapshots separated by stabilization."""
    # Track call counts to verify snapshots are re-checked
    call_counts = {"snapshots": 0}

    original_snapshot = helper.check_launch_ready_snapshot
    def counting_snapshot():
        call_counts["snapshots"] += 1
        # Simulate: all green for snapshots 1-2, then regress
        if call_counts["snapshots"] <= 2:
            with mock.patch.object(helper, "_k8s_request", return_value=_mock_deployment()):
                with mock.patch("urllib.request.urlopen", return_value=_mock_ping_response(capacity=20)):
                    with mock.patch.object(helper, "_read_secret", return_value="tok"):
                        return original_snapshot()
        # After 2 snapshots, simulate regression
        return {"web_ready": True, "task_ready": True, "api_ok": True, "auth_ok": False, "all_ready": False}

    with mock.patch.object(helper, "check_launch_ready_snapshot", side_effect=counting_snapshot):
        result = helper.wait_awx_ready(timeout=2.0)
        # Should succeed on the second complete snapshot
        assert result is True, "two green snapshots should result in ready"
        assert call_counts["snapshots"] >= 2, "should have checked at least two snapshots"
