"""Concrete clients for the rotation engine in `rotation.py`: `kubectl` for
Kubernetes and `kubectl exec ... bao` for OpenBao.

Every process call goes through one `runner(argv, stdin_text, timeout)`
function, so a test can replace the process boundary. Two rules hold for every
call:

- No credential travels in argv. The OpenBao client token is the first stdin
  line, and `bao` reads any further value as `key=-` or as a JSON body on
  stdin. A Kubernetes request that carries a credential reads it from a
  temporary file that only its owner can read, and removes the file afterwards.
- Each client declares `request_timeout` and enforces it on every call: the
  process gets that timeout, and every `kubectl` command line carries the same
  value as `--request-timeout`.

Failures use the engine's exception classes. An error that the server
answered (a conflict, a missing object, an API error) raises the matching
class from `rotation.py`. Anything else, such as a timeout, a failed `kubectl`
or a dropped connection, raises `TransportError`, which the engine treats as
"the request may still run on the server". No message carries child output.
"""

import base64
import json
import os
import re
import subprocess
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone

from .rotation import (
    MAX_REQUEST_SECONDS,
    AlreadyRegistered,
    ApiError,
    LeaseConflict,
    LeaseExists,
    StagingExists,
)
from .transports import DEFAULT_KUBECONFIG, TransportError, _safe

_LEASE_API = "coordination.k8s.io/v1"
_RESOURCES = {
    "ClusterSecretStore": "clustersecretstores.external-secrets.io",
    "ExternalSecret": "externalsecrets.external-secrets.io",
}
# `sh` reads the first stdin line as the client token. Whatever stdin holds
# after that line stays in the pipe for `bao`.
_SELECTOR = re.compile(r"[A-Za-z0-9._/-]+=[A-Za-z0-9._-]+")
_BAO_SHELL = "IFS= read -r BAO_TOKEN; export BAO_TOKEN; exec bao {command}"
_BAO_LOGIN_SHELL = "exec bao {command}"
_SECRET_ID_KEYS = (
    "secret_id_accessor", "secret_id_ttl", "secret_id_num_uses", "creation_time", "expiration_time",
)


class CallTimedOut(TransportError):
    """A call did not finish within the request timeout."""


def run(argv, stdin_text="", timeout=None):
    """Run `argv` with `stdin_text` on stdin. Return (returncode, stdout,
    stderr). Raise `subprocess.TimeoutExpired` after `timeout` seconds. The
    only process boundary of this module."""
    done = subprocess.run(
        argv, input=stdin_text, capture_output=True, text=True, check=False, shell=False, timeout=timeout
    )
    return done.returncode, done.stdout, done.stderr


@contextmanager
def _private_file(text):
    """Write `text` to a file that only the owner can read and yield its path.
    The file is removed on every exit path."""
    handle, path = tempfile.mkstemp(prefix="dmf-rotation-")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
        yield path
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _epoch(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()


def _micro_time(seconds):
    return datetime.fromtimestamp(seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class _Bounded:
    def __init__(self, runner, request_timeout):
        if isinstance(request_timeout, bool) or not isinstance(request_timeout, int) or request_timeout <= 0:
            raise ValueError("request_timeout must be a positive whole number of seconds")
        self.request_timeout = request_timeout
        self._runner = runner

    def _process(self, argv, stdin_text=""):
        try:
            return self._runner(argv, stdin_text, self.request_timeout)
        except subprocess.TimeoutExpired:
            raise CallTimedOut("the call did not finish within the request timeout") from None


class RotationKubernetes(_Bounded):
    """The Kubernetes client of the engine and of its adapters.

    `as_user` makes every read and write run as that user, so a runner with a
    broad kubeconfig still acts as the scoped scheduler ServiceAccount. The
    TokenRequest runs as the runner."""

    def __init__(self, kubeconfig=DEFAULT_KUBECONFIG, as_user=None, runner=run, request_timeout=MAX_REQUEST_SECONDS):
        super().__init__(runner, request_timeout)
        self._base = ["kubectl", "--kubeconfig", kubeconfig, f"--request-timeout={request_timeout}s"]
        self._as = ["--as", as_user] if as_user else []

    def _kubectl(self, args, stdin_text="", act_as_user=True):
        argv = self._base + (self._as if act_as_user else []) + list(args)
        return self._process(argv, stdin_text)

    @staticmethod
    def _fail(err, message):
        """The server answered with an error, or the call failed without an answer."""
        if "Error from server" in err:
            raise ApiError()
        raise TransportError(message)

    # Lease

    def get_lease(self, namespace, name):
        rc, out, err = self._kubectl(
            ["-n", _safe(namespace), "get", "lease", _safe(name), "-o", "json", "--ignore-not-found"]
        )
        if rc != 0:
            self._fail(err, "the Lease read failed")
        if not out.strip():
            return None
        try:
            obj = json.loads(out)
            spec = obj["spec"]
            return {
                "holder": spec.get("holderIdentity"),
                "renew_time": _epoch(spec["renewTime"]),
                "duration_seconds": spec["leaseDurationSeconds"],
                "resource_version": obj["metadata"]["resourceVersion"],
            }
        except (ValueError, KeyError, TypeError, AttributeError):
            raise TransportError("the Lease response is malformed") from None

    @staticmethod
    def _lease(namespace, name, holder, renew_time, duration_seconds, resource_version=None):
        metadata = {"name": name, "namespace": namespace}
        if resource_version is not None:
            metadata["resourceVersion"] = str(resource_version)
        return json.dumps({
            "apiVersion": _LEASE_API,
            "kind": "Lease",
            "metadata": metadata,
            "spec": {
                "holderIdentity": holder,
                "leaseDurationSeconds": duration_seconds,
                "renewTime": _micro_time(renew_time),
            },
        })

    def create_lease(self, namespace, name, holder, renew_time, duration_seconds):
        body = self._lease(_safe(namespace), _safe(name), holder, renew_time, duration_seconds)
        rc, _out, err = self._kubectl(["create", "-f", "-"], body)
        if rc == 0:
            return None
        if "AlreadyExists" in err:
            raise LeaseExists()
        self._fail(err, "the Lease create failed")

    def update_lease(self, namespace, name, holder, renew_time, duration_seconds, resource_version):
        body = self._lease(_safe(namespace), _safe(name), holder, renew_time, duration_seconds, resource_version)
        rc, _out, err = self._kubectl(["replace", "-f", "-"], body)
        if rc == 0:
            return None
        if "(Conflict)" in err or "(NotFound)" in err:
            raise LeaseConflict()
        self._fail(err, "the Lease update failed")

    def delete_lease(self, namespace, name, resource_version):
        """Delete the Lease only while it still has `resource_version`."""
        options = json.dumps({
            "apiVersion": "v1",
            "kind": "DeleteOptions",
            "preconditions": {"resourceVersion": str(resource_version)},
        })
        uri = f"/apis/{_LEASE_API}/namespaces/{_safe(namespace)}/leases/{_safe(name)}"
        rc, _out, err = self._kubectl(["delete", "--raw", uri, "-f", "-"], options)
        if rc == 0:
            return None
        if "(Conflict)" in err or "(NotFound)" in err:
            raise LeaseConflict()
        self._fail(err, "the Lease delete failed")

    # Secrets

    def get_secret(self, namespace, name):
        rc, out, err = self._kubectl(
            ["-n", _safe(namespace), "get", "secret", _safe(name), "-o", "json", "--ignore-not-found"]
        )
        if rc != 0:
            self._fail(err, "the Secret read failed")
        if not out.strip():
            return None
        try:
            obj = json.loads(out)
            meta = obj["metadata"]
            data = {key: base64.b64decode(value).decode("utf-8") for key, value in (obj.get("data") or {}).items()}
            return {
                "data": data,
                "annotations": meta.get("annotations") or {},
                "resource_version": meta["resourceVersion"],
            }
        except (ValueError, KeyError, TypeError, AttributeError):
            raise TransportError("the Secret response is malformed") from None

    def patch_secret(self, namespace, name, key, value, annotations, resource_version):
        """Set one key and the annotations in one request that fails unless the
        Secret still has `resource_version`. A `resource_version` of None
        creates the Secret."""
        _safe(namespace), _safe(name)
        if resource_version is None:
            body = {
                "apiVersion": "v1",
                "kind": "Secret",
                "metadata": {"name": name, "namespace": namespace, "annotations": dict(annotations)},
                "stringData": {key: value},
            }
            args = ["create", "-f"]
        else:
            body = {
                "metadata": {"annotations": dict(annotations), "resourceVersion": str(resource_version)},
                "stringData": {key: value},
            }
            args = ["-n", namespace, "patch", "secret", name, "--type", "merge", "--patch-file"]
        self._write_secret(args, body, "the Secret update failed")

    def annotate_secret(self, namespace, name, annotations, resource_version):
        """Set annotations only, under the same version condition."""
        if resource_version is None:
            raise TransportError("an annotation needs the version of an existing Secret")
        body = {"metadata": {"annotations": dict(annotations), "resourceVersion": str(resource_version)}}
        args = ["-n", _safe(namespace), "patch", "secret", _safe(name), "--type", "merge", "--patch-file"]
        self._write_secret(args, body, "the Secret annotation failed")

    def _write_secret(self, args, body, message):
        with _private_file(json.dumps(body)) as path:
            rc, _out, err = self._kubectl(args + [path])
        if rc != 0:
            self._fail(err, message)

    # External Secrets objects

    def get_object(self, kind, namespace, name):
        resource = self._resource(kind)
        scope = ["-n", _safe(namespace)] if namespace else []
        rc, out, err = self._kubectl(scope + ["get", resource, _safe(name), "-o", "json", "--ignore-not-found"])
        if rc != 0:
            self._fail(err, "the object read failed")
        if not out.strip():
            return None
        return self._json(out, dict)

    def list_objects(self, kind, namespace):
        resource = self._resource(kind)
        rc, out, err = self._kubectl(["-n", _safe(namespace), "get", resource, "-o", "json"])
        if rc != 0:
            self._fail(err, "the object list failed")
        items = self._json(out, dict).get("items")
        if not isinstance(items, list):
            raise TransportError("the list response is malformed")
        return items

    @staticmethod
    def _resource(kind):
        if kind not in _RESOURCES:
            raise TransportError("the kind is not one that the rotation reads")
        return _RESOURCES[kind]

    @staticmethod
    def _json(text, expected):
        try:
            value = json.loads(text)
        except ValueError:
            raise TransportError("the response is not JSON") from None
        if not isinstance(value, expected):
            raise TransportError("the response has the wrong shape")
        return value

    # Reads for the checks before a run

    def get_configmap(self, namespace, name):
        """The data of a ConfigMap and the Unix times of its managed-field
        updates, which show when its content last changed."""
        rc, out, err = self._kubectl(
            ["-n", _safe(namespace), "get", "configmap", _safe(name), "-o", "json", "--show-managed-fields"],
            act_as_user=False,
        )
        if rc != 0:
            self._fail(err, "the ConfigMap read failed")
        obj = self._json(out, dict)
        try:
            fields = obj["metadata"].get("managedFields") or []
            times = [_epoch(field["time"]) for field in fields if field.get("time")]
        except (KeyError, TypeError, ValueError, AttributeError):
            raise TransportError("the ConfigMap response is malformed") from None
        return {"data": obj.get("data") or {}, "update_times": times}

    def list_pods(self, namespace, selector):
        """The pods that match a label selector, each with its containers and
        the Unix time each container started (None when it is not running)."""
        if not isinstance(selector, str) or not _SELECTOR.fullmatch(selector):
            raise TransportError("the selector is not a plain label selector")
        rc, out, err = self._kubectl(
            ["-n", _safe(namespace), "get", "pods", f"--selector={selector}", "-o", "json"], act_as_user=False
        )
        if rc != 0:
            self._fail(err, "the pod list failed")
        pods = []
        try:
            for item in self._json(out, dict)["items"]:
                containers = []
                for status in item.get("status", {}).get("containerStatuses") or []:
                    running = (status.get("state") or {}).get("running")
                    started = _epoch(running["startedAt"]) if running and running.get("startedAt") else None
                    containers.append({"name": status["name"], "started_at": started})
                pods.append({"name": item["metadata"]["name"], "containers": containers})
        except (KeyError, TypeError, ValueError, AttributeError):
            raise TransportError("the pod list response is malformed") from None
        return pods

    def request_token(self, namespace, service_account, audience, duration="10m"):
        """Mint a short-lived JWT for the ServiceAccount. The runner's own
        identity makes the TokenRequest."""
        rc, out, err = self._kubectl(
            ["-n", _safe(namespace), "create", "token", _safe(service_account),
             "--duration", _safe(duration), "--audience", _safe(audience)],
            act_as_user=False,
        )
        jwt = out.strip()
        if rc != 0 or not jwt:
            self._fail(err, "the TokenRequest failed")
        return jwt


def _answered(err):
    """OpenBao answered the request, with an error status."""
    return "Error making API request" in err


def _metadata_path(data_path):
    """The KV v2 metadata path of a data path. Deleting it removes every
    version of the secret."""
    head, marker, tail = data_path.partition("/data/")
    if not marker:
        raise TransportError("the path is not a KV v2 data path")
    return f"{head}/metadata/{tail}"


class RotationOpenBao(_Bounded):
    """The OpenBao client of the engine, through `kubectl exec` into the pod.

    `login_kubernetes` stores the client token in memory, and every later call
    sends it as the first stdin line. `revoke_self` ends the session."""

    def __init__(
        self, namespace, pod, kubeconfig=DEFAULT_KUBECONFIG, runner=run, request_timeout=MAX_REQUEST_SECONDS
    ):
        super().__init__(runner, request_timeout)
        self._exec = [
            "kubectl", "--kubeconfig", kubeconfig, f"--request-timeout={request_timeout}s",
            "-n", namespace, "exec", "-i", pod, "--", "sh", "-c",
        ]
        self._token = None

    def _bao(self, command, payload="", *, authenticated=True):
        """Return (returncode, stdout, stderr) of one `bao` command."""
        if not authenticated:
            return self._process(self._exec + [_BAO_LOGIN_SHELL.format(command=command)], payload)
        if not self._token:
            raise TransportError("there is no OpenBao session")
        stdin_text = f"{self._token}\n{payload}"
        return self._process(self._exec + [_BAO_SHELL.format(command=command)], stdin_text)

    @staticmethod
    def _fail(err, message):
        if _answered(err):
            raise ApiError()
        raise TransportError(message)

    @staticmethod
    def _data(text, message):
        try:
            data = json.loads(text)["data"]
        except (ValueError, KeyError, TypeError):
            raise TransportError(message) from None
        if not isinstance(data, dict):
            raise TransportError(message)
        return data

    # Session

    def login_kubernetes(self, role, jwt):
        if not isinstance(jwt, str) or not jwt.strip():
            raise TransportError("the ServiceAccount JWT is empty")
        rc, out, err = self._bao(
            f"write -format=json auth/kubernetes/login role={_safe(role)} jwt=-", jwt, authenticated=False
        )
        if rc != 0:
            self._fail(err, "the OpenBao login failed")
        try:
            token = json.loads(out)["auth"]["client_token"]
        except (ValueError, KeyError, TypeError):
            raise TransportError("the login response has no client token") from None
        if not isinstance(token, str) or not token.strip():
            raise TransportError("the login response has no client token")
        self._token = token

    def revoke_self(self):
        rc, _out, err = self._bao("token revoke -self")
        self._token = None
        if rc != 0:
            self._fail(err, "the token revoke failed")

    # AppRole

    def read_role(self, approle_name):
        rc, out, err = self._bao(f"read -format=json auth/approle/role/{_safe(approle_name)}")
        if rc == 2 and "No value found" in err:
            return None
        if rc != 0:
            self._fail(err, "the AppRole read failed")
        return {"secret_id_ttl": self._data(out, "the AppRole response is malformed").get("secret_id_ttl")}

    def lookup_secret_id(self, approle_name, secret_id):
        rc, out, err = self._bao(
            f"write -format=json auth/approle/role/{_safe(approle_name)}/secret-id/lookup secret_id=-", secret_id
        )
        if rc != 0:
            self._fail(err, "the SecretID lookup failed")
        if not out.strip():
            return None
        data = self._data(out, "the lookup response is malformed")
        return {key: data.get(key) for key in _SECRET_ID_KEYS}

    def create_custom_secret_id(self, approle_name, secret_id, params):
        pairs = " ".join(f"{_safe(key)}={_safe(str(value))}" for key, value in sorted(params.items()))
        rc, out, err = self._bao(
            f"write -format=json auth/approle/role/{_safe(approle_name)}/custom-secret-id secret_id=- {pairs}",
            secret_id,
        )
        if rc != 0:
            if _answered(err) and "already" in err.lower():
                raise AlreadyRegistered()
            self._fail(err, "the SecretID create failed")
        data = self._data(out, "the create response is malformed")
        return {key: data.get(key) for key in _SECRET_ID_KEYS[:3]}

    def destroy_secret_id_accessor(self, approle_name, accessor):
        rc, _out, err = self._bao(
            f"write auth/approle/role/{_safe(approle_name)}/secret-id-accessor/destroy secret_id_accessor=-",
            accessor,
        )
        if rc == 0:
            return None
        lowered = err.lower()
        if _answered(err) and ("not found" in lowered or "failed to find" in lowered):
            return None  # the accessor is already gone
        self._fail(err, "the accessor destroy failed")

    # KV v2

    def read_kv2(self, path):
        rc, out, err = self._bao(f"read -format=json {_safe(path)}")
        if rc == 2 and "No value found" in err:
            return None
        if rc != 0:
            self._fail(err, "the secret read failed")
        data = self._data(out, "the secret response is malformed")
        values, meta = data.get("data"), data.get("metadata")
        if values is None:
            return None  # the latest version is deleted
        if not isinstance(values, dict) or not isinstance(meta, dict) or not isinstance(meta.get("version"), int):
            raise TransportError("the secret response is malformed")
        return values, meta["version"]

    def _write(self, path, data, cas):
        body = json.dumps({"options": {"cas": cas}, "data": data})
        return self._bao(f"write -format=json {_safe(path)} -", body)

    def write_kv2(self, path, data, cas):
        rc, _out, err = self._write(path, data, cas)
        if rc != 0:
            self._fail(err, "the secret write failed")

    def create_staging(self, path, data):
        rc, _out, err = self._write(path, data, 0)
        if rc == 0:
            return None
        if _answered(err) and "check-and-set" in err:
            raise StagingExists()
        self._fail(err, "the staging create failed")

    def read_staging(self, path):
        current = self.read_kv2(path)
        return None if current is None else current[0]

    def delete_staging(self, path):
        rc, _out, err = self._bao(f"delete {_safe(_metadata_path(path))}")
        if rc != 0:
            self._fail(err, "the staging delete failed")
