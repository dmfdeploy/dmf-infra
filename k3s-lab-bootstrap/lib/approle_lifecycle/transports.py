"""Concrete clients for `SetupOnlyClients`: `kubectl` for Kubernetes and
`kubectl exec ... bao` for OpenBao.

Every process call goes through one `run(argv, stdin_text)` function, so a
test can replace the process boundary. No credential travels in argv. A JWT, a
client token, a SecretID or an accessor is written to the child's stdin. The
token is the first stdin line. Any further value follows it and is read by
`bao` itself as `key=-`, so it never enters a shell variable or an argument.

`bao` runs inside the OpenBao pod, so the cluster CA and the pod's own
address apply and no OpenBao client is needed on the runner.
"""

import json
import re
import subprocess

from .setup_only import ClaimConflict

DEFAULT_KUBECONFIG = "/etc/rancher/k3s/k3s.yaml"
_LEASE_API = "coordination.k8s.io/v1"
# `sh` reads the first stdin line as the client token. Whatever stdin holds
# after that line stays in the pipe for `bao`.
_BAO_SHELL = "IFS= read -r BAO_TOKEN; export BAO_TOKEN; exec bao {command}"
_SAFE_WORD = re.compile(r"[A-Za-z0-9._/-]+")


class TransportError(RuntimeError):
    """A transport call failed. The message is fixed text: it never carries
    child output, which can hold a response body."""


def run(argv, stdin_text=""):
    """Run `argv` with `stdin_text` on stdin. Return (returncode, stdout,
    stderr). The only process boundary of this module."""
    done = subprocess.run(
        argv, input=stdin_text, capture_output=True, text=True, check=False, shell=False
    )
    return done.returncode, done.stdout, done.stderr


def _safe(value):
    """Names and paths reach a remote shell command line, so only a plain word
    is accepted."""
    if not isinstance(value, str) or not _SAFE_WORD.fullmatch(value):
        raise TransportError("a name or path is not a plain word")
    return value


def _non_empty_string(value):
    return isinstance(value, str) and value.strip() != ""


class KubernetesTransport:
    """Receipt read, claim create, kube-system UID and TokenRequest through
    `kubectl`. `as_user` makes the receipt read and the claim create run as
    that user, so a runner with a broad kubeconfig still acts as the scoped
    executor identity. The TokenRequest and the kube-system read run as the
    runner."""

    def __init__(self, kubeconfig=DEFAULT_KUBECONFIG, as_user=None, runner=run):
        self._base = ["kubectl", "--kubeconfig", kubeconfig]
        self._as = ["--as", as_user] if as_user else []
        self._run = runner

    def _kubectl(self, args, stdin_text=""):
        return self._run(self._base + self._as + list(args), stdin_text)

    def get_receipt(self, namespace, name):
        rc, out, err = self._kubectl(
            ["-n", namespace, "get", "configmap", name, "-o", "json", "--ignore-not-found"]
        )
        if rc != 0:
            raise TransportError("the receipt read failed")
        if not out.strip():
            return None
        try:
            obj = json.loads(out)
            meta = obj["metadata"]
            return {
                "name": meta["name"],
                "namespace": meta["namespace"],
                "uid": meta.get("uid"),
                "immutable": obj.get("immutable"),
                "data": obj.get("data"),
            }
        except (ValueError, KeyError, TypeError):
            raise TransportError("the receipt response is not a ConfigMap") from None

    def get_configmap_value(self, namespace, name, key):
        """Read one string key of a ConfigMap. Raise unless it is a non-empty
        plain word, which a role id is."""
        rc, out, _err = self._kubectl(["-n", namespace, "get", "configmap", name, "-o", "json"])
        if rc != 0:
            raise TransportError("the ConfigMap read failed")
        try:
            value = json.loads(out)["data"][key]
        except (ValueError, KeyError, TypeError):
            raise TransportError("the ConfigMap has no such key") from None
        if not isinstance(value, str) or not _SAFE_WORD.fullmatch(value):
            raise TransportError("the ConfigMap value is not a plain word")
        return value

    def create_claim(self, namespace, name, annotations):
        lease = {
            "apiVersion": _LEASE_API,
            "kind": "Lease",
            "metadata": {"name": name, "namespace": namespace, "annotations": dict(annotations)},
        }
        rc, _out, err = self._kubectl(["create", "-f", "-"], json.dumps(lease))
        if rc == 0:
            return None
        if "AlreadyExists" in err or "already exists" in err:
            raise ClaimConflict()
        raise TransportError("the claim create failed")

    def kube_system_uid(self):
        # The executor ServiceAccount has no cluster-scoped access, so the
        # runner's own kubeconfig identity reads the Namespace.
        rc, out, _err = self._run(
            self._base + ["get", "namespace", "kube-system", "-o", "jsonpath={.metadata.uid}"], ""
        )
        uid = out.strip()
        if rc != 0 or not uid:
            raise TransportError("the kube-system UID read failed")
        return uid

    def request_token(self, namespace, service_account, audiences):
        """Mint a short-lived JWT for the ServiceAccount. The caller's own
        kubeconfig identity makes the TokenRequest, so no `--as` applies."""
        args = ["-n", namespace, "create", "token", service_account, "--duration", "10m"]
        for audience in audiences:
            args += ["--audience", audience]
        rc, out, _err = self._run(self._base + args, "")
        jwt = out.strip()
        if rc != 0 or not jwt:
            raise TransportError("the TokenRequest failed")
        return jwt


class OpenBaoTransport:
    """Kubernetes-auth login, SecretID issue, accessor destroy, self-revoke and
    the AppRole login and KV read that the caller's `use` function needs, all
    through `kubectl exec` into the OpenBao pod."""

    def __init__(self, namespace, pod, kubeconfig=DEFAULT_KUBECONFIG, runner=run):
        self._exec = [
            "kubectl", "--kubeconfig", kubeconfig, "-n", namespace, "exec", "-i", pod,
            "--", "sh", "-c",
        ]
        self._run = runner

    def _bao(self, command, stdin_text):
        shell = _BAO_SHELL.format(command=command)
        rc, out, _err = self._run(self._exec + [shell], stdin_text)
        if rc != 0:
            raise TransportError("an OpenBao call failed")
        return out

    def _login(self, command, stdin_text):
        """Login calls run without a token, so they skip the token read."""
        rc, out, _err = self._run(self._exec + [f"exec bao {command}"], stdin_text)
        if rc != 0:
            raise TransportError("an OpenBao login failed")
        try:
            token = json.loads(out)["auth"]["client_token"]
        except (ValueError, KeyError, TypeError):
            raise TransportError("the login response has no client token") from None
        if not _non_empty_string(token):
            raise TransportError("the login response has no client token")
        return token

    def login_kubernetes(self, role, jwt):
        if not _non_empty_string(jwt):
            raise TransportError("the ServiceAccount JWT is empty")
        return self._login(f"write -format=json auth/kubernetes/login role={_safe(role)} jwt=-", jwt)

    def login_approle(self, role_id, secret_id):
        """AppRole login. The SecretID is the only stdin content, and the role
        id is not a credential, so it may sit in argv."""
        if not _non_empty_string(secret_id):
            raise TransportError("the SecretID is empty")
        return self._login(
            f"write -format=json auth/approle/login role_id={_safe(role_id)} secret_id=-", secret_id
        )

    def issue_secret_id(self, token, approle_name, params):
        pairs = " ".join(f"{_safe(key)}={_safe(str(value))}" for key, value in sorted(params.items()))
        out = self._bao(
            f"write -format=json -force auth/approle/role/{_safe(approle_name)}/secret-id {pairs}",
            f"{token}\n",
        )
        try:
            data = json.loads(out)["data"]
        except (ValueError, KeyError, TypeError):
            raise TransportError("the create response is not a SecretID response") from None
        if not isinstance(data, dict):
            raise TransportError("the create response is not a SecretID response")
        # A valid accessor is returned even when the rest of the response is
        # malformed, so the library can destroy it. The SecretID is blanked
        # unless it is a non-empty string, and the library rejects a blank
        # SecretID before it reaches the caller's `use` function.
        if not _non_empty_string(data.get("secret_id_accessor")):
            raise TransportError("the create response has no accessor")
        secret_id = data.get("secret_id")
        return {
            "secret_id": secret_id if _non_empty_string(secret_id) else "",
            "secret_id_accessor": data["secret_id_accessor"],
            "secret_id_ttl": data.get("secret_id_ttl"),
            "secret_id_num_uses": data.get("secret_id_num_uses"),
        }

    def destroy_secret_id_accessor(self, token, approle_name, accessor):
        if not _non_empty_string(accessor):
            raise TransportError("the accessor is empty")
        self._bao(
            f"write auth/approle/role/{_safe(approle_name)}/secret-id-accessor/destroy secret_id_accessor=-",
            f"{token}\n{accessor}",
        )

    def revoke_self(self, token):
        self._bao("token revoke -self", f"{token}\n")

    def read_kv2_field(self, token, path, field):
        """Read one string field of a KV v2 secret. `path` is the API path,
        for example `secret/data/apps/netbox/runtime`."""
        out = self._bao(f"read -format=json {_safe(path)}", f"{token}\n")
        try:
            value = json.loads(out)["data"]["data"][field]
        except (ValueError, KeyError, TypeError):
            raise TransportError("the secret has no such field") from None
        if not _non_empty_string(value):
            raise TransportError("the secret field is empty")
        return value.strip()
