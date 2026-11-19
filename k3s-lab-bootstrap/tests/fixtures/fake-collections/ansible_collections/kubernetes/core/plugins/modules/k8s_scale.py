#!/usr/bin/python
"""Fake kubernetes.core.k8s_scale: sets spec.replicas of a stored object.

- Every call goes to $FAKE_STATE/k8s-mutations.log and to $FAKE_STATE/events.log as
  "scale KIND NAME replicas=N".
- An object that is not in $FAKE_STATE/k8s fails with "not found", as the real module does.
- Scaling to zero removes the pods of $FAKE_STATE/k8s/Pod-all.json whose label
  app.kubernetes.io/name equals the object's name, as the Deployment controller ends them.
  With FAKE_K8S_PODS_LINGER set, the pods stay, as pods that are slow to end do.
- When FAKE_K8S_UNREACHABLE is KIND or KIND/NAME, the call is logged and its result says
  the host is unreachable."""
import json
import os

from ansible.module_utils.basic import AnsibleModule


def main():
    module = AnsibleModule(
        argument_spec=dict(
            kubeconfig=dict(type="str"),
            api_version=dict(type="str"),
            kind=dict(type="str", required=True),
            name=dict(type="str", required=True),
            namespace=dict(type="str"),
            replicas=dict(type="int", required=True),
            wait=dict(type="bool", default=True),
        ),
    )
    p = module.params
    state = os.environ.get("FAKE_STATE", "")
    os.makedirs(state, exist_ok=True)
    with open(os.path.join(state, "k8s-mutations.log"), "a") as handle:
        handle.write("scale %s %s replicas=%s\n" % (p["kind"], p["name"], p["replicas"]))
    with open(os.path.join(state, "events.log"), "a") as handle:
        handle.write("k8s scale %s %s replicas=%s\n" % (p["kind"], p["name"], p["replicas"]))
    if os.environ.get("FAKE_K8S_UNREACHABLE") in (p["kind"], "%s/%s" % (p["kind"], p["name"])):
        module.exit_json(unreachable=True, msg="the host is unreachable")
    path = os.path.join(state, "k8s", "%s-%s.json" % (p["kind"], p["name"]))
    if not os.path.exists(path):
        module.fail_json(msg="%s %s not found" % (p["kind"], p["name"]))
    with open(path) as handle:
        data = json.load(handle)
    data.setdefault("spec", {})["replicas"] = p["replicas"]
    with open(path, "w") as handle:
        json.dump(data, handle)
    pods_path = os.path.join(state, "k8s", "Pod-all.json")
    if p["replicas"] == 0 and os.path.exists(pods_path) and not os.environ.get("FAKE_K8S_PODS_LINGER"):
        with open(pods_path) as handle:
            pods = json.load(handle)
        kept = [pod for pod in pods
                if (pod.get("metadata", {}).get("labels", {}) or {}).get("app.kubernetes.io/name") != p["name"]]
        with open(pods_path, "w") as handle:
            json.dump(kept, handle)
    module.exit_json(changed=True)


if __name__ == "__main__":
    main()
