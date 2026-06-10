# OpenBao / NetBox Bootstrap Reconciliation Prompt

**STATUS: ✅ RESOLVED (2026-05-03)**

This handoff prompt documents a previously-resolved issue. The NetBox idempotency
problem has been fixed. For historical context and testing methodology, see
[netbox-idempotency-fix.md](netbox-idempotency-fix.md).

---

## Historical Context (Previously Resolved)

The immediate goal was to make the NetBox bootstrap and its dependent policy
pipeline truly idempotent under lifecycle reruns.

The recurring failure mode is:

- `lifecycle-provision.yml` reaches the NetBox stage or the downstream AWX /
  DMF Console stage.
- NetBox rolls again or stays slow because a chart-rendered secret changes on
  every run.
- PostgreSQL then rejects the NetBox app with:
  `FATAL: password authentication failed for user "netbox"`
- Downstream consumers such as `691-netbox-sot.yml`, `693-awx-integration.yml`,
  `694-born-inventory.yml`, and `699-cms-smoke-test.yml` fail or stall because
  the NetBox / AWX / DMF token chain is not stable.

The desired end state is:

1. NetBox can be deleted and rebuilt cleanly.
2. A rerun of `lifecycle-provision.yml` does not invent new NetBox secrets.
3. NetBox, AWX, and NetBox inventory consumers all read from the same durable
   bootstrap source of truth.
4. OpenBao remains the security boundary and durable secret store.

## Read First

Before touching code, read these files:

- [k3s-lab-bootstrap/docs/netbox-deployment-notes.md](netbox-deployment-notes.md)
- [k3s-lab-bootstrap/docs/netbox-token-journey.md](netbox-token-journey.md)
- [k3s-lab-bootstrap/docs/integration-sot.md](integration-sot.md)
- [k3s-lab-bootstrap/playbooks/691-netbox-sot.yml](../playbooks/691-netbox-sot.yml)
- [k3s-lab-bootstrap/playbooks/693-awx-integration.yml](../playbooks/693-awx-integration.yml)
- [k3s-lab-bootstrap/playbooks/694-born-inventory.yml](../playbooks/694-born-inventory.yml)
- [k3s-lab-bootstrap/playbooks/697-cms-awx-token.yml](../playbooks/697-cms-awx-token.yml)
- [k3s-lab-bootstrap/playbooks/699-cms-smoke-test.yml](../playbooks/699-cms-smoke-test.yml)
- [k3s-lab-bootstrap/roles/stack/operator/netbox/tasks/main.yml](../roles/stack/operator/netbox/tasks/main.yml)
- [k3s-lab-bootstrap/roles/stack/operator/netbox/templates/values.yml.j2](../roles/stack/operator/netbox/templates/values.yml.j2)
- [k3s-lab-bootstrap/roles/stack/operator/netbox-sot/tasks/main.yml](../roles/stack/operator/netbox-sot/tasks/main.yml)
- [k3s-lab-bootstrap/roles/stack/operator/awx-integration/tasks/main.yml](../roles/stack/operator/awx-integration/tasks/main.yml)
- [k3s-lab-bootstrap/roles/common/dmf-born-inventory/tasks/main.yml](../roles/common/dmf-born-inventory/tasks/main.yml)

## Repo Model

This repo is environment-agnostic. Do not put real IPs, tokens, or operator
material into `dmf-infra`.

Environment-specific values live in the private repo:

- `<repos>/dmf-env`

That private repo contains:

- `inventories/hetzner-arm/group_vars/all/bootstrap.yml`
- `inventories/hetzner-arm/group_vars/all/openbao_secrets.yml`
- `inventories/hetzner-arm/group_vars/all/main.yml`
- `bin/run-playbook.sh`
- `bin/export-openbao-vars.sh`

## Cluster Access

Use the control node directly or SSH to the admin host.

Primary cluster access pattern:

```bash
ssh k3s-admin@<control-node-public-ip> 'sudo k3s kubectl get pods -A'
```

Useful variants:

```bash
ssh k3s-admin@<control-node-public-ip> 'sudo k3s kubectl -n netbox get pods -o wide'
ssh k3s-admin@<control-node-public-ip> 'sudo k3s kubectl -n netbox logs deploy/netbox --tail=200'
ssh k3s-admin@<control-node-public-ip> 'sudo k3s kubectl -n awx get secret awx-admin-password -o yaml'
```

Do not assume the local workstation has cluster access or a usable kubeconfig.
Use the SSH pattern above unless you have explicitly exported kubeconfig from
the control node.

## How To Run Playbooks

Always run through the environment wrapper in `dmf-env`.

Examples:

```bash
cd <repos>/dmf-env
bin/run-playbook.sh hetzner-arm ../dmf-infra/k3s-lab-bootstrap/playbooks/610-netbox.yml
bin/run-playbook.sh hetzner-arm ../dmf-infra/k3s-lab-bootstrap/playbooks/691-netbox-sot.yml
bin/run-playbook.sh hetzner-arm ../dmf-infra/k3s-lab-bootstrap/playbooks/693-awx-integration.yml
bin/run-playbook.sh hetzner-arm ../dmf-infra/k3s-lab-bootstrap/playbooks/694-born-inventory.yml
bin/run-playbook.sh hetzner-arm ../dmf-infra/k3s-lab-bootstrap/playbooks/697-cms-awx-token.yml
bin/run-playbook.sh hetzner-arm ../dmf-infra/k3s-lab-bootstrap/playbooks/699-cms-smoke-test.yml
```

The wrapper is responsible for exporting bootstrap/runtime vars from OpenBao
or the local bootstrap sources before Ansible starts.

## Security Model

OpenBao is the security boundary and the durable secret store.

The intended concept is:

- Operator break-glass material lives offline in the private operator store.
- OpenBao is initialized with Shamir quorum and a durable in-cluster runtime.
- Bootstrap playbooks use a narrow operator or AppRole path to seed secrets.
- Runtime consumers read from OpenBao, not from ad hoc local files.
- After bootstrap, secret consumption should flow from in-cluster OpenBao
  through the wrapper and playbooks.

Key security rules:

1. Do not store live secrets in `dmf-infra`.
2. Do not rely on a root token as a runtime dependency.
3. Preserve ultimate access through the offline break-glass material and
   Shamir quorum, not by baking privileged tokens into playbooks.
4. Treat the operator JSON / break-glass material as recovery-only material.
5. Keep the read paths narrow:
   - born-inventory should only read `secret/apps/netbox/runtime`
   - AWX integration should only read the NetBox and Forgejo runtime secrets it needs
   - NetBox bootstrap should own the NetBox runtime secret path

## OpenBao / NetBox Findings So Far

These are the important live findings that define the current problem:

### 1. NetBox is stateful

NetBox is not a stateless redeploy.

The PostgreSQL PVC survives namespace recreation unless you explicitly delete
the namespace and its storage. If the password stored in OpenBao / K8s no
longer matches the existing PostgreSQL data directory, NetBox fails with:

```text
FATAL: password authentication failed for user "netbox"
```

That failure means the DB password and the persisted database state drifted.

### 2. Helm-rendered random values were causing repeated rollouts

The NetBox chart was rendering random secret values on each run unless those
values were pinned by the wrapper:

- `netbox.secretKey`
- `netbox.superuser.apiToken`

When those values changed, the NetBox Deployment checksum changed and the pod
rolled again.

### 3. OpenBao runtime secret is the correct anchor point

The NetBox runtime secret path is:

```text
secret/apps/netbox/runtime
```

That secret is the canonical source for:

- `db_password`
- `valkey_password`
- `api_token_pepper`
- `secret_key`
- `superuser_api_token`

The NetBox role must read those values from OpenBao and only generate them
when missing.

### 4. NetBox consumers depend on the NetBox SoT token chain

The downstream playbooks depend on NetBox being healthy and on the NetBox SoT
token chain being consistent:

- `691-netbox-sot.yml` seeds NetBox admin and AWX inventory tokens
- `693-awx-integration.yml` grants AWX access to the NetBox inventory and job template roles
- `694-born-inventory.yml` registers the deployed cluster in NetBox
- `697-cms-awx-token.yml` wires the DMF Console token
- `699-cms-smoke-test.yml` verifies the DMF Console token path

If any of those steps are built on stale assumptions about the NetBox runtime
secret, the whole pipeline destabilizes.

## Current Workflow in dmf-env

The private env repo is the operational wrapper around this bootstrap.

Bootstrap values live here:

- `inventories/hetzner-arm/group_vars/all/bootstrap.yml`

OpenBao metadata lives here:

- `inventories/hetzner-arm/group_vars/all/openbao_secrets.yml`

Runtime / environment values live here:

- `inventories/hetzner-arm/group_vars/all/main.yml`

Important value categories:

- `vault_hcloud_token`
- `vault_cloudflare_dns_token`
- `vault_k3s_token`
- `vault_zot_admin_password`
- `vault_awx_admin_password`
- `vault_netbox_db_password`
- `vault_netbox_valkey_password`
- `vault_netbox_api_token_pepper`
- `vault_netbox_secret_key`
- `vault_netbox_superuser_api_token`

The wrapper flow is:

1. Read bootstrap metadata and operator inputs.
2. Export temp `vault_*` values to Ansible.
3. Run the selected bootstrap playbook through `bin/run-playbook.sh`.
4. After OpenBao is live, read runtime secrets back from in-cluster OpenBao.

The important distinction is:

- bootstrap values seed the first secure state
- OpenBao runtime values become the durable state

## Working Hypothesis

The NetBox deployment is not yet fully idempotent because one or more of the
following still occurs:

1. A Helm-rendered NetBox secret input changes between runs.
2. The wrapper uses a generated default instead of a durable OpenBao-backed value.
3. The chart re-renders a checksum because the secret material changes.
4. The DB password stored in OpenBao does not match the existing PostgreSQL
   volume after a rerun or reset.
5. A downstream consumer assumes a NetBox token exists before `691-netbox-sot.yml`
   has reseeded it.

The immediate visible symptom is a long NetBox startup wait followed by the DB
auth failure loop.

## Required Plan Structure

Work in three phases:

### Phase 1: Reasoning

Before changing code:

1. Confirm the live cluster state.
2. Identify which secret or config input changed.
3. Determine whether the problem is:
   - first-boot migration delay
   - a checksum-triggered rollout
   - a true DB password mismatch
   - a downstream consumer using a stale token path
4. Decide whether the fix belongs in:
   - NetBox role
   - NetBox SoT role
   - AWX integration role
   - born-inventory role
   - environment bootstrap values

Do not jump straight to editing if the live state has not been checked.

### Phase 2: Implementation

Likely files to adjust:

- `roles/stack/operator/netbox/defaults/main.yml`
- `roles/stack/operator/netbox/tasks/main.yml`
- `roles/stack/operator/netbox/templates/values.yml.j2`
- `roles/stack/operator/netbox-sot/tasks/main.yml`
- `roles/stack/operator/awx-integration/tasks/main.yml`
- `roles/common/dmf-born-inventory/tasks/main.yml`
- possibly `playbooks/697-cms-awx-token.yml`
- possibly `playbooks/699-cms-smoke-test.yml`

What the implementation should converge toward:

1. The NetBox role reads all durable secret inputs from `secret/apps/netbox/runtime`.
2. The role only generates missing values once.
3. The wrapper chart receives pinned values for every field that otherwise
   defaults to random generation.
4. Any verification step compares live service state against the durable secret
   source before proceeding.
5. Downstream playbooks treat missing secrets as a bootstrap sequencing issue,
   not as a reason to silently proceed with partial data.

### Phase 3: Verification

The minimum verification set should include:

1. `git diff --check`
2. `--syntax-check` through the environment wrapper
3. A clean bootstrap or clean rerun of:
   - `610-netbox.yml`
   - `691-netbox-sot.yml`
   - `693-awx-integration.yml`
   - `694-born-inventory.yml`
   - `697-cms-awx-token.yml`
   - `699-cms-smoke-test.yml`
4. A second rerun of the same lifecycle stage to confirm idempotency
5. Cluster logs showing no fresh DB-auth failure on the second run

## Known Live Failure Signatures

Be alert for these exact patterns:

- `password authentication failed for user "netbox"`
- `role_definitions/` returning 404 under an inventory-specific path
- AWX `/api/v2/me/` returning an envelope with `results` rather than a bare object
- role lookup assumptions like `read_inventory` when the live role is `Inventory Use`
- `NetBox admin token not found in OpenBao`
- `Born-inventory registration will be skipped`

These are usually sequencing or API-shape bugs, not random noise.

## Operator Rules

1. Keep unrelated local changes untouched.
2. Do not revert user-owned work.
3. Use `apply_patch` for edits.
4. Commit meaningful checkpoints as you go.
5. If you need to delete the NetBox namespace and PVCs for recovery, do it
   intentionally and state that the current lifecycle run must be resumed or
   rerun afterward.

## Suggested First Actions For The Next Agent

1. Read the files listed in the “Read First” section.
2. Confirm the current live cluster state.
3. Reproduce the NetBox idempotency failure on the cluster logs.
4. Map the failing secret path back to the NetBox role or wrapper values.
5. Draft a concrete fix plan before editing.

## Outcome We Want

After the fix, a fresh lifecycle rerun should behave like this:

- NetBox starts once for a real bootstrap, not on every rerun.
- The DB password remains stable unless a rotation playbook intentionally changes it.
- AWX and born-inventory can read NetBox-derived secrets only after `691-netbox-sot.yml`
  has seeded them.
- The DMF Console token and smoke test work against the real service account.
- OpenBao remains the durable source of truth for the bootstrap and runtime
  secrets that need to survive deletion/recreation of Kubernetes resources.

---

## Resolution Summary

This issue has been resolved. See [netbox-idempotency-fix.md](netbox-idempotency-fix.md) for:
- Complete technical documentation of the root cause
- Solution implementation details
- Testing results confirming idempotency
- Verification checklist for future maintainers

**Key changes**:
- Enhanced `roles/stack/operator/netbox/tasks/main.yml` with 3-layer OpenBao persistence validation
- Adds verification read after write, assertion that values match, conditional logging
- Tested successfully: clean bootstrap + rerun idempotency test

**Commits**: b039171, 16d2e29, 6d27cd9

Next agents working on the automation pipeline should focus on downstream integrations
(AWX, NetBox SoT tokens, DMF Console) rather than NetBox bootstrap stability.
