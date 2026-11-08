"""The OpenBao policy and the Kubernetes RBAC of the rotation scheduler
identity, both rendered from the registry.

The identity may only reach what the rotation engine in `rotation.py` calls,
for the targets the registry lists. The policy names every path in full: no
glob, no `sys/*`, no policy or auth administration, no application data, no
random `secret-id` endpoint and no accessor listing. The custom SecretID
grant lists every parameter of the request, because OpenBao denies a
parameter that an `allowed_parameters` map leaves out. The candidate value
itself is free (an empty list); the TTL and the use limit are fixed to the
registry's. The design is the
"Least-privilege identities" section of
`docs/plans/openbao-approle-credential-lifecycle-plan.md` in the dmfdeploy
umbrella repository.

Kubernetes RBAC follows the same rule. Writes (`update`, `patch`, `delete`)
name their objects. The one exception is `create` on Leases, which RBAC cannot
limit by name, so it is limited to the Lease namespace. The rule set also has
narrow reads that the readiness gates of the registry need.

Run `python3 -m approle_lifecycle.scheduler_policy policy` to print the policy
and `... rbac` to print the manifests as JSON.
"""

import argparse
import json
import sys

from . import adapters, registry, rotation

POLICY_RELATIVE = "roles/stack/operator/openbao/files/policies/rotation-scheduler.hcl"
_KV_READ_WRITE = ["create", "read", "update"]


def _quoted(items):
    return "[" + ", ".join(f'"{item}"' for item in items) + "]"


def _block(path, capabilities, extra=()):
    lines = [f'path "{path}" {{', f"  capabilities = {_quoted(capabilities)}"]
    lines.extend(f"  {line}" for line in extra)
    lines.append("}")
    return "\n".join(lines)


def _kv_destinations(entry):
    return [d["path"] for d in entry["destinations"] if d["kind"] == "openbao_kv2_record"]


def render_policy(registry_data):
    """The policy text for every runtime target of `registry_data`."""
    sections = [
        "# Rotation scheduler: runs the rotation transaction for the runtime AppRole targets.\n"
        "# Rendered from vars/openbao-approle-lifecycle-registry.json by\n"
        "# lib/approle_lifecycle/scheduler_policy.py. Do not edit by hand.",
        _block("auth/token/revoke-self", ["update"]),
    ]
    for target in sorted(registry_data["runtime_targets"]):
        entry = registry_data["runtime_targets"][target]
        role = f"auth/approle/role/{entry['approle_name']}"
        ttl = registry.ttl_seconds(entry["secret_id_ttl"])
        staging = rotation.staging_path(entry)
        blocks = [
            f"# {target}",
            _block(role, ["read"]),
            _block(f"{role}/secret-id/lookup", ["update"]),
            _block(
                f"{role}/custom-secret-id",
                ["update"],
                [
                    'required_parameters = ["secret_id", "ttl", "num_uses"]',
                    "allowed_parameters = {",
                    '  "secret_id" = []',
                    f'  "ttl"       = ["{ttl}s"]',
                    f'  "num_uses"  = ["{rotation.SECRET_ID_NUM_USES}"]',
                    "}",
                ],
            ),
            _block(f"{role}/secret-id-accessor/destroy", ["update"]),
            _block(entry["metadata_path"], _KV_READ_WRITE),
            _block(rotation.transaction_path(entry), _KV_READ_WRITE),
            _block(staging, ["create", "read"]),
            _block(staging.replace("/data/", "/metadata/", 1), ["delete"]),
        ]
        blocks.extend(_block(path, _KV_READ_WRITE) for path in _kv_destinations(entry))
        sections.append("\n\n".join(blocks))
    return "\n\n".join(sections) + "\n"


def _gate_namespaces(gate, inventory):
    if gate.get("namespace"):
        return [gate["namespace"]]
    namespaces = adapters._lookup(inventory, gate["namespaces_from"])
    if not namespaces or not isinstance(namespaces, (list, tuple)):
        raise ValueError(f"the namespaces of the gate {gate['namespaces_from']} are not in the inventory")
    return list(namespaces)


def render_rbac(registry_data, inventory, *, namespace, service_account, lease_namespace, lease_name):
    """The Kubernetes manifests for the scheduler ServiceAccount, as a list of
    dicts: the ServiceAccount, one Role and RoleBinding per namespace it
    touches, and a ClusterRole and ClusterRoleBinding for the stores it reads."""
    rules = {}
    stores = set()

    def add(ns, api_groups, resources, verbs, names=None):
        rule = {"apiGroups": api_groups, "resources": resources, "verbs": verbs}
        if names is not None:
            rule["resourceNames"] = sorted(names)
        rules.setdefault(ns, []).append(rule)

    add(lease_namespace, ["coordination.k8s.io"], ["leases"], ["get", "update", "delete"], [lease_name])
    add(lease_namespace, ["coordination.k8s.io"], ["leases"], ["create"])
    written, read = {}, {}
    for entry in registry_data["runtime_targets"].values():
        for destination in entry["destinations"]:
            if destination["kind"] == "kubernetes_secret_key":
                written.setdefault(destination["namespace"], set()).add(destination["name"])
        for gate in entry["readiness_gates"]:
            if gate["kind"] == "ClusterSecretStore":
                stores.add(gate["name"])
            elif gate["kind"] == "ExternalSecret":
                for ns in _gate_namespaces(gate, inventory):
                    add(ns, ["external-secrets.io"], ["externalsecrets"], ["list"])
            elif gate["kind"] == "KubernetesSecret":
                read.setdefault(gate["namespace"], set()).add(gate["name"])
    for ns in sorted(written):
        add(ns, [""], ["secrets"], ["get", "patch", "update"], written[ns])
    for ns in sorted(read):
        add(ns, [""], ["secrets"], ["get"], read[ns])

    def meta(name, ns=None):
        found = {"name": name}
        if ns is not None:
            found["namespace"] = ns
        return found

    subject = [{"kind": "ServiceAccount", "name": service_account, "namespace": namespace}]
    role_name = service_account
    manifests = [{"apiVersion": "v1", "kind": "ServiceAccount", "metadata": meta(service_account, namespace)}]
    for ns in sorted(rules):
        manifests.append({
            "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
            "metadata": meta(role_name, ns), "rules": rules[ns],
        })
        manifests.append({
            "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": meta(role_name, ns),
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": role_name},
            "subjects": subject,
        })
    if stores:
        manifests.append({
            "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRole", "metadata": meta(role_name),
            "rules": [{
                "apiGroups": ["external-secrets.io"], "resources": ["clustersecretstores"],
                "verbs": ["get"], "resourceNames": sorted(stores),
            }],
        })
        manifests.append({
            "apiVersion": "rbac.authorization.k8s.io/v1", "kind": "ClusterRoleBinding", "metadata": meta(role_name),
            "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "ClusterRole", "name": role_name},
            "subjects": subject,
        })
    return manifests


def split_rbac(manifests, lease_namespace):
    """Split the manifests of `render_rbac` into the identity part (the
    ServiceAccount and everything in the Lease namespace) and the consumer part
    (everything else). The identity part can be applied when OpenBao is set up.
    The consumer part names namespaces that other roles create later."""
    identity = [
        m for m in manifests
        if m["kind"] == "ServiceAccount" or m["metadata"].get("namespace") == lease_namespace
    ]
    consumers = [m for m in manifests if m not in identity]
    return identity, consumers


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("policy", "rbac"):
        command = commands.add_parser(name)
        command.add_argument("--registry", required=True)
    rbac = commands.choices["rbac"]
    rbac.add_argument("--inventory-json", required=True)
    rbac.add_argument("--scope", choices=("all", "identity", "consumers"), default="all")
    rbac.add_argument("--namespace", required=True)
    rbac.add_argument("--service-account", required=True)
    rbac.add_argument("--lease-namespace", required=True)
    rbac.add_argument("--lease-name", default=rotation.LEASE_NAME)
    args = parser.parse_args(argv)
    registry_data = registry.load(args.registry)
    if args.command == "policy":
        sys.stdout.write(render_policy(registry_data))
        return 0
    manifests = render_rbac(
        registry_data, json.loads(args.inventory_json), namespace=args.namespace,
        service_account=args.service_account, lease_namespace=args.lease_namespace, lease_name=args.lease_name,
    )
    identity, consumers = split_rbac(manifests, args.lease_namespace)
    print(json.dumps({"all": manifests, "identity": identity, "consumers": consumers}[args.scope]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
