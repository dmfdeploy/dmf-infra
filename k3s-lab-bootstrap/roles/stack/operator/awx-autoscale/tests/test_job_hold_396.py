"""Discriminating tests for the bounded AWX job-hold API and idle reaper."""
import copy
import importlib.util
import io
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

import pytest

os.environ.setdefault("AWX_AUTOSCALE_NAMESPACE", "awx")
os.environ.setdefault("AWX_AUTOSCALE_AWX_API_URL", "http://awx.test")

_HELPER = Path(__file__).resolve().parents[1] / "files" / "awx_autoscale_helper.py"
_spec = importlib.util.spec_from_file_location("awx_autoscale_helper", _HELPER)
helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helper)
PRINCIPAL = "authorized-rotation-trigger"


def test_reaper_claim_minimum_is_enforced_at_helper_startup():
    module_path = str(_HELPER)
    loader = (
        "import importlib.util; "
        f"spec=importlib.util.spec_from_file_location('helper', {module_path!r}); "
        "module=importlib.util.module_from_spec(spec); spec.loader.exec_module(module)"
    )

    def load_with_claim_seconds(seconds):
        env = os.environ.copy()
        env["AWX_AUTOSCALE_AWX_API_URL"] = "http://awx.test"
        env["AWX_AUTOSCALE_REAPER_CLAIM_SECONDS"] = str(seconds)
        return subprocess.run(
            [sys.executable, "-c", loader], env=env, capture_output=True,
            text=True, check=False)

    below_minimum = load_with_claim_seconds(199)
    assert below_minimum.returncode != 0
    assert "at least 200s" in below_minimum.stderr
    at_minimum = load_with_claim_seconds(200)
    assert at_minimum.returncode == 0, at_minimum.stderr


class StopLoop(Exception):
    pass


class FakeClock:
    def __init__(self, start=1_800_000_000.0, max_sleeps=100, on_sleep=None):
        self.now = start
        self.sleeps = 0
        self.max_sleeps = max_sleeps
        self.on_sleep = on_sleep

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps += 1
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(self)
        if self.sleeps > self.max_sleeps:
            raise StopLoop


class FakeLeaseApi:
    """Coordination Lease fake with resourceVersion compare-and-swap."""

    def __init__(self):
        self.lease = None
        self.version = 0
        self.calls = []
        self.conflict_next_create = None

    def __call__(self, method, path, body=None, **kwargs):
        self.calls.append((method, path))
        if path.endswith(f"/leases/{helper.JOB_HOLD_LEASE_NAME}"):
            if method == "GET":
                if self.lease is None:
                    raise HTTPError(path, 404, "not found", {}, None)
                return copy.deepcopy(self.lease)
            if method == "PUT":
                if self.lease is None:
                    raise HTTPError(path, 404, "not found", {}, None)
                expected = self.lease["metadata"]["resourceVersion"]
                if body["metadata"].get("resourceVersion") != expected:
                    raise HTTPError(path, 409, "conflict", {}, None)
                self.version += 1
                self.lease = copy.deepcopy(body)
                self.lease["metadata"]["resourceVersion"] = str(self.version)
                return copy.deepcopy(self.lease)
        if path.endswith("/leases") and method == "POST":
            if self.conflict_next_create is not None:
                self.version += 1
                self.lease = copy.deepcopy(self.conflict_next_create)
                self.lease["metadata"]["resourceVersion"] = str(self.version)
                self.conflict_next_create = None
                raise HTTPError(path, 409, "created by another helper", {}, None)
            if self.lease is not None:
                raise HTTPError(path, 409, "conflict", {}, None)
            self.version += 1
            self.lease = copy.deepcopy(body)
            self.lease["metadata"]["resourceVersion"] = str(self.version)
            return copy.deepcopy(self.lease)
        raise AssertionError(f"unrouted Lease request: {method} {path}")


def _http_response(payload):
    response = mock.MagicMock()
    response.__enter__.return_value = response
    response.__exit__.return_value = False
    response.read.return_value = json.dumps(payload).encode()
    response.status = 200
    return response


def _active_owner(api):
    return (api.lease or {}).get("spec", {}).get("holderIdentity", "")


def test_job_hold_endpoint_requires_bearer_auth_before_mutation():
    body = json.dumps({"action": "acquire", "run_id": "run-1",
                       "duration_seconds": 10}).encode()
    handler = helper.HelperHandler.__new__(helper.HelperHandler)
    handler.path = "/job-hold"
    handler.headers = {"Authorization": "Bearer wrong", "Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    handler.client_address = ("127.0.0.1", 1234)
    handler._send_json = mock.Mock()

    with mock.patch.object(helper, "_read_secret", return_value="right"), \
            mock.patch.object(helper, "_job_hold") as hold:
        handler.do_POST()

    handler._send_json.assert_called_once_with(401, {"error": "unauthorized"})
    hold.acquire.assert_not_called()


@pytest.mark.parametrize("payload", [
    None,
    {"action": [], "run_id": "run-1"},
    {"action": "bogus", "run_id": "run-1"},
    {"action": "acquire", "run_id": "bad run", "duration_seconds": 10},
    {"action": "acquire", "run_id": "run-1", "duration_seconds": True},
    {"action": "acquire", "run_id": "run-1", "duration_seconds": 0},
    {"action": "acquire", "run_id": "run-1", "duration_seconds": 901},
])
def test_malformed_or_unbounded_hold_requests_fail_closed(payload):
    with mock.patch.object(helper, "JOB_HOLD_MAX_SECONDS", 900):
        code, result = helper.handle_job_hold_request(payload, PRINCIPAL)
    assert code == 400
    assert result["ok"] is False


def test_acquire_conflicts_with_another_live_owner_and_owner_must_renew_or_release():
    clock = FakeClock()
    api = FakeLeaseApi()
    manager = helper.JobHoldLeaseManager()
    with mock.patch.object(helper, "time", clock), \
            mock.patch.object(helper, "_k8s_request", api), \
            mock.patch.object(helper, "check_launch_ready_snapshot",
                              return_value={"all_ready": True}):
        assert manager.acquire("rotation-a", 10, PRINCIPAL) == (True, "hold active")
        assert manager.acquire("rotation-b", 10, PRINCIPAL)[0] is False
        assert manager.acquire("rotation-a", 10, "other-principal")[0] is False
        assert manager.renew("rotation-b", 10, PRINCIPAL)[0] is False
        assert manager.renew("rotation-a", 10, "other-principal")[0] is False
        assert manager.release("rotation-b", PRINCIPAL)[0] is False
        assert manager.renew("rotation-a", 10, PRINCIPAL) == (True, "hold renewed")
        assert manager.release("rotation-a", PRINCIPAL) == (True, "hold released")
        assert manager.release("rotation-a", PRINCIPAL) == (True, "hold already released")
        assert manager.is_active() is False


def test_acquire_requires_awx_ready_and_does_not_create_a_hold_when_asleep():
    api = FakeLeaseApi()
    manager = helper.JobHoldLeaseManager()
    with mock.patch.object(helper, "_k8s_request", api), \
            mock.patch.object(helper, "check_launch_ready_snapshot",
                              return_value={"all_ready": False}):
        ok, detail = manager.acquire("rotation-a", 10, PRINCIPAL)
    assert ok is False
    assert "ensure-awake" in detail
    assert api.lease is None


def test_holder_cannot_renew_after_expiry_and_new_run_can_take_over():
    clock = FakeClock()
    api = FakeLeaseApi()
    manager = helper.JobHoldLeaseManager()
    with mock.patch.object(helper, "time", clock), \
            mock.patch.object(helper, "_k8s_request", api), \
            mock.patch.object(helper, "check_launch_ready_snapshot",
                              return_value={"all_ready": True}):
        assert manager.acquire("rotation-a", 10, PRINCIPAL)[0] is True
        clock.now += 11
        assert manager.is_active() is False
        assert manager.renew("rotation-a", 10, PRINCIPAL)[0] is False
        assert manager.acquire("rotation-b", 10, PRINCIPAL)[0] is True
        assert manager.release("rotation-a", PRINCIPAL)[0] is False
        assert manager.is_active() is True
        assert _active_owner(api) == manager._holder(PRINCIPAL, "rotation-b")


def test_reaper_and_job_must_cas_own_the_lease_before_touching_awx():
    clock = FakeClock()
    api = FakeLeaseApi()
    reaper = helper.JobHoldLeaseManager()
    caller = helper.JobHoldLeaseManager()
    with mock.patch.object(helper, "time", clock), \
            mock.patch.object(helper, "_k8s_request", api), \
            mock.patch.object(helper, "check_launch_ready_snapshot",
                              return_value={"all_ready": True}):
        claimed, owner = reaper.claim_for_reaper()
        assert claimed is True
        assert caller.acquire("rotation-a", 10, PRINCIPAL)[0] is False
        assert helper.JobHoldLeaseManager().claim_for_reaper()[0] is False
        assert caller.release_coordination_claim(owner + "-foreign") is False
        assert reaper.release_coordination_claim(owner) is True
        assert caller.acquire("rotation-a", 10, PRINCIPAL)[0] is True


def test_reaper_cannot_claim_a_live_job_hold():
    clock = FakeClock()
    api = FakeLeaseApi()
    manager = helper.JobHoldLeaseManager()
    with mock.patch.object(helper, "time", clock), \
            mock.patch.object(helper, "_k8s_request", api), \
            mock.patch.object(helper, "check_launch_ready_snapshot",
                              return_value={"all_ready": True}):
        assert manager.acquire("rotation-a", 10, PRINCIPAL)[0] is True
        assert manager.claim_for_reaper()[0] is False
        assert _active_owner(api) == manager._holder(PRINCIPAL, "rotation-a")


def test_reaper_claim_expires_after_crash():
    clock = FakeClock()
    api = FakeLeaseApi()
    manager = helper.JobHoldLeaseManager()
    with mock.patch.object(helper, "time", clock), \
            mock.patch.object(helper, "_k8s_request", api), \
            mock.patch.object(helper, "REAPER_CLAIM_SECONDS", 5):
        claimed, _owner = manager.claim_for_reaper()
        assert claimed is True
        clock.now += 6
        assert manager.is_active() is False
        assert manager.claim_for_reaper()[0] is True


def test_create_conflict_reloads_and_never_overwrites_other_helper_owner():
    clock = FakeClock()
    api = FakeLeaseApi()
    manager = helper.JobHoldLeaseManager()
    now = helper.datetime.fromtimestamp(clock.now, tz=helper.timezone.utc).isoformat()
    api.conflict_next_create = {
        "apiVersion": "coordination.k8s.io/v1",
        "kind": "Lease",
        "metadata": {"name": helper.JOB_HOLD_LEASE_NAME,
                     "namespace": helper.NAMESPACE},
        "spec": {"holderIdentity": manager._holder(PRINCIPAL, "rotation-b"),
                 "leaseDurationSeconds": 10, "acquireTime": now,
                 "renewTime": now},
    }
    with mock.patch.object(helper, "time", clock), \
            mock.patch.object(helper, "_k8s_request", api), \
            mock.patch.object(helper, "check_launch_ready_snapshot",
                              return_value={"all_ready": True}):
        assert manager.acquire("rotation-a", 10, PRINCIPAL)[0] is False

    assert _active_owner(api) == manager._holder(PRINCIPAL, "rotation-b")


def test_concurrent_acquires_leave_one_owner_in_the_named_lease():
    clock = FakeClock()
    api = FakeLeaseApi()
    manager = helper.JobHoldLeaseManager()
    barrier = Barrier(2)

    def acquire(run_id):
        barrier.wait()
        return manager.acquire(run_id, 10, PRINCIPAL)[0]

    with mock.patch.object(helper, "time", clock), \
            mock.patch.object(helper, "_k8s_request", api), \
            mock.patch.object(helper, "check_launch_ready_snapshot",
                              return_value={"all_ready": True}), \
            ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(acquire, ["rotation-a", "rotation-b"]))

    assert sorted(results) == [False, True]
    assert _active_owner(api) in {
        manager._holder(PRINCIPAL, "rotation-a"),
        manager._holder(PRINCIPAL, "rotation-b")}
    assert api.lease["metadata"]["name"] == helper.JOB_HOLD_LEASE_NAME
    assert all(
        helper.JOB_HOLD_LEASE_NAME in path
        for method, path in api.calls if method != "POST")


def test_job_hold_http_dispatch_accepts_authenticated_body_shape():
    body = json.dumps({"action": "renew", "run_id": "run-1",
                       "duration_seconds": 10}).encode()
    handler = helper.HelperHandler.__new__(helper.HelperHandler)
    handler.path = "/job-hold"
    handler.headers = {"Authorization": "Bearer right", "Content-Length": str(len(body))}
    handler.rfile = io.BytesIO(body)
    handler.client_address = ("127.0.0.1", 1234)
    handler._send_json = mock.Mock()

    with mock.patch.object(helper, "_read_secret", return_value="right"), \
            mock.patch.object(helper, "_job_hold") as hold:
        hold.renew.return_value = (True, "hold renewed")
        handler.do_POST()

    principal = __import__("hashlib").sha256(b"right").hexdigest()
    hold.renew.assert_called_once_with("run-1", 10, principal)
    handler._send_json.assert_called_once_with(
        200, {"ok": True, "detail": "hold renewed"})


def test_noop_job_longer_than_wake_grace_renews_hold_until_cleanup():
    """A no-op job outlives the normal wake floor without being slept."""
    api = FakeLeaseApi()
    manager = helper.JobHoldLeaseManager()
    job_done = False
    renewals = []

    clock = FakeClock(max_sleeps=40)
    job_end = clock.now + 45  # normal wake grace is only 30 seconds

    def on_sleep(current):
        nonlocal job_done
        with mock.patch.object(helper, "time", current), \
                mock.patch.object(helper, "_k8s_request", api):
            lease = manager.get()
            if not job_done and current.now >= job_end:
                assert manager.release("noop-rotation", PRINCIPAL)[0] is True
                job_done = True
            elif not job_done:
                assert manager.renew("noop-rotation", 10, PRINCIPAL)[0] is True
                renewals.append(current.now)

    clock.on_sleep = on_sleep
    asleep_at = []
    wake_lease = mock.MagicMock()
    wake_lease.get_min_awake_until.return_value = clock.now + 30

    def urlopen(req, **kwargs):
        url = req.full_url
        if "unified_jobs" in url:
            return _http_response({"count": 0, "results": []})
        if "activity_stream" in url:
            return _http_response({
                "count": 1,
                "results": [{"timestamp": "2000-01-01T00:00:00+00:00"}],
            })
        raise AssertionError(url)

    with mock.patch.object(helper, "JOB_HOLD_MAX_SECONDS", 10), \
            mock.patch.object(helper, "GRACE_PERIOD", 30), \
            mock.patch.object(helper, "REAPER_POLL_INTERVAL", 5), \
            mock.patch.object(helper, "time", clock), \
            mock.patch.object(helper, "_k8s_request", api), \
            mock.patch.object(helper, "_job_hold", manager), \
            mock.patch.object(helper, "_lease", wake_lease), \
            mock.patch.object(helper, "_read_secret", return_value="token"), \
            mock.patch.object(helper, "check_launch_ready_snapshot",
                              return_value={"all_ready": True}), \
            mock.patch.object(helper, "urlopen", side_effect=urlopen), \
            mock.patch.object(helper, "patch_awx_asleep",
                              side_effect=lambda: asleep_at.append(clock.now)):
        assert manager.acquire("noop-rotation", 10, PRINCIPAL)[0] is True
        assert manager.is_job_hold_active(), api.lease
        with pytest.raises(StopLoop):
            helper.idle_reaper_loop()

    assert job_done is True
    assert renewals, "the job hold must be renewed before each short lease expires"
    assert asleep_at and asleep_at[0] >= job_end + 30


def test_reaper_releases_its_claim_when_sleep_patch_fails():
    api = FakeLeaseApi()
    manager = helper.JobHoldLeaseManager()
    clock = FakeClock(max_sleeps=5)
    wake_lease = mock.MagicMock()
    wake_lease.get_min_awake_until.return_value = 0.0

    def urlopen(req, **kwargs):
        if "unified_jobs" in req.full_url:
            return _http_response({"count": 0, "results": []})
        if "activity_stream" in req.full_url:
            return _http_response({
                "count": 1,
                "results": [{"timestamp": "2000-01-01T00:00:00+00:00"}],
            })
        raise AssertionError(req.full_url)

    with mock.patch.object(helper, "JOB_HOLD_MAX_SECONDS", 10), \
            mock.patch.object(helper, "GRACE_PERIOD", 1), \
            mock.patch.object(helper, "REAPER_POLL_INTERVAL", 1), \
            mock.patch.object(helper, "time", clock), \
            mock.patch.object(helper, "_k8s_request", api), \
            mock.patch.object(helper, "_job_hold", manager), \
            mock.patch.object(helper, "_lease", wake_lease), \
            mock.patch.object(helper, "_read_secret", return_value="token"), \
            mock.patch.object(helper, "urlopen", side_effect=urlopen), \
            mock.patch.object(helper, "patch_awx_asleep",
                              side_effect=OSError("synthetic patch failure")), \
            mock.patch.object(helper.logger, "exception"):
        with pytest.raises(StopLoop):
            helper.idle_reaper_loop()
        assert manager.is_active() is False


def test_reaper_rereads_wake_floor_after_distributed_claim():
    """A wake published after the early park check must defer final sleep."""
    clock = FakeClock(max_sleeps=6)
    wake_lease = mock.MagicMock()
    floor = [0.0]
    wake_lease.get_min_awake_until.side_effect = lambda: floor[0]
    job_hold = mock.MagicMock()
    job_hold.is_job_hold_active.return_value = False

    def claim():
        floor[0] = clock.now + 30
        return True, "test-reaper"

    job_hold.claim_for_reaper.side_effect = claim
    work = lambda: helper.WORK_IDLE
    activity = lambda _age: helper.ACTIVITY_QUIET
    asleep = []
    with mock.patch.object(helper, "time", clock), \
            mock.patch.object(helper, "GRACE_PERIOD", 1), \
            mock.patch.object(helper, "REAPER_POLL_INTERVAL", 1), \
            mock.patch.object(helper, "_lease", wake_lease), \
            mock.patch.object(helper, "_job_hold", job_hold), \
            mock.patch.object(helper, "_read_secret", return_value="token"), \
            mock.patch.object(helper, "query_awx_work", side_effect=work), \
            mock.patch.object(helper, "query_awx_activity", side_effect=activity), \
            mock.patch.object(helper, "patch_awx_asleep",
                              side_effect=lambda: asleep.append(clock.now)), \
            mock.patch.object(helper, "audit"):
        with pytest.raises(StopLoop):
            helper.idle_reaper_loop()

    assert asleep == []
    job_hold.release_coordination_claim.assert_called_once_with("test-reaper")


def test_wake_retries_after_reaper_claim_then_publishes_floor():
    api = FakeLeaseApi()
    manager = helper.JobHoldLeaseManager()
    with mock.patch.object(helper, "_k8s_request", api):
        claimed, owner = manager.claim_for_reaper()
        assert claimed

        state, _ = manager.claim_for_wake()
        assert state == "busy"
        assert manager.release_coordination_claim(owner)
        state, wake_owner = manager.claim_for_wake()
        assert state == "acquired"
        assert wake_owner != owner
        assert manager.release_coordination_claim(wake_owner)


def test_ensure_awake_waits_under_live_job_hold_without_rewriting_wake_floor():
    hold_owner = "awx-job-authorized-run"
    job_hold = mock.MagicMock()
    job_hold.claim_for_wake.return_value = ("job-hold", hold_owner)
    wake_lease = mock.MagicMock()
    with mock.patch.object(helper, "_wake_lock", helper.threading.Lock()), \
            mock.patch.object(helper, "_job_hold", job_hold), \
            mock.patch.object(helper, "_lease", wake_lease), \
            mock.patch.object(helper, "wait_awx_ready", return_value=True):
        result = helper.do_ensure_awake()
    assert result["ok"] is True
    assert "active job hold" in result["detail"]
    wake_lease.create_or_update.assert_not_called()
    job_hold.release_coordination_claim.assert_not_called()


def test_concurrent_helpers_cannot_both_win_wake_claim():
    api = FakeLeaseApi()
    first = helper.JobHoldLeaseManager()
    second = helper.JobHoldLeaseManager()
    barrier = Barrier(2)

    def claim(manager):
        barrier.wait()
        return manager.claim_for_wake()

    with mock.patch.object(helper, "MAX_STARTUP_WAIT", 0.02), \
            mock.patch.object(helper, "_k8s_request", api), \
            mock.patch.object(helper.time, "sleep", return_value=None), \
            ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(claim, (first, second)))
        assert sum(state == "acquired" for state, _ in results) == 1
        assert sum(state == "busy" for state, _ in results) == 1
