#!/usr/bin/python
"""Fake kubernetes.core.k8s_info: reads objects from $FAKE_STATE/k8s. It fails when
FAKE_K8S_INFO_FAILS is set. When FAKE_K8S_UNREACHABLE is KIND or KIND/NAME and the read
is of that object, the result says the host is unreachable, as a lost connection does.
When FAKE_K8S_WRITE_AFTER_READ is set, a read of a ClusterSecretStore returns the stored
object and then raises its metadata.resourceVersion by one, as another writer that
changes the store right after the read does. A label selector of the form KEY=VALUE keeps
the objects whose metadata.labels hold it. An object with no labels counts as a pod of
the OpenBao server (app.kubernetes.io/name=openbao)."""
import json
import os

from ansible.module_utils.basic import AnsibleModule


def selected(resource, selectors):
    labels = (resource.get("metadata", {}) or {}).get("labels") or {"app.kubernetes.io/name": "openbao"}
    for selector in selectors:
        if "=" in selector and "!=" not in selector:
            key, value = selector.split("=", 1)
            if labels.get(key) != value:
                return False
    return True


def main():
    module = AnsibleModule(
        argument_spec=dict(
            kubeconfig=dict(type="str"),
            api_version=dict(type="str"),
            kind=dict(type="str", required=True),
            name=dict(type="str"),
            namespace=dict(type="str"),
            label_selectors=dict(type="list", elements="str"),
        ),
        supports_check_mode=True,
    )
    unreachable = os.environ.get("FAKE_K8S_UNREACHABLE")
    if unreachable in (module.params["kind"], "%s/%s" % (module.params["kind"], module.params["name"])):
        module.exit_json(unreachable=True, msg="the host is unreachable")
    if os.environ.get("FAKE_K8S_INFO_FAILS"):
        module.fail_json(msg="the API server is not reachable")
    state = os.environ.get("FAKE_STATE", "")
    path = os.path.join(state, "k8s", "%s-%s.json" % (module.params["kind"], module.params["name"] or "all"))
    resources = []
    if os.path.exists(path):
        with open(path) as handle:
            data = json.load(handle)
        resources = data if isinstance(data, list) else [data]
    if os.environ.get("FAKE_K8S_WRITE_AFTER_READ") and module.params["kind"] == "ClusterSecretStore" and resources:
        with open(path) as handle:
            data = json.load(handle)
        data["metadata"]["resourceVersion"] = str(int(data["metadata"]["resourceVersion"]) + 1)
        with open(path, "w") as handle:
            json.dump(data, handle)
    resources = [r for r in resources if selected(r, module.params["label_selectors"] or [])]
    module.exit_json(changed=False, resources=resources)


if __name__ == "__main__":
    main()
