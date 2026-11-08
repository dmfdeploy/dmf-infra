"""Delivery and readiness adapters, one per kind that the registry declares.

A delivery adapter writes a SecretID to one authoritative destination and
stamps the transaction id and target version with it. It can also report
whether that destination already carries a transaction's marker. A readiness
adapter checks one gate and returns a bounded reason code, never a credential.

The adapters take client objects, so the same code runs in a playbook, a job
or a test. The client methods they call are listed on each class.
"""

import fnmatch
import re
from dataclasses import dataclass
from datetime import datetime, timezone

TRANSACTION_ANNOTATION = "approle-lifecycle.dmf.example.com/transaction-id"
VERSION_ANNOTATION = "approle-lifecycle.dmf.example.com/target-version"
FENCE_ANNOTATION = "approle-lifecycle.dmf.example.com/aborted-transaction-id"
TRANSACTION_KEY = "transaction_id"
VERSION_KEY = "target_version"
FENCE_KEY = "aborted_transaction_id"
_TRANSACTION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class AdapterError(RuntimeError):
    """An adapter failed. `reason_code` is one of the metadata schema codes.
    The message never contains a SecretID."""

    def __init__(self, reason_code, message):
        super().__init__(message)
        self.reason_code = reason_code


def _guarded(reason_code, message, function, *args):
    """Call a client method so that nothing it raises can carry a credential.

    An `Exception` becomes an AdapterError with a fixed message. A
    `BaseException` such as KeyboardInterrupt becomes a fresh instance of its
    class with no arguments. Both are raised after the handler, so neither has
    a cause or a context. Every client call in this module goes through here.
    """
    interrupt = None
    failed = False
    try:
        return function(*args)
    except Exception:
        failed = True
    except BaseException as caught:
        interrupt = type(caught)
    if interrupt is not None:
        raise interrupt()
    if failed:
        raise AdapterError(reason_code, message)


def _check_transaction(transaction_id, target_version):
    if not isinstance(transaction_id, str) or not _TRANSACTION_ID.fullmatch(transaction_id):
        raise AdapterError("invalid_state", "transaction id is missing or malformed")
    if isinstance(target_version, bool) or not isinstance(target_version, int) or target_version <= 0:
        raise AdapterError("invalid_state", "target version must be a positive integer")


class KubernetesSecretKeyDestination:
    """A named key of a Kubernetes Secret.

    Client methods: `get_secret(namespace, name)` returns None or a dict with
    `data` (key to value), `annotations` and `resource_version`;
    `patch_secret(namespace, name, key, value, annotations, resource_version)`
    sets the key and the annotations in one request that the API server
    rejects unless the Secret still has `resource_version` (None creates the
    Secret); `annotate_secret(namespace, name, annotations, resource_version)`
    sets annotations only, under the same condition.

    A delivery is conditional on the version read just before it, so a write
    that was in flight when another actor changed the Secret fails instead of
    replacing the newer state. `fence` changes the version without touching
    the credential.
    """

    def __init__(self, destination, client):
        self.namespace = destination["namespace"]
        self.name = destination["name"]
        self.key = destination["key"]
        self.client = client

    def label(self):
        return f"secret {self.namespace}/{self.name} key {self.key}"

    def marker(self):
        current = _guarded("lookup_failed", f"could not read {self.label()}", self.client.get_secret, self.namespace, self.name)
        if current is None:
            return None
        annotations = current.get("annotations") or {}
        transaction_id = annotations.get(TRANSACTION_ANNOTATION)
        if not transaction_id:
            return None
        return {"transaction_id": transaction_id, "target_version": annotations.get(VERSION_ANNOTATION)}

    def has_marker(self, transaction_id):
        marker = self.marker()
        return marker is not None and marker["transaction_id"] == transaction_id

    def _current(self, reason_code):
        """Read the Secret and its resource version. A Secret that exists
        without a version cannot be written conditionally, so it is refused."""
        message = f"could not read {self.label()}"
        current = _guarded(reason_code, message, self.client.get_secret, self.namespace, self.name)
        if current is not None and not current.get("resource_version"):
            raise AdapterError(reason_code, f"{self.label()} has no resource version")
        return current

    def deliver(self, secret_id, transaction_id, target_version):
        _check_transaction(transaction_id, target_version)
        current = self._current("delivery_failed")
        _guarded(
            "delivery_failed",
            f"could not update {self.label()}",
            self.client.patch_secret,
            self.namespace,
            self.name,
            self.key,
            secret_id,
            {TRANSACTION_ANNOTATION: transaction_id, VERSION_ANNOTATION: str(target_version)},
            current["resource_version"] if current is not None else None,
        )

    def fence(self, transaction_id):
        """Change the Secret's version so that a delivery still in flight for
        `transaction_id` fails. The Secret must exist."""
        _check_transaction(transaction_id, 1)
        current = self._current("delivery_failed")
        if current is None:
            raise AdapterError("delivery_failed", f"{self.label()} does not exist, so it cannot be fenced")
        _guarded(
            "delivery_failed",
            f"could not fence {self.label()}",
            self.client.annotate_secret,
            self.namespace,
            self.name,
            {FENCE_ANNOTATION: transaction_id},
            current["resource_version"],
        )


class OpenBaoKv2RecordDestination:
    """A key of an OpenBao KV v2 record, such as an ESO binding.

    Client methods: `read_kv2(path)` returns None or `(data, version)`;
    `write_kv2(path, data, cas)` writes the whole record only when its current
    version equals `cas`. The adapter keeps every other key of the record, and
    it writes with the version it read just before, so a delivery in flight
    when another actor wrote the record fails.
    """

    def __init__(self, destination, client):
        self.path = destination["path"]
        self.key = destination["key"]
        self.client = client

    def label(self):
        return f"record {self.path} key {self.key}"

    def marker(self):
        current = _guarded("lookup_failed", f"could not read {self.label()}", self.client.read_kv2, self.path)
        if current is None:
            return None
        data, _ = current
        transaction_id = data.get(TRANSACTION_KEY)
        if not transaction_id:
            return None
        return {"transaction_id": transaction_id, "target_version": data.get(VERSION_KEY)}

    def has_marker(self, transaction_id):
        marker = self.marker()
        return marker is not None and marker["transaction_id"] == transaction_id

    def deliver(self, secret_id, transaction_id, target_version):
        _check_transaction(transaction_id, target_version)
        message = f"could not update {self.label()}"
        current = _guarded("delivery_failed", message, self.client.read_kv2, self.path)
        data, version = current if current is not None else ({}, 0)
        merged = dict(data)
        merged[self.key] = secret_id
        merged[TRANSACTION_KEY] = transaction_id
        merged[VERSION_KEY] = target_version
        _guarded("delivery_failed", message, self.client.write_kv2, self.path, merged, version)

    def fence(self, transaction_id):
        """Write the record under the version just read, naming
        `transaction_id`, so that a delivery in flight for it fails. A missing
        record is created, which also fails a stale create."""
        _check_transaction(transaction_id, 1)
        message = f"could not fence {self.label()}"
        current = _guarded("delivery_failed", message, self.client.read_kv2, self.path)
        data, version = current if current is not None else ({}, 0)
        _guarded(
            "delivery_failed", message, self.client.write_kv2, self.path,
            {**data, FENCE_KEY: transaction_id}, version,
        )


def build_destination(destination, clients):
    """Return the adapter for one registry destination. An unknown kind or a
    missing client is an error, never a silent skip."""
    kind = destination.get("kind")
    if kind == "kubernetes_secret_key":
        return KubernetesSecretKeyDestination(destination, _client(clients, "kubernetes"))
    if kind == "openbao_kv2_record":
        return OpenBaoKv2RecordDestination(destination, _client(clients, "openbao"))
    raise AdapterError("invalid_state", f"unsupported destination kind {kind!r}")


def _client(clients, name):
    client = clients.get(name)
    if client is None:
        raise AdapterError("invalid_state", f"no {name} client was supplied")
    return client


@dataclass(frozen=True)
class GateResult:
    ok: bool
    reason_code: str
    gate: str
    detail: str


def _pass(gate, detail):
    return GateResult(True, "", gate, detail)


def _fail(gate, detail):
    return GateResult(False, "readiness_failed", gate, detail)


def _condition(obj, name):
    for condition in (obj.get("status") or {}).get("conditions") or []:
        if condition.get("type") == name:
            return condition
    return None


def _parse_time(text):
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except (AttributeError, ValueError):
        return None


_NO_DELIVERY_TIME = "no delivery time was supplied, so the replacement cannot be shown"
FRESH_AUTHENTICATION_MISSING = "fresh_authentication_proof_missing"


def _read(function, *args):
    """Run a gate's client read. Return (value, failed). A failed read is a
    failed gate, never an exception that could carry a response body."""
    try:
        return _guarded("readiness_failed", "the read failed", function, *args), False
    except AdapterError:
        return None, True


class ClusterSecretStoreGate:
    """The store reports Ready=True.

    Client method: `get_object(kind, namespace, name)` returns None or the
    object as a dict. A Ready store may still hold its earlier condition after
    a bad replacement, so this gate is a precondition and never accepts a
    replacement alone. Like every gate, it fails without a delivery time.
    """

    proves_acceptance = False

    def __init__(self, gate, client):
        self.gate = gate
        self.client = client

    def check(self, since=None, inventory=None):
        name = f"ClusterSecretStore {self.gate['name']}"
        if since is None:
            return _fail(name, _NO_DELIVERY_TIME)
        store, failed = _read(self.client.get_object, "ClusterSecretStore", None, self.gate["name"])
        if failed:
            return _fail(name, "the read failed")
        if store is None:
            return _fail(name, "not found")
        ready = _condition(store, "Ready")
        if ready is None or ready.get("status") != "True":
            return _fail(name, "not Ready")
        return _pass(name, "Ready")


class ExternalSecretGate:
    """Every matching ExternalSecret reads the expected store, is Ready, and
    refreshed strictly after `since`.

    Client method: `list_objects(kind, namespace)` returns a list of dicts.
    The gate names its namespaces with `namespace` or with `namespaces_from`,
    a dotted path into the inventory mapping. `store` is the name of the
    ClusterSecretStore that the registry declares for the target. Without it
    the gate fails.

    What a pass proves: a successful refresh through the expected store after
    the delivery. It does not prove authentication with the replacement,
    because ESO may reuse a cached token. That proof belongs to step 3.
    """

    proves_acceptance = True

    def __init__(self, gate, client, store=None):
        self.gate = gate
        self.client = client
        self.store = store

    def check(self, since=None, inventory=None):
        gate = f"ExternalSecret {self.gate['name_pattern']}"
        if since is None:
            return _fail(gate, _NO_DELIVERY_TIME)
        if not self.store:
            return _fail(gate, "no expected store was supplied")
        if self.gate.get("namespace"):
            namespaces = [self.gate["namespace"]]
        else:
            namespaces = _lookup(inventory or {}, self.gate["namespaces_from"])
        if not namespaces or not isinstance(namespaces, (list, tuple)):
            return _fail(gate, "no namespaces could be resolved")
        for namespace in namespaces:
            listed, failed = _read(self.client.list_objects, "ExternalSecret", namespace)
            if failed:
                return _fail(gate, f"the read failed in {namespace}")
            matches = [
                item
                for item in listed
                if fnmatch.fnmatchcase(item.get("metadata", {}).get("name", ""), self.gate["name_pattern"])
            ]
            if not matches:
                return _fail(gate, f"none found in {namespace}")
            for item in matches:
                reference = (item.get("spec") or {}).get("secretStoreRef") or {}
                if reference.get("kind") != "ClusterSecretStore" or reference.get("name") != self.store:
                    return _fail(gate, f"not read through the expected store in {namespace}")
                ready = _condition(item, "Ready")
                if ready is None or ready.get("status") != "True":
                    return _fail(gate, f"not Ready in {namespace}")
                refreshed = _parse_time((item.get("status") or {}).get("refreshTime"))
                if refreshed is None or refreshed <= since:
                    return _fail(gate, f"not refreshed after the delivery in {namespace}")
        return _pass(gate, "Ready and refreshed through the expected store")


class KubernetesSecretKeyGate:
    """A Secret holds a non-empty value for the required key.

    Client method: `get_secret(namespace, name)`. A key that is present does
    not show that it is the replacement, so this gate never accepts one alone.
    """

    proves_acceptance = False

    def __init__(self, gate, client):
        self.gate = gate
        self.client = client

    def check(self, since=None, inventory=None):
        name = f"Secret {self.gate['namespace']}/{self.gate['name']} key {self.gate['required_key']}"
        if since is None:
            return _fail(name, _NO_DELIVERY_TIME)
        current, failed = _read(self.client.get_secret, self.gate["namespace"], self.gate["name"])
        if failed:
            return _fail(name, "the read failed")
        if current is None:
            return _fail(name, "not found")
        if not (current.get("data") or {}).get(self.gate["required_key"]):
            return _fail(name, "key is missing or empty")
        return _pass(name, "key present")


def _lookup(mapping, dotted):
    value = mapping
    for part in dotted.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def build_gate(gate, clients, store=None):
    """Return the readiness adapter for one registry gate, or raise. `store` is
    the ClusterSecretStore name that an ExternalSecret gate must see."""
    kind = gate.get("kind")
    if kind == "ClusterSecretStore":
        return ClusterSecretStoreGate(gate, _client(clients, "kubernetes"))
    if kind == "ExternalSecret":
        return ExternalSecretGate(gate, _client(clients, "kubernetes"), store)
    if kind == "KubernetesSecret":
        return KubernetesSecretKeyGate(gate, _client(clients, "kubernetes"))
    raise AdapterError("invalid_state", f"unsupported readiness gate kind {kind!r}")


@dataclass(frozen=True)
class TargetReadiness:
    ok: bool
    results: tuple
    reason_code: str = ""


def store_name(entry):
    """The ClusterSecretStore that the registry declares for a target."""
    for gate in entry["readiness_gates"]:
        if gate.get("kind") == "ClusterSecretStore":
            return gate["name"]
    return None


def evaluate_target(entry, gates, since, inventory=None):
    """Decide whether a target accepted a replacement delivered at `since`.

    In this step, no target is accepted. A refresh through the expected store
    does not prove that the replacement authenticated, and step 3 of the
    credential lifecycle plan defines the proof that can. Until then every
    runtime target gets the reason code `fresh_authentication_proof_missing`,
    whatever its proof status string and whatever the gates return. The
    individual gate results are returned either way.
    """
    results = tuple(gate.check(since, inventory) for gate in gates)
    note = GateResult(
        False,
        FRESH_AUTHENTICATION_MISSING,
        "target",
        "a refresh through the expected store does not prove authentication with the replacement",
    )
    return TargetReadiness(False, results + (note,), FRESH_AUTHENTICATION_MISSING)


def adapters_for(entry, clients):
    """Build every delivery and readiness adapter for one runtime target."""
    store = store_name(entry)
    return (
        [build_destination(destination, clients) for destination in entry["destinations"]],
        [build_gate(gate, clients, store) for gate in entry["readiness_gates"]],
    )
