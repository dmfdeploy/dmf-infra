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

## Library: adapters and the setup-only path

`lib/approle_lifecycle/` holds the code that the rotation transaction calls. It
uses the standard library only and takes client objects, so a playbook, a job
or a test can run it. It does not read or print a credential.

- `registry.py` loads the registry, validates its shape and derives the
  thresholds. A runtime target also names its `consumer_type` and the
  `metadata_path` of its rotation record, which must equal
  `secret/data/platform/approle-rotation/<target>`.
- `adapters.py` has one delivery adapter for each destination kind and one
  readiness adapter for each gate kind that the registry declares. A delivery
  adapter writes the SecretID and stamps the transaction id and target version
  with it. A Kubernetes Secret takes them as annotations in the same request.
  A KV v2 record takes them as keys, and the write keeps every other key and
  uses check-and-set. An unknown kind or a missing client is an error.
  Every readiness adapter needs the delivery time and fails with
  `readiness_failed` without it. The ClusterSecretStore gate and the Secret key
  gate are preconditions and never accept a replacement alone. The
  ExternalSecret gate checks that the ExternalSecret reads the expected
  ClusterSecretStore (the one the target's own store gate names), is Ready,
  and has a `refreshTime` strictly after the delivery. A pass proves a
  successful refresh through the expected store after the delivery. It does
  not prove authentication with the replacement, because ESO may reuse a
  cached token. That proof belongs to step 3, so no status can make a target
  accepted until step 3 defines it. Every runtime target carries
  `fresh_authentication_proof`, because each one's acceptance rests on a
  ClusterSecretStore or ExternalSecret refresh, and today all three
  (`external-secrets` and both object-storage targets) are `missing`, each
  with an `evidence_gap`. The validator accepts only `missing` in this step
  and refuses a runtime target without the field. `evaluate_target` never
  accepts a runtime target in this step. It returns the reason code
  `fresh_authentication_proof_missing` for every runtime target, whatever the
  status string and whatever the gates return, and it still returns each
  gate's result.
  The `external-secrets` target names `authentik-runtime` in the `authentik`
  namespace as its ExternalSecret
  (`roles/stack/operator/authentik/tasks/main.yml`, created from the main
  `openbao` store by default). Its refresh interval is one hour, so the
  rotation transaction has to trigger a refresh. The registry records that as
  `required_rotation_gate` with an `evidence_gap`.
  Every client call goes through a wrapper that replaces any exception, a
  `BaseException` included, with a fixed-message error or a fresh instance of
  the interrupt class, so a response body cannot reach a traceback. A failed
  readiness read is a failed gate.
- `setup_only.py` runs the fresh-on-rerun path for `born-inventory-netbox-reader`:
  validate the immutable request receipt, claim it once with a Lease, log in as
  the issuer, mint one SecretID with `ttl=1h` and `num_uses=1`, confirm an
  accessor and both bounds in the response, and hand the SecretID to the
  caller's `use` function. The code destroys the accessor on every exit path
  that the process survives, and a failed destroy raises. After a crash, or
  when no response arrives, the accessor is not known. The one-use, one-hour
  bound on what remains holds only when the create response confirmed both
  values exactly, as integers or the exact decimal strings. Every message says
  "unconfirmed" otherwise, and no message renders a value that OpenBao
  returned. The issuer policy that enforces the bound is
  `files/policies/setup-issuer.hcl` in the openbao role.
  Every transport or `use` exception becomes a `SetupOnlyError` with a fixed
  message. An interrupt (`KeyboardInterrupt`, `SystemExit`,
  `GeneratorExit`) becomes a fresh instance of its class with no arguments
  after cleanup, and it stays an interrupt even when cleanup fails: in that
  case it carries one fixed note, "cleanup failed, so the residue is
  unconfirmed". A cleanup failure after an ordinary failure is a
  `SetupOnlyError` whose cause is the module's own fixed-message error.
  None of them carries the original exception, so a SecretID cannot reach a
  traceback. Any failure before the claim leaves OpenBao and
  Kubernetes untouched.

The setup-only disposition in the registry lists the checked Ansible
entrypoints, the fresh-on-rerun requirement, the issuance and revocation
requirements with an evidence path for each, and the `create_site`. The
validator rejects a disposition without them, and a test fails when a named
path does not exist.

## What steps A.1 and A.2a deliver

Step A.1 delivers the library and its tests. Step A.2a adds the concrete
clients (`lib/approle_lifecycle/transports.py`), the command line that 694
runs (`lib/approle_lifecycle/born_inventory_cli.py`), the setup issuer
identity and policy in the openbao role, the receipt namespace and executor
RBAC, and the identity verifier probes.

Playbook 694 keeps reading the born-inventory SecretID from the persistent
break-glass file by default. The variable
`dmf_born_inventory_receipt_path_enabled` selects the receipt-driven path.
It is `false` by default. With it off, `roles/common/dmf-born-inventory/tasks/main.yml`
includes `tasks/openbao-breakglass.yml`, which holds the tasks that were in
`main.yml` unchanged: the file check, the assert, the load, the extraction,
the assert, the pod discovery, the login, the client token, the secret read
and its parse, plus the break-glass input check that used to share the first
assert. With it on, `main.yml` includes `tasks/openbao-setup-only.yml`
instead. That path needs the opaque request id in `DMF_SETUP_REQUEST_ID`,
runs the library through the transports, and never reads the break-glass
file. It refuses a direct NetBox admin token, because the token would skip the
receipt. It takes the role id, which is not a secret, from a ConfigMap that the
openbao role publishes in `dmf-setup-requests`, or from
`dmf_born_inventory_setup_role_id`, and fails closed when neither exists. Both
paths wait for live verification, which needs the Kubernetes
auth reviewer fix. The dmf-cms receipt writer (step A.2b) and the dmf-init
handoff are separate dependencies in other repositories.

## Writing the issuer role

The setup issuer's Kubernetes-auth role needs `auth/kubernetes/role/*`, which
no rerun identity holds: `policy-writer` covers only `sys/policies/acl/*`, and
ADR-0021 closes `approle-reconciler`. A fresh install writes the role with the
initial root token. An existing environment gets it from
`playbooks/vertical-security/106-openbao-kubernetes-auth.yml`, which writes it
when it is missing with the temporary root token from the generate-root
procedure, then revokes that token. The rotation scheduler's role takes the
same two paths. A rerun of the openbao role leaves an
existing role as it is, and the identity verifier fails when the role is
missing. The policy itself is written on every pass.

## Publishing the role id

The openbao role publishes the AppRole role id in the ConfigMap
`born-inventory-approle` of `dmf-setup-requests` whenever it knows the id: on
a fresh install, and on a rerun when the break-glass state holds it. A missing,
null or empty value skips the publication with one message and never fails the
role. No identity that a rerun uses can read the role id of
`born-inventory-netbox-reader`: `ops-admin` and `policy-reconciler` have no
path for it, and ADR-0021 limits `approle-reconciler` to the `object-storage-*`
roles. No policy is widened for it. On an existing environment whose break-glass
state lacks the id, set `dmf_born_inventory_setup_role_id` to the current role
id, or write it to that ConfigMap.

The enabled 694 path looks for the role id before it loads or claims the
receipt, and fails with a message that names `dmf_born_inventory_setup_role_id`,
so a missing role id consumes no receipt. A role id that is present but stale
fails at the AppRole login, after the claim. Fix the role id, then create a new
receipt and run again.

## Completeness check

`tests/test_openbao_approle_lifecycle_registry.py` applies a two-part rule.

1. Every file whose text mentions a SecretID TTL, in any spelling or case, or
   an `auth/approle/role/` path must be the registry, a file that the
   registry names as a create site, or an entry in the exclusion list in the
   test, which gives a reason for each entry. The file list comes from
   `git ls-files` (tracked files plus untracked files that git does not
   ignore) with no directory pruning. Files that git ignores are out of scope,
   because CI cannot see them. A new file that mentions either fails until
   someone registers it or excludes it with a reason, and a stale exclusion
   entry fails.
2. Inside a registered or excluded file, the number of such mentions is pinned
   in the test. A new declaration in an allowed file changes the count and
   fails with a message to register the declaration and update the pin.

The create-site checks stay: each `bao write auth/approle/role/<name>` command
must set a registry TTL, a TTL key must not appear outside the registry, and
the number of create sites must stay at three. That check knows one spelling of
a create command. The two-part rule covers the others.
