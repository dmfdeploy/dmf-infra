# OpenBao AppRole lifecycle registry

`vars/openbao-approle-lifecycle-registry.json` is the configured inventory of
finite-TTL AppRoles. The OpenBao and object-storage roles load it when they
create AppRoles; the configured SecretID TTL is read from the registry.
`tests/test_openbao_approle_lifecycle_registry.py` compares its target set with
the configured role names and generated object-storage targets, and fails if
a finite-TTL write site appears outside the registry.

The registry separates continuously managed targets from explicit
dispositions. `born-inventory-netbox-reader` is setup-only: it has no rotation
metadata record and is not an observer target. The setup-only record names the
checked entrypoints that require a fresh one-use credential on each invocation.

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
