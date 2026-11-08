"""In-memory clients for the AppRole lifecycle tests. They hold synthetic
values only and record every call."""

import copy

from approle_lifecycle.setup_only import ClaimConflict

SECRET = "synthetic-secret-id-not-a-credential"


class FakeKubernetes:
    def __init__(self):
        self.secrets = {}
        self.objects = {}
        self.lists = {}
        self.patches = []
        self.versions = {}
        self.fail_patch = False
        self.read_error = None
        self.receipts = {}
        self.claims = {}
        self.cluster_uid = "cluster-uid-1"
        self.token_requests = []
        self.calls = []

    # adapters
    def get_secret(self, namespace, name):
        self.calls.append(("get_secret", namespace, name))
        if self.read_error is not None:
            raise self.read_error
        value = self.secrets.get((namespace, name))
        if value is None:
            return None
        found = copy.deepcopy(value)
        found["resource_version"] = str(self.versions.get((namespace, name), 1))
        return found

    def _check_version(self, namespace, name, resource_version):
        if resource_version is not None and str(self.versions.get((namespace, name), 1)) != resource_version:
            raise RuntimeError("the object has been modified")
        self.versions[(namespace, name)] = self.versions.get((namespace, name), 1) + 1

    def annotate_secret(self, namespace, name, annotations, resource_version):
        self.calls.append(("annotate_secret", namespace, name))
        self._check_version(namespace, name, resource_version)
        self.secrets[(namespace, name)]["annotations"].update(annotations)

    def patch_secret(self, namespace, name, key, value, annotations, resource_version=None):
        self.calls.append(("patch_secret", namespace, name, key))
        if self.fail_patch:
            raise RuntimeError(f"transport said the body was {value}")
        self._check_version(namespace, name, resource_version)
        current = self.secrets.setdefault((namespace, name), {"data": {}, "annotations": {}})
        current["data"][key] = value
        current["annotations"].update(annotations)
        self.patches.append((namespace, name, key, dict(annotations)))

    def get_object(self, kind, namespace, name):
        self.calls.append(("get_object", kind, namespace, name))
        if self.read_error is not None:
            raise self.read_error
        return copy.deepcopy(self.objects.get((kind, namespace, name)))

    def list_objects(self, kind, namespace):
        self.calls.append(("list_objects", kind, namespace))
        if self.read_error is not None:
            raise self.read_error
        return copy.deepcopy(self.lists.get((kind, namespace), []))

    # setup-only path
    def get_receipt(self, namespace, name):
        self.calls.append(("get_receipt", namespace, name))
        return copy.deepcopy(self.receipts.get((namespace, name)))

    def create_claim(self, namespace, name, annotations):
        self.calls.append(("create_claim", namespace, name))
        if (namespace, name) in self.claims:
            raise ClaimConflict()
        self.claims[(namespace, name)] = dict(annotations)

    def kube_system_uid(self):
        self.calls.append(("kube_system_uid",))
        return self.cluster_uid

    def request_token(self, namespace, service_account, audiences):
        self.calls.append(("request_token", namespace, service_account))
        self.token_requests.append((namespace, service_account, tuple(audiences)))
        return "synthetic-service-account-jwt"


class FakeOpenBao:
    def __init__(self):
        self.records = {}
        self.fail_write = False
        self.calls = []
        self.issued = []
        self.destroyed = []
        self.revoked_tokens = []
        self.issue_response = None
        self.fail_destroy = False
        self.fail_revoke_self = False
        self.fail_login = False
        self.read_error = None
        self.issue_error = None
        self.destroy_error = None

    # adapters
    def read_kv2(self, path):
        self.calls.append(("read_kv2", path))
        if self.read_error is not None:
            raise self.read_error
        value = self.records.get(path)
        return (copy.deepcopy(value[0]), value[1]) if value is not None else None

    def write_kv2(self, path, data, cas):
        self.calls.append(("write_kv2", path, cas))
        if self.fail_write:
            raise RuntimeError(f"check-and-set failed for {data}")
        current = self.records.get(path)
        version = current[1] if current is not None else 0
        if cas != version:
            raise RuntimeError("check-and-set parameter did not match the current version")
        self.records[path] = (copy.deepcopy(data), version + 1)

    # setup-only path
    def login_kubernetes(self, role, jwt):
        self.calls.append(("login_kubernetes", role))
        if self.fail_login:
            raise RuntimeError(f"login refused for {jwt}")
        return "synthetic-client-token"

    def issue_secret_id(self, token, approle_name, params):
        self.calls.append(("issue_secret_id", approle_name, dict(params)))
        self.issued.append((approle_name, dict(params)))
        if self.issue_error is not None:
            raise self.issue_error
        if self.issue_response is not None:
            return dict(self.issue_response)
        return {
            "secret_id": SECRET,
            "secret_id_accessor": "synthetic-accessor",
            "secret_id_ttl": 3600,
            "secret_id_num_uses": 1,
        }

    def destroy_secret_id_accessor(self, token, approle_name, accessor):
        self.calls.append(("destroy_secret_id_accessor", approle_name))
        if self.destroy_error is not None:
            raise self.destroy_error
        if self.fail_destroy:
            raise RuntimeError("destroy unavailable")
        self.destroyed.append((approle_name, accessor))

    def revoke_self(self, token):
        self.calls.append(("revoke_self",))
        if self.fail_revoke_self:
            raise RuntimeError("revoke unavailable")
        self.revoked_tokens.append(token)
