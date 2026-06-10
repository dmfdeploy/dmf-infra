# openbao-raft-snapshot

Daily CronJob that snapshots the OpenBao Raft log and uploads the result
to the `s3://dmf-openbao-snapshots-<env>/<cluster>/<timestamp>.snap` path.

## Authentication

**Kubernetes auth method** — NOT AppRole.

This is a deliberate divergence from the rest of Phase 3 (the
`object-storage-credentials` role uses AppRoles for ESO; backup CronJobs
that read S3 credentials use ESO-materialized Secrets via AppRole). The
snapshot CronJob skips AppRole entirely because:

- The identity is naturally a pod's ServiceAccount, which the Kubernetes
  auth method already handles end-to-end.
- No secret-id to rotate, store, or materialize via ESO.
- Smaller blast radius: the SA token can only ever call the bound role,
  which can only ever call the snapshot endpoint.

The CronJob pod uses its ServiceAccount (`openbao-raft-snapshot` in
`openbao-system`) with a projected SA token at
`/var/run/secrets/kubernetes.io/serviceaccount/token`. The startup
script calls `bao login -method=kubernetes role=openbao-raft-snapshot
-token-only` which returns a short-lived (10m TTL) OpenBao token scoped
to the `openbao-raft-snapshot` policy, which grants **read-only** on
`sys/storage/raft/snapshot`.

Policy:
```hcl
path "sys/storage/raft/snapshot" {
  capabilities = ["read"]
}
```

Note: `read` only — `bao operator raft snapshot save` calls the read
endpoint. The `update`/`sudo` capabilities are for `/sys/storage/raft/snapshot-auto`
(auto-snapshot config), which is not used here. This is least-privilege.

## Credential handling

S3 credentials come from the ESO-materialized Secret
`s3-creds-openbao-snapshots` (Frozen Secret Contract, Phase 3). The
Secret is mounted read-only at `/etc/s3-creds/`. The snapshot script
reads individual keys via `cat /etc/s3-creds/access_key_id` — never
passed on argv, never in env until the immediate aws s3 cp invocation
(where they are process-local).

## Object keys

Timestamp-prefixed: `<cluster>/<YYYY-MM-DDTHH-MM-SSZ>.snap`. Never
overwrites a fixed key. B2 lifecycle rules (configured by
`b2-buckets.sh`) handle expiry at 90 days.

## Recovery

```bash
# List snapshots
aws s3 ls s3://dmf-openbao-snapshots-<env>/<cluster>/ --endpoint-url <endpoint>

# Restore a snapshot into a new OpenBao instance
bao operator raft snapshot restore /path/to/snapshot.snap
```

## Defaults

| Variable | Default |
|---|---|
| `openbao_raft_snapshot_schedule` | `30 2 * * *` (daily 02:30 UTC) |
| `openbao_raft_snapshot_namespace` | `openbao-system` |
| `openbao_raft_snapshot_secret_name` | `s3-creds-openbao-snapshots` |
| `openbao_raft_snapshot_retention_days` | 90 |
