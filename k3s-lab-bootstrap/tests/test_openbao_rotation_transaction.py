"""Rotation transaction engine: states, invariants, Lease, redaction and sweep.

The fakes sit at the client boundary (OpenBao and Kubernetes). Every value is
synthetic. A `ProcessLost` models a crash: it is a BaseException, so the engine
passes it on without recording a failure, like a killed process.
"""

import copy
import json
from datetime import datetime, timezone
import logging
import sys
import traceback
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
sys.path.insert(0, str(ROOT / "tests"))

from approle_lifecycle import adapters, registry, rotation  # noqa: E402
from approle_lifecycle.rotation import (  # noqa: E402
    ACTIVE, ABORTED, DELIVERED_PENDING_READINESS, ISSUED_PENDING_DELIVERY, PENDING_CREATE,
    AlreadyRegistered, ApiError, LeaseConflict, LeaseExists, RotationClients, RotationConfig, RotationError,
    RunContext, StagingExists,
)
from test_openbao_approle_rotation_metadata_v1 import validate_record  # noqa: E402

REGISTRY = registry.load(ROOT / "vars/openbao-approle-lifecycle-registry.json")
ONE = "external-secrets"  # one destination
TWO = "object-storage-openbao-snapshots"  # a KV record and a Kubernetes Secret
THREE = "object-storage-app-backups"
NOW = 1_700_000_000
TOKEN = "synthetic-client-token"
LEASE_NAMESPACE = "approle-rotation"


class ProcessLost(BaseException):
    """The process died at this point."""


class Clock:
    def __init__(self, start):
        self.t = start

    def __call__(self):
        return self.t


class FakeBao:
    """OpenBao at the client boundary. A method the protocol does not list
    raises, so a call to the random `secret-id` endpoint or an accessor list
    fails a test."""

    request_timeout = 60  # the bound the client declares, in seconds

    def __init__(self, events):
        self.events = events
        self.records = {}
        self.staging = {}
        self.registered = {}  # SecretID -> accessor
        self.create_calls = []  # every candidate passed to custom-secret-id
        self.create_params = []
        self.lookup_calls = []
        self.destroyed = []
        self.crash_before = set()
        self.crash_after = set()
        self.lose_create_response = False
        self.already_registered = False
        self.fail = {}
        self.wrong_bounds = False
        self.num_uses = 0
        self.as_strings = False
        self.lookup_override = None
        self.role_missing = False
        self.staging_race = False
        self.now = None  # the test clock, set by the harness
        self.created = {}
        self.emit_times = True
        self.times_override = None
        self.counter = 0
        self.ttls = {t: registry.ttl_seconds(e["secret_id_ttl"]) for t, e in REGISTRY["runtime_targets"].items()}

    def __getattr__(self, name):
        raise AssertionError(f"unexpected OpenBao method {name}")

    def _point(self, name, when):
        points = self.crash_before if when == "before" else self.crash_after
        if name in points:
            points.discard(name)
            raise ProcessLost()

    def _maybe_fail(self, name):
        error = self.fail.get(name)
        if error is not None:
            raise error

    def seed_existing(self, approle, accessor):
        self.registered[f"old-secret-id-of-{approle}"] = accessor

    def read_role(self, approle):
        self.events.append(("read_role", approle))
        self._maybe_fail("read_role")
        if self.role_missing:
            return None
        return {"secret_id_ttl": self.ttls[approle]}

    def lookup_secret_id(self, approle, secret_id):
        self.events.append(("lookup_secret_id", approle))
        self.lookup_calls.append(secret_id)
        self._maybe_fail("lookup_secret_id")
        if self.lookup_override is not None:
            return self.lookup_override
        accessor = self.registered.get(secret_id)
        return None if accessor is None else self._response(approle, accessor, secret_id)

    def _response(self, approle, accessor, secret_id=None):
        ttl = self.ttls[approle] + (1 if self.wrong_bounds else 0)
        uses = self.num_uses
        if self.as_strings:
            ttl, uses = str(ttl), str(uses)
        response = {"secret_id_accessor": accessor, "secret_id_ttl": ttl, "secret_id_num_uses": uses}
        if self.emit_times and secret_id in self.created:
            made = self.created[secret_id]
            stamp = lambda t: datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.123456789Z")  # noqa: E731
            response["creation_time"] = stamp(made)
            response["expiration_time"] = stamp(made + self.ttls[approle])
            response.update(self.times_override or {})
        return response

    def create_custom_secret_id(self, approle, secret_id, params):
        self._point("create_custom_secret_id", "before")
        self.events.append(("create_custom_secret_id", approle))
        self.create_calls.append(secret_id)
        self.create_params.append(dict(params))
        self._maybe_fail("create_custom_secret_id")
        if secret_id in self.registered or self.already_registered:
            self.registered.setdefault(secret_id, f"accessor-{len(self.registered)}")
            raise AlreadyRegistered()
        self.counter += 1
        accessor = f"accessor-{self.counter}"
        self.registered[secret_id] = accessor
        self.created[secret_id] = self.now()
        if self.lose_create_response:
            self.lose_create_response = False
            raise RuntimeError(f"connection reset while creating {secret_id} with token {TOKEN}")
        self._point("create_custom_secret_id", "after")
        return self._response(approle, accessor, secret_id)

    def destroy_secret_id_accessor(self, approle, accessor):
        self.events.append(("destroy_secret_id_accessor", accessor))
        self._maybe_fail("destroy_secret_id_accessor")
        self.destroyed.append(accessor)
        for secret_id, held in list(self.registered.items()):
            if held == accessor:
                del self.registered[secret_id]

    def read_kv2(self, path):
        self.events.append(("read_kv2", path))
        self._maybe_fail("read_kv2")
        value = self.records.get(path)
        return (copy.deepcopy(value[0]), value[1]) if value is not None else None

    def write_kv2(self, path, data, cas):
        self._point("write_kv2", "before")
        self.events.append(("write_kv2", path))
        self._maybe_fail("write_kv2")
        current = self.records.get(path)
        version = current[1] if current is not None else 0
        if cas != version:
            raise ApiError("check-and-set parameter did not match the current version")
        self.records[path] = (copy.deepcopy(data), version + 1)

    def create_staging(self, path, data):
        self._point("create_staging", "before")
        self.events.append(("create_staging", path))
        self._maybe_fail("create_staging")
        if self.staging_race:
            raise StagingExists()
        if path in self.staging:
            raise StagingExists()
        self.staging[path] = copy.deepcopy(data)
        self._point("create_staging", "after")

    def read_staging(self, path):
        self.events.append(("read_staging", path))
        value = self.staging.get(path)
        return copy.deepcopy(value) if value is not None else None

    def delete_staging(self, path):
        self.events.append(("delete_staging", path))
        self._maybe_fail("delete_staging")
        self.staging.pop(path, None)


class FakeKube:
    request_timeout = 60

    def __init__(self, events, bao):
        self.events = events
        self.bao = bao
        self.secrets = {}
        self.versions = {}
        self.leases = {}
        self.crash_after = set()
        self.crash_before = set()
        self.fail_patch = set()
        self.patched_values = []
        self.version = 0
        self.fail_lease_update = False
        self.fail_get_secret = False
        self.skip_marker = False
        self.lease_race = False
        self.fail_lease_delete = False

    def __getattr__(self, name):
        raise AssertionError(f"unexpected Kubernetes method {name}")

    def get_secret(self, namespace, name):
        if self.fail_get_secret:
            raise ApiError(f"read refused with token {TOKEN}")
        value = self.secrets.get((namespace, name))
        if value is None:
            return None
        found = copy.deepcopy(value)
        found["resource_version"] = str(self.versions.get((namespace, name), 1))
        return found

    def _check_version(self, namespace, name, resource_version):
        """Like the API server: a write that names a stale version fails."""
        current = str(self.versions.get((namespace, name), 1))
        if resource_version is not None and resource_version != current:
            raise ApiError("the object has been modified")
        self.versions[(namespace, name)] = int(current) + 1

    def annotate_secret(self, namespace, name, annotations, resource_version):
        self.events.append(("annotate_secret", namespace, name))
        self._check_version(namespace, name, resource_version)
        self.secrets[(namespace, name)]["annotations"].update(annotations)

    def patch_secret(self, namespace, name, key, value, annotations, resource_version=None):
        if (namespace, name) in self.fail_patch:
            raise ApiError(f"patch refused for {value} using token {TOKEN}")
        if "patch_secret" in self.crash_before:
            self.crash_before.discard("patch_secret")
            raise ProcessLost()
        self._check_version(namespace, name, resource_version)
        self.events.append(("patch_secret", namespace, name))
        current = self.secrets.setdefault((namespace, name), {"data": {}, "annotations": {}})
        current["data"][key] = value
        if not self.skip_marker:
            current["annotations"].update(annotations)
        self.patched_values.append(value)
        if "patch_secret" in self.crash_after:
            self.crash_after.discard("patch_secret")
            raise ProcessLost()

    def get_lease(self, namespace, name):
        if self.lease_race:
            return None
        value = self.leases.get((namespace, name))
        return dict(value) if value is not None else None

    def _bump(self):
        self.version += 1
        return str(self.version)

    def create_lease(self, namespace, name, holder, renew_time, duration_seconds):
        if self.lease_race:
            raise LeaseExists()
        if (namespace, name) in self.leases:
            raise LeaseExists()
        self.events.append(("create_lease", holder))
        self.leases[(namespace, name)] = {
            "holder": holder, "renew_time": renew_time, "duration_seconds": duration_seconds,
            "resource_version": self._bump(),
        }

    def update_lease(self, namespace, name, holder, renew_time, duration_seconds, resource_version):
        if self.fail_lease_update:
            raise ApiError("lease update failed")
        current = self.leases[(namespace, name)]
        # Like the API server, an update without a resource version is unconditional.
        if resource_version is not None and current["resource_version"] != resource_version:
            raise LeaseConflict()
        self.events.append(("update_lease", holder))
        self.leases[(namespace, name)] = {
            "holder": holder, "renew_time": renew_time, "duration_seconds": duration_seconds,
            "resource_version": self._bump(),
        }

    def delete_lease(self, namespace, name, resource_version):
        if self.fail_lease_delete:
            raise ApiError("lease delete failed")
        current = self.leases.get((namespace, name))
        if current is None or current["resource_version"] != resource_version:
            raise LeaseConflict()
        self.events.append(("delete_lease",))
        del self.leases[(namespace, name)]


class Readiness:
    """Stands in for the step-3 proof. It is a fake evaluator, so the engine's
    default (which never accepts) is tested separately."""

    def __init__(self, events):
        self.events = events
        self.ok = True
        self.sinces = []

    def __call__(self, entry, gates, since, inventory):
        self.events.append(("readiness", entry["approle_name"]))
        self.sinces.append(since)
        return adapters.TargetReadiness(self.ok, (), "" if self.ok else "readiness_failed")


class Candidates:
    def __init__(self):
        self.issued = []

    def __call__(self):
        value = f"synthetic-candidate-{len(self.issued) + 1}"
        self.issued.append(value)
        return value


def context(**overrides):
    values = {
        "actor": "system:serviceaccount:approle-rotation:scheduler",
        "role": rotation.SCHEDULER_ROLE,
        "request_id": "run-0001",
        "reason": "scheduled-pre-expiry-rotation",
    }
    values.update(overrides)
    return RunContext(**values)


class Harness:
    def __init__(self, seed=(ONE, TWO, THREE)):
        self.events = []
        self.clock = Clock(NOW)
        self.bao = FakeBao(self.events)
        self.kube = FakeKube(self.events, self.bao)
        self.bao.now = self.clock
        self.ready = Readiness(self.events)
        self.candidates = Candidates()
        self.holders = iter(f"holder-{n}" for n in range(1, 100))
        self.registry = REGISTRY
        self.config = RotationConfig(
            lease_namespace=LEASE_NAMESPACE,
            evaluate=self.ready,
            token_source=self.candidates,
            holder_source=lambda: next(self.holders),
            inventory={"namespaces": {"apps": ["authentik"]}},
        )
        self.clients = RotationClients(self.kube, self.bao)
        for target in REGISTRY["runtime_targets"]:
            for namespace, name, key in self.destinations(target):
                self.kube.secrets[(namespace, name)] = {"data": {key: f"old-value-of-{name}"}, "annotations": {}}
        for target in seed:
            self.seed(target)

    def entry(self, target):
        return REGISTRY["runtime_targets"][target]

    def seed(self, target, due=True, age=None):
        """An active credential, issued so that rotation is due (or not)."""
        entry = self.entry(target)
        limits = registry.thresholds(entry)
        if age is None:
            age = limits.rotation_due_after + 10 if due else 10
        issued = self.clock.t - age
        accessor = f"old-accessor-{target}"
        self.bao.seed_existing(target, accessor)
        record = {
            "schema_version": 1, "target": target, "approle_name": target, "target_version": 3,
            "secret_id_accessor": accessor, "issued_at": issued, "expires_at": issued + limits.ttl,
            "ttl_seconds": limits.ttl, "last_successful_rotation_at": issued,
            "last_scheduled_success_at": issued, "last_failure_at": None,
            "last_failure_reason_code": None, "latest_run_kind": "scheduled",
            "latest_run_outcome": "success", "latest_run_at": issued, "actor": "earlier-actor",
            "role": rotation.SCHEDULER_ROLE, "request_id": "earlier-run", "reason": "earlier",
        }
        self.bao.records[entry["metadata_path"]] = (record, 1)

    def run(self, target=ONE, ctx=None):
        return rotation.rotate_target(self.registry, target, ctx or context(), self.clients, self.config, self.clock)

    def sweep(self, ctx=None):
        return rotation.rotate_all(self.registry, ctx or context(), self.clients, self.config, self.clock)

    def main(self, target):
        return self.bao.records[self.entry(target)["metadata_path"]][0]

    def tx(self, target):
        return self.bao.records[rotation.transaction_path(self.entry(target))][0]

    def destinations(self, target):
        """(namespace, name) of every Kubernetes destination of the target."""
        return [
            (d["namespace"], d["name"], d["key"])
            for d in self.entry(target)["destinations"] if d["kind"] == "kubernetes_secret_key"
        ]

    def delivered_values(self, target):
        return [self.kube.secrets[(ns, name)]["data"][key] for ns, name, key in self.destinations(target)]

    def kinds_with(self, name, path):
        return len([e for e in self.events if e[0] == name and e[1:] == (path,)])

    def kinds(self, name):
        return [event for event in self.events if event[0] == name]

    def index(self, name, nth=0):
        return [i for i, event in enumerate(self.events) if event[0] == name][nth]

    def crash_run(self, target=ONE, ctx=None):
        """Run until the process is lost. Fail if the run does not stop."""
        try:
            self.run(target, ctx)
        except ProcessLost:
            self.wait_out_the_lease()
            return
        raise AssertionError("the run was expected to stop at a crash point")

    def wait_out_the_lease(self):
        """A lost process leaves its Lease in place. A successor starts after
        expiry plus the lock delay."""
        self.clock.t += self.config.lease_duration + self.config.lock_delay


class TransactionTests(unittest.TestCase):
    def test_normal_delivery_rotates_and_destroys_the_old_accessor_last(self):
        h = Harness()
        result = h.run(ONE)
        self.assertEqual((result.outcome, result.state, result.reason_code), ("rotated", ACTIVE, ""))
        self.assertEqual(h.candidates.issued, ["synthetic-candidate-1"])
        self.assertEqual(h.delivered_values(ONE), ["synthetic-candidate-1"])
        ttl = registry.ttl_seconds(h.entry(ONE)["secret_id_ttl"])
        self.assertEqual(h.bao.create_params, [{"ttl": f"{ttl}s", "num_uses": 0}])
        main = h.main(ONE)
        self.assertTrue(validate_record(main))
        self.assertEqual(main["target_version"], 4)
        self.assertEqual(main["secret_id_accessor"], "accessor-1")
        self.assertEqual(main["last_scheduled_success_at"], NOW)
        self.assertEqual(h.bao.destroyed, [f"old-accessor-{ONE}"])
        self.assertEqual(h.bao.staging, {})
        self.assertTrue(h.tx(ONE)["cleanup_done"])
        # ordering: stage, create, deliver, readiness, then destroy and delete
        order = [h.index(name) for name in (
            "create_staging", "create_custom_secret_id", "patch_secret", "readiness",
            "destroy_secret_id_accessor", "delete_staging")]
        self.assertEqual(order, sorted(order))
        self.assertLess(order[-3], order[-2])

    def test_candidate_is_staged_before_it_is_created(self):
        h = Harness()
        h.run(ONE)
        self.assertLess(h.index("create_staging"), h.index("create_custom_secret_id"))

    def test_the_default_candidate_source_is_a_csprng_and_never_repeats(self):
        first, second = (Harness() for _ in range(2))
        for h in (first, second):
            h.config.token_source = RotationConfig(lease_namespace="x").token_source
            h.run(ONE)
        a, b = first.bao.create_calls[0], second.bao.create_calls[0]
        self.assertGreaterEqual(len(a), 43)
        self.assertGreater(len(set(a)), 15)
        self.assertNotEqual(a, b)

    def test_no_prior_accessor_creates_version_one_and_destroys_nothing(self):
        h = Harness(seed=())
        result = h.run(ONE)
        self.assertEqual(result.outcome, "rotated")
        self.assertEqual(h.main(ONE)["target_version"], 1)
        self.assertTrue(validate_record(h.main(ONE)))
        self.assertEqual(h.bao.destroyed, [])
        self.assertIsNone(h.tx(ONE)["previous_accessor"])
        self.assertEqual(h.bao.staging, {})

    def test_target_that_is_not_due_does_nothing(self):
        h = Harness()
        h.seed(ONE, due=False)
        result = h.run(ONE)
        self.assertEqual(result.outcome, "not_due")
        self.assertEqual(h.bao.create_calls, [])
        self.assertEqual(h.kinds("create_staging"), [])

    def test_interruption_before_mint_resumes_with_the_staged_candidate(self):
        h = Harness()
        h.bao.crash_before.add("create_custom_secret_id")
        h.crash_run(ONE)
        self.assertEqual(h.tx(ONE)["state"], PENDING_CREATE)
        staged = h.bao.staging[rotation.staging_path(h.entry(ONE))]["secret_id"]
        self.assertEqual(h.bao.registered.get(staged), None)
        h.clock.t += 1000
        result = h.run(ONE, context(request_id="run-0002"))
        self.assertEqual(result.outcome, "rotated")
        self.assertEqual(h.candidates.issued, [staged])
        self.assertEqual(h.bao.create_calls, [staged])
        self.assertEqual(h.delivered_values(ONE), [staged])

    def test_interruption_between_intent_and_staging_generates_the_first_candidate_once(self):
        h = Harness()
        h.bao.crash_before.add("create_staging")
        h.crash_run(ONE)
        self.assertEqual(h.tx(ONE)["state"], PENDING_CREATE)
        self.assertEqual(h.bao.create_calls, [])
        h.clock.t += 1000
        self.assertEqual(h.run(ONE).outcome, "rotated")
        self.assertEqual(len(h.bao.create_calls), 1)
        self.assertEqual(h.bao.create_calls, h.candidates.issued[-1:])

    def test_lost_create_response_resolves_by_looking_up_the_same_candidate(self):
        h = Harness()
        h.bao.lose_create_response = True
        result = h.run(ONE)
        self.assertEqual(result.outcome, "rotated")
        self.assertEqual(len(h.candidates.issued), 1)
        self.assertEqual(len(h.bao.create_calls), 1)
        self.assertEqual(h.delivered_values(ONE), h.candidates.issued)
        self.assertEqual(h.main(ONE)["secret_id_accessor"], "accessor-1")

    def test_lost_create_response_across_runs_never_makes_a_second_candidate(self):
        h = Harness()
        h.bao.lose_create_response = True
        original = h.bao.lookup_secret_id
        state = {"lookups": 0}

        def flaky(approle, secret_id):
            state["lookups"] += 1
            if state["lookups"] == 2:  # the lookup that follows the lost response
                raise ApiError("lookup unavailable")
            return original(approle, secret_id)

        h.bao.lookup_secret_id = flaky
        first = h.run(ONE)
        self.assertEqual((first.outcome, first.reason_code, first.state), ("failed", "lookup_failed", PENDING_CREATE))
        h.clock.t += 1000
        second = h.run(ONE, context(request_id="run-0002"))
        self.assertEqual(second.outcome, "rotated")
        self.assertEqual(len(h.candidates.issued), 1)
        self.assertEqual(len(set(h.bao.create_calls)), 1)
        self.assertEqual([k for k in h.bao.registered if k.startswith("synthetic-candidate")], ["synthetic-candidate-1"])

    def test_duplicate_create_response_resolves_by_lookup(self):
        h = Harness()
        h.bao.registered["synthetic-candidate-1"] = "accessor-concurrent"
        original = h.bao.lookup_secret_id
        seen = {"n": 0}

        def hide_first(approle, secret_id):
            seen["n"] += 1
            return None if seen["n"] == 1 else original(approle, secret_id)

        h.bao.lookup_secret_id = hide_first
        result = h.run(ONE)
        self.assertEqual(result.outcome, "rotated")
        self.assertEqual(len(h.bao.create_calls), 1)
        self.assertEqual(h.main(ONE)["secret_id_accessor"], "accessor-concurrent")

    def test_duplicate_invocation_with_the_same_run_id_resumes_the_pending_transaction(self):
        h = Harness()
        h.ready.ok = False
        first = h.run(ONE)
        self.assertEqual(first.reason_code, "readiness_failed")
        transaction_id = first.transaction_id
        h.ready.ok = True
        h.clock.t += 60
        second = h.run(ONE)
        self.assertEqual(second.outcome, "rotated")
        self.assertEqual(second.transaction_id, transaction_id)
        self.assertEqual(len(h.candidates.issued), 1)
        self.assertEqual(len(h.bao.create_calls), 1)

    def test_duplicate_invocation_with_another_run_id_resumes_the_pending_transaction(self):
        h = Harness()
        h.ready.ok = False
        first = h.run(ONE, context(request_id="run-0001"))
        h.ready.ok = True
        h.clock.t += 60
        second = h.run(ONE, context(request_id="run-0002"))
        self.assertEqual(second.outcome, "rotated")
        self.assertEqual(second.transaction_id, first.transaction_id)
        self.assertTrue(second.transaction_id.startswith("run-0001-"))
        self.assertEqual(len(h.candidates.issued), 1)
        self.assertEqual(len(h.bao.create_calls), 1)
        self.assertEqual(h.main(ONE)["request_id"], "run-0002")

    def test_interruption_before_delivery_resumes_from_staging_without_a_new_candidate(self):
        h = Harness()
        h.kube.crash_before.add("patch_secret")
        h.crash_run(ONE)
        self.assertEqual(h.tx(ONE)["state"], ISSUED_PENDING_DELIVERY)
        self.assertIsNotNone(h.tx(ONE)["new_accessor"])
        self.assertEqual(h.kube.patched_values, [])
        h.clock.t += 1000
        result = h.run(ONE, context(request_id="run-0002"))
        self.assertEqual(result.outcome, "rotated")
        self.assertEqual(h.candidates.issued, ["synthetic-candidate-1"])
        self.assertEqual(h.kube.patched_values, ["synthetic-candidate-1"])
        self.assertEqual(len(h.bao.create_calls), 1)

    def test_no_destination_write_happens_before_the_accessor_is_recorded(self):
        h = Harness()
        seen = {}
        original = h.kube.patch_secret

        def checking(*args):
            transaction = h.tx(ONE)
            seen["state"] = transaction["state"]
            seen["accessor"] = transaction["new_accessor"]
            return original(*args)

        h.kube.patch_secret = checking
        h.run(ONE)
        self.assertEqual(seen["state"], ISSUED_PENDING_DELIVERY)
        self.assertEqual(seen["accessor"], "accessor-1")

    def test_partial_delivery_resumes_the_missing_destination_and_keeps_the_replacement(self):
        h = Harness()
        # TWO has a KV record destination first and a Kubernetes Secret second.
        original = h.bao.write_kv2
        record_path = h.entry(TWO)["destinations"][0]["path"]
        writes = []

        def crash_after_record(path, data, cas):
            original(path, data, cas)
            if path == record_path:
                writes.append(path)
                raise ProcessLost()

        h.bao.write_kv2 = crash_after_record
        h.crash_run(TWO)
        h.bao.write_kv2 = original
        self.assertEqual(h.kube.patched_values, [])  # only the first destination holds the marker
        self.assertEqual(h.bao.destroyed, [])
        staged = h.bao.staging[rotation.staging_path(h.entry(TWO))]
        self.assertIn(staged["secret_id"], h.bao.registered)
        # the replacement survives even after the abort window
        h.clock.t += h.config.abort_after + 1
        result = h.run(TWO, context(request_id="run-0002"))
        self.assertEqual(result.outcome, "rotated")
        self.assertEqual(h.delivered_values(TWO), [staged["secret_id"]])
        self.assertEqual(len(h.bao.create_calls), 1)
        self.assertEqual(h.bao.destroyed, [f"old-accessor-{TWO}"])
        # the destination that already carried the marker is not written again
        self.assertEqual(h.kinds_with("write_kv2", record_path), 1)

    def test_a_marked_replacement_is_never_aborted_however_old(self):
        h = Harness()
        h.kube.crash_after.add("patch_secret")
        h.crash_run(ONE)
        new_accessor = "accessor-1"
        h.ready.ok = False
        h.clock.t += h.config.abort_after * 10
        result = h.run(ONE, context(request_id="run-0002"))
        self.assertEqual(result.reason_code, "readiness_failed")
        self.assertEqual(result.state, DELIVERED_PENDING_READINESS)
        self.assertEqual(h.bao.destroyed, [])
        self.assertIn(new_accessor, h.bao.registered.values())
        self.assertNotEqual(h.tx(ONE)["state"], ABORTED)

    def test_delivered_but_unready_recovers_across_two_runs(self):
        h = Harness()
        h.ready.ok = False
        first = h.run(ONE)
        self.assertEqual((first.outcome, first.reason_code, first.state), ("failed", "readiness_failed", DELIVERED_PENDING_READINESS))
        self.assertEqual(h.bao.destroyed, [])
        self.assertEqual(h.main(ONE)["secret_id_accessor"], f"old-accessor-{ONE}")
        self.assertIn(f"old-accessor-{ONE}", h.bao.registered.values())
        self.assertEqual(h.main(ONE)["last_failure_reason_code"], "readiness_failed")
        self.assertEqual(h.tx(ONE)["last_failure_reason_code"], "readiness_failed")
        self.assertTrue(validate_record(h.main(ONE)))
        h.ready.ok = True
        h.clock.t += 3600
        second = h.run(ONE, context(request_id="run-0002"))
        self.assertEqual(second.outcome, "rotated")
        self.assertEqual(len(h.bao.create_calls), 1)
        self.assertEqual(h.kube.patched_values, ["synthetic-candidate-1"])  # delivered once, not again
        self.assertEqual(h.bao.destroyed, [f"old-accessor-{ONE}"])
        self.assertEqual(h.main(ONE)["secret_id_accessor"], "accessor-1")

    def test_a_retry_keeps_the_original_delivery_time(self):
        h = Harness()
        h.ready.ok = False
        h.run(ONE)
        h.clock.t += 3600
        h.run(ONE, context(request_id="run-0002"))
        self.assertEqual(h.ready.sinces, [NOW, NOW])

    def test_readiness_is_judged_against_the_latest_delivery_time(self):
        h = Harness()
        h.ready.ok = False
        h.run(ONE)
        self.assertEqual(h.ready.sinces, [NOW])

    def test_old_accessor_is_destroyed_only_after_success(self):
        for name, setup in {
            "readiness": lambda h: setattr(h.ready, "ok", False),
            "delivery": lambda h: h.kube.fail_patch.add(h.destinations(ONE)[0][:2]),
            "metadata": lambda h: h.bao.fail.update(write_kv2=ApiError("write failed")),
        }.items():
            with self.subTest(failure=name):
                h = Harness()
                setup(h)
                result = h.run(ONE)
                self.assertEqual(result.outcome, "failed")
                self.assertEqual(h.bao.destroyed, [])
                self.assertEqual(h.kinds("destroy_secret_id_accessor"), [])

    def test_a_failed_destroy_leaves_cleanup_to_the_next_run_without_a_new_candidate(self):
        h = Harness()
        h.bao.fail["destroy_secret_id_accessor"] = ApiError("destroy unavailable")
        first = h.run(ONE)
        self.assertEqual((first.outcome, first.reason_code, first.state), ("failed", "cleanup_failed", ACTIVE))
        self.assertEqual(h.main(ONE)["secret_id_accessor"], "accessor-1")
        del h.bao.fail["destroy_secret_id_accessor"]
        h.clock.t += 60
        second = h.run(ONE, context(request_id="run-0002"))
        self.assertEqual(second.outcome, "not_due")
        self.assertEqual(h.bao.destroyed, [f"old-accessor-{ONE}"])
        self.assertEqual(h.bao.staging, {})
        self.assertEqual(len(h.bao.create_calls), 1)

    def test_a_safe_abort_destroys_only_the_unused_replacement(self):
        h = Harness()
        h.kube.crash_before.add("patch_secret")
        h.crash_run(ONE)
        replacement = h.tx(ONE)["new_accessor"]
        h.clock.t += h.config.abort_after + 1
        aborted = h.run(ONE, context(request_id="run-0002"))
        self.assertEqual((aborted.outcome, aborted.reason_code, aborted.state), ("failed", "delivery_failed", ABORTED))
        self.assertEqual(h.bao.destroyed, [replacement])
        self.assertNotIn(replacement, h.bao.registered.values())
        self.assertIn(f"old-accessor-{ONE}", h.bao.registered.values())
        self.assertEqual(h.bao.staging, {})
        self.assertEqual(h.kube.patched_values, [])
        # the next run starts a fresh transaction with a new candidate
        h.clock.t += 60
        again = h.run(ONE, context(request_id="run-0003"))
        self.assertEqual(again.outcome, "rotated")
        self.assertEqual(h.candidates.issued, ["synthetic-candidate-1", "synthetic-candidate-2"])
        self.assertEqual(h.kube.patched_values, ["synthetic-candidate-2"])
        # the finished abort is not repeated: the replacement is destroyed once
        self.assertEqual(h.bao.destroyed, [replacement, f"old-accessor-{ONE}"])

    def test_an_abort_that_crashes_midway_never_recreates_the_destroyed_candidate(self):
        h = Harness()
        h.kube.crash_before.add("patch_secret")
        h.crash_run(ONE)
        h.clock.t += h.config.abort_after + 1
        h.bao.fail["destroy_secret_id_accessor"] = ApiError("destroy unavailable")
        first = h.run(ONE, context(request_id="run-0002"))
        self.assertEqual(h.tx(ONE)["state"], ABORTED)
        self.assertEqual(first.outcome, "failed")
        del h.bao.fail["destroy_secret_id_accessor"]
        second = h.run(ONE, context(request_id="run-0003"))
        self.assertEqual(second.outcome, "rotated")
        first_candidate, later_candidate = h.bao.create_calls
        self.assertNotEqual(first_candidate, later_candidate)
        self.assertEqual(h.kube.patched_values, [later_candidate])

    def test_the_default_evaluator_never_promotes_so_the_replacement_is_kept(self):
        h = Harness()
        h.config.evaluate = adapters.evaluate_target
        h.kube.objects = {}
        result = h.run(ONE)
        self.assertEqual((result.outcome, result.reason_code, result.state), ("failed", "readiness_failed", DELIVERED_PENDING_READINESS))
        self.assertEqual(h.bao.destroyed, [])
        self.assertEqual(h.main(ONE)["secret_id_accessor"], f"old-accessor-{ONE}")

    def test_unconfirmed_bounds_stop_before_any_delivery(self):
        h = Harness()
        h.bao.wrong_bounds = True
        result = h.run(ONE)
        self.assertEqual((result.outcome, result.reason_code), ("failed", "invalid_state"))
        self.assertEqual(h.kube.patched_values, [])

    def test_unknown_role_and_ttl_mismatch_are_refused_before_any_write(self):
        for name, mutate in {
            "ttl": lambda h: h.bao.ttls.__setitem__(ONE, 5),
            "role": lambda h: h.bao.fail.update(read_role=ApiError("no such role")),
        }.items():
            with self.subTest(case=name):
                h = Harness()
                mutate(h)
                result = h.run(ONE)
                self.assertEqual(result.outcome, "failed")
                self.assertEqual(h.kinds("create_staging"), [])
                self.assertEqual(h.kube.patched_values, [])

    def test_an_expired_credential_without_recovery_state_is_refused(self):
        h = Harness()
        h.clock.t = h.main(ONE)["expires_at"] + 1
        result = h.run(ONE)
        self.assertEqual((result.outcome, result.reason_code), ("failed", "invalid_state"))
        self.assertEqual(h.kinds("create_staging"), [])

    def test_the_random_secret_id_endpoint_and_accessor_listing_are_never_called(self):
        h = Harness()
        h.bao.lose_create_response = True
        h.run(ONE)
        names = {event[0] for event in h.events}
        self.assertTrue(names <= {
            "read_role", "lookup_secret_id", "create_custom_secret_id", "destroy_secret_id_accessor",
            "read_kv2", "write_kv2", "create_staging", "read_staging", "delete_staging",
            "patch_secret", "readiness", "create_lease", "update_lease", "delete_lease",
        }, names)


class LeaseTests(unittest.TestCase):
    def live_lease(self, h, holder="someone-else", renewed=NOW - 10, duration=900):
        h.kube.leases[(LEASE_NAMESPACE, rotation.LEASE_NAME)] = {
            "holder": holder, "renew_time": renewed, "duration_seconds": duration, "resource_version": "7",
        }

    def test_a_live_lease_held_by_another_run_blocks_before_any_openbao_call(self):
        h = Harness()
        self.live_lease(h)
        with self.assertRaises(RotationError) as caught:
            h.run(ONE)
        self.assertEqual(caught.exception.reason_code, "lease_conflict")
        self.assertEqual(h.kinds("read_role"), [])
        self.assertEqual(h.kinds("read_kv2"), [])
        self.assertEqual(h.kube.leases[(LEASE_NAMESPACE, rotation.LEASE_NAME)]["holder"], "someone-else")

    def test_a_sweep_is_blocked_by_the_same_lease(self):
        h = Harness()
        self.live_lease(h)
        with self.assertRaises(RotationError) as caught:
            h.sweep()
        self.assertEqual(caught.exception.reason_code, "lease_conflict")
        self.assertEqual(h.kinds("read_role"), [])

    def test_an_expired_lease_is_taken_over_after_the_lock_delay_and_then_released(self):
        h = Harness()
        self.live_lease(h, renewed=NOW - 900 - 90)
        result = h.run(ONE)
        self.assertEqual(result.outcome, "rotated")
        self.assertGreaterEqual(len(h.kinds("update_lease")), 1)
        self.assertNotIn((LEASE_NAMESPACE, rotation.LEASE_NAME), h.kube.leases)

    def test_a_configured_lock_delay_is_the_one_that_applies(self):
        h = Harness()
        h.config.lock_delay = 500
        self.live_lease(h, renewed=NOW - 900 - 499)
        with self.assertRaises(RotationError):
            h.run(ONE)
        h = Harness()
        h.config.lock_delay = 500
        self.live_lease(h, renewed=NOW - 900 - 500)
        self.assertEqual(h.run(ONE).outcome, "rotated")

    def test_a_takeover_waits_for_expiry_plus_the_lock_delay_to_the_second(self):
        for renewed, allowed in ((NOW - 900 - 90, True), (NOW - 900 - 89, False), (NOW - 900, False), (NOW - 899, False)):
            with self.subTest(renewed_ago=NOW - renewed):
                h = Harness()
                self.live_lease(h, renewed=renewed)
                if allowed:
                    self.assertEqual(h.run(ONE).outcome, "rotated")
                else:
                    with self.assertRaises(RotationError) as caught:
                        h.run(ONE)
                    self.assertEqual(caught.exception.reason_code, "lease_conflict")
                    self.assertEqual(h.kinds("read_role"), [])

    def test_two_takers_of_an_expired_lease_cannot_both_win(self):
        h = Harness()
        self.live_lease(h, renewed=NOW - 5000)
        original = h.kube.update_lease

        def lose_race(*args):
            # another run takes the lease between our read and our update
            h.kube.leases[(LEASE_NAMESPACE, rotation.LEASE_NAME)]["resource_version"] = "99"
            return original(*args)

        h.kube.update_lease = lose_race
        with self.assertRaises(RotationError) as caught:
            h.run(ONE)
        self.assertEqual(caught.exception.reason_code, "lease_conflict")
        self.assertEqual(h.kinds("read_role"), [])

    def test_the_lease_is_acquired_before_the_first_read_and_released_on_success(self):
        h = Harness()
        h.run(ONE)
        self.assertLess(h.index("create_lease"), h.index("read_role"))
        self.assertGreater(h.index("delete_lease"), h.index("delete_staging"))
        self.assertEqual(h.kube.leases, {})

    def test_the_lease_is_released_after_a_failed_target(self):
        h = Harness()
        h.ready.ok = False
        self.assertEqual(h.run(ONE).outcome, "failed")
        self.assertEqual(h.kube.leases, {})

    def test_a_lost_process_leaves_its_lease_in_place(self):
        h = Harness()
        h.kube.crash_before.add("patch_secret")
        try:
            h.run(ONE)
        except ProcessLost:
            pass
        self.assertEqual(len(h.kube.leases), 1)

    def test_a_release_that_fails_is_logged_and_leaves_the_lease_to_expire(self):
        h = Harness()
        h.kube.fail_lease_delete = True
        with self.assertLogs(rotation.LOG, level="WARNING") as logs:
            self.assertEqual(h.run(ONE).outcome, "rotated")
        self.assertTrue(any("lease not released" in line for line in logs.output))
        self.assertIn((LEASE_NAMESPACE, rotation.LEASE_NAME), h.kube.leases)

    def test_release_never_deletes_a_lease_that_another_run_now_holds(self):
        h = Harness()
        original = h.kube.patch_secret

        def steal(*args):
            h.kube.leases[(LEASE_NAMESPACE, rotation.LEASE_NAME)]["holder"] = "thief"
            return original(*args)

        h.kube.patch_secret = steal
        h.run(ONE)
        self.assertEqual(h.kube.leases[(LEASE_NAMESPACE, rotation.LEASE_NAME)]["holder"], "thief")

    def test_a_sweep_holds_one_lease_for_every_target(self):
        h = Harness()
        h.sweep()
        self.assertEqual(len(h.kinds("create_lease")), 1)
        self.assertGreater(len(h.kinds("update_lease")), 2)  # renewals between targets and steps
        self.assertEqual(len(h.kinds("delete_lease")), 1)


class ContextTests(unittest.TestCase):
    BAD = {
        "no request id": {"request_id": ""},
        "path in request id": {"request_id": "../x"},
        "request id too long": {"request_id": "a" * 101},
        "no actor": {"actor": ""},
        "blank role": {"role": "   "},
        "no reason": {"reason": ""},
        "control character": {"reason": "line\nbreak"},
        "delete character": {"reason": "a\x7fb"},
        "oversize": {"actor": "a" * 257},
        "not a string": {"actor": 5},
    }

    def test_malformed_context_is_rejected_before_any_client_call(self):
        for name, overrides in self.BAD.items():
            with self.subTest(context=name):
                h = Harness()
                with self.assertRaises(RotationError) as caught:
                    h.run(ONE, context(**overrides))
                self.assertEqual(caught.exception.reason_code, "invalid_state")
                self.assertEqual(h.events, [])
                h2 = Harness()
                with self.assertRaises(RotationError):
                    h2.sweep(context(**overrides))
                self.assertEqual(h2.events, [])

    def test_a_missing_context_is_rejected_before_any_client_call(self):
        h = Harness()
        with self.assertRaises(RotationError):
            rotation.rotate_target(h.registry, ONE, None, h.clients, h.config, h.clock)
        self.assertEqual(h.events, [])

    def test_an_unknown_target_is_rejected_before_any_client_call(self):
        h = Harness()
        with self.assertRaises(RotationError):
            h.run("born-inventory-netbox-reader")
        self.assertEqual(h.events, [])

    def test_a_manual_context_is_recorded_as_manual_and_leaves_the_scheduled_stamp(self):
        h = Harness()
        before = h.main(ONE)["last_scheduled_success_at"]
        h.run(ONE, context(role="engineer", actor="admin-subject", reason="drill"))
        main = h.main(ONE)
        self.assertEqual((main["latest_run_kind"], main["actor"], main["role"], main["reason"]), ("manual", "admin-subject", "engineer", "drill"))
        self.assertEqual(main["last_scheduled_success_at"], before)
        self.assertTrue(validate_record(main))


class SweepTests(unittest.TestCase):
    def test_a_sweep_rotates_every_runtime_target(self):
        h = Harness()
        sweep = h.sweep()
        self.assertEqual([r.target for r in sweep.results], sorted([ONE, TWO, THREE]))
        self.assertEqual({r.outcome for r in sweep.results}, {"rotated"})
        self.assertEqual(sweep.failed, ())

    def test_a_sweep_continues_past_a_failed_target_and_reports_it(self):
        h = Harness()
        namespace, name, _ = h.destinations(ONE)[0]
        h.kube.fail_patch.add((namespace, name))
        sweep = h.sweep()
        by_target = {r.target: r for r in sweep.results}
        self.assertEqual(by_target[ONE].outcome, "failed")
        self.assertEqual(by_target[ONE].reason_code, "delivery_failed")
        self.assertEqual(by_target[TWO].outcome, "rotated")
        self.assertEqual(by_target[THREE].outcome, "rotated")
        self.assertEqual([r.target for r in sweep.failed], [ONE])
        self.assertEqual(h.kube.leases, {})
        self.assertEqual(h.main(ONE)["last_failure_reason_code"], "delivery_failed")
        self.assertNotIn(f"old-accessor-{ONE}", h.bao.destroyed)

    def test_a_sweep_stops_when_it_loses_the_lease(self):
        h = Harness()
        original = h.kube.patch_secret

        def steal(*args):
            h.kube.leases[(LEASE_NAMESPACE, rotation.LEASE_NAME)]["holder"] = "thief"
            return original(*args)

        h.kube.patch_secret = steal
        with self.assertRaises(RotationError) as caught:
            h.sweep()
        self.assertEqual(caught.exception.reason_code, "lease_conflict")
        self.assertEqual(len(h.kinds("create_custom_secret_id")), 1)


class GuardTests(unittest.TestCase):
    """One test per guard of the engine that no other test reaches."""

    def crash_before_delivery(self, h, target=ONE):
        h.kube.crash_before.add("patch_secret")
        h.crash_run(target)

    def test_a_destination_that_leaves_no_marker_blocks_the_promotion(self):
        h = Harness()
        h.kube.skip_marker = True
        result = h.run(ONE)
        self.assertEqual((result.outcome, result.reason_code), ("failed", "delivery_failed"))
        self.assertEqual(h.bao.destroyed, [])
        self.assertEqual(h.main(ONE)["secret_id_accessor"], f"old-accessor-{ONE}")
        self.assertNotEqual(h.tx(ONE)["state"], ACTIVE)

    def test_a_candidate_with_the_wrong_use_limit_is_refused(self):
        for uses in (1, False, "1"):
            with self.subTest(num_uses=uses):
                h = Harness()
                h.bao.num_uses = uses
                result = h.run(ONE)
                self.assertEqual((result.outcome, result.reason_code), ("failed", "invalid_state"))
                self.assertEqual(h.kube.patched_values, [])

    def test_the_documented_decimal_strings_are_accepted_as_bounds(self):
        h = Harness()
        h.bao.as_strings = True
        self.assertEqual(h.run(ONE).outcome, "rotated")

    def test_a_malformed_lookup_response_is_refused(self):
        responses = {
            "not a mapping": "garbage",
            "no accessor": {"secret_id_ttl": 1, "secret_id_num_uses": 0},
        }
        for name, response in responses.items():
            with self.subTest(response=name):
                h = Harness()
                h.bao.lookup_override = response
                result = h.run(ONE)
                self.assertEqual((result.outcome, result.reason_code), ("failed", "invalid_state"))
                self.assertEqual(h.kinds("create_custom_secret_id"), [])
                self.assertEqual(h.kube.patched_values, [])

    def test_a_missing_role_is_refused_before_any_write(self):
        h = Harness()
        h.bao.role_missing = True
        result = h.run(ONE)
        self.assertEqual((result.outcome, result.reason_code), ("failed", "invalid_state"))
        self.assertEqual(h.kinds("create_staging"), [])
        self.assertEqual(h.kinds("write_kv2"), [h.kinds("write_kv2")[0]] if h.kinds("write_kv2") else [])

    def test_unusable_issue_or_expiry_times_are_refused(self):
        for field in ("issued_at", "expires_at"):
            with self.subTest(field=field):
                h = Harness()
                record, version = h.bao.records[h.entry(ONE)["metadata_path"]]
                record[field] = "soon"
                h.bao.records[h.entry(ONE)["metadata_path"]] = (record, version)
                result = h.run(ONE)
                self.assertEqual((result.outcome, result.reason_code), ("failed", "invalid_state"))
                self.assertEqual(h.kinds("create_staging"), [])

    def test_rotation_is_due_exactly_at_the_threshold_and_not_a_second_before(self):
        due = registry.thresholds(REGISTRY["runtime_targets"][ONE]).rotation_due_after
        h = Harness()
        h.seed(ONE, age=due)
        self.assertEqual(h.run(ONE).outcome, "rotated")
        h = Harness()
        h.seed(ONE, age=due - 1)
        self.assertEqual(h.run(ONE).outcome, "not_due")

    def test_a_credential_that_expires_this_second_is_refused(self):
        h = Harness()
        h.clock.t = h.main(ONE)["expires_at"]
        self.assertEqual(h.run(ONE).reason_code, "invalid_state")
        h = Harness()
        h.clock.t = h.main(ONE)["expires_at"] - 1
        self.assertEqual(h.run(ONE).outcome, "rotated")

    def test_the_abort_window_closes_exactly_at_abort_after(self):
        h = Harness()
        self.crash_before_delivery(h)
        h.clock.t = NOW + h.config.abort_after - 1
        self.assertEqual(h.run(ONE, context(request_id="run-0002")).outcome, "rotated")
        h = Harness()
        self.crash_before_delivery(h)
        h.clock.t = NOW + h.config.abort_after
        self.assertEqual(h.run(ONE, context(request_id="run-0002")).state, ABORTED)

    def corrupt_transaction(self, change):
        """Corrupt the record of a finished rotation. The target is then not
        due, so only the record check can stop the next run."""
        h = Harness()
        self.assertEqual(h.run(ONE).outcome, "rotated")
        path = rotation.transaction_path(h.entry(ONE))
        record, version = h.bao.records[path]
        h.bao.records[path] = (change(record), version)
        h.clock.t += 60
        return h, h.run(ONE, context(request_id="run-0002"))

    def test_a_malformed_transaction_record_is_refused_before_any_write(self):
        def drop(key):
            return lambda r: {k: v for k, v in r.items() if k != key}
        changes = {
            "not a mapping": lambda r: 5,
            "extra key": lambda r: {**r, "extra": 1},
            "missing key": drop("cleanup_done"),
            "other schema": lambda r: {**r, "schema_version": 2},
            "unknown state": lambda r: {**r, "state": "bogus"},
            "other target": lambda r: {**r, "target": "other"},
            "numeric transaction id": lambda r: {**r, "transaction_id": 7},
        }
        for name, change in changes.items():
            with self.subTest(record=name):
                h, result = self.corrupt_transaction(change)
                self.assertEqual((result.outcome, result.reason_code), ("failed", "invalid_state"))
                self.assertEqual(len(h.bao.create_calls), 1)

    def corrupt_staging(self, change):
        h = Harness()
        h.bao.crash_before.add("create_custom_secret_id")
        h.crash_run(ONE)
        path = rotation.staging_path(h.entry(ONE))
        h.bao.staging[path] = change(h.bao.staging[path])
        h.clock.t += 60
        return h, h.run(ONE, context(request_id="run-0002"))

    def test_a_staging_record_that_does_not_fit_the_transaction_is_refused(self):
        changes = {
            "not a mapping": lambda r: "text",
            "other transaction": lambda r: {**r, "transaction_id": "run-9999-v1"},
            "numeric secret": lambda r: {**r, "secret_id": 5},
            "empty secret": lambda r: {**r, "secret_id": ""},
        }
        for name, change in changes.items():
            with self.subTest(staging=name):
                h, result = self.corrupt_staging(change)
                self.assertEqual((result.outcome, result.reason_code), ("failed", "invalid_state"))
                self.assertEqual(h.bao.create_calls, [])

    def test_a_staging_record_that_appears_during_the_first_write_is_refused(self):
        h = Harness()
        h.bao.staging_race = True
        result = h.run(ONE)
        self.assertEqual((result.outcome, result.reason_code), ("failed", "invalid_state"))
        self.assertEqual(h.bao.create_calls, [])

    def test_a_create_that_fails_without_registering_is_retried_with_the_same_candidate(self):
        h = Harness()
        h.bao.fail["create_custom_secret_id"] = ApiError("denied")
        first = h.run(ONE)
        self.assertEqual((first.outcome, first.reason_code, first.state), ("failed", "create_failed", PENDING_CREATE))
        del h.bao.fail["create_custom_secret_id"]
        h.clock.t += 60
        second = h.run(ONE, context(request_id="run-0002"))
        self.assertEqual(second.outcome, "rotated")
        self.assertEqual(h.candidates.issued, ["synthetic-candidate-1"])
        self.assertEqual(h.bao.create_calls, ["synthetic-candidate-1"] * 2)

    def test_a_missing_staged_secret_stops_an_undelivered_transaction(self):
        h = Harness()
        self.crash_before_delivery(h)
        h.bao.staging.clear()
        result = h.run(ONE, context(request_id="run-0002"))
        self.assertEqual((result.outcome, result.reason_code), ("failed", "invalid_state"))
        self.assertEqual(h.kube.patched_values, [])

    def test_a_fully_delivered_transaction_does_not_need_its_staged_secret_to_retry_readiness(self):
        h = Harness()
        h.ready.ok = False
        h.run(ONE)
        h.bao.staging.clear()
        h.ready.ok = True
        h.clock.t += 60
        self.assertEqual(h.run(ONE, context(request_id="run-0002")).outcome, "rotated")

    def test_the_reconcile_hook_runs_after_delivery_and_before_readiness(self):
        h = Harness()
        calls = []
        h.config.reconcile = lambda entry, transaction_id: (
            calls.append(transaction_id), h.events.append(("reconcile",)))
        result = h.run(ONE)
        self.assertEqual(calls, [result.transaction_id])
        self.assertLess(h.index("patch_secret"), h.index("reconcile"))
        self.assertLess(h.index("reconcile"), h.index("readiness"))

    def test_a_failed_reconcile_keeps_the_replacement_and_both_accessors(self):
        h = Harness()

        def broken(entry, transaction_id):
            raise ApiError(f"restart refused with token {TOKEN}")

        h.config.reconcile = broken
        result = h.run(ONE)
        self.assertEqual((result.outcome, result.reason_code, result.state), ("failed", "readiness_failed", DELIVERED_PENDING_READINESS))
        self.assertEqual(h.bao.destroyed, [])
        self.assertEqual(h.kinds("readiness"), [])

    def test_a_marker_found_before_the_abort_window_makes_the_transaction_delivered(self):
        h = Harness()
        h.kube.crash_after.add("patch_secret")
        h.crash_run(ONE)
        self.assertEqual(h.tx(ONE)["state"], ISSUED_PENDING_DELIVERY)
        h.ready.ok = False
        result = h.run(ONE, context(request_id="run-0002"))
        self.assertEqual(result.state, DELIVERED_PENDING_READINESS)

    def test_a_failed_marker_read_stops_before_any_delivery_and_says_why(self):
        h = Harness()
        h.kube.fail_get_secret = True
        result = h.run(ONE)
        self.assertEqual((result.outcome, result.reason_code), ("failed", "lookup_failed"))
        self.assertEqual(h.kube.patched_values, [])

    def test_a_lease_created_by_another_run_during_acquire_is_a_conflict(self):
        h = Harness()
        h.kube.lease_race = True
        with self.assertRaises(RotationError) as caught:
            h.run(ONE)
        self.assertEqual(caught.exception.reason_code, "lease_conflict")
        self.assertEqual(h.kinds("read_role"), [])

    def test_a_failed_lease_update_during_takeover_is_a_conflict(self):
        h = Harness()
        h.kube.leases[(LEASE_NAMESPACE, rotation.LEASE_NAME)] = {
            "holder": "gone", "renew_time": NOW - 5000, "duration_seconds": 900, "resource_version": "7"}
        h.kube.fail_lease_update = True
        with self.assertRaises(RotationError) as caught:
            h.run(ONE)
        self.assertEqual(caught.exception.reason_code, "lease_conflict")
        self.assertEqual(h.kinds("read_role"), [])

    def test_a_lease_deleted_by_another_run_between_targets_stops_the_sweep(self):
        h = Harness()
        original = h.kube.patch_secret

        def vanish(*args):
            h.kube.leases.clear()
            return original(*args)

        h.kube.patch_secret = vanish
        with self.assertRaises(RotationError) as caught:
            h.sweep()
        self.assertEqual(caught.exception.reason_code, "lease_conflict")


class FencingTests(unittest.TestCase):
    """A Lease cannot stop a write that is already in flight, so the engine
    renews before every mutating step and the destinations reject stale writes."""

    def crash_before_first_delivery(self, h):
        """TWO delivers to its KV record first. Stop before that write."""
        record_path = h.entry(TWO)["destinations"][0]["path"]
        original = h.bao.write_kv2

        def crash(path, data, cas):
            if path == record_path and adapters.TRANSACTION_KEY in data:
                h.bao.write_kv2 = original
                raise ProcessLost()
            return original(path, data, cas)

        h.bao.write_kv2 = crash
        h.crash_run(TWO)

    def stalled_delivery(self, h, target=ONE):
        """Run 1 stalls inside its first delivery write. While it stalls the
        Lease expires, a second run takes over and, after the abort window,
        safely aborts and destroys the candidate. Then the stalled write goes
        on. Returns (first result, second result)."""
        original = h.kube.patch_secret
        outcome = {}

        def stall(*args):
            if "second" not in outcome:
                outcome["second"] = None  # not reentrant
                h.clock.t += h.config.abort_after + h.config.lease_duration + 1
                outcome["second"] = h.run(target, context(request_id="run-0002"))
            return original(*args)

        h.kube.patch_secret = stall
        first = h.run(target)
        return first, outcome["second"]

    def test_a_stalled_delivery_cannot_install_a_candidate_that_a_successor_destroyed(self):
        h = Harness()
        first, second = self.stalled_delivery(h)
        self.assertEqual((second.outcome, second.state), ("failed", ABORTED))
        self.assertEqual(first.outcome, "failed")
        namespace, name, key = h.destinations(ONE)[0]
        held = h.kube.secrets[(namespace, name)]["data"][key]
        self.assertNotIn(held, ("synthetic-candidate-1",))
        self.assertEqual(held, f"old-value-of-{name}")
        self.assertNotIn("synthetic-candidate-1", h.bao.registered)
        self.assertNotIn(h.kube.secrets[(namespace, name)]["annotations"].get(adapters.TRANSACTION_ANNOTATION), (first.transaction_id,))

    def test_the_abort_fences_every_destination_before_it_destroys_the_replacement(self):
        h = Harness()
        self.crash_before_first_delivery(h)
        h.clock.t += h.config.abort_after + 1
        result = h.run(TWO, context(request_id="run-0002"))
        self.assertEqual(result.state, ABORTED)
        record_path = h.entry(TWO)["destinations"][0]["path"]
        fenced = h.bao.records[record_path][0]
        self.assertEqual(fenced[adapters.FENCE_KEY], result.transaction_id)
        _, name, _key = h.destinations(TWO)[0]
        self.assertEqual(
            h.kube.secrets[("external-secrets", name)]["annotations"][adapters.FENCE_ANNOTATION], result.transaction_id)
        fences = [i for i, e in enumerate(h.events) if e[0] == "annotate_secret"]
        self.assertLess(fences[0], h.index("destroy_secret_id_accessor"))

    def test_a_marker_written_while_the_abort_fences_stops_the_abort(self):
        h = Harness()
        self.crash_before_first_delivery(h)
        transaction_id = h.tx(TWO)["transaction_id"]
        record_path = h.entry(TWO)["destinations"][0]["path"]
        _, name, key = h.destinations(TWO)[0]
        original_write = h.bao.write_kv2
        original_patch = h.kube.patch_secret

        def stale_delivery_to_the_other_destination(path, data, cas):
            original_write(path, data, cas)
            if path == record_path and adapters.FENCE_KEY in data:
                current = h.kube.get_secret("external-secrets", name)
                original_patch("external-secrets", name, key, "synthetic-candidate-1",
                               {adapters.TRANSACTION_ANNOTATION: transaction_id,
                                adapters.VERSION_ANNOTATION: "4"}, current["resource_version"])

        h.bao.write_kv2 = stale_delivery_to_the_other_destination
        h.clock.t += h.config.abort_after + 1
        h.ready.ok = False
        result = h.run(TWO, context(request_id="run-0002"))
        self.assertEqual(result.state, DELIVERED_PENDING_READINESS)
        self.assertEqual(h.bao.destroyed, [])
        self.assertIn("synthetic-candidate-1", h.bao.registered)

    def test_a_destination_that_changes_while_the_abort_fences_it_stops_the_abort(self):
        h = Harness()
        h.kube.crash_before.add("patch_secret")
        h.crash_run(ONE)
        namespace, name, key = h.destinations(ONE)[0]
        original_annotate = h.kube.annotate_secret
        original_patch = h.kube.patch_secret

        def stale_write_lands_first(ns, nm, annotations, resource_version):
            current = h.kube.get_secret(ns, nm)
            original_patch(ns, nm, key, "synthetic-candidate-1",
                           {adapters.TRANSACTION_ANNOTATION: h.tx(ONE)["transaction_id"],
                            adapters.VERSION_ANNOTATION: "4"}, current["resource_version"])
            return original_annotate(ns, nm, annotations, resource_version)

        h.kube.annotate_secret = stale_write_lands_first
        h.clock.t += h.config.abort_after + 1
        result = h.run(ONE, context(request_id="run-0002"))
        self.assertEqual((result.outcome, result.reason_code), ("failed", "delivery_failed"))
        self.assertEqual(h.bao.destroyed, [])
        self.assertIn("synthetic-candidate-1", h.bao.registered)

    def test_an_abort_stays_blocked_when_a_destination_secret_is_missing(self):
        """Delivery can create a missing Secret (`patch_secret` with no
        version creates it), so a stale write could still create it: absence
        is not a fence, and the abort fails closed."""
        h = Harness()
        h.kube.crash_before.add("patch_secret")
        h.crash_run(ONE)
        namespace, name, _key = h.destinations(ONE)[0]
        del h.kube.secrets[(namespace, name)]
        h.clock.t += h.config.abort_after + 1
        result = h.run(ONE, context(request_id="run-0002"))
        self.assertEqual((result.outcome, result.reason_code), ("failed", "delivery_failed"))
        self.assertNotEqual(h.tx(ONE)["state"], ABORTED)
        self.assertEqual(h.bao.destroyed, [])
        self.assertIn("synthetic-candidate-1", h.bao.registered)

    def test_a_run_that_outlives_its_lease_stops_before_it_creates_the_candidate(self):
        h = Harness()
        original = h.bao.lookup_secret_id

        def slow(approle, secret_id):
            h.clock.t += h.config.lease_duration + 1
            return original(approle, secret_id)

        h.bao.lookup_secret_id = slow
        result = h.run(ONE)
        self.assertEqual((result.outcome, result.reason_code), ("failed", "lease_conflict"))
        self.assertEqual(h.bao.create_calls, [])

    def test_a_run_that_outlives_its_lease_stops_before_it_delivers(self):
        h = Harness()
        original = h.bao.create_custom_secret_id

        def slow(approle, secret_id, params):
            result = original(approle, secret_id, params)
            h.clock.t += h.config.lease_duration + 1
            return result

        h.bao.create_custom_secret_id = slow
        result = h.run(ONE)
        self.assertEqual((result.outcome, result.reason_code), ("failed", "lease_conflict"))
        self.assertEqual(h.kube.patched_values, [])

    def test_a_run_that_outlives_its_lease_stops_before_it_destroys_the_old_accessor(self):
        h = Harness()
        original = h.ready.__call__

        def slow(entry, gates, since, inventory):
            h.clock.t += h.config.lease_duration + 1
            return original(entry, gates, since, inventory)

        h.config.evaluate = slow
        result = h.run(ONE)
        self.assertEqual((result.outcome, result.reason_code), ("failed", "lease_conflict"))
        self.assertEqual(h.bao.destroyed, [])
        self.assertEqual(h.main(ONE)["secret_id_accessor"], f"old-accessor-{ONE}")

    def test_a_run_that_loses_the_lease_to_another_holder_stops_at_the_next_step(self):
        h = Harness()
        original = h.bao.create_custom_secret_id

        def takeover(approle, secret_id, params):
            result = original(approle, secret_id, params)
            h.kube.leases[(LEASE_NAMESPACE, rotation.LEASE_NAME)]["holder"] = "thief"
            return result

        h.bao.create_custom_secret_id = takeover
        result = h.run(ONE)
        self.assertEqual((result.outcome, result.reason_code), ("failed", "lease_conflict"))
        self.assertEqual(h.kube.patched_values, [])
        self.assertIsNone(h.tx(ONE)["last_failure_at"])  # the thief owns the state


class UnansweredCallTests(unittest.TestCase):
    """A client call that ends without a server response can still run on the
    server. The run then keeps its Lease, so a successor waits for expiry plus
    the lock delay."""

    def wait_for_expiry_and_lock_delay(self, h):
        h.clock.t = NOW + h.config.lease_duration + h.config.lock_delay

    def test_a_create_that_times_out_but_runs_later_cannot_register_an_aborted_candidate(self):
        h = Harness()
        h.config.abort_after = 1
        original = h.bao.create_custom_secret_id
        queued = []

        def lost_connection(*args):
            if not queued:
                queued.append(args)
                h.clock.t += 1
                raise ConnectionError("the response was lost while the server was processing")
            return original(*args)

        h.bao.create_custom_secret_id = lost_connection
        first = h.run(ONE)
        self.assertEqual((first.outcome, first.reason_code), ("failed", "create_failed"))
        self.assertEqual(len(h.kube.leases), 1)
        h.clock.t += 1
        with self.assertRaises(RotationError) as refused:
            h.run(ONE, context(request_id="run-0002"))
        self.assertEqual(refused.exception.reason_code, "lease_conflict")
        original(*queued[0])  # the queued request now runs on the server
        self.assertIn("synthetic-candidate-1", h.bao.registered)
        self.wait_for_expiry_and_lock_delay(h)
        second = h.run(ONE, context(request_id="run-0002"))
        self.assertEqual(second.state, ABORTED)
        self.assertNotIn("synthetic-candidate-1", h.bao.registered)
        self.assertEqual(h.bao.staging, {})

    def test_a_delivery_write_that_times_out_after_the_server_applied_it_keeps_the_lease(self):
        h = Harness()
        original = h.kube.patch_secret

        def lost_response(*args, **kwargs):
            original(*args, **kwargs)
            raise TimeoutError("the response was lost after the server applied the write")

        h.kube.patch_secret = lost_response
        first = h.run(ONE)
        self.assertEqual((first.outcome, first.reason_code), ("failed", "delivery_failed"))
        self.assertEqual(len(h.kube.leases), 1)
        h.clock.t += 1
        with self.assertRaises(RotationError) as refused:
            h.run(ONE, context(request_id="run-0002"))
        self.assertEqual(refused.exception.reason_code, "lease_conflict")
        self.assertEqual(h.bao.destroyed, [])
        h.kube.patch_secret = original
        self.wait_for_expiry_and_lock_delay(h)
        second = h.run(ONE, context(request_id="run-0002"))
        self.assertEqual(second.outcome, "rotated")
        self.assertEqual(h.delivered_values(ONE), ["synthetic-candidate-1"])
        self.assertEqual(h.bao.destroyed, [f"old-accessor-{ONE}"])

    def test_a_run_whose_calls_were_all_answered_deletes_its_lease(self):
        h = Harness()
        self.assertEqual(h.run(ONE).outcome, "rotated")
        self.assertEqual(h.kube.leases, {})

    def test_an_api_error_response_is_an_answer_and_the_lease_is_deleted(self):
        h = Harness()
        h.bao.fail["create_custom_secret_id"] = ApiError("denied")
        first = h.run(ONE)
        self.assertEqual((first.outcome, first.reason_code), ("failed", "create_failed"))
        self.assertEqual(h.kube.leases, {})
        del h.bao.fail["create_custom_secret_id"]
        self.assertEqual(h.run(ONE, context(request_id="run-0002")).outcome, "rotated")

    def test_an_early_unanswered_call_keeps_the_lease_though_later_targets_are_answered(self):
        h = Harness()
        original = h.bao.create_custom_secret_id
        queued = []

        def lost_connection(approle, *args):
            if approle == ONE and not queued:
                queued.append((approle,) + args)
                raise ConnectionError("the response was lost while the server was processing")
            return original(approle, *args)

        h.bao.create_custom_secret_id = lost_connection
        sweep = h.sweep()
        by_target = {r.target: r for r in sweep.results}
        self.assertEqual(by_target[ONE].reason_code, "create_failed")  # ONE sorts first
        self.assertEqual({by_target[TWO].outcome, by_target[THREE].outcome}, {"rotated"})
        self.assertEqual(len(h.kube.leases), 1)
        h.clock.t += 1
        with self.assertRaises(RotationError) as refused:
            h.run(ONE, context(request_id="run-0002"))
        self.assertEqual(refused.exception.reason_code, "lease_conflict")

    def renewal_that_loses_its_response(self, method):
        """Make the first renewal's call of `method` raise a transport error.
        `get_lease` runs once in `acquire`, so the renewal's read is the second."""
        h = Harness()
        original = getattr(h.kube, method)
        seen = []

        def lost(*args, **kwargs):
            seen.append(args)
            if len(seen) == (2 if method == "get_lease" else 1):
                raise ConnectionError("the response was lost while the server was processing")
            return original(*args, **kwargs)

        setattr(h.kube, method, lost)
        return h

    def assert_failed_run_keeps_lease_and_refuses_successor(self, h):
        first = h.run(ONE)
        self.assertEqual((first.outcome, first.reason_code), ("failed", "lease_conflict"))
        self.assertEqual(len(h.kube.leases), 1)
        h.clock.t += 1
        with self.assertRaises(RotationError) as refused:
            h.run(ONE, context(request_id="run-0002"))
        self.assertEqual(refused.exception.reason_code, "lease_conflict")

    def test_a_renewal_update_that_times_out_keeps_the_lease(self):
        h = self.renewal_that_loses_its_response("update_lease")
        self.assert_failed_run_keeps_lease_and_refuses_successor(h)

    def test_a_renewal_read_that_times_out_keeps_the_lease(self):
        h = self.renewal_that_loses_its_response("get_lease")
        self.assert_failed_run_keeps_lease_and_refuses_successor(h)

    def test_an_unanswered_call_in_a_sweep_keeps_the_lease_after_the_sweep(self):
        h = Harness()
        original = h.bao.read_role

        def lost(*args):
            raise ConnectionError("no response")

        h.bao.read_role = lost
        sweep = h.sweep()
        self.assertEqual({r.reason_code for r in sweep.failed}, {"lookup_failed"})
        self.assertEqual(len(h.kube.leases), 1)
        h.bao.read_role = original


class InFlightRaceTests(unittest.TestCase):
    """Codex round 2: a stalled read-then-write and a create delayed across a
    takeover. Neither can be fenced at the server, so the protocol is a Lease
    with a lock delay, a self-stop after every call, and an abort that resolves
    by lookup."""

    DELAY = 90  # the default lock delay: request_timeout 60 + clock_skew_margin 30
    PAST_EVERYTHING = 6 * 3600 + 900 + 1000

    @staticmethod
    def stall_nth_call(h, owner, method, nth, when=lambda *args: True, then=None):
        """Wrap `owner.method`. Its `nth` call that satisfies `when` stalls: time
        advances past every Lease window, `then()` runs (the successor), and
        only then does the call execute and return."""
        original = getattr(owner, method)
        state = {"seen": 0, "fired": False}

        def wrapper(*args, **kwargs):
            if when(*args):
                state["seen"] += 1
                if state["seen"] == nth and not state["fired"]:
                    state["fired"] = True
                    h.clock.t += InFlightRaceTests.PAST_EVERYTHING
                    state["successor"] = then()
            return original(*args, **kwargs)

        setattr(owner, method, wrapper)
        return state

    def test_a_delivery_stalled_before_its_version_read_cannot_install_a_destroyed_secret(self):
        h = Harness()
        stall = self.stall_nth_call(
            h, h.kube, "get_secret", 3, then=lambda: h.run(ONE, context(request_id="run-0002")))
        first = h.run(ONE)
        self.assertTrue(stall["fired"])
        self.assertEqual((stall["successor"].outcome, stall["successor"].state), ("failed", ABORTED))
        namespace, name, key = h.destinations(ONE)[0]
        self.assertEqual(h.kube.secrets[(namespace, name)]["data"][key], f"old-value-of-{name}")
        self.assertNotIn("synthetic-candidate-1", h.bao.registered)
        self.assertEqual((first.outcome, first.reason_code), ("failed", "lease_conflict"))

    def test_a_record_delivery_stalled_before_its_version_read_cannot_install_a_destroyed_secret(self):
        h = Harness()
        record_path = h.entry(TWO)["destinations"][0]["path"]
        stall = self.stall_nth_call(
            h, h.bao, "read_kv2", 3, when=lambda path: path == record_path,
            then=lambda: h.run(TWO, context(request_id="run-0002")))
        first = h.run(TWO)
        self.assertTrue(stall["fired"])
        self.assertEqual(stall["successor"].state, ABORTED)
        held = h.bao.records[record_path][0]
        self.assertNotEqual(held.get("secret_id"), "synthetic-candidate-1")
        self.assertNotIn("synthetic-candidate-1", h.bao.registered)
        self.assertEqual((first.outcome, first.reason_code), ("failed", "lease_conflict"))

    def test_a_create_in_flight_across_a_takeover_is_refused_a_successor_until_the_lock_delay_ends(self):
        h = Harness()
        outcome = {}
        original = h.bao.create_custom_secret_id

        def slow_create(approle, secret_id, params):
            lease = h.kube.leases[(LEASE_NAMESPACE, rotation.LEASE_NAME)]
            expiry = lease["renew_time"] + lease["duration_seconds"]
            h.clock.t = expiry + self.DELAY - 1  # past expiry, inside the lock delay
            try:
                h.run(ONE, context(request_id="run-0002"))
            except RotationError as error:
                outcome["successor"] = error.reason_code
            return original(approle, secret_id, params)

        h.bao.create_custom_secret_id = slow_create
        first = h.run(ONE)
        self.assertEqual(outcome.get("successor"), "lease_conflict")
        self.assertEqual((first.outcome, first.reason_code), ("failed", "lease_conflict"))
        self.assertEqual(h.kube.patched_values, [])
        # the create landed, and the transaction still owns it
        self.assertEqual(len(h.bao.create_calls), 1)
        h.clock.t += 1000
        resumed = h.run(ONE, context(request_id="run-0003"))
        self.assertEqual(resumed.outcome, "rotated")
        self.assertEqual(h.bao.create_calls, ["synthetic-candidate-1"])
        self.assertEqual(h.main(ONE)["secret_id_accessor"], "accessor-1")


class ConfigValidationTests(unittest.TestCase):
    """The protocol's bounds are checked before any client call."""

    def refused(self, h):
        for call in (lambda: h.run(ONE), h.sweep):
            with self.assertRaises(RotationError) as caught:
                call()
            self.assertEqual(caught.exception.reason_code, "invalid_state")
            self.assertEqual(h.events, [])

    def test_a_lock_delay_shorter_than_the_request_timeout_plus_the_skew_margin_is_refused(self):
        h = Harness()
        h.config.lock_delay = h.config.request_timeout + h.config.clock_skew_margin - 1
        self.refused(h)

    def test_the_least_allowed_lock_delay_is_accepted(self):
        h = Harness()
        h.config.lock_delay = h.config.request_timeout + h.config.clock_skew_margin
        self.assertEqual(h.run(ONE).outcome, "rotated")

    def test_the_default_lock_delay_is_the_least_allowed_one(self):
        config = RotationConfig(lease_namespace="x", request_timeout=40, clock_skew_margin=5)
        self.assertEqual(config.lock_delay, 45)
        self.assertEqual(rotation.MAX_REQUEST_SECONDS, RotationConfig(lease_namespace="x").request_timeout)

    def test_a_configured_lock_delay_is_kept(self):
        self.assertEqual(RotationConfig(lease_namespace="x", lock_delay=500).lock_delay, 500)

    def test_non_positive_or_non_integer_timing_values_are_refused(self):
        for name, value in (("request_timeout", 0), ("clock_skew_margin", -1), ("lease_duration", 0),
                            ("abort_after", 0), ("request_timeout", 1.5), ("lock_delay", True)):
            with self.subTest(name=name, value=value):
                h = Harness()
                setattr(h.config, name, value)
                self.refused(h)

    def test_a_lease_that_is_not_longer_than_the_skew_margin_is_refused(self):
        h = Harness()
        h.config.lease_duration = h.config.clock_skew_margin
        self.refused(h)

    def test_a_client_whose_declared_timeout_is_longer_than_the_configs_is_refused(self):
        for owner in ("bao", "kube"):
            with self.subTest(client=owner):
                h = Harness()
                getattr(h, owner).request_timeout = h.config.request_timeout + 1
                self.refused(h)

    def test_a_client_that_declares_no_timeout_is_refused(self):
        for owner in ("bao", "kube"):
            with self.subTest(client=owner):
                h = Harness()
                client = getattr(h, owner)
                type(client).request_timeout = None
                try:
                    self.refused(h)
                finally:
                    type(client).request_timeout = 60


class SelfStopTests(unittest.TestCase):
    """A call that returns after the Lease lapsed causes no further write."""

    LEASE_EVENTS = {"create_lease", "update_lease", "delete_lease"}

    def stalled_return(self, h, owner, method, when=lambda *args: True, nth=1):
        """Wrap `owner.method`: the `nth` call that satisfies `when` executes,
        then the clock moves past the Lease. Returns a dict with the event
        index just after that call."""
        original = getattr(owner, method)
        state = {"seen": 0}

        def wrapper(*args, **kwargs):
            result = original(*args, **kwargs)
            if when(*args):
                state["seen"] += 1
                if state["seen"] == nth:
                    state["after"] = len(h.events)
                    h.clock.t += h.config.lease_duration + 1
            return result

        setattr(owner, method, wrapper)
        return state

    def assert_nothing_after(self, h, state):
        self.assertIn("after", state, "the stalled call was never made")
        later = [e[0] for e in h.events[state["after"]:] if e[0] not in self.LEASE_EVENTS]
        self.assertEqual(later, [])

    def cases(self, h):
        main = h.entry(ONE)["metadata_path"]
        kube_owner = h.kube
        return {
            "staging create": (h.bao, "create_staging", lambda *a: True, 1, "fresh"),
            "create": (h.bao, "create_custom_secret_id", lambda *a: True, 1, "fresh"),
            "delivery": (kube_owner, "patch_secret", lambda *a: True, 1, "fresh"),
            "promote": (h.bao, "write_kv2", lambda path, data, cas: path == main, 1, "fresh"),
            "cleanup destroy": (h.bao, "destroy_secret_id_accessor", lambda *a: True, 1, "fresh"),
            "cleanup staging delete": (h.bao, "delete_staging", lambda *a: True, 1, "fresh"),
            "heartbeat stamp": (h.bao, "write_kv2", lambda path, data, cas: path == main, 2, "fresh"),
            "abort fence": (kube_owner, "annotate_secret", lambda *a: True, 1, "abort"),
            "abort destroy": (h.bao, "destroy_secret_id_accessor", lambda *a: True, 1, "abort"),
            "abort staging delete": (h.bao, "delete_staging", lambda *a: True, 1, "abort"),
        }

    def test_no_client_call_follows_a_call_that_returns_after_the_lease_lapsed(self):
        names = list(self.cases(Harness()))
        for name in names:
            with self.subTest(step=name):
                h = Harness()
                owner, method, when, nth, scenario = self.cases(h)[name]
                ctx = None
                if scenario == "abort":
                    h.kube.crash_before.add("patch_secret")
                    h.crash_run(ONE)
                    h.clock.t += h.config.abort_after + 1
                    ctx = context(request_id="run-0002")
                state = self.stalled_return(h, owner, method, when, nth)
                result = h.run(ONE, ctx)
                self.assertEqual((result.outcome, result.reason_code), ("failed", "lease_conflict"))
                self.assert_nothing_after(h, state)

    def test_a_run_that_lost_its_lease_records_no_failure(self):
        h = Harness()
        state = self.stalled_return(h, h.bao, "create_custom_secret_id")
        before = h.main(ONE)
        h.run(ONE)
        self.assertIsNone(h.tx(ONE)["last_failure_at"])
        self.assertEqual(h.main(ONE), before)

    def test_an_adapter_failure_after_the_lease_lapsed_reports_the_lease(self):
        h = Harness()

        def late_failure(namespace, name):
            h.clock.t += h.config.lease_duration + 1
            raise ApiError("the read failed")

        h.kube.get_secret = late_failure
        result = h.run(ONE)
        self.assertEqual((result.outcome, result.reason_code), ("failed", "lease_conflict"))

    def test_a_reconcile_hook_that_returns_after_the_lease_lapsed_stops_before_readiness(self):
        h = Harness()
        h.config.reconcile = lambda entry, tx: setattr(h.clock, "t", h.clock.t + h.config.lease_duration + 1)
        result = h.run(ONE)
        self.assertEqual((result.outcome, result.reason_code), ("failed", "lease_conflict"))
        self.assertEqual(h.kinds("readiness"), [])

    def test_a_readiness_check_that_returns_after_the_lease_lapsed_cannot_promote(self):
        h = Harness()
        original = h.ready.__call__

        def slow(entry, gates, since, inventory):
            result = original(entry, gates, since, inventory)
            h.clock.t += h.config.lease_duration + 1
            return result

        h.config.evaluate = slow
        result = h.run(ONE)
        self.assertEqual((result.outcome, result.reason_code), ("failed", "lease_conflict"))
        self.assertEqual(h.bao.destroyed, [])
        self.assertEqual(h.main(ONE)["secret_id_accessor"], f"old-accessor-{ONE}")

    def test_a_readiness_verdict_that_arrives_after_the_lease_lapsed_reports_the_lease(self):
        h = Harness()
        h.ready.ok = False
        original = h.ready.__call__

        def slow(entry, gates, since, inventory):
            result = original(entry, gates, since, inventory)
            h.clock.t += h.config.lease_duration + 1
            return result

        h.config.evaluate = slow
        result = h.run(ONE)
        self.assertEqual((result.outcome, result.reason_code), ("failed", "lease_conflict"))
        self.assertIsNone(h.tx(ONE)["last_failure_at"])

    def test_a_failure_after_the_lease_lapsed_is_not_recorded(self):
        h = Harness()
        before = h.main(ONE)

        def late_error(approle, secret_id):
            h.clock.t += h.config.lease_duration + 1
            raise ApiError("the lookup failed")

        h.bao.lookup_secret_id = late_error
        result = h.run(ONE)
        self.assertEqual((result.outcome, result.reason_code), ("failed", "lookup_failed"))
        self.assertEqual(h.main(ONE), before)
        self.assertIsNone(h.tx(ONE)["last_failure_at"])

    def test_a_pause_between_steps_is_caught_by_the_renewal(self):
        h = Harness()

        def slow_token_source():
            h.clock.t += h.config.lease_duration + 1  # a pause, not a client call
            return "synthetic-candidate-1"

        h.config.token_source = slow_token_source
        result = h.run(ONE)
        self.assertEqual((result.outcome, result.reason_code), ("failed", "lease_conflict"))
        self.assertEqual(h.kinds("create_staging"), [])

    def test_a_call_that_returns_exactly_at_the_margin_is_followed_by_no_call(self):
        h = Harness()
        state = {}
        original = h.bao.read_role

        def at_the_margin(approle):
            result = original(approle)
            state["after"] = len(h.events)
            h.clock.t += h.config.lease_duration - h.config.clock_skew_margin
            return result

        h.bao.read_role = at_the_margin
        self.assertEqual(h.run(ONE).reason_code, "lease_conflict")
        later = [e[0] for e in h.events[state["after"]:] if e[0] not in self.LEASE_EVENTS]
        self.assertEqual(later, [])

    def test_the_renewal_refuses_exactly_at_the_margin_and_not_a_second_before(self):
        for pause, allowed in ((900 - 30, False), (900 - 30 - 1, True)):
            with self.subTest(pause=pause):
                h = Harness()

                def paused():
                    h.clock.t += pause
                    return "synthetic-candidate-1"

                h.config.token_source = paused
                result = h.run(ONE)
                if allowed:
                    self.assertNotEqual(result.reason_code, "lease_conflict")
                else:
                    self.assertEqual(result.reason_code, "lease_conflict")
                    self.assertEqual(h.kinds("create_staging"), [])

    def test_the_margin_counts_a_call_that_returns_inside_the_skew_margin_as_late(self):
        h = Harness()
        original = h.bao.lookup_secret_id

        def inside_margin(approle, secret_id):
            h.clock.t += h.config.lease_duration - h.config.clock_skew_margin
            return original(approle, secret_id)

        h.bao.lookup_secret_id = inside_margin
        self.assertEqual(h.run(ONE).reason_code, "lease_conflict")
        h = Harness()
        original = h.bao.lookup_secret_id

        def just_before_margin(approle, secret_id):
            h.clock.t += h.config.lease_duration - h.config.clock_skew_margin - 1
            return original(approle, secret_id)

        h.bao.lookup_secret_id = just_before_margin
        self.assertNotEqual(h.run(ONE).reason_code, "lease_conflict")


class AbortByLookupTests(unittest.TestCase):
    """An abort resolves the candidate by lookup, not by the recorded accessor."""

    def aborted_setup(self):
        h = Harness()
        h.kube.crash_before.add("patch_secret")
        h.crash_run(ONE)
        h.clock.t += h.config.abort_after + 1
        return h

    def reregister_after_first_destroy(self, h):
        """A create that landed late registers the candidate again under a new
        accessor, right after the first destroy."""
        original = h.bao.destroy_secret_id_accessor
        state = {"n": 0}

        def destroy(approle, accessor):
            original(approle, accessor)
            state["n"] += 1
            if state["n"] == 1:
                h.bao.registered["synthetic-candidate-1"] = "accessor-late"

        h.bao.destroy_secret_id_accessor = destroy

    def test_a_candidate_registered_again_by_a_late_create_is_destroyed_too(self):
        h = self.aborted_setup()
        self.reregister_after_first_destroy(h)
        result = h.run(ONE, context(request_id="run-0002"))
        self.assertEqual((result.outcome, result.state), ("failed", ABORTED))
        self.assertEqual(h.bao.destroyed, ["accessor-1", "accessor-late"])
        self.assertNotIn("synthetic-candidate-1", h.bao.registered)
        self.assertEqual(h.bao.staging, {})

    def test_staging_is_kept_until_a_lookup_shows_the_candidate_unregistered(self):
        h = self.aborted_setup()
        original = h.bao.delete_staging

        def check(path):
            self.assertNotIn("synthetic-candidate-1", h.bao.registered)
            return original(path)

        h.bao.delete_staging = check
        h.run(ONE, context(request_id="run-0002"))
        lookups = [i for i, e in enumerate(h.events) if e[0] == "lookup_secret_id"]
        self.assertGreater(len(lookups), 1)
        self.assertGreater(h.index("delete_staging"), lookups[-1] - 1)

    def test_a_candidate_that_keeps_coming_back_keeps_its_staging_and_is_reported(self):
        h = self.aborted_setup()
        original = h.bao.destroy_secret_id_accessor

        def destroy(approle, accessor):
            original(approle, accessor)
            h.bao.registered["synthetic-candidate-1"] = f"accessor-again-{len(h.bao.destroyed)}"

        h.bao.destroy_secret_id_accessor = destroy
        result = h.run(ONE, context(request_id="run-0002"))
        self.assertEqual((result.outcome, result.reason_code), ("failed", "cleanup_failed"))
        self.assertEqual(len(h.bao.destroyed), rotation.ABORT_RESOLVE_ATTEMPTS)
        self.assertIn(rotation.staging_path(h.entry(ONE)), h.bao.staging)

    def test_a_later_run_finishes_an_abort_whose_cleanup_failed(self):
        h = self.aborted_setup()
        h.bao.fail["destroy_secret_id_accessor"] = ApiError("unavailable")
        first = h.run(ONE, context(request_id="run-0002"))
        self.assertEqual(first.reason_code, "cleanup_failed")
        del h.bao.fail["destroy_secret_id_accessor"]
        h.clock.t += 120
        h.run(ONE, context(request_id="run-0003"))
        self.assertNotIn("synthetic-candidate-1", h.bao.registered)
        self.assertEqual(h.bao.staging, {})

    def test_an_abort_resolves_a_candidate_whose_recorded_accessor_is_not_the_registered_one(self):
        h = self.aborted_setup()
        h.bao.registered["synthetic-candidate-1"] = "accessor-unrecorded"
        h.run(ONE, context(request_id="run-0002"))
        self.assertEqual(h.bao.destroyed, ["accessor-unrecorded"])
        self.assertNotIn("synthetic-candidate-1", h.bao.registered)

    def test_an_abort_with_staging_already_gone_has_nothing_to_resolve(self):
        h = self.aborted_setup()
        h.bao.fail["destroy_secret_id_accessor"] = ApiError("unavailable")
        h.run(ONE, context(request_id="run-0002"))
        del h.bao.fail["destroy_secret_id_accessor"]
        h.bao.staging.clear()
        h.clock.t += 120
        start = len(h.events)
        h.run(ONE, context(request_id="run-0003"))
        # no destroy for the aborted transaction; the later transaction destroys only the old accessor
        self.assertEqual(h.bao.destroyed, [f"old-accessor-{ONE}"])
        before_new_transaction = [e[0] for e in h.events[start:h.events.index(("create_staging", rotation.staging_path(h.entry(ONE))), start)]]
        self.assertNotIn("lookup_secret_id", before_new_transaction)
        self.assertNotIn("delete_staging", before_new_transaction)


class _PausingClock:
    """A clock that jumps past the Lease when the engine renews it from a
    named method: a pause between two steps, not inside a client call."""

    def __init__(self, base, events, method, nth, jump):
        self.base, self.events, self.method, self.nth, self.jump = base, events, method, nth, jump
        self.count = 0
        self.after = None

    def __call__(self):
        caller = sys._getframe(1).f_code.co_name
        if self.after is None and caller == "renew":
            # now() <- renew <- _fence <- the step that asked for the renewal
            if sys._getframe(3).f_code.co_name == self.method:
                self.count += 1
                if self.count == self.nth:
                    self.after = len(self.events)
                    self.base.t += self.jump
        return self.base()


class PauseBetweenStepsTests(unittest.TestCase):
    """Each renewal before a mutating step catches a pause that no client call
    shows: the step after it does not happen."""

    MUTATING = {
        "create_staging", "create_custom_secret_id", "patch_secret", "annotate_secret",
        "write_kv2", "destroy_secret_id_accessor", "delete_staging",
    }
    SITES = {
        "before the staging create": ("_stage_candidate", 1, "fresh"),
        "before the create": ("_issue", 1, "fresh"),
        "before the promote write": ("_promote", 1, "fresh"),
        "before the previous accessor is destroyed": ("_cleanup", 1, "fresh"),
        "before the staging delete": ("_cleanup", 2, "fresh"),
        "before the heartbeat stamp": ("_stamp_heartbeat", 1, "fresh"),
        "before the destination fences": ("_abort", 1, "abort"),
        "before the abort is recorded": ("_abort", 2, "abort"),
        "before the abort looks the candidate up": ("_finish_abort", 1, "abort"),
        "before the abort destroys": ("_finish_abort", 2, "abort"),
        "before the abort looks the candidate up again": ("_finish_abort", 3, "abort"),
        "before the abort deletes staging": ("_finish_abort", 4, "abort"),
    }

    def test_a_pause_before_each_mutating_step_stops_that_step(self):
        for name, (method, nth, scenario) in self.SITES.items():
            with self.subTest(site=name):
                h = Harness()
                ctx = None
                if scenario == "abort":
                    h.kube.crash_before.add("patch_secret")
                    h.crash_run(ONE)
                    h.clock.t += h.config.abort_after + 1
                    ctx = context(request_id="run-0002")
                pausing = _PausingClock(h.clock, h.events, method, nth, h.config.lease_duration)
                h.clock = pausing
                result = h.run(ONE, ctx)
                self.assertIsNotNone(pausing.after, "the pause never happened")
                self.assertEqual((result.outcome, result.reason_code), ("failed", "lease_conflict"))
                later = [e[0] for e in h.events[pausing.after:] if e[0] in self.MUTATING]
                self.assertEqual(later, [])


class EveryStepFencesTests(unittest.TestCase):
    """The Lease runs out right after one client call. The next mutating step
    must not happen."""

    @staticmethod
    def expire_after(h, owner, method, when=lambda *args: True):
        """Wrap `owner.method`: once, after a call that satisfies `when`, the
        clock moves past the Lease duration."""
        original = getattr(owner, method)
        state = {"done": False}

        def wrapper(*args, **kwargs):
            result = original(*args, **kwargs)
            if not state["done"] and when(*args):
                state["done"] = True
                h.clock.t += h.config.lease_duration + 1
            return result

        setattr(owner, method, wrapper)

    def aborting_run(self, h):
        """A transaction that is issued and undelivered, past the abort window."""
        h.kube.crash_before.add("patch_secret")
        h.crash_run(ONE)
        h.clock.t += h.config.abort_after + 1

    def assert_stopped(self, result):
        self.assertEqual((result.outcome, result.reason_code), ("failed", "lease_conflict"))

    def test_the_lease_is_confirmed_before_the_staging_record_is_created(self):
        h = Harness()
        self.expire_after(h, h.bao, "read_staging")
        self.assert_stopped(h.run(ONE))
        self.assertEqual(h.kinds("create_staging"), [])

    def test_the_lease_is_confirmed_before_the_previous_accessor_is_destroyed(self):
        h = Harness()
        self.expire_after(h, h.bao, "write_kv2", lambda path, data, cas: data.get("state") == ACTIVE)
        self.assert_stopped(h.run(ONE))
        self.assertEqual(h.bao.destroyed, [])

    def test_the_lease_is_confirmed_before_the_staging_record_is_deleted(self):
        h = Harness()
        self.expire_after(h, h.bao, "destroy_secret_id_accessor")
        self.assert_stopped(h.run(ONE))
        self.assertEqual(h.kinds("delete_staging"), [])

    def test_the_lease_is_confirmed_before_the_heartbeat_is_stamped(self):
        h = Harness()
        before = (h.main(ONE)["last_successful_rotation_at"], h.main(ONE)["last_scheduled_success_at"])
        self.expire_after(h, h.bao, "delete_staging")
        self.assert_stopped(h.run(ONE))
        main = h.main(ONE)
        self.assertEqual((main["last_successful_rotation_at"], main["last_scheduled_success_at"]), before)

    def test_the_lease_is_confirmed_before_the_destinations_are_fenced(self):
        h = Harness()
        self.aborting_run(h)
        self.expire_after(h, h.kube, "get_secret")
        self.assert_stopped(h.run(ONE, context(request_id="run-0002")))
        self.assertEqual(h.kinds("annotate_secret"), [])
        self.assertEqual(h.bao.destroyed, [])

    def test_the_lease_is_confirmed_before_the_abort_is_recorded(self):
        h = Harness()
        self.aborting_run(h)
        self.expire_after(h, h.kube, "annotate_secret")
        self.assert_stopped(h.run(ONE, context(request_id="run-0002")))
        self.assertNotEqual(h.tx(ONE)["state"], ABORTED)
        self.assertEqual(h.bao.destroyed, [])

    def test_the_lease_is_confirmed_before_an_aborted_replacement_is_destroyed(self):
        h = Harness()
        self.aborting_run(h)
        self.expire_after(h, h.bao, "write_kv2", lambda path, data, cas: data.get("state") == ABORTED)
        self.assert_stopped(h.run(ONE, context(request_id="run-0002")))
        self.assertEqual(h.bao.destroyed, [])

    def test_the_lease_is_confirmed_before_an_aborted_staging_record_is_deleted(self):
        h = Harness()
        self.aborting_run(h)
        self.expire_after(h, h.bao, "destroy_secret_id_accessor")
        self.assert_stopped(h.run(ONE, context(request_id="run-0002")))
        self.assertEqual(h.kinds("delete_staging"), [])


class DestinationFencingTests(unittest.TestCase):
    """The destination adapters write conditionally and can fence."""

    def kube_destination(self):
        from openbao_fakes import FakeKubernetes
        kube = FakeKubernetes()
        kube.secrets[("ns", "s")] = {"data": {"id": "old"}, "annotations": {}}
        destination = adapters.KubernetesSecretKeyDestination(
            {"namespace": "ns", "name": "s", "key": "id"}, kube)
        return kube, destination

    def test_a_secret_delivery_carries_the_version_read_just_before_it(self):
        kube, destination = self.kube_destination()
        original = kube.get_secret

        def read_then_change(namespace, name):
            found = original(namespace, name)
            kube.versions[(namespace, name)] = kube.versions.get((namespace, name), 1) + 1  # another actor writes
            return found

        kube.get_secret = read_then_change
        with self.assertRaises(adapters.AdapterError) as caught:
            destination.deliver("candidate", "tx-1", 1)
        self.assertEqual(caught.exception.reason_code, "delivery_failed")
        self.assertEqual(kube.secrets[("ns", "s")]["data"]["id"], "old")

    def test_a_delivery_to_a_missing_secret_creates_it(self):
        from openbao_fakes import FakeKubernetes
        kube = FakeKubernetes()
        destination = adapters.KubernetesSecretKeyDestination({"namespace": "ns", "name": "new", "key": "id"}, kube)
        destination.deliver("candidate", "tx-1", 1)
        self.assertEqual(kube.secrets[("ns", "new")]["data"]["id"], "candidate")

    def test_a_fence_is_conditional_on_the_version_read_just_before_it(self):
        kube, destination = self.kube_destination()
        original = kube.get_secret

        def read_then_change(namespace, name):
            found = original(namespace, name)
            kube.versions[(namespace, name)] = kube.versions.get((namespace, name), 1) + 1
            return found

        kube.get_secret = read_then_change
        with self.assertRaises(adapters.AdapterError):
            destination.fence("tx-1")
        self.assertNotIn(adapters.FENCE_ANNOTATION, kube.secrets[("ns", "s")]["annotations"])

    def test_a_secret_without_a_version_is_refused(self):
        kube, destination = self.kube_destination()
        kube.get_secret = lambda namespace, name: {"data": {"id": "old"}, "annotations": {}}
        with self.assertRaises(adapters.AdapterError):
            destination.deliver("candidate", "tx-1", 1)

    def test_a_fence_changes_the_secret_version_and_names_the_transaction(self):
        kube, destination = self.kube_destination()
        before = kube.get_secret("ns", "s")["resource_version"]
        destination.fence("tx-1")
        after = kube.get_secret("ns", "s")
        self.assertNotEqual(before, after["resource_version"])
        self.assertEqual(after["annotations"][adapters.FENCE_ANNOTATION], "tx-1")
        self.assertEqual(after["data"]["id"], "old")
        self.assertIsNone(destination.marker())

    def test_a_missing_secret_cannot_be_fenced(self):
        from openbao_fakes import FakeKubernetes
        destination = adapters.KubernetesSecretKeyDestination(
            {"namespace": "ns", "name": "gone", "key": "id"}, FakeKubernetes())
        with self.assertRaises(adapters.AdapterError):
            destination.fence("tx-1")

    def test_a_record_fence_keeps_the_other_keys_and_fails_a_stale_write(self):
        from openbao_fakes import FakeOpenBao
        bao = FakeOpenBao()
        bao.records["secret/data/x"] = ({"secret_id": "old", "other": "kept"}, 3)
        destination = adapters.OpenBaoKv2RecordDestination({"path": "secret/data/x", "key": "secret_id"}, bao)
        destination.fence("tx-1")
        data, version = bao.records["secret/data/x"]
        self.assertEqual(data, {"secret_id": "old", "other": "kept", adapters.FENCE_KEY: "tx-1"})
        self.assertEqual(version, 4)
        with self.assertRaises(RuntimeError):
            bao.write_kv2("secret/data/x", {"secret_id": "stale"}, 3)

    def test_a_missing_record_is_created_by_the_fence_so_a_stale_create_fails(self):
        from openbao_fakes import FakeOpenBao
        bao = FakeOpenBao()
        destination = adapters.OpenBaoKv2RecordDestination({"path": "secret/data/y", "key": "secret_id"}, bao)
        destination.fence("tx-1")
        with self.assertRaises(RuntimeError):
            bao.write_kv2("secret/data/y", {"secret_id": "stale"}, 0)


class IssuanceTimeTests(unittest.TestCase):
    """The recorded issue time is never later than the real one."""

    def lost_response_hour_later(self, h, target=ONE):
        h.bao.crash_after.add("create_custom_secret_id")
        h.crash_run(target)
        h.clock.t += 3600
        return h.run(target, context(request_id="run-0002"))

    def test_a_lost_response_recovered_an_hour_later_records_the_true_creation_time(self):
        h = Harness()
        self.assertEqual(self.lost_response_hour_later(h).outcome, "rotated")
        main = h.main(ONE)
        self.assertEqual(main["issued_at"], NOW)
        self.assertEqual(main["expires_at"], NOW + registry.ttl_seconds(h.entry(ONE)["secret_id_ttl"]))
        self.assertTrue(validate_record(main))

    def test_without_lookup_times_a_recovery_uses_the_time_persisted_before_the_create(self):
        h = Harness()
        h.bao.emit_times = False
        self.assertEqual(self.lost_response_hour_later(h).outcome, "rotated")
        self.assertEqual(h.main(ONE)["issued_at"], NOW)
        self.assertEqual(h.tx(ONE)["create_attempted_at"], NOW)

    def test_the_persisted_time_is_earlier_than_a_creation_that_happens_later(self):
        h = Harness()
        h.bao.emit_times = False
        h.bao.crash_before.add("create_custom_secret_id")
        h.crash_run(ONE)
        h.clock.t += 3600
        self.assertEqual(h.run(ONE, context(request_id="run-0002")).outcome, "rotated")
        self.assertEqual(h.main(ONE)["issued_at"], NOW)  # errs early, never late

    def test_the_time_is_persisted_before_the_first_create_call(self):
        h = Harness()
        seen = {}
        original = h.bao.create_custom_secret_id

        def check(approle, secret_id, params):
            seen["persisted"] = h.tx(ONE)["create_attempted_at"]
            return original(approle, secret_id, params)

        h.bao.create_custom_secret_id = check
        h.run(ONE)
        self.assertEqual(seen["persisted"], NOW)

    def test_malformed_or_inconsistent_lookup_times_fall_back_to_the_persisted_time(self):
        cases = {
            "unparseable": {"creation_time": "yesterday", "expiration_time": "tomorrow"},
            "not a string": {"creation_time": 5, "expiration_time": 6},
            "creation in the future": {"creation_time": "2999-01-01T00:00:00Z", "expiration_time": "2999-02-01T00:00:00Z"},
            "expiry before creation": {"creation_time": "2023-11-14T22:13:20Z", "expiration_time": "2023-11-14T22:00:00Z"},
            "only one": {"expiration_time": "2999-02-01T00:00:00Z"},
        }
        for name, override in cases.items():
            with self.subTest(times=name):
                h = Harness()
                h.bao.times_override = override
                if name == "only one":
                    h.bao.times_override = None
                    original = h.bao._response
                    h.bao._response = lambda *a, original=original: {
                        k: v for k, v in original(*a).items() if k != "creation_time"}
                h.bao.crash_before.add("create_custom_secret_id")
                h.crash_run(ONE)
                h.clock.t += 3600
                h.run(ONE, context(request_id="run-0002"))
                self.assertEqual(h.main(ONE)["issued_at"], NOW)

    def test_a_reported_expiry_earlier_than_creation_plus_ttl_is_used(self):
        h = Harness()
        ttl = registry.ttl_seconds(h.entry(ONE)["secret_id_ttl"])
        early = datetime.fromtimestamp(NOW + ttl - 100, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        h.bao.times_override = {"expiration_time": early}
        h.run(ONE)
        main = h.main(ONE)
        self.assertEqual(main["expires_at"], NOW + ttl - 100)
        self.assertTrue(validate_record(main))

    def stage_then_age(self, h, age, target=ONE):
        """A candidate that is staged, issued and not delivered, then `age`
        seconds pass."""
        h.config.abort_after = 10 ** 9
        start = h.clock.t
        h.kube.crash_before.add("patch_secret")
        h.crash_run(target)
        h.clock.t = start + age  # measured from the crash, not from the wait for the Lease

    def test_an_expired_staged_candidate_is_never_delivered(self):
        h = Harness()
        ttl = registry.ttl_seconds(h.entry(ONE)["secret_id_ttl"])
        self.stage_then_age(h, ttl + 1)
        replacement = h.tx(ONE)["new_accessor"]
        result = h.run(ONE, context(request_id="run-0002"))
        self.assertEqual((result.outcome, result.reason_code, result.state), ("failed", "delivery_failed", ABORTED))
        self.assertEqual(result.cause, "credential_expiring")
        self.assertEqual(h.kube.patched_values, [])
        self.assertEqual(h.bao.destroyed, [replacement])
        self.assertEqual(h.bao.staging, {})

    def test_the_v1_record_names_delivery_failed_and_the_transaction_record_names_the_cause(self):
        h = Harness()
        ttl = registry.ttl_seconds(h.entry(ONE)["secret_id_ttl"])
        self.stage_then_age(h, ttl - 100)
        h.seed(ONE)
        result = h.run(ONE, context(request_id="run-0002"))
        self.assertEqual((result.reason_code, result.cause), ("delivery_failed", "credential_expiring"))
        main = h.main(ONE)
        self.assertTrue(validate_record(main))
        self.assertEqual(main["last_failure_reason_code"], "delivery_failed")
        self.assertNotIn("credential_expiring", json.dumps(main))
        self.assertEqual(h.tx(ONE)["last_failure_reason_code"], "delivery_failed")
        self.assertEqual(h.tx(ONE)["last_failure_cause"], "credential_expiring")

    def test_a_failure_without_a_special_cause_leaves_the_cause_empty(self):
        h = Harness()
        h.ready.ok = False
        result = h.run(ONE)
        self.assertEqual(result.cause, "")
        self.assertIsNone(h.tx(ONE)["last_failure_cause"])
        self.assertTrue(validate_record(h.main(ONE)))

    def test_the_threshold_is_the_critical_share_of_the_ttl(self):
        limits = registry.thresholds(REGISTRY["runtime_targets"][ONE])
        edge = limits.ttl - limits.critical_remaining
        h = Harness()
        self.stage_then_age(h, edge)
        h.seed(ONE)  # the old credential, due and still current at the edge
        self.assertEqual(h.run(ONE, context(request_id="run-0002")).outcome, "rotated")
        h = Harness()
        self.stage_then_age(h, edge + 1)
        h.seed(ONE)
        self.assertEqual(h.run(ONE, context(request_id="run-0002")).cause, "credential_expiring")

    def test_an_aborted_expired_candidate_is_replaced_by_a_new_transaction_on_a_later_run(self):
        h = Harness()
        ttl = registry.ttl_seconds(h.entry(ONE)["secret_id_ttl"])
        self.stage_then_age(h, ttl - 100)
        h.seed(ONE)
        h.run(ONE, context(request_id="run-0002"))
        h.clock.t += 60
        later = h.run(ONE, context(request_id="run-0003"))
        self.assertEqual(later.outcome, "rotated")
        self.assertEqual(h.candidates.issued, ["synthetic-candidate-1", "synthetic-candidate-2"])
        self.assertEqual(h.kube.patched_values, ["synthetic-candidate-2"])

    def test_an_expiring_replacement_keeps_its_original_delivery_time(self):
        h = Harness()
        h.ready.ok = False
        h.run(ONE)
        ttl = registry.ttl_seconds(h.entry(ONE)["secret_id_ttl"])
        h.clock.t += ttl
        result = h.run(ONE, context(request_id="run-0002"))
        self.assertEqual((result.reason_code, result.cause, result.state), ("delivery_failed", "credential_expiring", DELIVERED_PENDING_READINESS))
        self.assertEqual(h.tx(ONE)["delivered_at"], NOW)
        self.assertEqual(h.bao.destroyed, [])

    def test_an_expiring_candidate_that_a_destination_already_carries_is_kept_and_reported(self):
        h = Harness()
        h.config.abort_after = 10 ** 9
        ttl = registry.ttl_seconds(h.entry(TWO)["secret_id_ttl"])
        record_path = h.entry(TWO)["destinations"][0]["path"]
        original = h.bao.write_kv2

        def crash_after_record(path, data, cas):
            original(path, data, cas)
            if path == record_path:
                raise ProcessLost()

        h.bao.write_kv2 = crash_after_record
        h.crash_run(TWO)
        h.bao.write_kv2 = original
        h.clock.t += ttl
        result = h.run(TWO, context(request_id="run-0002"))
        self.assertEqual((result.outcome, result.reason_code, result.state), ("failed", "delivery_failed", DELIVERED_PENDING_READINESS))
        self.assertEqual(result.cause, "credential_expiring")
        self.assertEqual(h.bao.destroyed, [])
        self.assertEqual(h.kube.patched_values, [])
        self.assertIn("synthetic-candidate-1", h.bao.registered)


class HeartbeatTests(unittest.TestCase):
    """The success heartbeat moves only after cleanup succeeds."""

    def beat(self, h):
        main = h.main(ONE)
        return main["last_successful_rotation_at"], main["last_scheduled_success_at"]

    def test_a_failed_destroy_leaves_the_heartbeat_unchanged_and_switches_the_accessor(self):
        h = Harness()
        before = self.beat(h)
        h.bao.fail["destroy_secret_id_accessor"] = ApiError("destroy unavailable")
        result = h.run(ONE)
        self.assertEqual((result.outcome, result.reason_code), ("failed", "cleanup_failed"))
        self.assertEqual(self.beat(h), before)
        self.assertEqual(h.main(ONE)["secret_id_accessor"], "accessor-1")
        self.assertTrue(validate_record(h.main(ONE)))

    def test_a_failed_staging_delete_leaves_the_heartbeat_unchanged(self):
        h = Harness()
        before = self.beat(h)
        h.bao.fail["delete_staging"] = ApiError("delete unavailable")
        result = h.run(ONE)
        self.assertEqual((result.outcome, result.reason_code), ("failed", "cleanup_failed"))
        self.assertEqual(self.beat(h), before)

    def test_the_run_that_finishes_a_failed_cleanup_stamps_the_heartbeat(self):
        for failure in ("destroy_secret_id_accessor", "delete_staging"):
            with self.subTest(failure=failure):
                h = Harness()
                before = self.beat(h)
                h.bao.fail[failure] = ApiError("unavailable")
                h.run(ONE)
                del h.bao.fail[failure]
                h.clock.t += 120
                resumed = h.run(ONE, context(request_id="run-0002"))
                self.assertEqual(resumed.outcome, "not_due")
                self.assertEqual(self.beat(h), (NOW + 120, NOW + 120))
                self.assertNotEqual(self.beat(h), before)
                self.assertTrue(h.tx(ONE)["cleanup_done"])

    def test_a_manual_run_that_finishes_the_cleanup_leaves_the_scheduled_stamp(self):
        h = Harness()
        before = self.beat(h)
        h.bao.fail["destroy_secret_id_accessor"] = ApiError("unavailable")
        h.run(ONE)
        del h.bao.fail["destroy_secret_id_accessor"]
        h.clock.t += 120
        h.run(ONE, context(request_id="run-0002", role="engineer", actor="admin", reason="drill"))
        self.assertEqual(self.beat(h), (NOW + 120, before[1]))

    def test_a_failed_stamp_is_retried_with_the_whole_cleanup(self):
        h = Harness()
        original = h.bao.write_kv2
        path = h.entry(ONE)["metadata_path"]
        writes = {"n": 0}

        def fail_the_stamp(p, data, cas):
            if p == path:
                writes["n"] += 1
                if writes["n"] == 2:  # the promote is write 1, the stamp write 2
                    raise ApiError("write failed")
            return original(p, data, cas)

        h.bao.write_kv2 = fail_the_stamp
        before = self.beat(h)
        self.assertEqual(h.run(ONE).reason_code, "metadata_failed")
        self.assertEqual(self.beat(h), before)
        self.assertFalse(h.tx(ONE)["cleanup_done"])
        h.clock.t += 120
        h.run(ONE, context(request_id="run-0002"))
        self.assertEqual(self.beat(h), (NOW + 120, NOW + 120))
        self.assertTrue(h.tx(ONE)["cleanup_done"])

    def test_a_successful_run_stamps_the_heartbeat_once_cleanup_is_done(self):
        h = Harness()
        h.run(ONE)
        self.assertEqual(self.beat(h), (NOW, NOW))
        stamp = h.index("delete_staging")
        main_writes = [i for i, e in enumerate(h.events) if e == ("write_kv2", h.entry(ONE)["metadata_path"])]
        self.assertGreater(main_writes[-1], stamp)


class RedactionTests(unittest.TestCase):
    def collect(self, h, scenario):
        """Run `scenario` and return all text the engine produced."""
        texts = []
        logger = rotation.LOG
        handler = _Capture()
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.DEBUG)
        try:
            try:
                value = scenario()
                texts.append(repr(value))
            except BaseException as error:  # noqa: BLE001 - the test inspects every exit
                texts.append("".join(traceback.format_exception(type(error), error, error.__traceback__)))
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)
        return "\n".join(texts + handler.lines)

    def assert_clean(self, h, text):
        sensitive = {TOKEN, "synthetic-candidate-1", "accessor-1", f"old-accessor-{ONE}", f"old-secret-id-of-{ONE}"}
        sensitive |= set(h.bao.registered) | set(h.bao.registered.values()) | {v for v in h.kube.patched_values}
        for value in sorted(sensitive):
            self.assertNotIn(value, text)

    def test_no_secret_reaches_a_log_exception_result_or_reason_code(self):
        scenarios = {
            "lost create response": lambda h: setattr(h.bao, "lose_create_response", True),
            "delivery failure": lambda h: h.kube.fail_patch.add(h.destinations(ONE)[0][:2]),
            "readiness failure": lambda h: setattr(h.ready, "ok", False),
            "metadata failure": lambda h: h.bao.fail.update(
                write_kv2=ApiError(f"write rejected {TOKEN} synthetic-candidate-1")),
            "create failure": lambda h: h.bao.fail.update(
                create_custom_secret_id=ApiError(f"denied for synthetic-candidate-1 {TOKEN}")),
            "lookup failure": lambda h: h.bao.fail.update(
                lookup_secret_id=ApiError(f"denied for synthetic-candidate-1 {TOKEN}")),
            "cleanup failure": lambda h: h.bao.fail.update(
                destroy_secret_id_accessor=ApiError(f"old-accessor-{ONE} {TOKEN}")),
        }
        for name, setup in scenarios.items():
            with self.subTest(scenario=name):
                h = Harness()
                setup(h)
                text = self.collect(h, lambda: h.run(ONE))
                self.assertIn("target", text)  # something was captured
                self.assert_clean(h, text)

    def test_an_interrupt_carries_no_text_and_no_chained_secret(self):
        h = Harness()
        h.kube.crash_before.add("patch_secret")
        text = self.collect(h, lambda: h.run(ONE))
        self.assertIn("ProcessLost", text)
        self.assert_clean(h, text)

    def test_a_lease_conflict_exception_is_clean(self):
        h = Harness()
        h.kube.leases[(LEASE_NAMESPACE, rotation.LEASE_NAME)] = {
            "holder": "x", "renew_time": NOW, "duration_seconds": 900, "resource_version": "1"}
        text = self.collect(h, lambda: h.run(ONE))
        self.assertIn("another run holds the Lease", text)
        self.assert_clean(h, text)

    def test_exceptions_from_clients_leave_no_cause_or_context(self):
        h = Harness()
        h.bao.fail["lookup_secret_id"] = ApiError(f"body {TOKEN}")
        with self.assertRaises(RotationError) as caught:
            rotation._call("lookup_failed", "fixed", h.bao.lookup_secret_id, ONE, "x")
        self.assertIsNone(caught.exception.__cause__)
        self.assertIsNone(caught.exception.__context__)
        self.assertEqual(str(caught.exception), "fixed")

    def test_the_sweep_result_and_records_hold_no_secret_id(self):
        h = Harness()
        text = self.collect(h, h.sweep)
        self.assert_clean(h, text)
        for path, (data, _version) in h.bao.records.items():
            if path.startswith(rotation.TRANSACTION_PREFIX) or path.startswith("secret/data/platform/approle-rotation/"):
                self.assertNotIn("synthetic-candidate", json.dumps(data))


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(logging.DEBUG)
        self.lines = []

    def emit(self, record):
        self.lines.append(record.getMessage() + " " + repr(record.args) + " " + repr(record.exc_info))


if __name__ == "__main__":
    unittest.main()
