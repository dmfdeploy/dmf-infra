# postgres-backups

Per-app PostgreSQL backup CronJobs. Each app (netbox, awx, authentik,
forgejo by default) gets a daily CronJob that runs `pg_dump` and uploads
the gzipped result to `s3://dmf-app-backups-<env>/pg/<app>/<timestamp>.sql.gz`.

## Supported source PostgreSQL versions

The CronJob image is `postgres:16`. `pg_dump 16` supports source servers
from PostgreSQL **9.2 through 16 inclusive**. It will **fail against
PostgreSQL 17+** (pg_dump is forward-compatible but not backward).

When bumping any app to PostgreSQL 17+, also bump
`postgres_backups_image` to a matching major version and verify pg_dump
output compatibility against any restore environment (downgrades break
silently).

Verify the source server version per app namespace:

```bash
kubectl -n <app> exec <postgres-pod> -- psql --version
```

## Credential handling

**DB credentials**: Each CronJob mounts the app's own runtime Secret
(e.g., `netbox-runtime`, `awx-postgres-configuration`) read-only at
`/etc/db-creds/`. The password is read via `cat` inside the script —
never passed on argv, never in env except for the immediate PGPASSWORD
assignment to pg_dump.

**S3 credentials**: From the ESO-materialized Secret `s3-creds-app-backups`
(Frozen Secret Contract, Phase 3). Mounted read-only at `/etc/s3-creds/`.
Read via `cat` — never in argv.

## Object keys

Timestamp-prefixed: `pg/<app>/<YYYY-MM-DDTHH-MM-SSZ>.sql.gz`. Never
overwrites a fixed key. B2 lifecycle rules handle expiry at 365 days.

## Recovery

```bash
# List backups for an app
aws s3 ls s3://dmf-app-backups-<env>/pg/<app>/ --endpoint-url <endpoint>

# Restore
aws s3 cp s3://dmf-app-backups-<env>/pg/<app>/<latest>.sql.gz - \
  --endpoint-url <endpoint> | gunzip | psql -h <db-host> -U <db-user> -d <db-name>
```

## Defaults

| Variable | Default |
|---|---|
| `postgres_backups_schedule` | `0 3 * * *` (daily 03:00 UTC) |
| `postgres_backups_apps` | `[netbox, awx, authentik, forgejo]` |
| `postgres_backups_secret_name` | `s3-creds-app-backups` |
| `postgres_backups_deadline_seconds` | 1800 |

## Per-app overrides

See `defaults/main.yml` — `postgres_backups_app_overrides` maps each
app to its namespace, DB host/port/user/name, and the Secret that holds
its password. Override per-env in group_vars if the pattern differs.
