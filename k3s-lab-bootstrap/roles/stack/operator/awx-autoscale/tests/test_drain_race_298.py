"""Discriminating tests for #298: a wake arriving during a sleep-drain.

Live timeline, env <env> 2026-07-29 (helper audit log + AWX Postgres;
terminationGracePeriodSeconds is 30 on both awx-task and awx-web):

    06:54:01  reaper_sleep_trigger -> asleep; awx-task pod ...-ts6zx SIGTERMed
    06:54:26  wake arrives — 5s BEFORE ts6zx's grace period expires
              -> awake; replacement pod ...-dwqs7 created
    06:54:51  ensure_awake_ready
    06:54:52  dmf-cms dispatches

WHAT ACTUALLY WENT WRONG — established from the AWX database, and narrower
than it first looks:

  * The replacement pod dwqs7 served the dispatch and was NEVER killed: jobs
    304 and 305 ran on it and SUCCEEDED at 06:54:56 and 06:54:58, i.e. while
    302 was dying. So this is not a pod that died under the work.
  * The two jobs that failed, 302 and 303, are exactly the two that spawn an
    EE worker pod and stream results back over receptor. Both died with
    "Failed to JSON parse a line from worker stream" — the OUTGOING
    generation's dispatcher/receptor teardown (SignalExit -> receptor work
    cancel -> ConnectionResetError [Errno 104]) cancelling in-flight work
    units and corrupting the stream the new pod was reading.
  * So the harm is admitting work while the previous generation is still
    tearing down. It is NOT a dying pod satisfying the readiness gate, and it
    is NOT the replacement pod being killed.

WHY THE GATE COULD NOT SEE IT: every pre-#298 signal describes the replacement
generation or the AWX API. readyReplicas came from the fresh pod — AWX pods
carry no readinessProbe, so Ready arrives at container start — and the AWX API
answered normally throughout. Upstream excludes terminating pods from
readyReplicas the instant a deletionTimestamp is set, and the field that would
report them (status.terminatingReplicas, KEP-3973) is alpha/off-by-default
until k8s 1.35. Listing pods is the only way to know a drain is in flight.

THIS IS NOT A TIMING FIX. Two later wakes from a SETTLED asleep state went
green just as fast — 07:09:49 (16s queue) and 07:34:47 (25s) — and both
dispatched cleanly, all dependent jobs successful. The discriminator is
drain-in-flight, not elapsed time, and these tests are written so that a
"just wait longer" implementation would not pass them.

Each test below fails on the pre-#298 helper and passes after it.

Run:
  python3 -m pytest tests/test_drain_race_298.py -v
"""
import importlib.util
import itertools
import json
import os
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError, URLError

import pytest

import fake_k8s
from fake_k8s import FakeCluster, drained_cluster, draining_cluster, pod

# The helper reads these at import time (no defaults); set test values first.
os.environ.setdefault("AWX_AUTOSCALE_NAMESPACE", fake_k8s.NAMESPACE)
os.environ.setdefault("AWX_AUTOSCALE_AWX_API_URL", "http://awx.test")
os.environ.setdefault("AWX_AUTOSCALE_WAKE_POLL_INTERVAL", "1")
os.environ.setdefault("AWX_AUTOSCALE_STABILIZATION_DELAY", "0.01")

_HELPER = Path(__file__).resolve().parents[1] / "files" / "awx_autoscale_helper.py"
_spec = importlib.util.spec_from_file_location("awx_autoscale_helper", _HELPER)
helper = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(helper)


# ---------------------------------------------------------------------------
# AWX API responses — always green here, so every assertion below is about the
# Kubernetes-side drain signal and nothing else.
# ---------------------------------------------------------------------------

def _ping_ok(capacity=20):
    m = mock.MagicMock()
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    m.status = 200
    m.read.return_value = json.dumps({
        "instance_groups": [
            {"name": "controlplane", "capacity": capacity, "instances": ["awx-task-1"]},
        ],
    }).encode()
    return m


def _job_templates_ok():
    m = mock.MagicMock()
    m.__enter__.return_value = m
    m.__exit__.return_value = False
    m.status = 200
    m.read.return_value = json.dumps({"count": 5, "results": []}).encode()
    return m


def _awx_all_green():
    """An endless ping/auth response stream — AWX itself answers happily.

    This is what made #298 so hard to see, and it is faithful to the live
    incident: the AWX API was healthy the whole time. Nothing on the AWX side
    reports "a previous generation is still tearing down", so the Kubernetes
    signal is the only place the drain is visible.
    """
    return itertools.cycle([_ping_ok(), _job_templates_ok()])


@pytest.fixture
def awx_green():
    with mock.patch.object(helper, "_read_secret", return_value="tok"), \
            mock.patch.object(helper, "urlopen", side_effect=_awx_all_green()):
        yield


# ---------------------------------------------------------------------------
# The drain race itself
# ---------------------------------------------------------------------------

def test_terminating_pod_does_not_satisfy_the_gate(awx_green):
    """Ready-but-Terminating pod + a status that still claims a ready replica
    -> NOT launch-ready.

    Defensive rather than a replay of the incident: upstream would not
    normally publish readyReplicas=1 for a pod that already has a
    deletionTimestamp. The point is that the gate must not be reachable
    through the status field at all — its answer has to come from the pods.
    """
    cluster = draining_cluster()
    with mock.patch.object(helper, "_k8s_request", cluster):
        snapshot = helper.check_launch_ready_snapshot()

    assert snapshot["web_ready"] is False, \
        f"a terminating pod is not a ready web replica; got {snapshot}"
    assert snapshot["task_ready"] is False, \
        f"a terminating pod is not a ready task replica; got {snapshot}"
    assert snapshot["all_ready"] is False


def test_fresh_pod_alongside_a_draining_one_does_not_satisfy_the_gate(awx_green):
    """New pod Ready + old pod still terminating -> NOT launch-ready.

    THIS is the live shape of #298 (06:54:26: replacement pod dwqs7 up and
    Ready while ts6zx still had ~5s of its 30s grace period left). AWX's
    web/task pods define no readinessProbe, so the replacement's Ready
    condition flips as soon as its containers start — seconds, not minutes —
    and the AWX API is healthy. Every signal the pre-#298 gate had was
    therefore green and CORRECT about the new generation; none of them said
    anything about the old one still tearing down and cancelling its in-flight
    receptor work units.

    So "at least one ready pod exists" is exactly the wrong predicate. The
    gate must require the drain to have COMPLETED.
    """
    cluster = draining_cluster(with_new_pod=True)
    with mock.patch.object(helper, "_k8s_request", cluster):
        snapshot = helper.check_launch_ready_snapshot()

    assert snapshot["all_ready"] is False, \
        f"a still-draining previous generation must hold the gate shut; got {snapshot}"


def test_wait_awx_ready_times_out_while_a_pod_is_draining(awx_green):
    """End-to-end: wait_awx_ready must not return True mid-drain.

    The snapshot-level tests above pin the signal; this pins the caller
    contract that #298 actually violated — /ensure-awake reported ready and
    dmf-cms dispatched.
    """
    cluster = draining_cluster(with_new_pod=True)
    with mock.patch.object(helper, "_k8s_request", cluster):
        assert helper.wait_awx_ready(timeout=2.0) is False, \
            "wake must not report ready while the previous generation drains"


def test_drain_completion_opens_the_gate(awx_green):
    """Once the terminating pod is gone, the same cluster IS launch-ready.

    Positive counterpart — without this, "always return False" would pass
    every test above.
    """
    cluster = draining_cluster(with_new_pod=True)
    for dep in (cluster.web, cluster.task):
        dep.pods = [p for p in dep.pods if not p.terminating]

    with mock.patch.object(helper, "_k8s_request", cluster):
        snapshot = helper.check_launch_ready_snapshot()

    assert snapshot["all_ready"] is True, \
        f"a fully drained, ready deployment must pass the gate; got {snapshot}"


def test_gate_opens_on_a_settled_cluster(awx_green):
    """The ordinary settled case stays ready (no regression of #295's path)."""
    cluster = drained_cluster()
    with mock.patch.object(helper, "_k8s_request", cluster):
        assert helper.wait_awx_ready(timeout=5.0) is True


# ---------------------------------------------------------------------------
# Stale status: observedGeneration
# ---------------------------------------------------------------------------

def test_status_from_before_our_scale_patch_does_not_satisfy_the_gate(awx_green):
    """observedGeneration < generation -> NOT launch-ready.

    patch_awx_awake() bumps metadata.generation. Until the Deployment
    controller has observed it, every other field in ``status`` describes the
    world BEFORE the wake — including a readyReplicas left over from before the
    sleep. Deployments (unlike the AWX CR) do publish observedGeneration, so
    there is no excuse for reading status blind.
    """
    cluster = drained_cluster()
    for dep in (cluster.web, cluster.task):
        dep.generation = 9
        dep.observed_generation = 8

    with mock.patch.object(helper, "_k8s_request", cluster):
        snapshot = helper.check_launch_ready_snapshot()

    assert snapshot["all_ready"] is False, \
        f"status predating our own scale patch must not be trusted; got {snapshot}"


def test_observed_generation_catching_up_opens_the_gate(awx_green):
    """Positive counterpart: observedGeneration == generation is fine."""
    cluster = drained_cluster()
    for dep in (cluster.web, cluster.task):
        dep.generation = 9
        dep.observed_generation = 9

    with mock.patch.object(helper, "_k8s_request", cluster):
        assert helper.check_launch_ready_snapshot()["all_ready"] is True


def test_missing_observed_generation_does_not_satisfy_the_gate(awx_green):
    """A Deployment status with no observedGeneration at all -> NOT ready.

    Fail closed: the field is guaranteed on Deployments, so its absence means
    we are not looking at a settled status.
    """
    cluster = drained_cluster()
    for dep in (cluster.web, cluster.task):
        dep.observed_generation = None

    with mock.patch.object(helper, "_k8s_request", cluster):
        assert helper.check_launch_ready_snapshot()["all_ready"] is False


# ---------------------------------------------------------------------------
# spec.replicas — someone else wrote the asleep value back
# ---------------------------------------------------------------------------

def test_spec_replicas_zero_does_not_satisfy_the_gate(awx_green):
    """spec.replicas == 0 with a Ready pod still present -> NOT launch-ready.

    Defensive, not a replay: the live data REFUTES an operator lost-update as
    the #298 mechanism (the replacement pod was never scaled away — jobs 304
    and 305 succeeded on it while 302 was failing). But the awx-operator does
    reconcile the CR asynchronously, and a Deployment asking for zero replicas
    while a Ready pod is still present is unambiguously a generation on its way
    out, not a launch-ready one.
    """
    cluster = drained_cluster()
    for dep in (cluster.web, cluster.task):
        dep.spec_replicas = 0

    with mock.patch.object(helper, "_k8s_request", cluster):
        snapshot = helper.check_launch_ready_snapshot()

    assert snapshot["all_ready"] is False, \
        f"a deployment scaled to zero is never launch-ready; got {snapshot}"


# ---------------------------------------------------------------------------
# The gate must read pods at all, and fail closed when it cannot
# ---------------------------------------------------------------------------

def test_gate_lists_pods_for_both_deployments(awx_green):
    """The gate must actually ask the API for pods, using the Deployment's own
    selector — not hardcoded AWX labels, and not `status.readyReplicas`.
    """
    cluster = drained_cluster()
    with mock.patch.object(helper, "_k8s_request", cluster):
        helper.check_launch_ready_snapshot()

    assert cluster.pod_list_count("web") >= 1, \
        f"web readiness must be counted from pods; calls: {cluster.calls}"
    assert cluster.pod_list_count("task") >= 1, \
        f"task readiness must be counted from pods; calls: {cluster.calls}"

    pod_paths = [path for method, path in cluster.calls if "/pods" in path]
    for path in pod_paths:
        assert "labelSelector=" in path, \
            f"pod list must be label-scoped to the Deployment's selector; got {path}"
        assert f"/namespaces/{fake_k8s.NAMESPACE}/pods" in path, \
            f"pod list must be namespaced; got {path}"


def test_lying_ready_replicas_alone_never_opens_the_gate(awx_green):
    """readyReplicas=5 with zero pods behind it -> NOT launch-ready.

    The sharpest discriminator against the pre-#298 implementation: nothing
    the Deployment's status field claims can substitute for a real pod.
    """
    cluster = drained_cluster()
    for dep in (cluster.web, cluster.task):
        dep.ready_replicas = 5
        dep.pods = []

    with mock.patch.object(helper, "_k8s_request", cluster):
        assert helper.check_launch_ready_snapshot()["all_ready"] is False


def test_not_ready_pod_does_not_satisfy_the_gate(awx_green):
    """A live, non-terminating pod whose Ready condition is False -> NOT ready."""
    cluster = drained_cluster()
    for dep in (cluster.web, cluster.task):
        dep.pods = [pod(f"{dep.name}-starting", ready=False)]

    with mock.patch.object(helper, "_k8s_request", cluster):
        assert helper.check_launch_ready_snapshot()["all_ready"] is False


@pytest.mark.parametrize("error", [
    pytest.param(HTTPError("url", 403, "Forbidden", {}, None), id="rbac-403"),
    pytest.param(URLError("apiserver unreachable"), id="unreachable"),
])
def test_pod_list_failure_fails_closed(awx_green, error):
    """If the pod list cannot be read, the gate stays SHUT.

    This introduces a hard dependency on `pods: list` RBAC in the awx
    namespace. Failing open here would restore exactly the #298 behaviour, so
    the gate must fail closed and the wake must time out visibly instead.
    """
    cluster = drained_cluster()
    cluster.web.pod_list_error = error

    with mock.patch.object(helper, "_k8s_request", cluster):
        assert helper.check_launch_ready_snapshot()["web_ready"] is False


@pytest.mark.parametrize("error", [
    pytest.param(HTTPError("url", 403, "Forbidden", {}, None), id="rbac-403"),
    pytest.param(URLError("apiserver unreachable"), id="unreachable"),
])
def test_pod_list_failure_does_not_escape_the_wake_thread(awx_green, error):
    """A pod-list failure degrades to "not ready" — it must not raise.

    /ensure-awake runs wait_awx_ready on the request thread. An exception
    escaping here would abort the wake with a 500 and, on the reaper thread,
    trip the catch-all that resets idle_since. Degrading to a visible timeout
    is the required behaviour.
    """
    cluster = drained_cluster()
    cluster.web.pod_list_error = error
    cluster.task.pod_list_error = error

    with mock.patch.object(helper, "_k8s_request", cluster):
        assert helper.wait_awx_ready(timeout=1.0) is False


def test_deployment_read_failure_fails_closed(awx_green):
    """The Deployment GET itself failing -> not ready, no exception."""
    cluster = drained_cluster()

    def boom(method, path, body=None, **kwargs):
        raise HTTPError(path, 500, "boom", {}, None)

    with mock.patch.object(helper, "_k8s_request", side_effect=boom):
        snapshot = helper.check_launch_ready_snapshot()
    assert snapshot["all_ready"] is False


def test_a_drain_that_never_completes_never_opens_the_gate(awx_green):
    """Waiting longer must not help — the predicate is state, not elapsed time.

    Guards against a "just raise the stabilization delay" pseudo-fix: the
    cluster below is permanently mid-drain, and the gate must poll repeatedly
    and still refuse.
    """
    cluster = draining_cluster(with_new_pod=True)
    with mock.patch.object(helper, "_k8s_request", cluster):
        assert helper.wait_awx_ready(timeout=3.0) is False

    assert cluster.deployment_get_count("web") >= 3, \
        f"expected repeated polling, got {cluster.deployment_get_count('web')} snapshots"


def test_terminal_phase_pods_are_ignored(awx_green):
    """Evicted/Failed leftovers must neither satisfy nor jam the gate.

    Succeeded/Failed pods are not part of the live replica set, so a Failed
    pod lingering after a node eviction must not hold /ensure-awake shut for
    the full 1200s startup budget.

    This is the phase filter only (_pod_is_nonterminal). It is deliberately
    NOT upstream's IsPodActive, which also folds in deletionTimestamp — the
    gate needs terminating pods to stay visible so the drain predicate can
    refuse on them, and that is checked separately.
    """
    cluster = drained_cluster()
    for dep in (cluster.web, cluster.task):
        dep.pods = [
            pod(f"{dep.name}-evicted", ready=False, phase="Failed"),
            pod(f"{dep.name}-live", ready=True),
        ]

    with mock.patch.object(helper, "_k8s_request", cluster):
        assert helper.check_launch_ready_snapshot()["all_ready"] is True


def test_terminal_phase_pod_alone_does_not_satisfy_the_gate(awx_green):
    """...but a terminal-phase pod on its own is not a ready replica either."""
    cluster = drained_cluster()
    for dep in (cluster.web, cluster.task):
        dep.pods = [pod(f"{dep.name}-evicted", ready=True, phase="Failed")]

    with mock.patch.object(helper, "_k8s_request", cluster):
        assert helper.check_launch_ready_snapshot()["all_ready"] is False


# ---------------------------------------------------------------------------
# #295 semantics must survive: two complete snapshots, nothing latched
# ---------------------------------------------------------------------------

def test_drain_starting_between_snapshots_re_arms_the_gate(awx_green):
    """First snapshot clean, then a pod starts terminating -> NOT ready.

    #295's two-green-snapshot rule has to cover the new pod signal too: the
    confirmation snapshot must re-read the pods, not latch the first result.
    """
    cluster = drained_cluster()

    def start_draining(cl, method, path, n):
        # Snapshot 1 reads web+task; from the confirmation snapshot on, the
        # web pod is terminating.
        if cl.deployment_get_count("web") >= 2:
            cl.web.pods = [pod("awx-web-old", ready=True, terminating=True)]

    cluster.on_call = start_draining

    with mock.patch.object(helper, "_k8s_request", cluster):
        assert helper.wait_awx_ready(timeout=2.0) is False, \
            "a drain beginning between snapshots must re-arm the gate"


def test_ready_path_takes_two_complete_snapshots(awx_green):
    """The all-green path still costs exactly two complete k8s snapshots."""
    cluster = drained_cluster()
    with mock.patch.object(helper, "_k8s_request", cluster):
        assert helper.wait_awx_ready(timeout=5.0) is True

    assert cluster.deployment_get_count("web") == 2, \
        f"two snapshots read the web Deployment twice; calls: {cluster.calls}"
    assert cluster.deployment_get_count("task") == 2, \
        f"two snapshots read the task Deployment twice; calls: {cluster.calls}"
    assert cluster.pod_list_count("web") == 2, \
        f"two snapshots list web pods twice; calls: {cluster.calls}"
    assert cluster.pod_list_count("task") == 2, \
        f"two snapshots list task pods twice; calls: {cluster.calls}"
