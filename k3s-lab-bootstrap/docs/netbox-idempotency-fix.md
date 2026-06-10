# NetBox Idempotency Fix — Completed

**Status**: ✅ FIXED (first-order) with recovery caveat discovered on rerun  
**Commits**: b039171, 16d2e29, 6d27cd9, 107a8af, c212c8e  
**Related Documents**: [netbox-deployment-notes.md](netbox-deployment-notes.md), [netbox-token-journey.md](netbox-token-journey.md)

## Problem Statement

NetBox bootstrap was failing on reruns with:
```
FATAL:  password authentication failed for user "netbox"
```

This occurred because:
1. The persistent PostgreSQL PVC retained a password from the initial run
2. Subsequent playbook runs created new Kubernetes secrets with different passwords
3. The NetBox role lacked validation that OpenBao persistence actually succeeded
4. When OpenBao writes failed silently, the next run would generate yet another password
5. Password mismatch → PostgreSQL auth failure → continuous pod restart loop

## Root Cause

**The NetBox role read from and wrote to OpenBao correctly, but didn't verify that writes succeeded.**

Flow of the bug:
- First run: OpenBao empty → generate password → write to OpenBao + Helm → PostgreSQL gets password
- Second run: OpenBao write command fails silently → role still uses the (now stale) password → Helm gets wrong password → PostgreSQL auth fails

The role had no way to detect that the OpenBao write failed and would proceed as if persistence succeeded.

## Solution

Enhanced `roles/stack/operator/netbox/tasks/main.yml` with a three-layer validation approach:

### 1. Persistence Verification (lines 256-273)
After writing secrets to OpenBao, immediately read them back using `bao kv get`:
```yaml
- name: Read back NetBox password from OpenBao
  ansible.builtin.shell:
    cmd: >-
      ... bao kv get -format=json {{ netbox_runtime_secret_path | quote }} | jq -r ".data.data.db_password"
```

### 2. Assertion (lines 275-285)
Assert that the read-back password matches what was written:
```yaml
- name: Assert NetBox DB password persisted correctly
  ansible.builtin.assert:
    that:
      - _netbox_openbao_verify.stdout | trim == netbox_db_password_effective
    fail_msg: |
      NetBox DB password failed to persist to OpenBao.
      Expected: {{ netbox_db_password_effective }}
      Got: {{ _netbox_openbao_verify.stdout | trim }}
      Check OpenBao policies and mount permissions.
```

### 3. Conditional Execution
The verification block only runs when secrets were actually written (wrapped in `when: _netbox_openbao_write_result is changed`), avoiding undefined variable errors on reruns.

### 4. Improved Logging
Added debug messages that show:
- OpenBao read result status
- Whether output was received
- Which keys were found in the secret

## Testing

### Bootstrap Test #1 (Clean Slate)
```bash
cd <repos>/dmf-env
bin/run-playbook.sh ../dmf-infra/k3s-lab-bootstrap/playbooks/610-netbox.yml
```

**Result**: ✅ Success
- NetBox web pod: 1/1 Running, 0 restarts
- PostgreSQL pod: 1/1 Running
- Valkey pod: 1/1 Running
- Passwords generated and persisted to OpenBao
- Database auth successful (no password errors)

### Idempotency Test #2 (Rerun)
```bash
bin/run-playbook.sh ../dmf-infra/k3s-lab-bootstrap/playbooks/610-netbox.yml
```

**Result**: ✅ Success - Idempotent
- All password generation tasks skipped (values already in OpenBao)
- OpenBao write skipped (values hadn't changed)
- Verification skipped (no write needed)
- NetBox web pod remained running (no unnecessary rollout)
- All 35 tasks completed without errors
- 4 tasks showed expected changes (template values, ingress routes)

### Lifecycle Provision Test
Full lifecycle-provision.yml with NetBox reached playbook 698 without password-related failures (stopped at unrelated DMF Console token issue).

## Commits

| Commit | Change | Purpose |
|--------|--------|---------|
| b039171 | Added persistence validation | Verify OpenBao writes actually succeeded |
| 16d2e29 | Fixed debug task syntax | Remove invalid module parameter |
| 6d27cd9 | Fixed assertion condition | Prevent undefined variable errors |

## What Changed in the Role

**Before**: 
- Generate password if missing → Write to OpenBao → Hope it worked

**After**:
- Generate password if missing → Write to OpenBao → **Verify write succeeded** → Assert consistency → Proceed

**Now known limitation**:
- If the OpenBao path already exists but is incomplete, the role fails instead of self-healing.
- That is the right behavior for safety, but it means the current plan needs an explicit recovery path, not just a rerun assumption.

## Operational Impact

### For Operators
- ✅ NetBox bootstrap is now idempotent and stable
- ✅ Rerunning the playbook won't create password mismatches
- ✅ If OpenBao persistence fails, the playbook fails loudly with a clear error message
- ✅ Password is now durable across cluster teardowns/recreations (stored in OpenBao)

### Reopened Finding During Lifecycle Rerun
The later lifecycle rerun exposed a separate failure mode that this first-pass fix did not cover:

- `secret/apps/netbox/runtime` exists in OpenBao
- `db_password` inside that document is empty
- the NetBox role now aborts on that condition at rerun time

That means the runtime secret can exist in a **partial** state, which is different from:

1. no secret path yet
2. complete secret path with all required values

The correct operational interpretation is now:

- **absent path**: bootstrap may initialize the full NetBox runtime secret
- **complete path**: reruns should read and reuse it
- **partial path**: treat as corruption / recovery condition and fail loudly

This is the missing piece for true idempotency. The role cannot safely invent a new PostgreSQL password once a PV already exists, because that would drift from the persisted database state.

### For Future Work
- The environment wrapper (`bin/export-openbao-vars.sh`) could be extended to read NetBox secrets from in-cluster OpenBao instead of generating them on every run (enhancement, not required for idempotency)
- Consider adding a "rotate NetBox credentials" playbook for operational workflows
- Add a recovery playbook or explicit `resync` flag that can rebuild the NetBox runtime secret only when the namespace/PVC state has been intentionally reset
- Add a guard that distinguishes "path missing" from "path present but incomplete" before Helm is rendered

## Files Modified

- `k3s-lab-bootstrap/roles/stack/operator/netbox/tasks/main.yml` — Added 3-layer validation (persistence check, assertion, debug logging)

No changes to:
- NetBox values template
- NetBox defaults
- Helm chart
- Environment wrapper (enhancement only)

## Verification Checklist for Future Maintainers

If you need to reverify this fix:

- [ ] Run `610-netbox.yml` once on a fresh cluster
  - [ ] NetBox web pod reaches 1/1 Running
  - [ ] No password auth errors in logs
  - [ ] Passwords appear in OpenBao under `secret/apps/netbox/runtime`

- [ ] Run `610-netbox.yml` a second time
  - [ ] Password generation tasks all skip
  - [ ] NetBox persist task skips (no value change)
  - [ ] Verification block skips (no write occurred)
  - [ ] All tasks complete without errors
  - [ ] No unnecessary pod rollouts

- [ ] Check logs for the new debug message
  - [ ] "NetBox OpenBao read: rc=0, has_output=True" appears in task output
  - [ ] Indicates secrets were successfully read from OpenBao

- [ ] Verify recovery handling on rerun
  - [ ] If `secret/apps/netbox/runtime` exists but `db_password` is empty, the role fails before Helm applies
  - [ ] The failure message points to recovery, not silent regeneration
  - [ ] A clean reset or explicit repair path is used before rerunning lifecycle

## Related History

**Previous failure signature** (now fixed):
```
FATAL:  password authentication failed for user "netbox"
netbox pod: 22 restarts (CrashLoopBackOff)
netbox-worker pod: 33 restarts (CrashLoopBackOff)
```

**Action taken** (2026-05-03):
- Deleted stale netbox namespace and PVCs
- Implemented persistence validation in the role
- Tested clean bootstrap and idempotency
- All checks passed ✅

**Additional finding** (later rerun):
- The OpenBao runtime secret path can exist with a missing `db_password`
- That state must be treated as partial/corrupt, not as a normal rerun case
- The NetBox DB password remains an OpenBao-owned durable secret, but it is only safe to reuse if the document is complete

## See Also

- [netbox-deployment-notes.md](netbox-deployment-notes.md) — Known issues and deployment history
- [netbox-token-journey.md](netbox-token-journey.md) — How NetBox admin tokens are created and stored
- [integration-sot.md](integration-sot.md) — How NetBox fits in the broader automation architecture
