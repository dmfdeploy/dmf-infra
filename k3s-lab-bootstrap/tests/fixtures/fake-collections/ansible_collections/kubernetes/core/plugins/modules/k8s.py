#!/usr/bin/python
"""Fake kubernetes.core.k8s: logs the call and keeps the few objects that a full
run of the grow-HA play reads back.

- Every call goes to $FAKE_STATE/k8s-mutations.log and to $FAKE_STATE/events.log.
- Creating a StatefulSet writes k8s/Pod-all.json with one running pod per replica.
- Deleting a Pod writes k8s/Pod-<name>.json with a new UID, as the StatefulSet
  recreates the pod, and marks the pod sealed for the fake bao.
"""
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
    if p["state"] == "present" and kind == "StatefulSet":
        replicas = int(((p["definition"] or {}).get("spec", {}) or {}).get("replicas", 1))
        write_object(objects, "Pod-all", [running_pod("openbao-%d" % i, "uid-%d-0" % i) for i in range(replicas)])
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


def running_pod(name, uid):
    return {
        "metadata": {"name": name, "uid": uid},
        "status": {"phase": "Running", "containerStatuses": [{"state": {"running": {}}}]},
    }


def write_object(directory, stem, data):
    os.makedirs(directory, exist_ok=True)
    with open(os.path.join(directory, stem + ".json"), "w") as handle:
        json.dump(data, handle)


if __name__ == "__main__":
    main()
