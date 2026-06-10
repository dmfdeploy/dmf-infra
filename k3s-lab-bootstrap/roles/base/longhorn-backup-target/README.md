# longhorn-backup-target

Configures Longhorn's `BackupTarget` to point at the app-backups B2 bucket.

## Why this role exists separately from `object-storage-credentials`

Longhorn 1.10.1's `BackupTarget` CR requires the credential Secret to
carry **UPPERCASE keys** (`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_ENDPOINTS`, optionally `VIRTUAL_HOSTED_STYLE`). The Frozen Secret
Contract (Phase 3) uses **lowercase keys** to match the OpenBao stored
values directly. Rather than special-casing one namespace in
`object-storage-credentials` (which would couple a tier-A consumer's
quirks into a shared role), we keep the contract clean and let this
role create its own `ExternalSecret` with a per-field re-keying
`template:` block.

Same OpenBao path, different on-Secret projection.

## Flow

1. **ExternalSecret** in `longhorn-system` pulls all five Frozen Contract
   values from `secret/platform/object-storage/app_backups` via the
   `openbao-app-backups` ClusterSecretStore (created by
   `object-storage-credentials`). The `template:` block re-keys:
     - `access_key_id`        → `AWS_ACCESS_KEY_ID`
     - `secret_access_key`    → `AWS_SECRET_ACCESS_KEY`
     - `endpoint`             → `AWS_ENDPOINTS`
     - (static)               → `VIRTUAL_HOSTED_STYLE=false`
2. **BackupTarget CR** named `default` in `longhorn-system`. The URL is
   built as `s3://<bucket>@<region>/` (Longhorn-specific syntax — the
   `@<region>` is required even though credentials carry `AWS_ENDPOINTS`).
3. Longhorn re-reads the Secret every `pollInterval` (default 5m), so
   ESO rotations land without restart.

## Dependency

This role MUST run after `object-storage-credentials` — the
`openbao-app-backups` ClusterSecretStore must exist before this role's
ExternalSecret can resolve.

## Defaults

| Variable | Default |
|---|---|
| `longhorn_backup_target_namespace` | `longhorn-system` |
| `longhorn_backup_target_name` | `default` |
| `longhorn_backup_target_openbao_path` | `secret/platform/object-storage/app_backups` |
| `longhorn_backup_target_eso_cluster_secret_store` | `openbao-app-backups` |
| `longhorn_backup_target_secret_name` | `longhorn-s3-creds` |
| `longhorn_backup_target_poll_interval` | `5m0s` |
| `longhorn_backup_target_url_region` | `us-west-001` |
