# longhorn-recurring-jobs

Single Longhorn `RecurringJob` that backs up all Longhorn volumes daily
to the default BackupTarget (configured by `longhorn-backup-target`).

## Why a single RecurringJob

Longhorn 1.10.1 `RecurringJob` has **no `backupTargetName` field** —
it binds to the cluster's default BackupTarget only. Multi-target
binding is upstream issue #11421 (open as of 2026-05-12). For Tier A
this is fine: we have exactly one BackupTarget (app-backups bucket).

If a future phase adds a second BackupTarget (e.g., an off-site
secondary), revisit this — possibly by labelling volumes with
`recurringJobSelector` labels that route to different RecurringJobs.

## Group `default`

Every Longhorn volume is implicitly a member of the `default` group
unless explicitly tagged otherwise. So this single RecurringJob backs
up everything by default. Per-volume opt-out is possible via the
volume's `recurringJobSelector` field if needed later.

## Defaults

| Variable | Default |
|---|---|
| `longhorn_recurring_jobs_namespace` | `longhorn-system` |
| `longhorn_recurring_jobs_name` | `dmf-daily-backup` |
| `longhorn_recurring_jobs_cron` | `0 1 * * *` (daily 01:00 UTC) |
| `longhorn_recurring_jobs_task` | `backup` |
| `longhorn_recurring_jobs_groups` | `[default]` |
| `longhorn_recurring_jobs_retain` | `7` |
| `longhorn_recurring_jobs_concurrency` | `2` |
