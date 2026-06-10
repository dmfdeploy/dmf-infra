# DMF Console Token Pipeline Handoff

## Purpose

This document is a handoff for a freshly cleared agent that needs to continue the
DMF Console token pipeline refactor work in `k3s-lab-bootstrap`.

The immediate goal is not to merge every token-related playbook into one file.
The goal is to make the token creation and delivery path consistent, secure, and
idempotent across the DMF Console wiring stages, while keeping the upstream
service bootstrap playbooks separate.

## Executive Summary

We have three different classes of token work in this repo:

1. **Issuer/bootstrap playbooks** that create or refresh service-side tokens and
   persist them to OpenBao:
   - `playbooks/691-netbox-sot.yml`
   - `playbooks/692-forgejo-bootstrap.yml`
   - `playbooks/693-awx-integration.yml`

2. **DMF Console wiring playbooks** that read durable tokens from OpenBao,
   validate or create the consumer-side API credentials, patch
   `dmf-cms-runtime`, and roll the console deployment:
   - `playbooks/696-cms-authentik-api.yml`
   - `playbooks/697-cms-awx-token.yml`
   - `playbooks/698-cms-netbox-forgejo-tokens.yml`

3. **Support playbooks / roles** that use OpenBao as a durable secret boundary
   or read tokens for inventory and bootstrap recovery:
   - `roles/common/app-admin-facts`
   - `roles/common/dmf-born-inventory`
   - `roles/stack/operator/netbox`
   - `roles/stack/operator/forgejo-bootstrap`
   - `roles/stack/operator/awx-integration`
   - `roles/stack/operator/authentik`

The safe consolidation path is:

- keep issuer/bootstrap playbooks separate
- extract shared OpenBao session plumbing
- normalize token validation and persistence patterns
- make the DMF Console wiring playbooks follow one consistent shape

The most important correctness issue already observed in this area is in
`698-cms-netbox-forgejo-tokens.yml`: the NetBox token test uses
`Authorization: "Token ..."` even though the repo docs for NetBox v4 tokens say
full tokens must be sent as `Authorization: Bearer <full-token>`.

## Security Model

OpenBao is the durable secret boundary and the source of truth after first
bootstrap.

That means:

- runtime secrets belong in OpenBao, not `/tmp`, logs, or shell history
- K8s Secrets are delivery/cache artifacts for consumers
- a rerun should read durable values from OpenBao whenever possible
- fallback paths are recovery paths, not the normal path
- do not add an initial/root token to environment values as the default fix

Relevant repo guidance:

- `k3s-lab-bootstrap/docs/openbao-policy-reconciliation-agent-prompt.md`
- `k3s-lab-bootstrap/docs/netbox-token-journey.md`
- `docs/SECURITY-REMEDIATION-GUIDE.md`
- `docs/security-compliance-framework-plan.md`

## Cluster Access

Use the environment wrapper from `dmf-env`.

```bash
cd ../dmf-env
bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/site.yml
```

Targeted reruns:

```bash
cd ../dmf-env
bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/playbooks/691-netbox-sot.yml
bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/playbooks/692-forgejo-bootstrap.yml
bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/playbooks/693-awx-integration.yml
bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/playbooks/696-cms-authentik-api.yml
bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/playbooks/697-cms-awx-token.yml
bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/playbooks/698-cms-netbox-forgejo-tokens.yml
```

Cluster inspection:

```bash
sudo k3s kubectl get pods -A
sudo k3s kubectl get svc -A
sudo k3s kubectl get pvc -A
sudo k3s kubectl logs -n <namespace> deploy/<deployment>
```

OpenBao inspection is always done through the in-cluster pod and the operator
credentials from the break-glass JSON.

## dmf-env Workflow and Bootstrap Values

Environment-specific values live in the private repo:

- `../dmf-env/inventories/<env-name>/hosts.ini`
- `../dmf-env/inventories/<env-name>/group_vars/all/main.yml`
- `../dmf-env/inventories/<env-name>/group_vars/all/openbao_secrets.yml`
- `../dmf-env/inventories/<env-name>/group_vars/all/vault.yml` if the
  environment still uses a vault fallback

The break-glass JSON used by the OpenBao client plumbing is typically:

```text
<secure-store>/openbao-breakglass/<env>/openbao-keys-automation.json
```

That file contains the operator userpass credentials used to log into the
OpenBao pod for bootstrap writes and recovery reads.

## Current Findings

### 1. NetBox token validation bug in 698

`698-cms-netbox-forgejo-tokens.yml` reads and writes the NetBox token from
`secret/apps/netbox/runtime` and then validates it with:

```yaml
Authorization: "Token {{ _cms_netbox_svc_token }}"
```

The repo docs for NetBox v4 tokens say the full token must be used with:

```yaml
Authorization: "Bearer <full-token>"
```

This is a correctness bug and should be treated as a first-order fix.

### 2. TLS verification is disabled in several token playbooks

Several playbooks use `validate_certs: false` for NetBox or Forgejo API calls.
That is acceptable only if the lab certificate setup is intentionally
self-signed and the playbook documents that decision clearly.

The security remediation docs treat this as a gap to either:

- replace with a CA bundle, or
- explicitly justify as a development-lab exception

### 3. Issuer playbooks should stay separate

Do not fold `691`, `692`, and `693` into one giant playbook.
They represent different issuers and different blast radii:

- NetBox service-token bootstrap
- Forgejo service-token bootstrap
- AWX integration bootstrap

Those are good independent stages.

### 4. The DMF Console wiring playbooks are the right consolidation seam

The DMF Console-facing token playbooks all repeat the same plumbing:

- load OpenBao break-glass credentials
- discover the OpenBao pod
- log in to OpenBao as `ops-admin`
- read a durable source token
- persist that token back to OpenBao
- patch `dmf-cms-runtime`
- roll the console deployment

This is the seam to normalize.

## Recommended Refactor Shape

### Phase 1: Extract shared OpenBao session plumbing

Create a small reusable helper role, for example:

- `roles/common/openbao-session`

Responsibilities:

- load the break-glass JSON from the operator host
- extract `ops_admin_username` and `ops_admin_password`
- discover the OpenBao pod by selector
- log into OpenBao with userpass
- export the client token into facts the caller can reuse

This role should not read or write app-specific secrets.
It should only standardize the OpenBao session setup.

### Phase 2: Refactor DMF Console token playbooks to use the shared helper

Update:

- `playbooks/696-cms-authentik-api.yml`
- `playbooks/697-cms-awx-token.yml`
- `playbooks/698-cms-netbox-forgejo-tokens.yml`

so they all use the same OpenBao session setup and the same recovery logic
shape.

The playbooks can still stay separate, but they should share the same bootstrap
plumbing and logging style.

### Phase 3: Standardize token validation and persistence

Use one policy for each integration:

- validate against the live service with the product-native auth scheme
- store the durable token in OpenBao
- patch the consumer Secret from OpenBao
- treat K8s Secret presence as delivery state, not the source of truth

For NetBox:

- use `Bearer` for the full token
- keep the OpenBao runtime secret as the durable token store

For Forgejo:

- keep the Forgejo API auth style consistent with the server’s API
- document whether `validate_certs: false` is temporary or permanent

For AWX:

- preserve the current service-user-first behavior
- keep the OpenBao fallback path
- avoid reintroducing admin-only token creation as the normal path

## What the Fresh Agent Should Do First

1. Read:
   - `k3s-lab-bootstrap/docs/openbao-policy-reconciliation-agent-prompt.md`
   - `k3s-lab-bootstrap/docs/netbox-token-journey.md`
   - `docs/SECURITY-REMEDIATION-GUIDE.md`
   - `docs/security-compliance-framework-plan.md`

2. Inspect:
   - `playbooks/696-cms-authentik-api.yml`
   - `playbooks/697-cms-awx-token.yml`
   - `playbooks/698-cms-netbox-forgejo-tokens.yml`

3. Confirm the shared duplication:
   - OpenBao break-glass JSON load
   - OpenBao pod discovery
   - OpenBao userpass login
   - client token extraction

4. Decide whether to extract:
   - a reusable `common/openbao-session` role, or
   - a shared include file under `roles/common`

5. Fix the NetBox auth header mismatch in `698`.

6. Review all `validate_certs: false` call sites in the token pipeline and add
   a CA-bundle path or an explicit lab-only justification.

## Important Constraints

- Do not commit real IPs or secrets to this repo.
- Do not move durable credentials into `/tmp`.
- Do not introduce a root token as a convenience shortcut.
- Do not fold issuer playbooks into one monolithic stage unless there is a
  clear operational reason.
- Keep recovery paths explicit and separate from the normal rerun path.

## Verification Expectations

Any refactor should be checked with:

```bash
git diff --check
./bin/run-playbook.sh hetzner-arm ../dmf-infra/k3s-lab-bootstrap/playbooks/691-netbox-sot.yml --syntax-check
./bin/run-playbook.sh hetzner-arm ../dmf-infra/k3s-lab-bootstrap/playbooks/692-forgejo-bootstrap.yml --syntax-check
./bin/run-playbook.sh hetzner-arm ../dmf-infra/k3s-lab-bootstrap/playbooks/693-awx-integration.yml --syntax-check
./bin/run-playbook.sh hetzner-arm ../dmf-infra/k3s-lab-bootstrap/playbooks/696-cms-authentik-api.yml --syntax-check
./bin/run-playbook.sh hetzner-arm ../dmf-infra/k3s-lab-bootstrap/playbooks/697-cms-awx-token.yml --syntax-check
./bin/run-playbook.sh hetzner-arm ../dmf-infra/k3s-lab-bootstrap/playbooks/698-cms-netbox-forgejo-tokens.yml --syntax-check
```

Runtime validation should start with the narrowest affected stage, not a full
`lifecycle-provision` rerun unless the stateful prerequisites have already been
reset.

