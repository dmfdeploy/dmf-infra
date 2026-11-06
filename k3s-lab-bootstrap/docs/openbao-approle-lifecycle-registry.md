# OpenBao AppRole lifecycle registry

`vars/openbao-approle-lifecycle-registry.json` is the configured inventory of
finite-TTL AppRoles. The OpenBao and object-storage roles load it when they
create AppRoles; the configured SecretID TTL is read from the registry.
`tests/test_openbao_approle_lifecycle_registry.py` compares its target set with
the configured role names and generated object-storage targets, and fails if
a finite-TTL write site appears outside the registry.

The registry separates continuously managed targets from explicit
dispositions. `born-inventory-netbox-reader` is setup-only: it has no rotation
metadata record and is not an observer target. Its registry entry holds the
AppRole name, the SecretID TTL, and the flags that mark it as setup-only with
no continuous rotation or expiry monitoring.

The roles check their inputs before they create anything. The OpenBao role
fails if an inventory sets `openbao_eso_approle_secret_id_ttl` or
`openbao_born_inventory_approle_secret_id_ttl`. Each message says to move the
value into `secret_id_ttl` in the registry. The role also fails if
`openbao_eso_role_name` or `openbao_born_inventory_role_name` differs from the
`approle_name` of its registry entry. Each of those messages prints the
configured value and the registry's value, and names the entry.

The object-storage role fails if an inventory sets
`openbao_approle_secret_id_ttl`; its message says to move the value into
`secret_id_ttl` of the matching runtime target. It also requires
`object_storage_logicals` to be non-empty and free of duplicates. Each listed
logical must equal the `logical_name` of a runtime target, spelled exactly, so a
dashed alias such as `app-backups` fails. The listed logicals may be a subset of
the registered ones. The message for an unregistered logical names
`logical_name` under `runtime_targets`.

Runtime records include every current delivery destination. For the generated
object-storage roles, the KV v2 ESO binding is retained alongside the
Kubernetes Secret used by ESO. `existing_check` names a readiness check already
implemented by the current role. `required_rotation_gate` names a check the
rotation work must add; `evidence_gap` states where the current role does not
yet wait for that condition. A registry entry does not claim that the new
rotation transaction or its readiness gate has been implemented.

The read-only metric record format is defined in
[`openbao-approle-rotation-metadata-v1.md`](openbao-approle-rotation-metadata-v1.md).
The observer reads each configured runtime target directly and does not list
the KV prefix.
