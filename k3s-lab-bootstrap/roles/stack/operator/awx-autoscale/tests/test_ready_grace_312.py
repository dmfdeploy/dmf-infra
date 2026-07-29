"""Discriminating tests for #312: the acquire-time startup floor can never
shrink to the (much nearer) ready+grace deadline.

THE BUG: do_ensure_awake's acquire step writes the Lease floor to
now + MAX_STARTUP_WAIT (~1200s, a generous ceiling for how long a cold wake
might take). Once wait_awx_ready() actually returns True — often in 16-25s —
the post-ready branch tried to rewrite the floor to ready + GRACE_PERIOD
(~300s after ready), but used LeaseManager.create_or_update's default
max(existing, requested) semantics, which can only ever EXTEND a floor, never
replace it with something nearer. So the acquire-time ~1200s floor survived
every wake untouched, and AWX stayed pinned awake for ~20 minutes regardless
of how fast readiness actually arrived. Confirmed live by prediction (#312);
latent since the original helper commit (a3fc93c, 2026-06-20) — NOT a #298
regression, since #298 only touched the readiness gate inside
wait_awx_ready(), which runs strictly before this write.

THE FIX: LeaseManager.create_or_update gains keyword-only
allow_owner_shrink=False. Only the do_ensure_awake post-ready branch passes
it True — and only a call from the SAME holder that currently owns an
unexpired lease gets the true-rewrite behavior; a different holder's active
lease is refused exactly as before, flag or no flag. That preserves the
intended invariant (active-wake ownership: a non-expired lease held by
another holder must never be stolen or shortened) while finally letting the
SAME holder replace its own acquire-time floor with the real ready+grace
deadline.

T1 and T3 below are written to FAIL against the pre-fix code and PASS after
it — see each test's own docstring for the pre-fix failure mode.

Run:
  python3 -m pytest tests/test_ready_grace_312.py -v
"""
import importlib.util
import os
from pathlib import Path
from unittest import mock

import pytest

os.environ.setdefault("AWX_AUTOSCALE_NAMESPACE", "awx")
os.environ.setdefault("AWX_AUTOSCALE_AWX_API_URL", "http://awx.test")

_HELPER = Path(__file__).resolve().parents[1] / "files" / "awx_autoscale_helper.py"
_spec = importlib.util.spec_from_file_location("awx_autoscale_helper", _HELPER)
helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helper)


class _StopLoop(Exception):
    """Breaks idle_reaper_loop's `while True` out of the fake clock."""


class FakeClock:
    """Stands in for the `time` module inside the helper.

    Same shape as test_reaper_activity_298.py's own FakeClock — advancing
    the clock inside sleep() lets a long grace/startup window elapse in
    microseconds and keeps every timestamp commensurable with whatever the
    helper thinks "now" is.
    """

    def __init__(self, start=1_800_000_000.0, max_sleeps=12):
        self.now = start
        self.sleeps = 0
        self.max_sleeps = max_sleeps

    def time(self):
        return self.now

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps += 1
        if self.sleeps > self.max_sleeps:
            raise _StopLoop
        self.now += seconds


class FakeLeaseApi:
    """In-memory fake for the coordination.k8s.io Lease REST surface.

    LeaseManager.get()/create_or_update() call the module-level
    `_k8s_request(method, path, body=None, *, timeout=10.0)` function
    directly — dropping a callable object in for it via
    mock.patch.object(helper, "_k8s_request", ...) intercepts every
    GET/POST/PUT the same way tests/fake_k8s.py's FakeCluster does for
    Deployments/Pods (see that module's own docstring for the pattern).

    Deliberately NOT thread-safe and NOT CAS-aware beyond a plain
    create/exists check — every test here drives the LeaseManager from a
    single thread, so a real optimistic-concurrency model buys nothing.
    """

    def __init__(self):
        self.lease: dict | None = None

    def __call__(self, method, path, body=None, *, timeout=10.0):
        if method == "GET":
            if self.lease is None:
                raise helper.HTTPError(path, 404, "not found", {}, None)
            return self.lease
        if method == "POST":
            if self.lease is not None:
                raise helper.HTTPError(path, 409, "conflict", {}, None)
            self.lease = body
            return self.lease
        if method == "PUT":
            self.lease = body
            return self.lease
        raise AssertionError(f"FakeLeaseApi: unexpected method {method}")

    def annotation(self) -> float:
        """Read back the persisted min-awake-until annotation as a float."""
        if self.lease is None:
            return 0.0
        return helper.LeaseManager._annotation_min_awake(self.lease)

    def holder(self) -> str:
        if self.lease is None:
            return ""
        return self.lease.get("spec", {}).get("holderIdentity", "")


# ---------------------------------------------------------------------------
# T1 — the post-ready write must SHRINK the acquire-time startup floor down
# to the much nearer ready+grace deadline.
# ---------------------------------------------------------------------------

def test_t1_post_ready_write_shrinks_to_grace_floor():
    """MUST FAIL pre-fix (#312).

    Pre-fix: create_or_update's ONLY mode is max(existing, requested), so
    the post-ready write (ready+GRACE_PERIOD, a NEARER deadline than the
    acquire-time now+MAX_STARTUP_WAIT floor already on the Lease) is a
    no-op — the persisted annotation stays at the acquire-time floor,
    start+MAX_STARTUP_WAIT, which fails both assertions below (it is not
    start+25+GRACE_PERIOD, and it is not < start+MAX_STARTUP_WAIT — it IS
    start+MAX_STARTUP_WAIT).

    Post-fix: the post-ready branch passes allow_owner_shrink=True as the
    SAME holder that just acquired, so the floor is genuinely REPLACED.
    """
    clock = FakeClock()
    start = clock.now
    lease_api = FakeLeaseApi()

    def fake_wait_ready(timeout):
        clock.sleep(25)  # readiness arrives 25s after acquire
        return True

    with mock.patch.object(helper, "time", clock), \
            mock.patch.object(helper, "_k8s_request", lease_api), \
            mock.patch.object(helper, "patch_awx_awake", return_value=None), \
            mock.patch.object(helper, "wait_awx_ready", side_effect=fake_wait_ready), \
            mock.patch.object(helper, "audit") as audit_mock:
        result = helper.do_ensure_awake()

    assert result["ok"] is True

    persisted = lease_api.annotation()
    expected = start + 25 + helper.GRACE_PERIOD
    assert persisted == expected, (
        f"persisted min-awake-until={persisted} expected={expected} "
        "(ready+GRACE_PERIOD, not the acquire-time startup floor)"
    )
    assert persisted < start + helper.MAX_STARTUP_WAIT, (
        f"persisted floor {persisted} must shrink below the acquire-time "
        f"startup floor {start + helper.MAX_STARTUP_WAIT} — a wake that "
        "became ready quickly must not stay pinned awake for the full "
        "startup budget"
    )

    ready_events = [c for c in audit_mock.call_args_list
                    if c.args and c.args[0] == "ensure_awake_ready"]
    assert len(ready_events) == 1, \
        f"expected exactly one ensure_awake_ready audit event; got {audit_mock.call_args_list}"
    audited = ready_events[0].kwargs
    assert audited.get("min_awake_until") == persisted, (
        "the audited min_awake_until must equal what was actually "
        f"PERSISTED (audited={audited.get('min_awake_until')}, "
        f"persisted={persisted}) — not merely the locally-computed "
        "attempted value"
    )
    assert audited.get("requested_min_awake_until") == expected
    assert audited.get("holder")


# ---------------------------------------------------------------------------
# T2 — invariant guard: a different holder's active lease is never stolen or
# shortened, even with allow_owner_shrink exposed.
# ---------------------------------------------------------------------------

def test_t2_owner_mismatch_never_shrinks_even_with_flag():
    """allow_owner_shrink must NEVER let a different holder steal or shorten
    an active (unexpired) lease — only the CURRENT owner may rewrite.

    This is the invariant the frozen design explicitly preserves: it
    collapses concurrent /ensure-awake callers into one, keeps non-holders
    waiting through a cold wake/drain, and stops the reaper racing a fresh
    wake. Passes on both pre-fix and post-fix code (the owner check is
    untouched either way) — a guard against a WRONG fix that honored the
    flag for every caller, not a discriminator for THIS fix's presence.
    """
    clock = FakeClock()
    lease_api = FakeLeaseApi()
    holder_a = "holder-A"
    original_min_awake = clock.now + 1200

    with mock.patch.object(helper, "time", clock), \
            mock.patch.object(helper, "_k8s_request", lease_api):
        _, acquired_a = helper._lease.create_or_update(
            original_min_awake, holder_a)
        assert acquired_a is True, "holder A must win the initial acquire"

        result, acquired_b = helper._lease.create_or_update(
            clock.now + 300, "holder-B", allow_owner_shrink=True)

    assert acquired_b is False, \
        "a different holder must never acquire an active lease, flag or no flag"
    persisted = lease_api.annotation()
    assert persisted == original_min_awake, (
        f"a different holder must never shrink (or extend) an active "
        f"lease's floor (persisted={persisted}, "
        f"expected unchanged={original_min_awake})"
    )
    assert result.get("spec", {}).get("holderIdentity") == holder_a, \
        "holder identity must remain the ORIGINAL owner's"
    assert lease_api.holder() == holder_a


# ---------------------------------------------------------------------------
# T3 — reaper parked logging: an audit trail while parked, no per-poll spam,
# and zero AWX queries spent while the floor already says wait.
# ---------------------------------------------------------------------------

def _run_reaper_312(clock, lease_min_awake):
    """Drive idle_reaper_loop with a parked (unexpired) wake Lease floor.

    Returns (audit_mock, work_calls, activity_calls). The query functions
    are patched to RECORD-AND-RETURN-BENIGN rather than raise: anything
    raised from inside them would be swallowed by idle_reaper_loop's own
    `except Exception: logger.exception(...)` per-iteration handler and
    never surface as a test failure — recording calls and asserting the
    list stays empty afterward is the reliable way to prove they were
    never reached.
    """
    lease = mock.MagicMock()
    lease.get_min_awake_until.return_value = lease_min_awake

    work_calls = []
    activity_calls = []

    def _record_work():
        work_calls.append(clock.now)
        return helper.WORK_IDLE

    def _record_activity(max_age):
        activity_calls.append(clock.now)
        return helper.ACTIVITY_QUIET

    with mock.patch.object(helper, "time", clock), \
            mock.patch.object(helper, "_lease", lease), \
            mock.patch.object(helper, "query_awx_work", side_effect=_record_work), \
            mock.patch.object(helper, "query_awx_activity", side_effect=_record_activity), \
            mock.patch.object(helper, "audit") as audit_mock:
        with pytest.raises(_StopLoop):
            helper.idle_reaper_loop()

    return audit_mock, work_calls, activity_calls


def test_t3_parked_reaper_emits_audit_and_makes_no_awx_queries():
    """MUST FAIL pre-fix (#312).

    Pre-fix: the parked branch (`if now < min_awake: idle_since = None;
    continue`) already made zero AWX queries — that part passes either
    way — but emitted NO audit event at all. An operator watching the log
    during a long park (the common case right after this fix ships, since
    the lease floor is now correctly much nearer) has no signal the
    reaper is even aware of the lease; `parked_events` is empty and the
    `assert parked_events` below fails.

    Post-fix: exactly ONE reaper_window_parked event fires — on the
    transition into parked — even though the fake clock drives several
    poll iterations at the SAME unchanged floor (no per-poll spam).
    """
    clock = FakeClock(max_sleeps=6)
    lease_floor = clock.now + 1200

    audit_mock, work_calls, activity_calls = _run_reaper_312(clock, lease_floor)

    assert work_calls == [], \
        f"a parked reaper must never query active work; got {work_calls}"
    assert activity_calls == [], \
        f"a parked reaper must never query the activity stream; got {activity_calls}"

    parked_events = [c for c in audit_mock.call_args_list
                     if c.args and c.args[0] == "reaper_window_parked"]
    assert parked_events, (
        "expected a reaper_window_parked audit event while parked — "
        "an operator must be able to see the reaper is aware of the "
        "lease, not just silently skipping"
    )
    assert len(parked_events) == 1, (
        f"expected exactly one reaper_window_parked event across "
        f"{clock.sleeps} polls at an unchanged floor (no per-poll spam); "
        f"got {len(parked_events)}: {parked_events}"
    )
    fields = parked_events[0].kwargs
    assert fields.get("min_awake_until") == lease_floor
    assert "remaining" in fields
