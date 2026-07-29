"""Tests for #298 scope B: supplementary activity-stream evidence in the reaper.

READ THIS FIRST — what this does NOT do. It does NOT fix the 693 guillotine.

The reaper measured idleness only via /api/v2/unified_jobs/, so
693-awx-integration.yml — ~152 REST-only tasks that launch nothing — looked
perfectly idle and was slept mid-run (2026-07-29: died on a 503 at "Lookup AWX
inventory source"). Consulting /api/v2/activity_stream/ delays that sleep when
a mutation happens to be visible, but it cannot establish that 693 is running:

  * the stream records MUTATIONS, so a read-only or idempotent stretch of a
    play produces nothing however long it runs;
  * the helper authenticates as awx-svc, deliberately NOT a superuser and NOT
    a system auditor, and AWX 24.6.1 filters the stream for normal users down
    to objects they can read — while 693 mutates under ADMIN credentials, so
    most of its entries are invisible here. Granting awx-svc system auditor to
    close that gap is explicitly rejected.

So a long play CAN still age past the grace period and get slept — whether it
does depends on whether awx-svc happens to keep seeing fresh entries, which
nothing guarantees (test_sustained_visible_mutations_delay_the_sleep pins the
case where it does keep seeing them, and the sleep is duly delayed). Pausing
the autoscaler remains the operator workaround. The real fix is an explicit
renewable hold on the wake Lease, taken by the AWX-consumer phase itself and
released in an always/finally path — tracked as its own issue.

test_visible_mutation_ages_out_and_reaper_still_sleeps below is the honest
anchor for that limitation, and is expected to FLIP when the Lease hold lands.

What this change IS: directionally fail-safe supplementary evidence. It can
only ever EXTEND the wake window, never shorten it. A degraded outcome either
falls back to the job-only behaviour that shipped before it (UNAVAILABLE,
QUIET) or fails open by holding the window open (UNKNOWN, which deliberately
does NOT fall back). Neither can produce an EARLIER sleep than the pre-#298
reaper would have taken.

The contract that must NOT regress (issue #103): an UNREACHABLE AWX must still
not extend the wake Lease, or the reaper starves /ensure-awake and AWX can
never wake. The activity check must not introduce a second route to that bug,
so it is only ever consulted on the reachable-and-job-idle path.

Run:
  python3 -m pytest tests/test_reaper_activity_298.py -v
"""
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

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

    Advancing the clock inside sleep() lets a 300s grace period elapse in
    microseconds, and keeps activity-stream timestamps commensurable with
    whatever the reaper thinks "now" is.
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

    def iso(self, age_seconds):
        """An ISO-8601 stamp `age_seconds` in the past on this clock."""
        return datetime.fromtimestamp(
            self.now - age_seconds, tz=timezone.utc).isoformat()


def _resp(payload, status=200):
    m = mock.MagicMock()
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    m.status = status
    m.read.return_value = (payload if isinstance(payload, bytes)
                           else json.dumps(payload).encode())
    return m


def _jobs(count):
    return _resp({"count": count, "results": []})


def _activity(timestamp):
    return _resp({"count": 1, "results": [{"id": 9, "timestamp": timestamp}]})


def _route(jobs_response, activity_response):
    """urlopen side effect routing on endpoint, order-independent."""
    def side_effect(req, **kwargs):
        url = req.full_url if hasattr(req, "full_url") else str(req)
        if "unified_jobs" in url:
            return (jobs_response() if callable(jobs_response) else jobs_response)
        if "activity_stream" in url:
            if isinstance(activity_response, Exception):
                raise activity_response
            return (activity_response() if callable(activity_response)
                    else activity_response)
        raise AssertionError(f"unexpected request {url}")
    return side_effect


# ---------------------------------------------------------------------------
# query_awx_activity — the predicate itself
# ---------------------------------------------------------------------------

@mock.patch.object(helper, "_read_secret", return_value="tok")
def test_recent_mutation_is_recent(_):
    clock = FakeClock()
    with mock.patch.object(helper, "time", clock), \
            mock.patch.object(helper, "urlopen",
                              return_value=_activity(clock.iso(30))):
        assert helper.query_awx_activity(300) == helper.ACTIVITY_RECENT


@mock.patch.object(helper, "_read_secret", return_value="tok")
def test_old_mutation_is_quiet(_):
    clock = FakeClock()
    with mock.patch.object(helper, "time", clock), \
            mock.patch.object(helper, "urlopen",
                              return_value=_activity(clock.iso(4000))):
        assert helper.query_awx_activity(300) == helper.ACTIVITY_QUIET


@mock.patch.object(helper, "_read_secret", return_value="tok")
def test_clock_skew_into_the_future_reads_as_recent(_):
    """A stamp ahead of our clock yields a negative age — stay awake."""
    clock = FakeClock()
    with mock.patch.object(helper, "time", clock), \
            mock.patch.object(helper, "urlopen",
                              return_value=_activity(clock.iso(-120))):
        assert helper.query_awx_activity(300) == helper.ACTIVITY_RECENT


@mock.patch.object(helper, "_read_secret", return_value="tok")
def test_empty_stream_is_quiet(_):
    with mock.patch.object(helper, "urlopen",
                           return_value=_resp({"count": 0, "results": []})):
        assert helper.query_awx_activity(300) == helper.ACTIVITY_QUIET


@pytest.mark.parametrize("code", [401, 403, 404])
@mock.patch.object(helper, "_read_secret", return_value="tok")
def test_structural_absence_is_unavailable(_, code):
    """No permission / not there → degrade to today's job-only behaviour."""
    err = HTTPError("url", code, "nope", {}, None)
    with mock.patch.object(helper, "urlopen", side_effect=err):
        assert helper.query_awx_activity(300) == helper.ACTIVITY_UNAVAILABLE


@pytest.mark.parametrize("failure", [
    pytest.param(HTTPError("url", 500, "boom", {}, None), id="http-500"),
    pytest.param(HTTPError("url", 503, "boom", {}, None), id="http-503"),
    pytest.param(URLError("connection refused"), id="urlerror"),
    pytest.param(OSError("reset"), id="oserror"),
])
@mock.patch.object(helper, "_read_secret", return_value="tok")
def test_transient_failure_is_unknown(_, failure):
    """Transient trouble is NOT permission to sleep."""
    with mock.patch.object(helper, "urlopen", side_effect=failure):
        assert helper.query_awx_activity(300) == helper.ACTIVITY_UNKNOWN


@pytest.mark.parametrize("payload", [
    pytest.param(b"not json", id="not-json"),
    pytest.param({"count": None, "results": None}, id="null-typed"),
    pytest.param({"count": 1, "results": "nope"}, id="results-not-a-list"),
    pytest.param({"count": 1, "results": [{"timestamp": "yesterday"}]},
                 id="unparseable-timestamp"),
    pytest.param({"count": 1, "results": [{"timestamp": None}]},
                 id="null-timestamp"),
    pytest.param({"count": 1, "results": [{}]}, id="timestamp-missing"),
])
@mock.patch.object(helper, "_read_secret", return_value="tok")
def test_malformed_body_is_unknown(_, payload):
    with mock.patch.object(helper, "urlopen", return_value=_resp(payload)):
        assert helper.query_awx_activity(300) == helper.ACTIVITY_UNKNOWN


@mock.patch.object(helper, "_read_secret", return_value="tok")
def test_activity_query_contract(read_secret):
    """Exact endpoint, ordering, page size, and authenticated identity."""
    clock = FakeClock()
    urlopen = mock.MagicMock(return_value=_activity(clock.iso(10)))
    with mock.patch.object(helper, "time", clock), \
            mock.patch.object(helper, "urlopen", urlopen):
        helper.query_awx_activity(300)

    req = urlopen.call_args.args[0]
    assert req.full_url == (
        f"{helper.AWX_API_URL}/api/v2/activity_stream/"
        "?order_by=-timestamp&page_size=1"), req.full_url
    assert req.get_method() == "GET", "the probe must be a safe read"
    assert req.get_header("Authorization") == "Bearer tok"
    assert read_secret.call_args_list == [mock.call(helper.AWX_TOKEN_PATH)]


# ---------------------------------------------------------------------------
# Reaper integration
# ---------------------------------------------------------------------------

def _run_reaper(clock, urlopen_side_effect, lease=None):
    """Drive idle_reaper_loop to the fake clock's sleep budget.

    Returns (sleep_calls, lease_mock) where sleep_calls records every
    patch_awx_asleep() the reaper decided to make.
    """
    lease = lease or mock.MagicMock()
    lease.get_min_awake_until.return_value = 0.0
    slept = []

    with mock.patch.object(helper, "time", clock), \
            mock.patch.object(helper, "_lease", lease), \
            mock.patch.object(helper, "_read_secret", return_value="tok"), \
            mock.patch.object(helper, "patch_awx_asleep",
                              side_effect=lambda: slept.append(clock.now)), \
            mock.patch.object(helper, "urlopen",
                              side_effect=urlopen_side_effect):
        with pytest.raises(_StopLoop):
            helper.idle_reaper_loop()

    return slept, lease


def test_visible_mutation_ages_out_and_reaper_still_sleeps():
    """THE 693 GUILLOTINE IS NOT FIXED — this pins the bounded behaviour.

    The realistic shape of a long REST-only play, as awx-svc can actually see
    it: unified_jobs reports zero throughout (the play launches nothing), ONE
    mutation is visible near the start, and the play then keeps driving the
    AWX API with GETs and admin-credentialed mutations awx-svc cannot see —
    for well over two grace periods.

    The single visible event ages out, idle_since starts, and AWX IS SLEPT
    mid-play. That is the current, honest behaviour and this test asserts it.

    An earlier version of this test returned a BRAND-NEW event stamped 5s ago
    on every poll, which made the reaper look like it held off indefinitely.
    That asserted a world that cannot happen: a finite, non-renewing set of
    visible events is what a real play produces.

    EXPECTED TO FLIP: when the explicit Lease hold lands (the AWX-consumer
    phase renewing the wake Lease around 693/697/699), this scenario must stop
    sleeping and this assertion should be inverted, not deleted.
    """
    clock = FakeClock(max_sleeps=20)  # 20 minutes, > 2x the 300s grace period
    fixed_event = clock.iso(5)  # one visible mutation, never renewed

    slept, _ = _run_reaper(
        clock,
        _route(lambda: _jobs(0), lambda: _activity(fixed_event)),
    )

    assert slept, (
        "documents the KNOWN GAP: one visible mutation ages out and the "
        "reaper sleeps AWX mid-play. If this now fails, the Lease hold has "
        "landed — invert the assertion rather than removing the test."
    )


def test_sustained_visible_mutations_delay_the_sleep():
    """Supplementary evidence does what it claims: while mutations REMAIN
    visible and recent, the sleep is delayed and the wake Lease is held.

    This is the narrow, honest claim for the activity check — not "693 is
    safe". It only holds for as long as awx-svc keeps seeing fresh entries,
    which a real admin-credentialed play does not guarantee (see the module
    docstring and the ageing-out test above).
    """
    clock = FakeClock(max_sleeps=12)
    slept, lease = _run_reaper(
        clock,
        _route(lambda: _jobs(0), lambda: _activity(clock.iso(5))),
    )

    assert slept == [], \
        f"continuously-visible recent mutations should delay the sleep; slept at {slept}"
    assert lease.create_or_update.called, \
        "visible sustained activity should hold the wake Lease open"


def test_quiet_awx_still_sleeps():
    """Genuinely idle — no jobs AND no recent mutations → sleep as before.

    The positive control: without this, "never sleep" would pass the test
    above and silently kill the whole scale-to-zero feature.
    """
    clock = FakeClock(max_sleeps=12)
    slept, _ = _run_reaper(
        clock,
        _route(lambda: _jobs(0), lambda: _activity(clock.iso(9999))),
    )

    assert slept, "an idle AWX past its grace period must still be slept"


def test_activity_stream_unavailable_degrades_to_job_only():
    """403 on the activity stream → today's behaviour, not a sleepier one."""
    clock = FakeClock(max_sleeps=12)
    slept, _ = _run_reaper(
        clock,
        _route(lambda: _jobs(0), HTTPError("url", 403, "no", {}, None)),
    )

    assert slept, \
        "an unreadable activity stream must fall back to the job-only decision"


def test_transient_activity_failure_does_not_sleep():
    """Unknown activity state is not permission to sleep."""
    clock = FakeClock(max_sleeps=12)
    slept, lease = _run_reaper(
        clock,
        _route(lambda: _jobs(0), URLError("reset")),
    )

    assert slept == [], f"must not sleep on an unknown activity state; {slept}"
    assert not lease.create_or_update.called, \
        "an unknown activity state must NOT extend the wake Lease (#103 shape)"


def test_unreachable_awx_never_consults_the_activity_stream():
    """#103 regression guard.

    When AWX is unreachable the reaper must bail out before the activity
    check — it must neither sleep nor extend the Lease, and it must not spend
    a request on a stream it cannot reach either.
    """
    clock = FakeClock(max_sleeps=6)
    seen = []

    def side_effect(req, **kwargs):
        seen.append(req.full_url)
        raise URLError("connection refused")

    slept, lease = _run_reaper(clock, side_effect)

    assert slept == [], "an unreachable AWX must never be slept"
    assert not lease.create_or_update.called, \
        "an unreachable AWX must NOT extend the wake Lease (issue #103)"
    assert all("activity_stream" not in url for url in seen), \
        f"activity stream must not be probed on the unreachable path; {seen}"


def test_active_jobs_still_short_circuit_the_activity_check():
    """Confirmed running work needs no activity-stream corroboration."""
    clock = FakeClock(max_sleeps=6)
    seen = []

    def side_effect(req, **kwargs):
        seen.append(req.full_url)
        if "unified_jobs" in req.full_url:
            return _jobs(3)
        raise AssertionError("activity stream must not be consulted")

    slept, lease = _run_reaper(clock, side_effect)

    assert slept == []
    assert lease.create_or_update.called, "active work extends the Lease"
    assert all("activity_stream" not in url for url in seen)
