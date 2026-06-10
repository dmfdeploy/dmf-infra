# object-storage-credentials

Provisions two OpenBao AppRoles, two ClusterSecretStores, and
ExternalSecrets in consumer namespaces for the `openbao_snapshots` and
`app_backups` object-storage buckets.

## What this role does

For each logical bucket (`openbao_snapshots`, `app_backups`):

1. **OpenBao policy** — `object-storage-<logical-dashed>-reader` grants
   `read` on the exact seeded path
   `secret/platform/object-storage/<logical>`, plus `read` + `list` on
   `secret/platform/object-storage/<logical>/*` for future nested keys.
   No write, no delete, no access to other KV paths. Applied via
   `policy-reconciler` session (separation of duties, ADR-0021).

2. **AppRole** (idempotent) — creates the role if it doesn't exist,
   generates a secret-id **exactly once**. On subsequent runs the
   existing AppRole is detected via `bao read role-id` and both role
   creation and secret-id generation are skipped. This deviates from
   the existing `stack/operator/openbao` role pattern which uses
   `-force` on every reconcile, rotating the secret-id each time.

   AppRole creation and secret-id generation use the `approle-reconciler`
   session (ADR-0021), not `ops-admin`.

3. **Secret-id storage** — the generated secret-id is stored in OpenBao
   at `secret/platform/eso-bindings/object-storage-<logical-dashed>` for
   operator reference and break-glass recovery. The `bao kv put` call
   reads from a short-lived pod-local temp file populated from stdin,
   keeping the secret-id off the command line (ADR-0007).

4. **ESO auth Secret** — a K8s Secret in the `external-secrets`
   namespace containing `id` (secret-id) and `roleId`, consumed by
   the ClusterSecretStore.

5. **Managed consumer namespaces** — creates only namespaces owned by
   the resilience stack (`openbao-system` by default). App namespaces
   must already exist from their app roles.

6. **ClusterSecretStore** — `openbao-<logical-dashed>` in the
   Vault-provider/AppRole shape, mirroring the existing
   `external-secrets` role's ClusterSecretStore.

7. **ExternalSecrets** — one per consumer namespace, materializing a
   K8s Secret `s3-creds-<logical-dashed>` with five keys:
   `bucket`, `endpoint`, `region`, `access_key_id`, `secret_access_key`.

## Naming convention

Data paths remain **underscored** to match seed-bao state:
`secret/platform/object-storage/openbao_snapshots`.

AppRole names, policy names, ESO binding names, K8s Secret names, and
ClusterSecretStore names are **dashed** for K8s compatibility:
`object-storage-openbao-snapshots`, `openbao-openbao-snapshots`.

## Token routing (ADR-0021)

| Step | Operation | Session |
|---|---|---|
| 3 | Write reader policies | `policy-reconciler` |
| 4-9 | AppRole + ESO binding reads/writes | `approle-reconciler` |
| 11-14 | K8s Secret / ClusterSecretStore / ExternalSecret | Kubernetes API (no OpenBao token) |

## Frozen Secret Contract

| Secret name | Namespaces | Keys |
|---|---|---|
| `s3-creds-openbao-snapshots` | `openbao-system` | `bucket`, `endpoint`, `region`, `access_key_id`, `secret_access_key` |
| `s3-creds-app-backups` | `awx`, `netbox`, `authentik`, `forgejo`, `zot` | `bucket`, `endpoint`, `region`, `access_key_id`, `secret_access_key` |

**Audit is NOT handled by this role.** Phase 1's `audit-log-archival`
role consumes audit credentials via inventory vars (`bootstrap-secrets.sh
export-vars`), not ESO.

**`longhorn-system` is intentionally excluded** from `app_backups`
consumer namespaces. Longhorn 1.10.1's `BackupTarget` requires
UPPERCASE Secret keys (`AWS_ACCESS_KEY_ID`, etc.) which differ from
the Frozen Contract lowercase keys. The `longhorn-backup-target` role
(Claude's slice) creates its own ExternalSecret in `longhorn-system`
that re-keys lowercase → uppercase.

## Deviations from existing patterns

| Existing pattern | This role | Why |
|---|---|---|
| `bao write -force .../secret-id` on every reconcile | Existence check first; secret-id generated once | Prevents ExternalSecret reconcile churn + CronJob auth disruption |
| Secret-id lives only in Ansible facts | Also stored at `secret/platform/eso-bindings/<role>` | Break-glass / operator reference |
| Single `operator` session for all OpenBao ops | Three sessions: `policy-reconciler`, `approle-reconciler`, K8s API | ADR-0021 separation of duties |

## Required variables

| Variable | Default | Description |
|---|---|---|
| `object_storage_logicals` | `["openbao_snapshots", "app_backups"]` | Logical bucket identifiers |
| `object_storage_consumer_namespaces` | (see defaults) | Per-logical namespace list |
| `object_storage_managed_consumer_namespaces` | `["openbao-system"]` | Consumer namespaces this role may create |
| `object_storage_eso_openbao_url` | `https://openbao.openbao.svc.cluster.local:8200` | OpenBao server URL |
| `openbao_namespace` | `openbao` | OpenBao K8s namespace |
| `openbao_pod` | `openbao-0` | OpenBao pod name |
| `openbao_tls_secret_name` | `openbao-tls` | TLS secret name for CA bundle |

## Playbook

`playbooks/vertical-resilience/100-object-storage-credentials.yml`

Must run after `vertical-security/100-openbao.yml` (OpenBao must be
initialized and unsealed, and the `approle-reconciler` identity must
exist in the break-glass JSON) and after Phase 2's `seed-bao` (the
`secret/platform/object-storage/*` paths must exist with credentials).
