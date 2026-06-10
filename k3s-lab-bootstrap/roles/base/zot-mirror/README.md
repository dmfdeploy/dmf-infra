# zot-mirror

Weekly CronJob that mirrors the in-cluster Zot container registry's
OCI manifests + blobs to the app-backups S3 bucket via the registry
HTTP API. The blob store is reconstructed in OCI directory layout
on B2 — restorable to a fresh Zot without Longhorn.

## Direction

**Backup** — Zot HTTP API → S3 target. NOT hydration (upstream → Zot).

## Why HTTP-API, not PVC mount

Zot stores its blob store on a Longhorn ReadWriteOnce PVC
(`data-zot-0`, from the StatefulSet's `volumeClaimTemplate` named
`data`). RWO single-attach means a backup CronJob cannot mount the same
PVC while the Zot StatefulSet is using it, even read-only. The
registry HTTP API is the source of truth anyway, and `skopeo sync`
iterates `/v2/_catalog` automatically.

## Pipeline

1. **Login**: read the `zot-svc` service-account password from mounted ESO
   Secret `zot-mirror-creds` (materialized from OpenBao at
   `secret/apps/zot/service` via the existing `openbao` ClusterSecretStore).
   Per ADR-0033 this routine backup authenticates as the scoped `zot-svc`
   account (read,create,update — no delete), never break-glass `admin`.
   Write a docker-format `auth.json` to scratch with base64 of
   `zot-svc:<password>` — never echoed to stdout.
2. **skopeo sync**: enumerate every repo:tag on the in-cluster Zot
   service, write to `/scratch/oci-mirror/` as OCI directory layout.
3. **aws s3 sync**: upload the OCI dir to
   `s3://dmf-app-backups-<env>/zot/<timestamp>/` with SSE AES256
   (B2's native SSE-B2). Credentials read from mounted Frozen Contract
   Secret `s3-creds-app-backups`; passed as per-command env, not argv.
4. **Cleanup**: scratch is `emptyDir` (ephemeral); job exit clears it.
   `auth.json` is `rm`'d before the upload step regardless.

## Container choice

Main container: `quay.io/skopeo/stable`. The `aws` CLI is copied out
of `amazon/aws-cli` via an initContainer into a shared `emptyDir`
mounted at `/shared-bin/`; main container's `PATH` includes it.
Single image build, no custom image to maintain.

## Important: NO `--remove` (or equivalent destructive flag)

Neither `skopeo sync` nor `aws s3 sync` is invoked with a flag that
would delete S3 objects when the source no longer has them. **Backup
follows prod into the grave** — if an image is deleted in Zot, the
copy in B2 stays there. B2 lifecycle rules (configured by
`b2-buckets.sh`) handle long-tail expiry at 365 days.

## Object keys

Timestamp-prefixed at the top level: `zot/<YYYY-MM-DDTHH-MM-SSZ>/<repo>:<tag>/...`.
Each mirror run creates a new snapshot tree. Never overwrites a fixed key.

## Recovery

To restore Zot from B2 — **must go via the registry HTTP API**, not by
dropping OCI dir contents into Zot's local filesystem layout (those are
different on-disk formats; bypassing the API will corrupt the registry):

```bash
# 1. Pick a snapshot timestamp from B2
aws s3 ls s3://dmf-app-backups-<env>/zot/ --endpoint-url <b2-endpoint>

# 2. Make sure a running Zot is available to push into. For an in-place
#    restore, the existing Zot StatefulSet works — Zot replaces existing
#    tags on push. For a side-by-side recovery, deploy a temporary Zot
#    with its own PVC.

# 3. Sync from B2 to a local OCI dir, then push into Zot's HTTP API:
kubectl -n zot run zot-restore --restart=Never --image=quay.io/skopeo/stable:latest \
  --overrides='{"spec":{"containers":[{"name":"r","image":"quay.io/skopeo/stable","command":["sleep","86400"]}]}}' \
  -- sleep 86400
kubectl -n zot exec -it zot-restore -- /bin/sh
# inside the pod:
mkdir /tmp/oci /tmp/auth
aws s3 sync s3://dmf-app-backups-<env>/zot/<snapshot-timestamp>/ /tmp/oci/ \
  --endpoint-url <b2-endpoint>
# auth.json with zot-svc creds (read from OpenBao at secret/apps/zot/service
# or the existing zot-mirror-creds Secret). zot-svc has create+update, enough
# to push the restored manifests. For a full break-glass restore the `admin`
# account (secret/apps/zot/admin) also works.
echo '{"auths":{"zot.zot.svc.cluster.local:5000":{"auth":"<base64(zot-svc:password)>"}}}' > /tmp/auth/config.json
skopeo sync --src dir --dst docker \
  --authfile /tmp/auth/config.json \
  --dest-tls-verify=false \
  /tmp/oci/ zot.zot.svc.cluster.local:5000

# 4. Verify the catalog
curl -u zot-svc:<password> https://zot.<base-domain>/v2/_catalog
```

Deleted images stay in B2 by design — see Phase 3 design notes in
the execution plan.

## Defaults

| Variable | Default |
|---|---|
| `zot_mirror_schedule` | `0 4 * * 0` (weekly Sunday 04:00 UTC) |
| `zot_mirror_namespace` | `zot` |
| `zot_mirror_source_registry` | `zot.zot.svc.cluster.local:5000` |
| `zot_mirror_source_tls_verify` | `false` (in-cluster service) |
| `zot_mirror_pull_creds_secret` | `zot-mirror-creds` (ESO target) |
| `zot_mirror_pull_creds_openbao_path` | `secret/apps/zot/service` |
| `zot_mirror_pull_creds_username` | `zot-svc` |
| `zot_mirror_eso_cluster_secret_store` | `openbao` |
| `zot_mirror_s3_creds_secret` | `s3-creds-app-backups` |
| `zot_mirror_skopeo_image` | `quay.io/skopeo/stable:latest` |
| `zot_mirror_awscli_image` | `amazon/aws-cli:latest` |
| `zot_mirror_scratch_size` | `25Gi` |
| `zot_mirror_deadline_seconds` | `10800` (3h) |
