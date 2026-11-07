#!/usr/bin/python
"""Fake kubernetes.core.k8s: logs the call and changes nothing."""
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
    module.exit_json(changed=True)


if __name__ == "__main__":
    main()
