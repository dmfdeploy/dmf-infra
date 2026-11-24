# zot-mirror

Weekly CronJob that mirrors the in-cluster Zot container registry's
manifests + blobs to the app-backups S3 bucket via the registry
HTTP API. The copy on B2 uses skopeo's `dir` layout, which is skopeo's own
format and not an OCI image layout. It keeps every platform of an image
index, with the index digest unchanged — restorable to a fresh Zot without
Longhorn.

## Direction

**Backup** — Zot HTTP API → S3 target. NOT hydration (upstream → Zot).

## Why HTTP-API, not PVC mount

Zot stores its blob store on a Longhorn ReadWriteOnce PVC
(`data-zot-0`, from the StatefulSet's `volumeClaimTemplate` named
`data`). RWO single-attach means a backup CronJob cannot mount the same
PVC while the Zot StatefulSet is using it, even read-only. The
registry HTTP API is the source of truth anyway. The job reads the
repository list from `/v2/_catalog` itself, because `skopeo sync` copies one
repository (or image) per run and does not read the catalog.

## Pipeline

1. **Login**: read the `zot-svc` service-account password from mounted ESO
   Secret `zot-mirror-creds` (materialized from OpenBao at
   `secret/apps/zot/service` via the existing `openbao` ClusterSecretStore).
   Per ADR-0033 this routine backup authenticates as the scoped `zot-svc`
   account (read,create,update — no delete), never break-glass `admin`.
   Write a docker-format `auth.json` to scratch with base64 of
   `zot-svc:<password>`, and a `curl` config file with the same login, both
   mode 0600 — never echoed to stdout and never on a command line.
2. **List repositories**: request `/v2/_catalog?n=<zot_mirror_catalog_page_size>`
   once (default 10000), then check the answer strictly. Any of these stops
   the job before anything is synced or uploaded, so a partial or misread list
   never becomes a backup:
   - a status other than 200;
   - any `Link` header, in any letter case. Zot sends one when the catalog is
     longer than the page size. The error message says so and names
     `zot_mirror_catalog_page_size`; raise it above the number of
     repositories;
   - a body that is not exactly `{"repositories":[` names `]}` once whitespace
     is removed (repository names can't contain whitespace), where each name
     is a quoted string without quotes, commas or backslashes;
   - an empty list;
   - a name outside the repository-name grammar of the OCI Distribution
     Specification ("Pulling manifests"): lowercase letters and digits in path
     components, separated by `.`, `_`, `__` or hyphens, joined by `/`.
     `foo__bar` and `foo--bar` pass; `Foo`, `-foo`, `foo/` and `..` don't.
3. **skopeo sync**: for each repository, run
   `skopeo sync --all --preserve-digests --scoped --src docker --dest dir`
   into `/scratch/dir-mirror/`. Every tag of the repository lands in
   `/scratch/dir-mirror/<registry>/<repo>:<tag>/` in skopeo's `dir` layout.
   `--scoped` keeps the full repository path, so same-named repositories
   such as `dmf/charts/nmos-cpp` and `dmf/nmos-cpp-node` can't collide.
   For an image index, `--all` copies the index and every platform manifest
   it lists (without it, skopeo copies only the platform that matches the
   backup container). `--preserve-digests` keeps each digest as it is in Zot
   and fails the copy if it can't. Both flags need skopeo 1.6.0 or later.
   Each index folder holds `manifest.json` (the index), one
   `<digest>.manifest.json` per platform, and the blobs.
   `skopeo sync` has no OCI layout destination (`--dest` takes `docker` or
   `dir`), so `dir` is the layout that stores a multi-image copy. One run
   per repository is simpler than the `--src yaml` source, which needs a
   generated YAML file that lists every repository and sets TLS options.
4. **aws s3 sync**: upload the `dir` mirror to
   `s3://dmf-app-backups-<env>/zot/<timestamp>/` with SSE AES256
   (B2's native SSE-B2). Credentials read from mounted Frozen Contract
   Secret `s3-creds-app-backups`; passed as per-command env, not argv.
5. **Cleanup**: scratch is `emptyDir` (ephemeral); job exit clears it.
   The `curl` config file is removed after the catalog listing and
   `auth.json` before the upload step.

## Scratch size

The `dir` layout stores each tag's blobs in its own folder, with no
deduplication across tags, and `--all` adds every platform of an image
index. The scratch volume (`zot_mirror_scratch_size`) must hold the sum of
the full image sizes of all tags. That sum can be larger than the registry's
own storage, because Zot shares blobs between tags and this copy does not.
Raise the value when the sum for an environment approaches it.

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

Timestamp-prefixed at the top level:
`zot/<YYYY-MM-DDTHH-MM-SSZ>/<registry>/<repo>:<tag>/...`, where `<registry>` is
the source registry's host and port (for example
`zot.zot.svc.cluster.local:5000`).
Each mirror run creates a new snapshot tree. Never overwrites a fixed key.

## Recovery

To restore Zot from B2 — **must go via the registry HTTP API**, not by
dropping the mirror's contents into Zot's local filesystem layout (those are
different on-disk formats; bypassing the API will corrupt the registry):

```bash
# 1. Pick a snapshot timestamp from B2
aws s3 ls s3://dmf-app-backups-<env>/zot/ --endpoint-url <b2-endpoint>

# 2. Make sure a running Zot is available to push into. This recipe
#    restores into the existing Zot service (`zot.zot.svc.cluster.local:5000`).
#    Zot replaces existing tags on push.

# 3. Download the snapshot, then push it into Zot's HTTP API. The skopeo
#    image has no AWS CLI, so the pod downloads in an init container that
#    uses the same AWS CLI image as the backup job, writes to a shared
#    volume, and then skopeo restores from that volume. Both credentials
#    are mounted from files, as in the backup job: `s3-creds-app-backups`
#    (S3) and `zot-mirror-creds` (the `zot-svc` password, which has create
#    and update, enough to push the restored manifests). For a full
#    break-glass restore you can use the `admin` account instead
#    (`secret/apps/zot/admin`): change the user name and mount a Secret
#    that holds its password.
#    Replace <snapshot-timestamp> with the value from step 1. If you
#    changed the image defaults, use the same images as the backup job
#    (`zot_mirror_awscli_image` and `zot_mirror_skopeo_image`).
#    The `mirror` volume must hold the whole snapshot. To cap it, add
#    `sizeLimit` with the value of `zot_mirror_scratch_size`.
kubectl -n zot apply -f - <<'EOF'
apiVersion: v1
kind: Pod
metadata:
  name: zot-restore
spec:
  restartPolicy: Never
  volumes:
    - name: s3-creds
      secret:
        secretName: s3-creds-app-backups
    - name: pull-creds
      secret:
        secretName: zot-mirror-creds
    - name: mirror
      emptyDir: {}
  initContainers:
    - name: download
      image: amazon/aws-cli:latest
      command: ["/bin/sh", "-c"]
      args:
        - |
          set -eu
          S3=/etc/s3-creds
          AWS_ACCESS_KEY_ID="$(cat $S3/access_key_id)" \
          AWS_SECRET_ACCESS_KEY="$(cat $S3/secret_access_key)" \
          AWS_DEFAULT_REGION="$(cat $S3/region)" \
            aws s3 sync "s3://$(cat $S3/bucket)/zot/<snapshot-timestamp>/" /mirror/ \
              --endpoint-url "$(cat $S3/endpoint)"
      volumeMounts:
        - { name: s3-creds, mountPath: /etc/s3-creds, readOnly: true }
        - { name: mirror, mountPath: /mirror }
  containers:
    - name: restore
      image: quay.io/skopeo/stable:latest
      command: ["/bin/bash", "-c"]
      args:
        - |
          set -eu
          # SRC_REG is the registry host the backup job read from. It names the
          # folder inside the snapshot. DEST_REG is the registry to push to.
          SRC_REG=zot.zot.svc.cluster.local:5000
          DEST_REG=zot.zot.svc.cluster.local:5000
          AUTH="$(printf 'zot-svc:%s' "$(cat /etc/pull-creds/password)" | base64 -w0)"
          umask 077
          printf '{"auths":{"%s":{"auth":"%s"}}}' "$DEST_REG" "$AUTH" > /tmp/auth.json
          # --scoped keeps the full repository path (dmf/charts/nmos-cpp, not
          # nmos-cpp). Without it, skopeo uses only the last path segment, and
          # same-named repositories overwrite each other.
          skopeo sync --scoped --all --preserve-digests --src dir --dest docker \
            --authfile /tmp/auth.json --dest-tls-verify=false \
            /mirror/$SRC_REG $DEST_REG
      volumeMounts:
        - { name: pull-creds, mountPath: /etc/pull-creds, readOnly: true }
        - { name: mirror, mountPath: /mirror, readOnly: true }
EOF
# Wait up to an hour for the pod to finish. With `restartPolicy: Never`, the
# phase becomes Succeeded only when the download and the restore both exit 0,
# and Failed when either one fails.
for _ in $(seq 360); do
  phase="$(kubectl -n zot get pod zot-restore -o jsonpath='{.status.phase}')"
  case "$phase" in Succeeded|Failed) break ;; esac
  sleep 10
done
kubectl -n zot logs zot-restore -c download || true
kubectl -n zot logs zot-restore -c restore || true
# Delete the pod only after a successful restore. On any other result, read
# the logs above first: they are lost when the pod is deleted.
if [ "$phase" = Succeeded ]; then
  kubectl -n zot delete pod zot-restore
else
  echo "Restore did not succeed (phase: $phase). Read the logs, then run: kubectl -n zot delete pod zot-restore"
fi

# 4. Verify the catalog
# curl prompts for the zot-svc password, so it stays off the command line.
# If the response carries a Link header, request the next page too.
curl -i --user zot-svc 'https://zot.<base-domain>/v2/_catalog?n=1000'
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
| `zot_mirror_catalog_page_size` | `10000` (catalog requested in one page) |
| `zot_mirror_pull_creds_secret` | `zot-mirror-creds` (ESO target) |
| `zot_mirror_pull_creds_openbao_path` | `secret/apps/zot/service` |
| `zot_mirror_pull_creds_username` | `zot-svc` |
| `zot_mirror_eso_cluster_secret_store` | `openbao` |
| `zot_mirror_s3_creds_secret` | `s3-creds-app-backups` |
| `zot_mirror_skopeo_image` | `quay.io/skopeo/stable:latest` |
| `zot_mirror_awscli_image` | `amazon/aws-cli:latest` |
| `zot_mirror_scratch_size` | `25Gi` |
| `zot_mirror_deadline_seconds` | `10800` (3h) |
