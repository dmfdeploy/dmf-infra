# AWX autoscale helper

The helper changes AWX availability only. AWX remains responsible for job
semantics, launch, and cancellation.

## Authenticated endpoints

Both POST endpoints require the mounted bearer token in the `Authorization`
header. The helper stores only a digest of that token as the caller principal.

`POST /ensure-awake` wakes AWX and waits for launch readiness. Its wake Lease
provides a startup and grace-period floor; that floor does not hold AWX awake
for a whole job.

`POST /job-hold` provides that bounded job-scoped hold. Send a JSON body with
an action, a caller-generated run id, and a per-renewal duration:

```json
{"action":"acquire","run_id":"rotation-2026-10-27T00","duration_seconds":600}
```

Use `renew` with the same run id and duration while the AWX job is active. Use
`release` with that run id in cleanup after every completion or failure path;
release does not need a duration. The default maximum duration is 900 seconds
per renewal and can be changed with
`awx_autoscale_job_hold_max_seconds`. Callers must renew before the current
Lease expires. If the caller crashes or stops renewing, the hold expires and
the reaper may sleep AWX after its normal idle grace period.

The `run_id` is a lock key, not an authentication credential. The Lease owner
digest includes both the bearer principal and run id. The deployment currently
uses one bearer credential, so every holder of that credential is the same
authenticated principal. A caller cannot renew or release a hold owned by a
different principal or run id. A conflicting acquire/renew means another
owner has the Lease. `/ensure-awake` waits for readiness without changing the
wake floor while a job hold is active; during the short reaper claim it returns
a retryable busy result.

The job-hold Lease and the reaper's short sleep claim use Kubernetes
resource-version compare-and-swap. This serializes hold acquisition against
the final sleep decision across helper replicas, including rollout overlap.
The reaper claim is at least 200 seconds. After claim acquisition, the
worst-case five-round wake-floor CAS takes 100 seconds; the final floor read,
sleep CR and Deployment patches, and sleep annotation write take at most 60
more seconds at their 10-second request timeouts. The remaining time also
covers the final 10-second claim-acquire request, leaving at least 90 seconds
of margin. The claim expires after a helper crash. Neither the wake endpoint
nor the reaper launches or cancels an AWX job.

## Network access

The current NetworkPolicy allows ingress from the CMS namespace and its
`app.kubernetes.io/name: dmf-cms` pod selector on port 8080. A rotation trigger
must run in a protected runtime namespace with its own dedicated identity and
pod selector. Add a narrowly scoped port-8080 allow rule for that selector
when the trigger is placed. Do not grant access by labeling the trigger as
`dmf-cms`.
