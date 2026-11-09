#!/usr/bin/python
"""Fake kubernetes.core.k8s: logs the call and keeps the few objects that a full
run of the grow-HA play reads back.

- Every call goes to $FAKE_STATE/k8s-mutations.log and to $FAKE_STATE/events.log.
- Creating a StatefulSet writes k8s/StatefulSet-<name>.json, ready, and k8s/Pod-all.json with one running pod per replica,
  each with the ServiceAccount of the pod template (or `default`).
- A call with force true is also logged to $FAKE_STATE/k8s-force.log.
- Applying a ClusterSecretStore writes k8s/ClusterSecretStore-<name>.json. An update
  merges maps into the stored object, as a patch of a custom resource does, unless
  force is true, which replaces the object as a replace does. The store is Ready.
  Every write stores a metadata.resourceVersion that is one higher than the last write
  (the first is "1"). As the API server does for a custom resource, a forced replace of
  an existing store fails unless the definition's metadata.resourceVersion equals the
  stored one. A create and an update without force do not check it.
- Deleting a Pod writes k8s/Pod-<name>.json with a new UID, as the StatefulSet
  recreates the pod, and marks the pod sealed for the fake bao.
- Creating a Certificate writes k8s/Secret-<secretName>.json with a certificate
  that holds the DNS names of the Certificate, as cert-manager replaces the secret.
  The fake openssl prints those names, so a test sees the names that the play
  rendered.
- Applying a ConfigMap whose data holds a value that is not a string, or a Secret whose
  stringData does, fails with the API server's message, as the API server does:
  the fields take strings only, so a template that renders a boolean or a number
  fails here too.
- Applying a ConfigMap writes k8s/ConfigMap-<name>.json with its data.
"""
import base64
import json
import os

from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(
        argument_spec=dict(
            kubeconfig=dict(type="str"),
            state=dict(type="str", default="present"),
            definition=dict(type="raw"),
            api_version=dict(type="str"),
            kind=dict(type="str"),
            name=dict(type="str"),
            namespace=dict(type="str"),
            delete_options=dict(type="dict"),
            wait=dict(type="bool"),
            force=dict(type="bool", default=False),
        ),
    )
    p = module.params
    kind = p["kind"] or (p["definition"] or {}).get("kind", "?")
    name = p["name"] or ((p["definition"] or {}).get("metadata", {}) or {}).get("name", "?")
    state = os.environ.get("FAKE_STATE", "")
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, "k8s-mutations.log"), "a") as handle:
        handle.write("%s %s %s\n" % (p["state"], kind, name))
    with open(os.path.join(state, "events.log"), "a") as handle:
        handle.write("k8s %s %s %s\n" % (p["state"], kind, name))
    objects = os.path.join(state, "k8s")
    if p["state"] == "present" and isinstance(p["definition"], dict):
        check_string_fields(module, kind, p["definition"])
    if p["state"] == "present" and kind == "ConfigMap":
        write_object(objects, "ConfigMap-" + name, {"data": (p["definition"] or {}).get("data") or {}})
    if p["force"]:
        with open(os.path.join(state, "k8s-force.log"), "a") as handle:
            handle.write("%s %s\n" % (kind, name))
    if p["state"] == "present" and kind == "StatefulSet":
        spec = (p["definition"] or {}).get("spec", {}) or {}
        replicas = int(spec.get("replicas", 1))
        account = (((spec.get("template") or {}).get("spec")) or {}).get("serviceAccountName", "default")
        write_object(objects, "StatefulSet-" + name, {"status": {"readyReplicas": replicas}})
        write_object(objects, "Pod-all", [running_pod("openbao-%d" % i, "uid-%d-0" % i, account) for i in range(replicas)])
    if p["state"] == "present" and kind == "ClusterSecretStore":
        stem = "ClusterSecretStore-" + name
        stored = os.path.join(objects, stem + ".json")
        new = json.loads(json.dumps(p["definition"]))
        version = 1
        if os.path.exists(stored):
            with open(stored) as handle:
                old = json.load(handle)
            version = int(old["metadata"]["resourceVersion"]) + 1
            if p["force"]:
                sent = (new.get("metadata") or {}).get("resourceVersion")
                if sent != old["metadata"]["resourceVersion"]:
                    module.fail_json(msg="%s.%s %r is invalid: metadata.resourceVersion: Invalid value: %r: must be specified for an update, and must equal the stored version %s"
                                     % (kind, "external-secrets.io", name, sent, old["metadata"]["resourceVersion"]))
            else:
                new = merge(old, new)
        new.setdefault("metadata", {})["resourceVersion"] = str(version)
        new["status"] = {"conditions": [{"type": "Ready", "status": "True"}]}
        write_object(objects, stem, new)
    if p["state"] == "present" and kind == "Certificate":
        spec = (p["definition"] or {}).get("spec", {}) or {}
        names = spec.get("dnsNames", [])
        if isinstance(names, str):
            names = [names]
        crt = "dnsNames:" + ",".join(names)
        write_object(objects, "Secret-" + spec.get("secretName", "tls"),
                     {"data": {"tls.crt": base64.b64encode(crt.encode()).decode(), "tls.key": "ZmFrZQ=="}})
    if p["state"] == "absent" and kind == "Pod":
        restarts = os.path.join(state, "restarts-%s" % name)
        count = int(open(restarts).read()) + 1 if os.path.exists(restarts) else 1
        with open(restarts, "w") as handle:
            handle.write(str(count))
        write_object(objects, "Pod-%s" % name, running_pod(name, "uid-%s-%d" % (name, count)))
        for stale in ("unsealed-%s" % name, "progress-%s" % name):
            if os.path.exists(os.path.join(state, stale)):
                os.remove(os.path.join(state, stale))
        open(os.path.join(state, "sealed-%s" % name), "w").close()
    module.exit_json(changed=True)


def check_string_fields(module, kind, definition):
    field = {"ConfigMap": "data", "Secret": "stringData"}.get(kind)
    values = definition.get(field) if field else None
    if not isinstance(values, dict):
        return
    for key, value in values.items():
        if not isinstance(value, str):
            module.fail_json(
                msg='%s in version "v1" cannot be handled as a %s: json: cannot unmarshal %s into Go struct field %s.%s of type string (400 Bad Request)'
                % (kind, kind, json_type(value), kind, field))


def json_type(value):
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, list):
        return "array"
    if isinstance(value, dict):
        return "object"
    return "null"


def merge(old, new):
    # A JSON merge patch: maps merge, any other value is replaced.
    if isinstance(old, dict) and isinstance(new, dict):
        merged = dict(old)
        for key, value in new.items():
            merged[key] = merge(old[key], value) if key in old else value
        return merged
    return new


def running_pod(name, uid, account="default"):
    return {
        "metadata": {"name": name, "uid": uid},
        "spec": {"serviceAccountName": account},
        "status": {"phase": "Running", "containerStatuses": [{"state": {"running": {}}}]},
    }


def write_object(directory, stem, data):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, stem + ".json"), "w") as handle:
        json.dump(data, handle)


if __name__ == "__main__":
    main()
