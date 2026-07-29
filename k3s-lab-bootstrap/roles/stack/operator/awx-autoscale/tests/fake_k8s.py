"""In-memory Kubernetes API fake for the awx-autoscale launch-ready gate tests.

The wake gate reads two kinds of object:
  - the awx-web / awx-task Deployments (generation, observedGeneration,
    spec.replicas, spec.selector.matchLabels);
  - the Pods those Deployments select, so a Ready-but-Terminating pod from the
    previous generation cannot satisfy the gate (#298).

`status.readyReplicas` is modelled DELIBERATELY as an independent field rather
than derived from the pod list: the whole point of #298 is that the Deployment
status and the real pod set can disagree, and a test harness that keeps them in
lockstep cannot express the drain race at all.

Usage::

    cluster = drained_cluster()          # settled, fully awake, gate-passing
    cluster.web.pods = [pod("old", terminating=True)]
    with mock.patch.object(helper, "_k8s_request", cluster):
        ...
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlparse

NAMESPACE = "awx"
CR_NAME = "awx"


class FakePod:
    """One pod in the fake cluster.

    terminating -> metadata.deletionTimestamp is set (the pod is draining but
    its Ready condition can still be True — that is the #298 trap).
    """

    def __init__(self, name, *, ready=True, terminating=False, phase="Running",
                 labels=None):
        self.name = name
        self.ready = ready
        self.terminating = terminating
        self.phase = phase
        self.labels = labels

    def to_json(self, default_labels):
        metadata = {
            "name": self.name,
            "namespace": NAMESPACE,
            "labels": dict(self.labels if self.labels is not None else default_labels),
        }
        if self.terminating:
            metadata["deletionTimestamp"] = "2026-07-29T06:54:01Z"
        return {
            "metadata": metadata,
            "status": {
                "phase": self.phase,
                "conditions": [
                    {"type": "Initialized", "status": "True"},
                    {"type": "Ready", "status": "True" if self.ready else "False"},
                ],
            },
        }


def pod(name="awx-web-1", **kwargs):
    """Shorthand FakePod constructor."""
    return FakePod(name, **kwargs)


class FakeDeployment:
    """One Deployment plus the pods it selects."""

    def __init__(self, component, *, generation=4, observed_generation=None,
                 spec_replicas=1, ready_replicas=1, pods=None):
        self.component = component  # "web" or "task"
        self.name = f"{CR_NAME}-{component}"
        self.generation = generation
        # None -> in sync with generation (the settled, non-racing case).
        self.observed_generation = (
            generation if observed_generation is None else observed_generation)
        self.spec_replicas = spec_replicas
        # Independent of `pods` on purpose — see module docstring.
        self.ready_replicas = ready_replicas
        self.pods = [pod(f"{self.name}-abc-1")] if pods is None else pods
        self.selector = {
            "app.kubernetes.io/name": CR_NAME,
            "app.kubernetes.io/component": self.name,
        }
        # Set to an exception instance to make the pod list fail.
        self.pod_list_error = None

    def to_json(self):
        return {
            "metadata": {"name": self.name, "namespace": NAMESPACE,
                         "generation": self.generation},
            "spec": {"replicas": self.spec_replicas,
                     "selector": {"matchLabels": dict(self.selector)}},
            "status": {"observedGeneration": self.observed_generation,
                       "replicas": self.spec_replicas,
                       "readyReplicas": self.ready_replicas},
        }

    def pod_list_json(self):
        return {"items": [p.to_json(self.selector) for p in self.pods]}


class FakeCluster:
    """Callable stand-in for helper._k8s_request.

    Routes on the request path. Every served request is appended to
    ``self.calls`` as ``(method, path)``. ``self.on_call`` — if set — is
    invoked as ``on_call(cluster, method, path, n)`` (n = 1-based call index)
    BEFORE the response is built, so a test can mutate cluster state partway
    through a snapshot sequence.
    """

    def __init__(self, web=None, task=None):
        self.web = web if web is not None else FakeDeployment("web")
        self.task = task if task is not None else FakeDeployment("task")
        self.calls = []
        self.on_call = None

    def deployment(self, name):
        if name == self.web.name:
            return self.web
        if name == self.task.name:
            return self.task
        raise KeyError(name)

    def deployment_get_count(self, component):
        """How many times the given Deployment object has been read."""
        suffix = f"/deployments/{CR_NAME}-{component}"
        return sum(1 for method, path in self.calls
                   if method == "GET" and path.endswith(suffix))

    def pod_list_count(self, component=None):
        """How many pod-list calls were made (optionally for one component)."""
        total = 0
        for method, path in self.calls:
            if method != "GET" or "/pods" not in path:
                continue
            if component is None:
                total += 1
            elif f"{CR_NAME}-{component}" in path:
                total += 1
        return total

    def __call__(self, method, path, body=None, **kwargs):
        self.calls.append((method, path))
        if self.on_call is not None:
            self.on_call(self, method, path, len(self.calls))

        parsed = urlparse(path)

        if "/deployments/" in parsed.path:
            name = parsed.path.rsplit("/", 1)[-1]
            return self.deployment(name).to_json()

        if parsed.path.endswith("/pods"):
            selector = parse_qs(parsed.query).get("labelSelector", [""])[0]
            for dep in (self.web, self.task):
                wanted = ",".join(f"{k}={v}" for k, v in sorted(dep.selector.items()))
                if selector == wanted:
                    if dep.pod_list_error is not None:
                        raise dep.pod_list_error
                    return dep.pod_list_json()
            return {"items": []}

        raise AssertionError(f"FakeCluster: unrouted {method} {path}")


def drained_cluster():
    """A settled, fully-awake AWX: both Deployments at generation, one Ready
    non-terminating pod each, nothing draining. The gate should pass."""
    return FakeCluster()


def draining_cluster(*, keep_stale_status=True, with_new_pod=False):
    """The #298 window: a sleep-drain is still in flight.

    keep_stale_status -> status.readyReplicas still advertises 1 (the value the
    pre-#298 gate keyed on).
    with_new_pod -> the post-wake replacement pod is already up and Ready
    ALONGSIDE the still-terminating previous-generation pod. AWX task/web pods
    carry no readinessProbe, so a fresh pod flips Ready as soon as its
    containers start — long before the old instance has finished draining.
    """
    cluster = FakeCluster()
    for dep in (cluster.web, cluster.task):
        dep.ready_replicas = 1 if keep_stale_status else 0
        old = pod(f"{dep.name}-old-1", ready=True, terminating=True)
        dep.pods = [pod(f"{dep.name}-new-1"), old] if with_new_pod else [old]
    return cluster
