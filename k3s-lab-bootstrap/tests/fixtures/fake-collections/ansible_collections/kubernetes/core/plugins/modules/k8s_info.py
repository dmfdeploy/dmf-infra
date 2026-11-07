#!/usr/bin/python
"""Fake kubernetes.core.k8s_info: reads objects from $FAKE_STATE/k8s."""
import json
import os

from ansible.module_utils.basic import AnsibleModule


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
    state = os.environ.get("FAKE_STATE", "")
    path = os.path.join(state, "k8s", "%s-%s.json" % (module.params["kind"], module.params["name"] or "all"))
    resources = []
    if os.path.exists(path):
        with open(path) as handle:
            data = json.load(handle)
        resources = data if isinstance(data, list) else [data]
    module.exit_json(changed=False, resources=resources)


if __name__ == "__main__":
    main()
