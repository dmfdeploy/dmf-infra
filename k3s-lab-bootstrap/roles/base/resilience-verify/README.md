# resilience-verify

End-to-end smoke test for Phase 3 (vertical-resilience). Confirms that
credentials, endpoint reachability, SSE, and Object Lock policy are all
working — without waiting 48h for the natural-schedule soak.

## Two-track design

Audit credentials flow via Phase 1's host-cron path (export-vars from
the bootstrap bundle), NOT ESO. There is no in-cluster `s3-creds-audit`
Secret to consume. Two tracks accommodate this asymmetry:

| Track | Buckets | Where it runs | Why |
|---|---|---|---|
| **A — in-cluster** | `openbao_snapshots`, `app_backups` | One-shot K8s Job per bucket, mounts the ESO-materialized Secret | Mirrors how the real backup CronJobs reach S3 |
| **B — host-side** | `audit` | `delegate_to: localhost`, uses Phase 1 `audit_log_*` inventory vars | Mirrors how the real audit-log-archival host cron reaches S3 |

Document the split here so future reviewers don't read it as a bug.

## What the round-trip asserts

For each bucket (both tracks):

1. **PUT** a small probe object with `--sse AES256` (B2 SSE-B2 default).
2. **HEAD** the object, assert `ServerSideEncryption` header is present.
   Catches silent SSE drift before it makes a real backup unreadable
   under a future hardening posture.
3. **GET** the object, diff against the source.
4. **DELETE**.

Track B (audit only) additionally:

5. **PUT with Object Lock COMPLIANCE** retention 1 minute.
6. Verify HEAD shows `ObjectLockMode: COMPLIANCE`.
7. **Attempt DELETE** — must be refused.
8. Wait the retention period, then clean up.

This proves the actually-load-bearing compliance posture, not just
credential validity.

## --soak-prewarm

Default `false`. Opt in to fire all four backup CronJobs as standalone
Jobs:

```bash
bin/run-playbook.sh <env> path/to/190-resilience-verify.yml \
  -e resilience_verify_soak_prewarm=true
```

This proves payload paths (real snapshot + dump + mirror) in ~30 min
instead of waiting 48h for natural schedules.

**Caveat (recorded 2026-05-12)**: `kubectl create job --from=cronjob/<name>`
spawns a standalone Job that is **not** a child of the CronJob, so
`concurrencyPolicy: Forbid` does NOT prevent collision with a
naturally-scheduled run. For daily/weekly schedules the collision risk
is vanishingly small in practice. If any schedule bumps to hourly, add
a label-selector guard before prewarm (check for a real Job already
running for this CronJob name).

## Acceptance criteria

| Check | Pass condition |
|---|---|
| in-cluster `openbao_snapshots` | Job logs end with `PASS: synthetic round-trip OK; SSE=...` |
| in-cluster `app_backups` | Same |
| host-side `audit` round-trip | `PASS: audit-bucket round-trip + Object Lock test complete` |
| audit Object Lock | `PASS: Object Lock COMPLIANCE delete refused as expected` |
| (optional) --soak-prewarm | All 4+2 backup CronJobs fire a standalone Job; respective bucket gets a real object within the active deadline |

Failure of any of the above fails the playbook.
