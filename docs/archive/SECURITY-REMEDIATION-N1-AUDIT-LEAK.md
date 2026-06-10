# Security Remediation N-1: Audit Log Captures Plaintext Secrets

**Date:** 2026-05-01
**Severity:** HIGH → CRITICAL (post-Issue #5 regression)
**Scope:** k3s audit policy + every operator role that authenticates to OpenBao via `kubectl exec`
**Parent document:** `docs/SECURITY-REMEDIATION-GUIDE.md` (Issues #1–#7 closed; this file extends that work)
**Estimated effort:** Phase 1 ≈ 30 min. Phase 2 ≈ 4–6 hrs across 10 task files. Phase 3 ≈ 1 hr cleanup.

---

## Executive Summary

Issue #5 of the parent remediation guide enabled k3s audit logging at `RequestResponse`
level for `pods/exec`, `pods/portforward`, `pods/attach`, `pods/ephemeralcontainers`,
`secrets`, `configmaps`, `serviceaccounts`, RBAC objects, and NetworkPolicies. That
remediation was correct in intent (forensic visibility into security-critical
namespaces) but it landed at the same time as the migration of every operator role
from the OpenBao root token to the `ops-admin` userpass identity. The combination
silently turned the audit log into a credential database:

- The `ops-admin` userpass password is passed as `kubectl exec` argv on every
  bootstrap login (10+ callers).
- The OpenBao client token returned from each login is then passed as `BAO_TOKEN=…`
  argv on every subsequent `bao kv put`/`bao kv get`/`bao policy write`/etc.
- DMF Console runtime tokens (`authentikApiToken`, `awxApiToken`) are written via
  `kubectl patch secret -p '{"stringData":{"…":"<token>"}}'` and via
  `kubernetes.core.k8s` Secret create/update — both of which are captured at
  `RequestResponse` by the audit policy.

`/var/log/kubernetes/audit.log` (and any S3 archive set up via
`audit_log_s3_bucket`) therefore contains plaintext copies of the credentials
the audit log was meant to *protect*. Anyone with read access to the audit log
or the archival bucket can replay these credentials.

This document specifies a two-phase remediation: a same-day audit-policy hotfix
that closes the leak surface immediately, followed by a refactor of the
operator-role exec pattern that keeps full `RequestResponse` forensics for shell
access while never letting credentials touch argv.

**Current status:** Phase 1 is completed and rolled out on 2026-05-01.
Phase 2 is functionally complete as of 2026-05-01; the refactor removed
literal secret values from the AWX, NetBox, Forgejo, Authentik, and OpenBao
bootstrap paths and the remaining task files now use the same env-var pattern.
Phase 3 cleanup and rotation completed and verified on 2026-05-01.
Verified live by rerunning `120-ops-admin-rotation.yml`,
`110-eso-secret-rotation.yml`, `696-cms-authentik-api.yml`, and
`697-cms-awx-token.yml` on the Hetzner cluster.

---

## Problem Statement

### Locations

**Audit policy (the lens):**
`k3s-lab-bootstrap/roles/base/k3s/templates/audit-policy.yaml.j2:7-23` —
`pods/exec` etc. logged at `RequestResponse` in namespaces `authentik`, `openbao`,
`dmf-cms`, `awx`.
`audit-policy.yaml.j2:26-36` — `secrets`, `configmaps`, `serviceaccounts`, RBAC
objects, NetworkPolicies logged at `RequestResponse` cluster-wide for create,
update, patch, delete, deletecollection verbs.

**The credential-bearing exec calls (the source):**

| File | Line(s) | What is in argv |
|------|---------|-----------------|
| `roles/common/app-admin-facts/tasks/main.yml` | ~59-86 | `password={{ ops_admin_password \| quote }}`, then `BAO_TOKEN=<client_token>` |
| `roles/common/dmf-born-inventory/tasks/main.yml` | ~109-145 | `secret_id=<approle_secret>`, then `BAO_TOKEN=<client_token>` |
| `roles/stack/operator/authentik/tasks/main.yml` | ~71-115, ~205-216, ~733-744 | `password=…`, then `BAO_TOKEN=…` for runtime + bootstrap-passkey writes |
| `roles/stack/operator/awx-integration/tasks/main.yml` | ~140-168, ~175-186, ~200-260, ~922-942 | `password=…`, then `BAO_TOKEN=…` for source-token reads and runtime writes |
| `roles/stack/operator/cms/tasks/main.yml` | ~199-244 | `password=…`, then `BAO_TOKEN=…` for Zot admin read |
| `roles/stack/operator/forgejo-bootstrap/tasks/main.yml` | ~430-478 | `password=…`, then `BAO_TOKEN=…` for runtime persistence |
| `roles/stack/operator/netbox/tasks/main.yml` | ~54-82 (and onwards) | `password=…`, then `BAO_TOKEN=…` for NetBox bootstrap writes |
| `roles/stack/operator/openbao/tasks/main.yml` | 612, 632, 652, 672, 692, 717, 777, 826, 850, 873, 893, 957, 977, 1000, 1020, 1111, 1131, 1185 | `BAO_TOKEN='{{ openbao_root_token }}' …` (root token, first-init only) |
| `playbooks/696-cms-authentik-api.yml` | 213-228, 239-254, 279-309, 311-327 | `password=…`, then `BAO_TOKEN=…`, then `kubectl patch secret -p '{"stringData":{"authentikApiToken":"<token>"}}'` |
| `playbooks/697-cms-awx-token.yml` | 79-103, 105-126, 595-621, 623-643, 668-699, 725-741 | Same pattern as 696, plus `awxApiToken` in patch body |
| `playbooks/vertical-orchestration/110-eso-secret-rotation.yml` | 69-100, 111-148 | `password=…`, then `BAO_TOKEN=…`, then `kubernetes.core.k8s` Secret update with `id: {{ new_secret_id }}` in `stringData` |

### Why It's This Way

The migration off the root token (Issue #2) was driven by a need to remove a
super-power credential from disk. The fastest port to userpass kept the same
shell idiom each role already used — `BAO_TOKEN='{{ token }}' bao …` — and just
swapped the source of the token. The audit policy (Issue #5) was reviewed as
"k3s emits an audit log; good," not as "the audit log is now a copy of every
secret value the bootstrap touches." The two changes were correct in
isolation; their interaction is the regression.

---

## Impact

### What is in the audit log right now

| Credential | Source | Persistence | Scope |
|------------|--------|-------------|-------|
| OpenBao root token | First-init bootstrap (`openbao/tasks/main.yml` exec calls) | Revoked at end of bootstrap by `:1185` (subject to N-2) | God-mode if not revoked; otherwise spent |
| `ops-admin` userpass password | Every operator-role `bao userpass login` exec | **Persistent — never rotated by any playbook** | `app-admin-writer` + `app-runtime-writer` policies; can write any `secret/data/apps/*` |
| OpenBao client tokens | Output of every userpass login, then echoed back as `BAO_TOKEN=…` argv | 1 h TTL | Whatever the userpass user has |
| ESO AppRole `secret_id` | `kubernetes.core.k8s` Secret update in `external-secrets` namespace + rotation playbook stringData | 30-day TTL; rotated each lifecycle run by 110-eso-secret-rotation | ESO read of `secret/data/*` |
| `authentikApiToken` | DMF Console runtime Secret patch in `dmf-cms` namespace | Rotated only when 696 reruns; `expiring=False` in Authentik | Authentik core API; can mint passkey invitations, read users/tokens |
| `awxApiToken` | DMF Console runtime Secret patch in `dmf-cms` namespace | Rotated when 697 reruns | AWX scope=`write` for the `dmf-cms-svc` user |
| `awx_svc_password`, `forgejo_admin_token`, `forgejo_svc_password`, NetBox tokens, Zot admin creds | `bao kv put secret/apps/<x>/runtime …=<value>` argv during persistence steps | Long-lived service credentials | Per-app admin/service surface |

**Highest-blast-radius item:** the `ops-admin` userpass password. It is the
only persistent credential in the list, it is logged on **every** operator-role
run, and its policies allow it to overwrite every app's admin and runtime
secret. Compromise of the audit log = full app-secret write access until the
password is rotated.

### Who can replay it

- Anyone with read access to `/var/log/kubernetes/audit.log` on a control node
  (root-equivalent on the host).
- Anyone with read access to the archival bucket if `audit_log_s3_bucket` is
  defined. This is the operator's S3 account scoped to log archival; treat the
  bucket policy as a load-bearing access boundary.
- Anyone who exfiltrates a backup of the control node's `/var/log` or the
  encrypted-at-rest disk if the disk key is also accessible.

### CVSS

- **Base vector (audit-log-on-disk only):** AV:L/AC:L/PR:H/UI:N/S:U/C:H/I:H/A:N → **6.7 (Medium)** — local read of root-owned file, but full secret recovery.
- **With S3 archival enabled (worst case):** AV:N/AC:L/PR:H/UI:N/S:C/C:H/I:H/A:N → **8.4 (High)** — network-reachable archive, scope-changed because credentials valid in OpenBao not just the audit-log host.
- **Including the persistent `ops-admin` password:** the credential outlives
  any short rotation window, so any one-time read of the log becomes a
  durable compromise. Effective severity therefore **CRITICAL** until
  Phase 1 lands.

---

## Recommended Phased Approach

### Phase 1 — Audit-policy hotfix (today)

Stop new credential-bearing entries from being written. Drop `pods/exec` body
capture in the `openbao` namespace and drop `secrets` write-body capture
cluster-wide; keep everything else at `RequestResponse`.

Cost: one template edit + one playbook rerun. Cost of the lost forensic
detail is bounded — see "Lost detail" below.

**Status on 2026-05-01:** completed and verified against the live Hetzner
cluster.

### Phase 2 — Refactor exec pattern (sequenced)

Move every credential out of `kubectl exec` argv:

- Userpass passwords → stdin (`password=-` is OpenBao native; argv stays clean).
- OpenBao client tokens → `~/.vault-token` inside the same single-shot
  `kubectl exec` shell (login + work + exit done in one exec, so the token
  never needs to be re-passed).
- KV write *values* → stdin via `bao kv put … -` reading a JSON document.
- DMF Console runtime Secret patches → switch from `kubectl patch -p '<json>'`
  to `kubernetes.core.k8s` with a structured definition; whether the body
  appears in the audit log is then governed by Phase 1 alone (Phase 1 already
  downgrades secrets writes to `Metadata`).

**Implementation progress on 2026-05-01:**

- Converted the shared OpenBao bootstrap path to pass `BAO_TOKEN` via task
  environment instead of inline `BAO_TOKEN='…'` argv.
- Converted `roles/common/app-admin-facts/tasks/main.yml`,
  `roles/common/dmf-born-inventory/tasks/main.yml`,
  `roles/stack/operator/authentik/tasks/main.yml`,
  `roles/stack/operator/awx-integration/tasks/main.yml`,
  `roles/stack/operator/cms/tasks/main.yml`,
  `roles/stack/operator/forgejo-bootstrap/tasks/main.yml`,
  `roles/stack/operator/netbox/tasks/main.yml`,
  `roles/stack/operator/netbox-sot/tasks/main.yml`,
  `playbooks/696-cms-authentik-api.yml`,
  `playbooks/697-cms-awx-token.yml`, and
  `playbooks/vertical-orchestration/110-eso-secret-rotation.yml` to the
  env-var-based command pattern.

---

## Live Validation

Completed on 2026-05-01 after the `300-k3s.yml` rollout:

- Creating a temporary Secret in `default` produced an audit event at
  `level":"Metadata"` with no secret payload in the log body.
- Executing a harmless `kubectl exec` into `openbao/openbao-0` also produced
  `level":"Metadata"`, which removed request-body capture for the OpenBao
  namespace.
- The exec request URI still includes the command query string, so command
  shape remains visible for forensics; the body is the part no longer recorded.

### Phase 3 — Post-fix cleanup (completed)

Archive note: the commands below are retained as historical remediation steps.
The live Hetzner cluster has already been rotated and verified.

- Rotate `ops-admin` password and re-write it to break-glass JSON.
- Rotate the current ESO AppRole `secret_id` (the rotation playbook will do
  this on next lifecycle run; trigger it explicitly).
- Rotate `authentikApiToken` and `awxApiToken` by rerunning 696 and 697.
- Audit existing `/var/log/kubernetes/audit.log` and any S3 archive for
  matches; document what was leaked, when, and which rotations covered each
  match. If retention is short and the archive is internal-only, this can be
  a "rotate everything once and move on" rather than a forensic exercise.

---

## Phase 1 — Audit-Policy Hotfix

### Step 1.1 Edit the audit-policy template

**File:** `k3s-lab-bootstrap/roles/base/k3s/templates/audit-policy.yaml.j2`

Replace the existing two `RequestResponse` rules with the four-rule version
below. The structure:

1. `pods/exec` etc. in `openbao` → `Metadata` (bootstrap calls smuggle
   credentials in argv; downgrade until Phase 2 lands).
2. `pods/exec` etc. in `authentik`, `dmf-cms`, `awx` → keep `RequestResponse`
   (these namespaces should never see a credential-bearing exec; if one shows
   up, that *is* the incident we want to forensically capture).
3. `secrets` create/update/patch cluster-wide → `Metadata` (the request body
   is the secret value; `Metadata` keeps "who patched what when" without
   recording the value).
4. Remaining security-sensitive writes (`configmaps`, `serviceaccounts`, RBAC,
   NetworkPolicy) → keep `RequestResponse`.

**Replacement content (full file):**

```yaml
---
apiVersion: audit.k8s.io/v1
kind: Policy
omitStages:
  - "RequestReceived"
rules:
  # OpenBao exec is bootstrap-only and credential-bearing today.
  # Keep "who/when" but drop request body until Phase 2 of N-1 lands.
  - level: Metadata
    verbs: ["create"]
    resources:
      - group: ""
        resources:
          - "pods/exec"
          - "pods/portforward"
          - "pods/attach"
          - "pods/ephemeralcontainers"
    namespaces:
      - openbao
    omitStages:
      - "RequestReceived"

  # Other critical namespaces: keep full request body for forensics.
  # Any credential-bearing exec into these namespaces is itself an incident.
  - level: RequestResponse
    verbs: ["create"]
    resources:
      - group: ""
        resources:
          - "pods/exec"
          - "pods/portforward"
          - "pods/attach"
          - "pods/ephemeralcontainers"
      - group: ""
        resources: ["serviceaccounts/token"]
      - group: authentication.k8s.io
        resources: ["tokenreviews"]
    namespaces:
      - authentik
      - dmf-cms
      - awx
    omitStages:
      - "RequestReceived"

  # Secret writes: log "who/when/which" but never capture the value.
  # The request body of a Secret create/update IS the secret.
  - level: Metadata
    verbs: ["create", "update", "patch", "delete", "deletecollection"]
    resources:
      - group: ""
        resources: ["secrets"]
    omitStages:
      - "RequestReceived"

  # Other security-sensitive writes: full body.
  - level: RequestResponse
    verbs: ["create", "update", "patch", "delete", "deletecollection"]
    resources:
      - group: ""
        resources: ["configmaps", "serviceaccounts"]
      - group: rbac.authorization.k8s.io
        resources: ["roles", "rolebindings", "clusterroles", "clusterrolebindings"]
      - group: networking.k8s.io
        resources: ["networkpolicies"]
    omitStages:
      - "RequestReceived"

  # Pod reads in kube-system are useful for incident reconstruction.
  - level: Metadata
    verbs: ["get", "list", "watch"]
    resources:
      - group: ""
        resources: ["pods", "pods/log"]
    namespaces:
      - kube-system
    omitStages:
      - "RequestReceived"

  # Default to metadata for the remaining API surface.
  - level: Metadata
    omitStages:
      - "RequestReceived"
```

### Step 1.2 Reroll k3s

The audit policy file is read by kube-apiserver at start. The k3s playbook
already wires the systemd drop-in; re-rendering the template plus a service
restart is enough.

```bash
cd ../dmf-env
bin/run-playbook.sh hetzner-lab \
  ../dmf-infra/k3s-lab-bootstrap/playbooks/300-k3s.yml --tags k3s
```

If the playbook does not include a handler for restarting k3s on policy
change, restart manually on each control node:

```bash
ssh k3s-admin@<control-node-public-ip> 'sudo systemctl restart k3s'
```

### Step 1.3 Validate the new policy is live

```bash
ssh k3s-admin@<control-node-public-ip>

# Confirm the new policy file matches the template
sudo md5sum /etc/k3s/audit-policy.yaml

# Generate test traffic
sudo k3s kubectl get secret -n dmf-cms dmf-cms-runtime -o name

# Confirm a Secret read still hits the log at Metadata (no body)
sudo tail -n 50 /var/log/kubernetes/audit.log \
  | jq 'select(.objectRef.resource=="secrets")
        | {level, verb, ns:.objectRef.namespace, name:.objectRef.name}'

# Run a no-op exec into openbao and confirm no requestObject body is captured
sudo k3s kubectl -n openbao exec deploy/openbao -- bao status >/dev/null
sudo grep -c '"requestObject"' /var/log/kubernetes/audit.log
# Expected: same count as before the exec (Metadata-only)
```

### Step 1.4 What you lose with this policy

- Shell command argv on `openbao` exec: gone. If someone runs an attack via
  `kubectl exec` into `openbao`, you see "user X exec'd into pod Y at time T",
  not which command they ran. **Compensating control:** `pods/exec` in
  `openbao` should only happen during bootstrap and rotation playbooks; any
  exec from an interactive operator session is itself a flag worth alerting
  on. After Phase 2 ships, restore `RequestResponse` on `openbao` exec.
- Secret request body: gone cluster-wide. Forensics for secret tampering now
  has to come from the application's own audit (e.g., OpenBao audit device,
  Authentik audit, AWX activity stream) plus the K8s `Metadata` "what was
  changed and by whom".

Both losses are acceptable for the leak window. Restore the lost surfaces in
Phase 2.

---

## Phase 2 — Refactor exec pattern

### Step 2.1 Patterns

#### Pattern A — userpass login with password on stdin

`bao` supports `password=-` for `bao write auth/userpass/login/<user>`,
which reads the password from stdin. Argv contains only the username.

**Before** (`roles/common/app-admin-facts/tasks/main.yml:60-78`-style):

```yaml
- name: Log into OpenBao with operator userpass
  ansible.builtin.command:
    argv:
      - kubectl
      - --kubeconfig
      - /etc/rancher/k3s/k3s.yaml
      - -n
      - "{{ app_admin_openbao_namespace }}"
      - exec
      - "{{ _app_admin_openbao_pod }}"
      - --
      - sh
      - -c
      - >-
        BAO_ADDR=https://127.0.0.1:8200
        bao write -format=json auth/userpass/login/{{ _app_admin_openbao_username | quote }}
        password={{ _app_admin_openbao_password | quote }}
  register: _app_admin_openbao_login_raw
  no_log: true
```

**After:**

```yaml
- name: Log into OpenBao with operator userpass
  ansible.builtin.command:
    stdin: "{{ _app_admin_openbao_password }}"
    argv:
      - kubectl
      - --kubeconfig
      - /etc/rancher/k3s/k3s.yaml
      - -n
      - "{{ app_admin_openbao_namespace }}"
      - exec
      - -i
      - "{{ _app_admin_openbao_pod }}"
      - --
      - sh
      - -c
      - >-
        BAO_ADDR=https://127.0.0.1:8200
        bao write -format=json auth/userpass/login/{{ _app_admin_openbao_username | quote }}
        password=-
  register: _app_admin_openbao_login_raw
  no_log: true
```

Three changes: `-i` added to `kubectl exec`, `stdin: "{{ password }}"` added,
literal `password=-` in the bao command. The argv now contains only the
username. The audit log `pods/exec` request body, if ever restored to
`RequestResponse`, contains only `…userpass/login/<user> password=-` — no
secret value.

#### Pattern B — single-exec login + work + exit (token never re-passes argv)

For roles that today do `login → set_fact client_token → exec kv put with BAO_TOKEN=...`,
collapse that into one `kubectl exec -i` invocation. Login writes the token to
`~/.vault-token` (default `bao` behavior with `bao login`); subsequent `bao`
calls in the same shell pick it up implicitly.

**Before** — two tasks:

```yaml
- name: Log into OpenBao with operator userpass
  ...
  register: _login

- name: Extract client token
  set_fact:
    _client_token: "{{ (_login.stdout | from_json).auth.client_token }}"

- name: bao kv put runtime secret
  command:
    argv:
      - kubectl
      - exec
      - "{{ pod }}"
      - --
      - sh
      - -c
      - >-
        BAO_TOKEN={{ _client_token | quote }}
        BAO_ADDR=https://127.0.0.1:8200
        bao kv put secret/apps/foo/runtime key1={{ value1 | quote }} key2={{ value2 | quote }}
```

**After** — one task, password and data both via stdin, JSON document for the
secret values so they never appear in argv:

```yaml
- name: Persist runtime secret to OpenBao via single-shot exec
  ansible.builtin.command:
    stdin: |
      {{ _ops_admin_password }}
      {{ {'key1': value1, 'key2': value2} | to_json }}
    argv:
      - kubectl
      - --kubeconfig
      - /etc/rancher/k3s/k3s.yaml
      - -n
      - openbao
      - exec
      - -i
      - "{{ _pod }}"
      - --
      - sh
      - -c
      - |
        set -eu
        export BAO_ADDR=https://127.0.0.1:8200
        # First line of stdin = userpass password
        IFS= read -r BAO_USERPASS_PW
        # Login; -no-print writes ~/.vault-token without echoing it.
        echo -n "$BAO_USERPASS_PW" \
          | bao login -no-print -method=userpass \
              -path=userpass username={{ _ops_admin_user | quote }} password=-
        unset BAO_USERPASS_PW
        # Second line of stdin = JSON of secret data
        bao kv put secret/apps/foo/runtime -
  no_log: true
```

`bao kv put <path> -` reads a JSON object from stdin and stores each key.
Neither the password nor the data values touch argv. The token lives only in
`~/.vault-token` inside the OpenBao pod for the lifetime of the exec, which
ends when the script returns.

#### Pattern C — Secret patches via `kubernetes.core.k8s`

**Before** (`playbooks/696-cms-authentik-api.yml:311-327`):

```yaml
- name: Patch DMF Console runtime Secret with API token
  ansible.builtin.command:
    argv:
      - sudo
      - k3s
      - kubectl
      - -n
      - "{{ cms_namespace | default('dmf-cms') }}"
      - patch
      - secret
      - "{{ cms_runtime_secret_name | default('dmf-cms-runtime') }}"
      - --type=merge
      - >-
        -p
        {"stringData":{"{{ cms_runtime_secret_authentik_token_key | default('authentikApiToken') }}":"{{ _cms_authentik_token_data.key }}"}}
  no_log: true
```

**After:**

```yaml
- name: Patch DMF Console runtime Secret with API token
  kubernetes.core.k8s:
    kubeconfig: /etc/rancher/k3s/k3s.yaml
    state: patched
    api_version: v1
    kind: Secret
    name: "{{ cms_runtime_secret_name | default('dmf-cms-runtime') }}"
    namespace: "{{ cms_namespace | default('dmf-cms') }}"
    definition:
      stringData:
        "{{ cms_runtime_secret_authentik_token_key | default('authentikApiToken') }}":
          "{{ _cms_authentik_token_data.key }}"
  no_log: true
```

Same K8s API call, structured body, no JSON-in-argv fragility (closes N-11
from the parent review). The Secret request body still contains the token —
that is what Phase 1 already downgraded to `Metadata`, so it never lands in
the audit log regardless.

### Step 2.2 Files to refactor

Apply Pattern A (userpass-via-stdin) and, where there are subsequent
`BAO_TOKEN=…` exec calls, Pattern B (single-shot exec) to:

1. `roles/common/app-admin-facts/tasks/main.yml` (login + read + write)
2. `roles/common/dmf-born-inventory/tasks/main.yml` (AppRole login pattern; same idea — `secret_id=-` via stdin)
3. `roles/stack/operator/authentik/tasks/main.yml` (login + read + write × 3 paths)
4. `roles/stack/operator/awx-integration/tasks/main.yml` (login + multiple reads + write)
5. `roles/stack/operator/cms/tasks/main.yml` (login + Zot admin read)
6. `roles/stack/operator/forgejo-bootstrap/tasks/main.yml` (login + write)
7. `roles/stack/operator/netbox/tasks/main.yml` (login + writes)
8. `playbooks/696-cms-authentik-api.yml` (login + read + write)
9. `playbooks/697-cms-awx-token.yml` (login + read + write)
10. `playbooks/vertical-orchestration/110-eso-secret-rotation.yml` (login + secret-id rotation)

Apply Pattern C (`kubernetes.core.k8s` for Secret patches) to:

- `playbooks/696-cms-authentik-api.yml:311-327`
- `playbooks/697-cms-awx-token.yml:725-741`

Apply Pattern B-style stdin for OpenBao bootstrap policies, AppRole writes,
and the userpass user creation in:

- `roles/stack/operator/openbao/tasks/main.yml` (lines 612, 632, 652, 672,
  692, 717, 777, 826, 850, 873, 893, 957, 977, 1000, 1020, 1111, 1131, 1185)

The bootstrap path is special: it runs only on first init, the root token is
revoked at the end (subject to N-2's silent-failure fix), and audit logging
on `openbao` exec is at `Metadata` after Phase 1. The bootstrap's leak
window is therefore already bounded. Refactor it last — when the operator
roles are clean it becomes the only remaining argv path and is worth
finishing.

### Step 2.3 Restore `RequestResponse` on `openbao` exec

After every operator role and bootstrap path uses Patterns A/B, revert the
first rule of the audit policy back to `RequestResponse`:

```yaml
  - level: RequestResponse
    verbs: ["create"]
    resources:
      - group: ""
        resources:
          - "pods/exec"
          - "pods/portforward"
          - "pods/attach"
          - "pods/ephemeralcontainers"
    namespaces:
      - authentik
      - openbao
      - dmf-cms
      - awx
    omitStages:
      - "RequestReceived"
```

This restores the forensic surface for `kubectl exec` into `openbao`. With
Patterns A/B in place, the only argv content for legitimate exec is the
shell script body itself — credential-free. Any exec whose argv now contains
something that looks like a credential is a real anomaly worth investigating.

---

## Phase 3 — Post-Fix Cleanup

### Step 3.1 Rotate `ops-admin` password

Until and unless this rotates, every existing audit-log entry that contains
`password=<value>` is still a valid credential for OpenBao.

```bash
# On the operator host: generate a new password
NEW_PW=$(openssl rand -base64 32 | tr -d '=+/' | head -c 32)

# Update the OpenBao userpass user (run via the wrapper so vault vars are loaded)
ssh k3s-admin@<control-node-public-ip> \
  "sudo k3s kubectl exec -n openbao deploy/openbao -i -- sh -c 'IFS= read -r PW; bao write auth/userpass/users/ops-admin password=-' <<<\"$NEW_PW\""

# Patch the break-glass JSON (operator host)
jq --arg pw "$NEW_PW" '.ops_admin_password = $pw' \
  <secure-store>/openbao-breakglass/hetzner-lab/openbao-keys-automation.json \
  > /tmp/breakglass.new.json
mv /tmp/breakglass.new.json \
  <secure-store>/openbao-breakglass/hetzner-lab/openbao-keys-automation.json
chmod 600 <secure-store>/openbao-breakglass/hetzner-lab/openbao-keys-automation.json
unset NEW_PW
```

Validate by re-running any operator role; the new password should authenticate
and the playbook should complete.

### Step 3.2 Rotate ESO AppRole secret_id

```bash
cd ../dmf-env
bin/run-playbook.sh hetzner-lab \
  ../dmf-infra/k3s-lab-bootstrap/playbooks/vertical-orchestration/110-eso-secret-rotation.yml
```

### Step 3.3 Rotate DMF Console runtime tokens

```bash
# Force re-mint by deleting the K8s Secret keys, then rerunning 696/697.
sudo k3s kubectl -n dmf-cms patch secret dmf-cms-runtime --type=json \
  -p='[{"op":"remove","path":"/data/authentikApiToken"},{"op":"remove","path":"/data/awxApiToken"}]' \
  || true

bin/run-playbook.sh hetzner-lab ../dmf-infra/k3s-lab-bootstrap/playbooks/696-cms-authentik-api.yml
bin/run-playbook.sh hetzner-lab ../dmf-infra/k3s-lab-bootstrap/playbooks/697-cms-awx-token.yml
```

### Step 3.4 Audit log retention review

Decide retention for the historical audit log entries that contain leaked
credentials:

- If the audit log is local-only (`audit_log_s3_bucket` unset) and disk
  rotation is `--audit-log-maxage=30 --audit-log-maxbackup=10`, the leaked
  values age out within a month. After Phase 3 rotations, those values are
  dead; no further action needed.
- If the log is shipped to S3, decide whether to tombstone the affected
  pre-fix archive objects. If retention is regulatory, scope access to the
  bucket to the smallest possible IAM principal set and document the
  exposure window in an internal incident note.

---

## Testing & Validation

### After Phase 1

- The audit log no longer contains `requestObject` for `pods/exec` in
  `openbao` (verified in Step 1.3).
- The audit log no longer contains `requestObject` for `secrets` writes in
  any namespace.
- The `Metadata` entries still include user, verb, resource, namespace,
  timestamp — enough for "who patched what when" forensics.

```bash
# Spot-check: count requestObject entries for secrets writes (should be 0)
sudo grep -c 'objectRef":{"resource":"secrets"' /var/log/kubernetes/audit.log
sudo jq 'select(.objectRef.resource=="secrets" and (.verb|test("create|update|patch")))
         | select(.requestObject != null)' /var/log/kubernetes/audit.log
# Second command should produce no output.
```

### After Phase 2

- Rerun every refactored role with a Vector-style audit-log probe in parallel:

```bash
# In a tmux pane on the control node:
sudo tail -F /var/log/kubernetes/audit.log \
  | jq -c 'select(.objectRef.resource=="pods" and .objectRef.subresource=="exec")
           | {ns:.objectRef.namespace, user:.user.username, args:(.requestObject.command // [])}'

# In another shell, run the refactored playbook. As each task fires,
# inspect the args field. None of them should contain "password=" with a value
# or "BAO_TOKEN=" with a value.
```

- Functional check: `external-secrets`, DMF Console, AWX integration all still
  authenticate and sync. Check `kubectl get externalsecret -A` and
  `kubectl logs -n external-secrets deploy/external-secrets`.

### After Phase 3

- `bao login -method=userpass username=ops-admin` with the **old** password
  fails (`permission denied`).
- `kubectl get secret -n dmf-cms dmf-cms-runtime -o jsonpath='{.data.authentikApiToken}' | base64 -d`
  is a fresh string; verify the corresponding token in Authentik admin UI shows
  a created-at timestamp matching the rerun.

---

## Rollback

### Phase 1 rollback

If the new audit policy breaks something — for instance, a downstream tool
parses `requestObject` on Secret writes — the rollback is to restore the prior
template content and rerun 300-k3s.yml. The prior audit policy is in git
history at `b56b30…` (or whichever commit added Issue #5).

```bash
git diff HEAD -- k3s-lab-bootstrap/roles/base/k3s/templates/audit-policy.yaml.j2
git checkout HEAD~1 -- k3s-lab-bootstrap/roles/base/k3s/templates/audit-policy.yaml.j2
bin/run-playbook.sh hetzner-lab ../dmf-infra/k3s-lab-bootstrap/playbooks/300-k3s.yml --tags k3s
```

### Phase 2 rollback

Phase 2 is a per-role refactor. Each role's change should be a single PR-sized
commit so it can be reverted independently. The fallback safety net is Phase 1
— even if a role's stdin-based call regresses to argv-based, the audit-policy
downgrade still prevents leakage.

### Phase 3 rollback

Password rotation cannot be rolled back. If the rotation is applied and the
break-glass JSON is corrupted, the recovery path is:

1. Use 3 of 5 Shamir shares to root-token-rebootstrap the userpass user (see
   `<secure-store>/openbao-breakglass/hetzner-lab/UNSEAL-PROCEDURE.md` if
   present, otherwise `roles/stack/operator/openbao/tasks/main.yml` is the
   recovery template).
2. Re-write the `ops_admin_password` field in the break-glass JSON.

---

## Open Questions / Known Limitations

1. **`ops-admin` rotation is now encoded.** Phase 3 is implemented as
   `vertical-orchestration/120-ops-admin-rotation.yml` and wired into
   lifecycle provisioning. There is still no periodic rotation policy for the
   operator userpass identity; this is a one-time remediation, not a cron job.

2. **Audit log retention is decoupled from this fix.** If `audit_log_s3_bucket`
   is set, the bucket policy and IAM principals are now load-bearing. This is
   out of scope for this document but worth a separate review.

3. **OpenBao-internal audit device.** OpenBao has its own audit log
   capability (`bao audit enable`). Enabling it would give an
   OpenBao-side record of every operation, redundant with the K8s audit log
   for the bootstrap path but invaluable post-bootstrap when ESO and
   apps talk to OpenBao directly. Suggested follow-up; not part of N-1.

4. **kubectl exec via the host’s `sudo k3s kubectl`.** Several callers use
   `sudo k3s kubectl …` which goes through the local kubeconfig. Audit
   logging captures these the same as any other apiserver request — the user
   identity will be `system:admin` rather than a named operator. This is a
   pre-existing observability gap; not part of N-1.

---

**Document Version:** 1.0
**Last Updated:** 2026-05-01
**Owner:** DevSecOps
**Review Cadence:** Archived after Phase 3 completion on 2026-05-01.
