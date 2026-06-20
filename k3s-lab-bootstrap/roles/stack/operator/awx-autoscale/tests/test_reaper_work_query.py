"""Unit tests for the awx-autoscale helper reaper work-query (issue #103).

The reaper must distinguish AWX-unreachable (asleep) from active work, so it
never claims the wake Lease for an unreachable AWX — which starved
/ensure-awake and made AWX un-wakeable. Run:
  python3 -m pytest tests/test_reaper_work_query.py
"""
import importlib.util
import os
from pathlib import Path
from unittest import mock
from urllib.error import URLError

# The helper reads these at import time (no defaults); set test values first.
os.environ.setdefault("AWX_AUTOSCALE_NAMESPACE", "awx")
os.environ.setdefault("AWX_AUTOSCALE_AWX_API_URL", "http://awx.test")

_HELPER = Path(__file__).resolve().parents[1] / "files" / "awx_autoscale_helper.py"
_spec = importlib.util.spec_from_file_location("awx_autoscale_helper", _HELPER)
helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helper)


def _resp(count):
    m = mock.MagicMock()
    m.read.return_value = ('{"count": %d}' % count).encode()
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    m.status = 200
    return m


@mock.patch.object(helper, "_read_secret", return_value="tok")
def test_unreachable_is_not_active(_):
    # #103: old code returned True (active) here -> reaper claimed the lease.
    with mock.patch.object(helper, "urlopen", side_effect=URLError("refused")):
        assert helper.query_awx_work() == helper.WORK_UNREACHABLE


@mock.patch.object(helper, "_read_secret", return_value="tok")
def test_idle_when_reachable_no_jobs(_):
    with mock.patch.object(helper, "urlopen", return_value=_resp(0)):
        assert helper.query_awx_work() == helper.WORK_IDLE


@mock.patch.object(helper, "_read_secret", return_value="tok")
def test_active_when_jobs_present(_):
    with mock.patch.object(helper, "urlopen", return_value=_resp(3)):
        assert helper.query_awx_work() == helper.WORK_ACTIVE


@mock.patch.object(helper, "_read_secret", return_value="tok")
def test_has_active_work_failopen_preserved(_):
    # Back-compat: sleep-safety (unreachable -> don't sleep) still holds.
    with mock.patch.object(helper, "urlopen", side_effect=URLError("x")):
        assert helper.has_active_work() is True
    with mock.patch.object(helper, "urlopen", return_value=_resp(0)):
        assert helper.has_active_work() is False
