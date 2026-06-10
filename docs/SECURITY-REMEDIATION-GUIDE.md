# dmf-infra Security Remediation Guide

**Date:** 2026-05-01
**Scope:** k3s-lab-bootstrap lifecycle-provision playbooks
**Severity Levels (initial review):** 3 CRITICAL, 4 HIGH, 7 MEDIUM, 1 LOW
**Severity Levels (follow-up review on 2026-05-01):** +1 CRITICAL, +1 HIGH, +6 MEDIUM, +3 LOW (issues N-1 through N-12)
**Target Completion:** Production deployment phase
**Companion document:** `docs/SECURITY-REMEDIATION-N1-AUDIT-LEAK.md` for the N-1 deep dive (Phase 1 already shipped)

---

## Executive Summary

Security review of the dmf-infra architecture identified 15 actionable issues across secrets management, RBAC, authentication, networking, and audit logging. This guide provides:

- **Detailed problem statement** for each issue
- **Root cause analysis** with file locations
- **Step-by-step remediation instructions** with code changes
- **Cluster access prerequisites** and commands
- **Testing/validation steps** to verify fix
- **Recommended priority order** for implementation

### Current Progress

As of 2026-05-01, the following items are already complete and rolled out in the live Hetzner cluster:

- ~~Issue #1, OpenBao TLS~~
- Issue #2, break-glass root token disposal
- Issue #3, scoped Authentik `kubectl exec` RBAC
- Issue #4, OpenBao NetworkPolicy boundary
- ~~Issue #5, Kubernetes audit logging~~
- Issue #6, automated ESO AppRole secret rotation
- Issue #7, AWX token bootstrap now prefers the durable service-user path
- OpenBao runtime-secret write path for DMF Console, AWX, and NetBox SoT callers
- ~~N-1 Phase 1 (audit-policy hotfix)~~ — `pods/exec` body capture in `openbao` and `secrets` write-body capture cluster-wide are now `Metadata`-level

A follow-up strict re-review on 2026-05-01 surfaced 12 additional findings (N-1
through N-12). Most are interactions between two correctly-implemented items
above (e.g. N-1 is the audit-log + operator-userpass migration interaction)
or scope gaps from the original remediations (e.g. N-6 is NetworkPolicy
implemented only for the `openbao` namespace, not the seven namespaces in the
original Issue #4 scope). The full set is documented in the new
"Follow-Up Findings" section below; everything not yet shipped is also
included in the bottom Summary table and Recommended Remediation Order.

---

## Cluster Access Prerequisites

### Before You Start

Ensure you have:
1. **Canonical Hetzner access path:**
   - Operator host: `<lan-ip>` on the Mac mini
   - Hetzner control node / bastion: `k3s-admin@<control-node-public-ip>`
   - Use the control node for live cluster reads and writes
   - Do **not** rely on the local Mac `kubectl` context for Hetzner truth; it may point at a different cluster

1. **Local kubeconfig access:**
   ```bash
   export KUBECONFIG=~/.kube/k3s.yaml
   # OR read from control node
   scp k3s-user@k3s-node-01:/etc/rancher/k3s/k3s.yaml ~/.kube/k3s-lab.yaml
   export KUBECONFIG=~/.kube/k3s-lab.yaml
   ```

2. **kubectl installed and working:**
   ```bash
   kubectl cluster-info
   kubectl get nodes
   ```

3. **SSH access to control node (k3s-node-01):**
   ```bash
   ssh k3s-user@k3s-node-01.dmf.example.com
   ```

4. **Ansible environment ready:**
   ```bash
   cd <repos>/dmf-env
   source bin/activate  # if using venv
   ansible-inventory --list  # verify inventory loads
   ```

5. **OpenBao break-glass credentials available:**
   ```bash
   ls -l <secure-store>/openbao-breakglass/hetzner-lab/
   # Should see: openbao-keys-automation.json + Shamir share files
   ```

### Canonical Working Pattern

When resuming this work, start from the control node and the cluster wrapper:

```bash
ssh k3s-admin@<control-node-public-ip>
sudo kubectl --kubeconfig /etc/rancher/k3s/k3s.yaml get nodes
cd ~/repos/dmf-env
bin/run-playbook.sh ../dmf-infra/k3s-lab-bootstrap/site.yml
```

### Local Context Warning

- The Mac's local `kubectl` context can point at the wrong cluster.
- For Hetzner rollout verification, treat `kubectl` output on the control node as authoritative.
- If a command appears to work locally but not on the control node, trust the control node and re-check the kubeconfig and context.

---

## Issue #1: CRITICAL — OpenBao TLS Disabled

**Status:** Completed and rolled out on 2026-05-01.

### Problem Statement

**Location:** `k3s-lab-bootstrap/roles/stack/operator/openbao/tasks/main.yml:48`

**Current Behavior:**
```yaml
tls_disable = 1
```

**Impact:**
- All in-cluster communication to OpenBao (port 8200) is **unencrypted HTTP**
- AppRole credentials, auth tokens, and secret values transmitted in **cleartext**
- ESO, app-admin-facts, and any pod with network access can sniff credentials
- Man-in-the-middle attacks possible within cluster network
- **CVSS: 9.1 (Critical)** — Network-adjacent attacker can intercept all secrets

**Why It's This Way:**
OpenBao was disabled for development ease. The comment in `openbao/defaults/main.yml` mentions "self-signed for internal mTLS" but was never implemented.

---

### Remediation Steps

#### Step 1: Generate Self-Signed Certificate for OpenBao

**File to create:** `k3s-lab-bootstrap/roles/stack/operator/openbao/files/openbao-tls.yml`

```bash
# On your local machine, generate a self-signed cert
cd <repos>/dmf-infra/k3s-lab-bootstrap/roles/stack/operator/openbao/files

# Generate private key
openssl genrsa -out openbao-tls.key 2048

# Generate self-signed certificate (365 days, CN=openbao.openbao.svc.cluster.local)
openssl req -new -x509 -key openbao-tls.key -out openbao-tls.crt \
  -days 365 \
  -subj "/CN=openbao.openbao.svc.cluster.local/O=DMF Lab/C=US"

# Verify
openssl x509 -in openbao-tls.crt -text -noout | grep -A2 "Subject:\|Not Before\|Not After"
```

#### Step 2: Update OpenBao Role to Enable TLS

**File to modify:** `k3s-lab-bootstrap/roles/stack/operator/openbao/tasks/main.yml`

**Current (line 48):**
```yaml
tls_disable = 1
```

**Change to:**
```yaml
tls_disable = 0
tls_cert_file = /openbao/config/tls/openbao-tls.crt
tls_key_file = /openbao/config/tls/openbao-tls.key
```

**Full context (lines 45-55):**
```yaml
    - name: Create OpenBao config
      ansible.builtin.copy:
        content: |
          storage "raft" {
            path = "/openbao/data"
            node_id = "{{ inventory_hostname }}"
          }
          listener "tcp" {
            address       = "0.0.0.0:8200"
            tls_disable   = 0
            tls_cert_file = /openbao/config/tls/openbao-tls.crt
            tls_key_file  = /openbao/config/tls/openbao-tls.key
          }
          api_addr = "https://{{ hostvars[groups['k3s_control'][0]].ansible_host }}:8200"
          ui = true
        dest: /etc/openbao/openbao.hcl
        owner: openbao
        group: openbao
        mode: '0640'
```

#### Step 3: Mount TLS Certificate in OpenBao Deployment

**File to modify:** `k3s-lab-bootstrap/roles/stack/operator/openbao/tasks/main.yml` (Helm values section)

Find the line with `helm upgrade openbao` (around line 76-92) and add volume mounts:

**Before:**
```yaml
spec:
  containers:
  - name: openbao
    image: ...
```

**After (add volumeMounts):**
```yaml
spec:
  containers:
  - name: openbao
    image: ...
    volumeMounts:
    - name: tls-certs
      mountPath: /openbao/config/tls
      readOnly: true
  volumes:
  - name: tls-certs
    secret:
      secretName: openbao-tls
      defaultMode: 0400
```

#### Step 4: Create Kubernetes Secret with TLS Certificate

**Add task before Helm upgrade (around line 74):**

```yaml
    - name: Create OpenBao TLS Secret
      kubernetes.core.k8s:
        kubeconfig: /etc/rancher/k3s/k3s.yaml
        state: present
        definition:
          apiVersion: v1
          kind: Secret
          metadata:
            name: openbao-tls
            namespace: openbao
          type: tls
          data:
            tls.crt: "{{ lookup('file', role_path ~ '/files/openbao-tls.crt') | b64encode }}"
            tls.key: "{{ lookup('file', role_path ~ '/files/openbao-tls.key') | b64encode }}"
```

#### Step 5: Update ESO ClusterSecretStore to Trust Self-Signed Cert

**File to modify:** `k3s-lab-bootstrap/roles/base/external-secrets/tasks/main.yml` (around line 53-92)

The ESO provider spec needs to trust the self-signed CA. Update the `secretStoreRef`:

```yaml
    - name: Create ESO ClusterSecretStore for OpenBao
      kubernetes.core.k8s:
        kubeconfig: /etc/rancher/k3s/k3s.yaml
        state: present
        definition:
          apiVersion: external-secrets.io/v1beta1
          kind: ClusterSecretStore
          metadata:
            name: openbao-kv-secret
          spec:
            provider:
              openbao:
                auth:
                  appRole:
                    path: "approle"
                    roleId: "{{ _openbao_eso_role_id }}"
                    secretRef:
                      name: openbao-eso-secret
                      key: secret_id
                caProvider:
                  type: Secret
                  name: openbao-tls
                  key: tls.crt
                  namespace: openbao
                server: "https://openbao.openbao.svc.cluster.local:8200"
                path: "secret/data"
```

#### Step 6: Update App-Admin-Facts to Use HTTPS

**File to modify:** `k3s-lab-bootstrap/roles/common/app-admin-facts/tasks/main.yml`

Find OpenBao client initialization (around line 155-170) and update:

**Before:**
```bash
BAO_ADDR=http://127.0.0.1:8200
```

**After:**
```bash
BAO_ADDR=https://127.0.0.1:8200
# Trust self-signed cert
REQUESTS_CA_BUNDLE=/etc/ssl/certs/ca-bundle.crt
# OR disable for self-signed (development only)
export PYTHONWARNINGS="ignore:Unverified HTTPS"
```

#### Step 7: Update CMS Playbooks (696, 697) for HTTPS

**Files to modify:**
- `k3s-lab-bootstrap/playbooks/696-cms-authentik-api.yml:147-153`
- `k3s-lab-bootstrap/playbooks/697-cms-awx-token.yml:371-390`

Find the `bao kv patch` kubectl exec calls and update the address:

**Before:**
```bash
BAO_ADDR=http://127.0.0.1:8200
```

**After:**
```bash
BAO_ADDR=https://127.0.0.1:8200
# Disable cert verification for self-signed (development only; production use mTLS)
BAO_SKIP_VERIFY=1
```

---

### Testing & Validation

#### 1. Verify TLS is Enabled

```bash
# SSH to control node
ssh k3s-user@k3s-node-01.dmf.example.com

# Check OpenBao listener
sudo k3s kubectl logs -n openbao deploy/openbao -f | grep -i "tls\|listener"

# Expected output:
# [INFO] listener.tcp: TLS enabled
```

#### 2. Test HTTPS Connection to OpenBao

```bash
# From control node, port-forward
kubectl port-forward -n openbao svc/openbao 8200:8200 &

# Test from localhost (ignore self-signed warning)
curl -k https://localhost:8200/v1/sys/health

# Expected:
# {"sealed":false,"standby":false,...}
```

#### 3. Verify ESO Can Authenticate

```bash
# Check ESO pod logs
kubectl logs -n external-secrets deploy/external-secrets-webhook -f | grep -i "openbao\|secret"

# Should NOT see TLS errors or connection refused
```

#### 4. Validate Secrets Still Accessible

```bash
# Trigger an ExternalSecret sync
kubectl annotate externalsecrets -n <namespace> <secret-name> \
  force-sync=$(date +%s) --overwrite

# Check that secret synced successfully
kubectl get secret <name> -n <namespace> -o yaml | grep -A5 data
```

---

### Rollback Plan

If TLS causes issues:

```bash
# Revert tls_disable to 1 in openbao/tasks/main.yml
# Revert BAO_ADDR=http in playbooks
# Re-run: bin/run-playbook.sh hetzner-lab ../dmf-infra/k3s-lab-bootstrap/playbooks/vertical-security/100-openbao.yml
```

---

## Issue #2: CRITICAL — Break-Glass Root Token Persisted on Disk

**Status:** Completed and rolled out on 2026-05-01.

### Problem Statement

**Location:** `k3s-lab-bootstrap/roles/stack/operator/openbao/tasks/main.yml:501-513`

**Current Behavior:**
```yaml
- name: Copy Shamir shares and root token to automation file
  copy:
    content: |
      {
        "root_token": "{{ _openbao_root_token }}",
        "shamir_keys": {{ _openbao_shamir_shares | to_json }}
      }
    dest: <secure-store>/openbao-breakglass/hetzner-lab/openbao-keys-automation.json
```

**Impact:**
- Root token (all-powerful) stored on **operator's local macOS** in plaintext JSON
- Accessible to any process running as the user
- Persists across playbook runs — never rotated or disposed
- File is user-readable (`0600`) but not encrypted
- Breach of file = complete OpenBao compromise
- **CVSS: 9.8 (Critical)** — Local attacker has read access to all secrets

**Why It's This Way:**
Break-glass JSON needed for idempotent re-runs of provisioning playbooks. Root token included so operator can manually unseal if needed.

---

### Remediation Steps

#### Step 1: Implement Root Token Disposal (Post-Bootstrap)

**File to modify:** `k3s-lab-bootstrap/roles/stack/operator/openbao/tasks/main.yml`

Add a new task after initial setup (around line 1042, after `openbao_dispose_root_token` check):

```yaml
    - name: Revoke root token (post-bootstrap security gate)
      ansible.builtin.uri:
        url: "{{ _openbao_api_addr }}/v1/auth/token/revoke-self"
        method: POST
        headers:
          X-Vault-Token: "{{ _openbao_root_token }}"
        status_code: [204, 400]  # 400 if already revoked
      when:
        - _openbao_initialized | bool
        - openbao_dispose_root_token | default(true) | bool
      no_log: true
      register: _openbao_token_revoke

    - name: Log root token revocation
      ansible.builtin.copy:
        content: |
          Root token revoked at {{ ansible_date_time.iso8601 }}
          Reason: Post-bootstrap security hardening
          Operator: {{ ansible_user_id }}
        dest: <secure-store>/openbao-breakglass/hetzner-lab/TOKEN_REVOKED.log
        mode: '0600'
      when:
        - _openbao_token_revoke.status | default(0) == 204
      delegate_to: localhost
      become: false
```

#### Step 2: Update Break-Glass JSON to Remove Root Token

**File to modify:** `k3s-lab-bootstrap/roles/stack/operator/openbao/tasks/main.yml` (line 501-513)

**Before:**
```yaml
- name: Copy Shamir shares and root token to automation file
  copy:
    content: |
      {
        "root_token": "{{ _openbao_root_token }}",
        "shamir_keys": {{ _openbao_shamir_shares | to_json }}
      }
```

**After:**
```yaml
- name: Copy Shamir shares to break-glass file (root token NOT included)
  copy:
    content: |
      {
        "created_at": "{{ ansible_date_time.iso8601 }}",
        "shamir_keys": {{ _openbao_shamir_shares | to_json }},
        "threshold": {{ _openbao_shamir_threshold }},
        "total_shares": {{ _openbao_shamir_shares | length }},
        "note": "Root token disposed post-bootstrap. Use Shamir shares to unseal. See UNSEAL-PROCEDURE.md"
      }
    dest: <secure-store>/openbao-breakglass/hetzner-lab/openbao-keys-automation.json
    mode: '0600'
    owner: "{{ ansible_user_id }}"
  delegate_to: localhost
  become: false
  no_log: true
```

#### Step 3: Create Unseal Procedure Documentation

**File to create:** `<secure-store>/openbao-breakglass/hetzner-lab/UNSEAL-PROCEDURE.md`

```markdown
# OpenBao Emergency Unseal Procedure

**Date Created:** 2026-05-01  
**Status:** Active (root token disposed, Shamir shares in use)

## Quick Reference

| Item | Details |
|------|---------|
| Threshold | 3 of 5 shares required |
| Share Locations | Keychain, JuiceFS, USB-A, USB-B, Automation file |
| Root Token | **REVOKED POST-BOOTSTRAP** (use Shamir instead) |
| Last Unseal | [Automated at bootstrap] |

## Emergency Unseal Steps

### Prerequisites
- SSH access to k3s-node-01 with k3s-user account
- OpenBao CLI tool (`bao`) installed locally
- Access to 3+ Shamir share locations

### Procedure

1. **Retrieve 3 Shamir shares:**
   ```bash
   # From macOS Keychain
   security find-generic-password -l "openbao-shamir-share-1" -w

   # From JuiceFS mount
   cat /Volumes/JuiceFS/openbao-keys/share-2.txt

   # From USB device
   cat /Volumes/USB-OpenBao-A/share-3.txt
   ```

2. **SSH to control node:**
   ```bash
   ssh k3s-user@k3s-node-01.dmf.example.com
   ```

3. **Port-forward OpenBao:**
   ```bash
   kubectl port-forward -n openbao svc/openbao 8200:8200 &
   ```

4. **Unseal with bao CLI:**
   ```bash
   bao operator unseal  # Enter each share when prompted
   # Repeat 3 times (threshold is 3 of 5)
   ```

5. **Verify unseal:**
   ```bash
   bao status  # Should show "Sealed: false"
   ```

## What NOT to Do

- ❌ Do NOT reuse Shamir shares after unseal
- ❌ Do NOT share shares via email/Slack
- ❌ Do NOT store shares in version control
- ❌ Do NOT attempt to recreate root token manually

## If All Shares Are Lost

Contact security team. Data in OpenBao cannot be recovered without threshold shares.
```

#### Step 4: Update Defaults to Enable Token Disposal

**File to modify:** `k3s-lab-bootstrap/roles/stack/operator/openbao/defaults/main.yml`

**Before (around line 60-65):**
```yaml
openbao_dispose_root_token: false
```

**After:**
```yaml
# Post-bootstrap security: revoke root token after initial setup
# Set to false only if re-initializing from scratch
openbao_dispose_root_token: true
```

#### Step 5: Implement Rotation Schedule for Shamir Shares

**File to create:** `k3s-lab-bootstrap/playbooks/vertical-security/102-openbao-shamir-rotation.yml`

This playbook (run annually) re-generates Shamir shares:

```yaml
---
# Annual Shamir share rotation for OpenBao
# Scheduled: First Monday of January (disaster recovery drill)
- name: Rotate OpenBao Shamir shares (annual security refresh)
  hosts: k3s_control[0]
  become: true
  gather_facts: true

  vars:
    openbao_shamir_threshold: 3
    openbao_shamir_total: 5

  roles:
    - cluster-ready

  tasks:
    - name: Verify root token is disposed
      ansible.builtin.uri:
        url: "https://openbao.openbao.svc.cluster.local:8200/v1/auth/token/lookup-self"
        method: GET
        headers:
          X-Vault-Token: "{{ _openbao_root_token_placeholder }}"
        status_code: [403, 400]  # Should be forbidden (no token)
      ignore_errors: true
      register: _root_token_check

    - name: Assert root token is revoked
      ansible.builtin.assert:
        that:
          - _root_token_check.status in [400, 403]
        fail_msg: "Root token still active. Cannot rotate Shamir shares while root token exists."

    - name: Generate new Shamir shares via key rotation API
      ansible.builtin.uri:
        url: "https://openbao.openbao.svc.cluster.local:8200/v1/sys/rekey/init"
        method: POST
        headers:
          X-Vault-Token: "{{ _openbao_recovery_token }}"  # Use recovery token instead
        body_format: json
        body:
          key_shares: "{{ openbao_shamir_total }}"
          key_threshold: "{{ openbao_shamir_threshold }}"
        status_code: 200
      register: _rekey_init
      no_log: true

    - name: Back up new shares to break-glass location
      ansible.builtin.copy:
        content: |
          {
            "rotation_date": "{{ ansible_date_time.iso8601 }}",
            "rotation_reason": "Annual security refresh",
            "rekey_nonce": "{{ _rekey_init.json.nonce }}",
            "note": "New shares generated. Old shares should be destroyed. Contact security team."
          }
        dest: <secure-store>/openbao-breakglass/hetzner-lab/SHAMIR_ROTATION_{{ ansible_date_time.date }}.json
        mode: '0600'
      delegate_to: localhost
      become: false

    - name: Alert security team
      ansible.builtin.debug:
        msg: |
          ⚠️ SHAMIR SHARE ROTATION COMPLETED
          Date: {{ ansible_date_time.iso8601 }}
          Action Required:
          1. Retrieve old shares from all locations
          2. Securely destroy old shares (shred, incinerate, etc.)
          3. Distribute new shares to break-glass locations
          4. Update keychain entries
          5. Confirm with security team that rotation is complete
```

---

### Testing & Validation

#### 1. Verify Token Disposal

```bash
# Check that root token is revoked
kubectl exec -n openbao <pod-name> -- bao token lookup-self

# Expected: "permission denied" (403) or "not found" (404)
```

#### 2. Verify Shamir Shares Still Work

```bash
# Simulate emergency unseal (requires 3 shares)
# See UNSEAL-PROCEDURE.md for detailed steps
kubectl port-forward -n openbao svc/openbao 8200:8200 &
bao operator unseal  # Should work with any 3 of 5 shares
```

#### 3. Verify Break-Glass JSON is Safe

```bash
# Check that root token is NOT in the file
cat <secure-store>/openbao-breakglass/hetzner-lab/openbao-keys-automation.json | jq .root_token

# Expected: null or field not present
```

---

## Issue #3: CRITICAL — No RBAC on kubectl exec for Authentik Shell

**Status:** Completed. The playbook now uses a dedicated namespace-scoped service account and Role/RoleBinding for the Authentik exec path, and that change was live-verified on 2026-05-01.

### Problem Statement

**Location:** `k3s-lab-bootstrap/playbooks/696-cms-authentik-api.yml:24-59`

**Current Behavior:**
```yaml
- name: Mint Authentik exec service-account token
  kubectl create token authentik-token-operator --duration=1h

- name: Create Authentik API token for DMF Console
  ansible.builtin.command:
    argv:
      - sudo
      - k3s
      - kubectl
      - --token
      - <short-lived service-account token>
      - -n
      - authentik
      - exec
      - deploy/authentik-server
      - --
      - ak
      - shell
      - -c
      - |-
        # Django Python REPL with full database access
        from authentik.core.models import Token, User
        # ... any code can be run here ...
```

**Impact:**
- `kubectl exec` **requires no RBAC check** — any operator account can execute
- Django shell has **full database access** (read, write, delete)
- Can read all user credentials, secrets, OAuth tokens
- Can modify authentication policies, add backdoor users
- No audit log of what code was executed
- **CVSS: 9.9 (Critical)** — Unauthorized code execution with database privileges

**Why This Is Done:**
The original security issue was the unconstrained use of a cluster-admin kubeconfig for `kubectl exec` into Authentik. That has been replaced with a namespace-scoped service account and Role/RoleBinding. A future API-only token-creation path would be a separate hardening improvement, not a blocker for closing this issue.

---

### Remediation Steps

#### Step 1: Scope the exec path to a dedicated service account

**File to modify:** `k3s-lab-bootstrap/playbooks/696-cms-authentik-api.yml`

Create a namespace-scoped ServiceAccount plus Role/RoleBinding in `authentik`, then mint a short-lived token for that identity and use it for the `kubectl exec` call. This removes the dependency on the full cluster-admin kubeconfig for the exec step.

```yaml
- name: Create Authentik exec ServiceAccount
  kubernetes.core.k8s:
    kubeconfig: /etc/rancher/k3s/k3s.yaml
    state: present
    definition:
      apiVersion: v1
      kind: ServiceAccount
      metadata:
        name: authentik-token-operator
        namespace: authentik

- name: Create Authentik exec Role
  kubernetes.core.k8s:
    kubeconfig: /etc/rancher/k3s/k3s.yaml
    state: present
    definition:
      apiVersion: rbac.authorization.k8s.io/v1
      kind: Role
      metadata:
        name: authentik-token-operator-exec
        namespace: authentik
      rules:
        - apiGroups: [""]
          resources: ["pods", "pods/exec"]
          verbs: ["get", "list", "create"]

- name: Create Authentik exec RoleBinding
  kubernetes.core.k8s:
    kubeconfig: /etc/rancher/k3s/k3s.yaml
    state: present
    definition:
      apiVersion: rbac.authorization.k8s.io/v1
      kind: RoleBinding
      metadata:
        name: authentik-token-operator-exec
        namespace: authentik
      roleRef:
        apiGroup: rbac.authorization.k8s.io
        kind: Role
        name: authentik-token-operator-exec
      subjects:
        - kind: ServiceAccount
          name: authentik-token-operator
          namespace: authentik

- name: Mint Authentik exec service-account token
  ansible.builtin.command:
    argv:
      - kubectl
      - --kubeconfig
      - /etc/rancher/k3s/k3s.yaml
      - -n
      - authentik
      - create
      - token
      - authentik-token-operator
      - --duration=1h
```

#### Follow-up: Replace the Shell Bootstrap With an API-Only Path

**Root cause:** The current playbook still uses `ak shell` for token creation. Authentik does expose a core token API, but this repo has not yet wired a bearer-authenticated API call into the bootstrap flow.

**Option A: Use Authentik token API with bearer auth**

Authentik exposes a `/api/v3/core/tokens/` API, but it requires an authenticated bearer token. If the deployment already has a bootstrap admin bearer token path, switch 696 to that API and remove the pod shell entirely.

**Option B: Keep the bootstrap shell path, but restrict it to the dedicated service account above**

**File to modify:** `k3s-lab-bootstrap/roles/stack/operator/authentik/tasks/main.yml`

If the token API is not practical in this cluster, keep token creation in the bootstrap shell, but only run it with the dedicated service account token from Step 1:

```yaml
    - name: Generate Authentik API token during bootstrap
      ansible.builtin.command:
        argv:
          - kubectl
          - -n
          - authentik
          - exec
          - deploy/authentik-server
          - --
          - ak
          - shell
          - -c
          - |-
            import json
            from authentik.core.models import Token, User
            # ONLY runs during bootstrap (not on subsequent runs)
            if not Token.objects.filter(intent="api", description="dmf-cms").exists():
              user = User.objects.get(username="akadmin")
              token = Token.objects.create(
                user=user,
                intent="api",
                description="dmf-cms",
                expiring=False,  # UPDATE: Set expiry later
              )
              # Output token for capture
              print(json.dumps({"token": token.key, "created": True}))
            else:
              # Token exists, do not create new one
              token = Token.objects.get(intent="api", description="dmf-cms")
              print(json.dumps({"token": "", "created": False, "message": "Token exists"}))
      register: _authentik_token_result
      # Runs only with the dedicated Authentik exec service-account token
      no_log: true

    - name: Persist existing token secret from OpenBao
      ansible.builtin.set_fact:
        _cms_authentik_api_token: "{{ lookup('hashi_vault', 'secret=secret/data/apps/authentik/runtime:cms_api_token') }}"
      no_log: true
      when: not (_authentik_first_run | default(false) | bool)
```

**Key improvement:** Token is created ONCE during bootstrap, then reused from OpenBao on subsequent runs. No cluster-admin kubeconfig is needed for the exec step.

#### Step 3: Document That Shell Access Should Only Be Used for Bootstrap

**File to create:** `k3s-lab-bootstrap/docs/AUTHENTIK-TOKEN-MANAGEMENT.md`

```markdown
# Authentik API Token Management

## Overview

DMF Console requires an Authentik API token to create passkey invitations. This token is provisioned during bootstrap and **should never require shell access after initial setup**.

## Bootstrap Phase (First Run Only)

1. **Authentik bootstrap playbook** creates `akadmin` user
2. **Token creation playbook** (696) runs `ak shell` ONCE to create token
3. Token is stored in OpenBao: `secret/apps/authentik/runtime:cms_api_token`
4. Token is injected into K8s Secret `dmf-cms-runtime`

## Production Phase (Subsequent Runs)

- Token is **read from OpenBao**, not created
- `kubectl exec` shell access should only occur through the dedicated Authentik service account
- No cluster-admin kubeconfig is used for the exec step

## Token Rotation (Quarterly)

Run this to rotate the token:

```bash
# 1. Generate new token in OpenBao
bao write -f secret/data/apps/authentik/tokens/cms_api_token \
  token=$(openssl rand -hex 32)

# 2. Re-run DMF Console playbook to pick up new token
bin/run-playbook.sh hetzner-lab ../dmf-infra/k3s-lab-bootstrap/playbooks/696-cms-authentik-api.yml
```

## Emergency: If Token is Leaked

1. **Revoke immediately:**
   ```bash
   kubectl exec -n authentik deploy/authentik-server -- ak shell << 'EOF'
   from authentik.core.models import Token
   Token.objects.filter(intent="api", description="dmf-cms").delete()
   print("Token revoked")
   EOF
   ```

2. **Generate new token** (see Quarterly Rotation above)

3. **Report incident** to security team

## Audit

All token operations are logged:
- Bootstrap creation: Ansible playbook output (check `/var/log/ansible/696-*.log`)
- Token reads: K8s audit log (see `kubectl logs -n kube-system kube-apiserver`)
- Token rotations: Documented in this file + OpenBao audit trail
```

#### Step 4: Enable Kubernetes Audit Logging for kubectl exec

**File to modify:** `k3s-lab-bootstrap/roles/base/k3s-bootstrap/files/k3s-audit-policy.yaml`

Add exec rule to audit policy:

```yaml
---
apiVersion: audit.k8s.io/v1
kind: Policy
rules:
# Audit all kubectl exec commands in critical namespaces
- level: RequestResponse
  verbs: ["create"]
  resources: ["pods/exec"]
  namespaces: ["authentik", "openbao", "dmf-cms", "awx"]
  omitStages:
  - RequestReceived

# Audit all pod access in kube-system
- level: Metadata
  verbs: ["get", "list"]
  resources: ["pods"]
  namespaces: ["kube-system"]
  omitStages:
  - RequestReceived

# Default: log all
- level: Metadata
  omitStages:
  - RequestReceived
```

**File to modify:** `k3s-lab-bootstrap/roles/base/k3s-bootstrap/tasks/main.yml`

Add to k3s systemd unit to enable audit logging:

```yaml
    - name: Enable k3s audit logging
      ansible.builtin.lineinfile:
        path: /etc/systemd/system/k3s.service
        regexp: "^ExecStart="
        line: 'ExecStart=/usr/local/bin/k3s server --audit-policy-file=/etc/k3s/audit-policy.yaml --audit-log-maxage=30 --audit-log-maxbackup=10 --audit-log-maxsize=100'
      notify: restart k3s
      register: _k3s_audit_updated

    - name: Copy audit policy to node
      ansible.builtin.copy:
        src: k3s-audit-policy.yaml
        dest: /etc/k3s/audit-policy.yaml
        mode: '0600'
      when: _k3s_audit_updated.changed
```

---

### Testing & Validation

#### 1. Verify RBAC Blocks Unauthorized Access

```bash
# Try to exec as unauthorized user (should fail)
kubectl config set-context test-user --cluster=default --user=test
kubectl -n authentik exec deploy/authentik-server -- /bin/bash

# Expected error: "Error from server (Forbidden): pods ... subresource "exec" is forbidden"
```

#### 2. Verify Authorized Users Can Still Access

```bash
# As authorized operator
kubectl -n authentik exec deploy/authentik-server -- ak shell << 'EOF'
print("Access granted")
EOF
```

#### 3. Verify Audit Log Records exec Attempts

```bash
# Check k3s audit log
sudo tail -f /var/log/kubernetes/audit.log | grep "subresource: exec"

# Should see all exec attempts logged with timestamp and user
```

#### 4. Verify Token Can Be Read from OpenBao

```bash
# Simulate what 696 playbook does
bao kv get secret/apps/authentik/runtime

# Should return cms_api_token without needing shell access
```

---

## Issue #4: HIGH — No NetworkPolicy for Secrets Lateral Movement

**Status:** Completed. The OpenBao network boundary is now enforced by `roles/base/network-policies/` and `playbooks/vertical-security/120-network-policies.yml`; live probes on 2026-05-01 confirmed OpenBao is reachable from `openbao` and refused from `default`.

### Problem Statement

**Location:** Entire `k3s-lab-bootstrap/roles/stack/` directory (no NetworkPolicy found)

**Current Behavior:**
- No Kubernetes NetworkPolicy deployed
- All pods can reach all services
- Compromised pod in `default` namespace can reach OpenBao on port 8200
- No egress restrictions

**Impact:**
- Lateral movement attack: app pod → OpenBao
- Credential theft: sniff AppRole secret_id from network
- Denial of service: flood OpenBao with requests
- **CVSS: 8.6 (High)** — Network-adjacent attacker can reach all services

**Why It's This Way:**
NetworkPolicy is optional in Kubernetes. Lab environment prioritized ease of deployment over defense-in-depth.

---

### Remediation Steps

#### Step 1: Create Default-Deny NetworkPolicy

**File to create:** `k3s-lab-bootstrap/roles/base/network-policies/tasks/main.yml`

```yaml
---
- name: Deploy default-deny NetworkPolicies
  hosts: k3s_control[0]
  gather_facts: false

  tasks:
    - name: Create network-policies namespace
      kubernetes.core.k8s:
        kubeconfig: /etc/rancher/k3s/k3s.yaml
        state: present
        definition:
          apiVersion: v1
          kind: Namespace
          metadata:
            name: network-policies

    - name: Deploy default-deny ingress policy
      kubernetes.core.k8s:
        kubeconfig: /etc/rancher/k3s/k3s.yaml
        state: present
        definition:
          apiVersion: networking.k8s.io/v1
          kind: NetworkPolicy
          metadata:
            name: default-deny-ingress
            namespace: "{{ item }}"
          spec:
            podSelector: {}
            policyTypes:
            - Ingress
      loop:
        - authentik
        - openbao
        - dmf-cms
        - awx
        - prometheus
        - monitoring
        - external-secrets

    - name: Deploy default-deny egress policy
      kubernetes.core.k8s:
        kubeconfig: /etc/rancher/k3s/k3s.yaml
        state: present
        definition:
          apiVersion: networking.k8s.io/v1
          kind: NetworkPolicy
          metadata:
            name: default-deny-egress
            namespace: "{{ item }}"
          spec:
            podSelector: {}
            policyTypes:
            - Egress
      loop:
        - authentik
        - openbao
        - dmf-cms
        - awx
        - prometheus
        - monitoring
        - external-secrets
```

#### Step 2: Create Allow Rules for OpenBao

**File to create:** `k3s-lab-bootstrap/roles/base/network-policies/files/openbao-allow-policy.yaml`

```yaml
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-from-eso
  namespace: openbao
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: openbao
  policyTypes:
  - Ingress
  ingress:
  # Allow ESO
  - from:
    - namespaceSelector:
        matchLabels:
          name: external-secrets
    - podSelector:
        matchLabels:
          app.kubernetes.io/name: external-secrets
    ports:
    - protocol: TCP
      port: 8200
  # Allow app-admin-facts
  - from:
    - namespaceSelector:
        matchLabels:
          name: kube-system
    ports:
    - protocol: TCP
      port: 8200
  # Allow Kubernetes API (for health checks)
  - from:
    - namespaceSelector:
        matchLabels:
          name: kube-system
    ports:
    - protocol: TCP
      port: 8200
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-dns-egress
  namespace: openbao
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: openbao
  policyTypes:
  - Egress
  egress:
  # DNS
  - to:
    - namespaceSelector: {}
    ports:
    - protocol: UDP
      port: 53
  # NTP (if needed)
  - to:
    - namespaceSelector: {}
    ports:
    - protocol: UDP
      port: 123
```

#### Step 3: Create Allow Rules for ESO

**File to create:** `k3s-lab-bootstrap/roles/base/network-policies/files/eso-allow-policy.yaml`

```yaml
---
apiVersion: networking.k8s.io/v1
kind: NetworkPolicy
metadata:
  name: allow-eso-to-openbao
  namespace: external-secrets
spec:
  podSelector:
    matchLabels:
      app.kubernetes.io/name: external-secrets
  policyTypes:
  - Egress
  egress:
  # Egress to OpenBao only
  - to:
    - namespaceSelector:
        matchLabels:
          name: openbao
    ports:
    - protocol: TCP
      port: 8200
  # DNS
  - to:
    - namespaceSelector: {}
    ports:
    - protocol: UDP
      port: 53
```

---

### Testing & Validation

```bash
# Verify default-deny is in place
kubectl get networkpolicies -n openbao
kubectl get networkpolicies -n external-secrets

# Try to reach OpenBao from unauthorized pod (should timeout)
kubectl run test-pod --image=alpine --restart=Never -n default -- sleep 3600
kubectl exec -it test-pod -n default -- nc -zv openbao.openbao.svc.cluster.local 8200

# Expected: Connection timeout
```

Live validation on 2026-05-01:

- `openbao` namespace probe returned `HTTP/1.1 200 OK` from `https://openbao.openbao.svc.cluster.local:8200/v1/sys/health`
- `default` namespace probe returned `Connection refused`

---

## Issue #5: HIGH — No Kubernetes Audit Logging Configuration

**Status:** Completed. The k3s control-plane playbook now installs a kube-apiserver audit policy, writes logs to `/var/log/kubernetes/audit.log`, and exposes an optional archival hook when `audit_log_s3_bucket` is defined. Live validation on 2026-05-01 confirmed the audit log is populated on the Hetzner cluster.

### Problem Statement

**Location:** `k3s-lab-bootstrap/` (entire project, no audit logging configured)

**Current Behavior:**
- k3s running with default audit policy (logs almost nothing)
- API calls to OpenBao, Authentik, AWX not logged
- Token creation events invisible
- RBAC changes not recorded
- No immutable audit trail

**Impact:**
- **No forensics capability** after security incident
- Compliance violations (PCI-DSS, SOC 2 require audit logs)
- Insider threats undetected
- **CVSS: 7.5 (High)** — Security events undocumented

---

### Remediation Steps

#### Step 1: Install kube-apiserver audit logging

**Files to modify:** `k3s-lab-bootstrap/playbooks/300-k3s.yml`, `k3s-lab-bootstrap/roles/base/k3s/templates/k3s-audit-logging.conf.j2`

The k3s playbook now renders a systemd drop-in that adds the audit policy and audit log arguments to kube-apiserver, plus a policy file at `/etc/k3s/audit-policy.yaml`.

#### Step 2: Add optional archival when a bucket is configured

**Files to modify:** `k3s-lab-bootstrap/roles/base/audit-log-archival/`

```yaml
---
- name: Archive audit logs to immutable storage
  hosts: k3s_control
  become: true
  gather_facts: true

  tasks:
    - name: Install awscli for S3 uploads
      ansible.builtin.package:
        name: awscli
        state: present
      when: audit_log_s3_bucket is defined

    - name: Upload audit logs daily to S3
      ansible.builtin.cron:
        name: "Archive k3s audit logs"
        minute: "0"
        hour: "2"
        job: "tar czf /tmp/audit-{{ ansible_date_time.date }}.tar.gz /var/log/kubernetes/audit.log && aws s3 cp /tmp/audit-*.tar.gz s3://{{ audit_log_s3_bucket }}/audit/ --sse-c-algorithm AES256 && rm /tmp/audit-*.tar.gz"
        user: root
        state: present
      when: audit_log_s3_bucket is defined

    - name: Set S3 bucket for audit logs (MFA delete + versioning)
      ansible.builtin.command: |
        aws s3api put-bucket-versioning \
          --bucket {{ audit_log_s3_bucket }} \
          --versioning-configuration Status=Enabled
      when: audit_log_s3_bucket is defined
      changed_when: false
```

When `audit_log_s3_bucket` is not defined, the archival role exits without changing the host.

---

### Testing & Validation

```bash
# Verify k3s is active
sudo systemctl is-active k3s

# Confirm audit log exists
sudo ls -l /var/log/kubernetes/audit.log

# Generate a Kubernetes API event
sudo k3s kubectl get pods -A >/dev/null

# Confirm the log contains audit events
sudo tail -n 3 /var/log/kubernetes/audit.log
```

Live validation on 2026-05-01:

- `/var/log/kubernetes/audit.log` contains `audit.k8s.io/v1` events after `kubectl get pods -A`
- The k3s service returned to `active` after the rollout
- The optional archival role no-ops cleanly when `audit_log_s3_bucket` is unset

---

## Issue #6: HIGH — No Automated AppRole Secret Rotation

**Status:** Completed and rolled out on 2026-05-01.

### Problem Statement

**Location:** `k3s-lab-bootstrap/roles/base/external-secrets/tasks/main.yml:53-66`

**Current Behavior:**
```yaml
# AppRole secret_id created once, stored in K8s Secret, never rotated
secret_id_ttl = 720h  # 30 days until expiry
```

**Impact:**
- Leaked secret_id valid for **30 days**
- No automated refresh before expiry
- If ESO pod loses Secret access, cannot renew (stale credential)
- **CVSS: 8.1 (High)** — Compromised credential useful for weeks

---

### Implemented In Repo

- `playbooks/vertical-orchestration/110-eso-secret-rotation.yml` rotates the ESO AppRole secret_id on each lifecycle run.
- `lifecycle-provision.yml` imports the rotation play immediately after `100-eso.yml`.
- The play logs into OpenBao as `ops-admin`, mints a fresh `secret_id`, patches the `openbao-approle` Secret, persists the new `eso_secret_id` back to the operator break-glass JSON, and restarts `external-secrets`.
- `roles/stack/operator/openbao/tasks/main.yml` now grants `auth/approle/role/external-secrets/secret-id` in the `app-runtime-writer` policy during bootstrap.

---

Live validation on 2026-05-01:

- `playbooks/vertical-orchestration/110-eso-secret-rotation.yml` completed successfully against the Hetzner cluster
- The live OpenBao `app-runtime-writer` policy now includes `auth/approle/role/external-secrets/secret-id`
- The rotated `eso_secret_id` was persisted back to `<secure-store>/openbao-breakglass/hetzner-lab/openbao-keys-automation.json`
- `external-secrets` restarted cleanly after the rotation

---

## Issue #7: HIGH — AWX Admin Creds for Token Bootstrap

**Status:** Completed and rolled out on 2026-05-01.

### Problem Statement

`697-cms-awx-token.yml` previously relied on the AWX admin Secret as the only auth path for token creation. That worked, but it made token bootstrap depend on admin credentials even though the AWX service-user password was already persisted in OpenBao by the AWX integration role.

### Implemented In Repo

- `playbooks/697-cms-awx-token.yml` now prefers the durable `awx_svc_password` from OpenBao for token creation.
- If the service-user password is unavailable or stale, the playbook falls back to the AWX admin Secret as a recovery path.
- The AWX integration role already persists `awx_svc_password` and `awx_svc_token` to the OpenBao runtime secret, so reruns can avoid the admin Secret in the normal path.

### Live Validation on 2026-05-01

- `697-cms-awx-token.yml` reran successfully with the service-user auth path available from OpenBao.
- The DMF Console runtime Secret still received the expected `awxApiToken`.
- The playbook remained idempotent with the existing skip gate.

---

## Follow-Up Findings (Re-Review 2026-05-01)

The strict follow-up review on 2026-05-01 surfaced 12 additional findings. Some are pre-existing issues that were in scope but missed (e.g., N-6 is NetworkPolicy scope incompleteness from Issue #4). Others are **regressions** created by the interaction of two correct fixes (e.g., N-1 is the audit-log + operator-userpass interaction). All are documented below with the same rigor as the original seven issues.

### N-1: CRITICAL — Audit Log Captures Plaintext Secrets in Command Argv

**Status:** Phase 1 (hotfix) completed on 2026-05-01. Phase 2 (refactor) in progress.

**Companion document:** `docs/SECURITY-REMEDIATION-N1-AUDIT-LEAK.md` (430 lines, full forensics and Phase 1/2 roadmap)

**Problem Statement**

Issue #5 enabled k3s audit logging at `RequestResponse` level for `pods/exec` and `secrets` operations in security-critical namespaces. At the same time, the migration from the root token to `ops-admin` userpass auth meant every operator role passes the userpass password as `kubectl exec` argv, and then captures the resulting OpenBao client token as `BAO_TOKEN=…` argv on subsequent operations. The combination silently turned `/var/log/kubernetes/audit.log` into a plaintext credential database:

**Locations (credentials in audit log):**
- `roles/common/app-admin-facts/tasks/main.yml:59-86` — `password=…` + `BAO_TOKEN=…`
- `roles/stack/operator/authentik/tasks/main.yml:71-115, 205-216, 733-744` — same pattern
- `roles/stack/operator/awx-integration/tasks/main.yml:140-168, 175-186, 200-260, 922-942` — same pattern
- `roles/stack/operator/openbao/tasks/main.yml:612, 632, 652, 672, 692, 717, 777, 826, 850, 873, 893, 957, 977, 1000, 1020, 1111, 1131, 1185` — root token in argv (first-init only, subject to N-2)
- `roles/stack/operator/cms/tasks/main.yml:199-244` — password + token
- `roles/stack/operator/forgejo-bootstrap/tasks/main.yml:430-478` — password + token
- `roles/stack/operator/netbox/tasks/main.yml:54-82+` — password + token
- `playbooks/696-cms-authentik-api.yml:213-228, 239-254, 279-309, 311-327` — password + token + Secret patch with `authentikApiToken`
- `playbooks/697-cms-awx-token.yml:79-103, 105-126, 595-621, 623-643, 668-699, 725-741` — password + token + Secret patch with `awxApiToken`
- `playbooks/vertical-orchestration/110-eso-secret-rotation.yml:69-100, 111-148` — password + token + Secret update with `secret_id`

**Impact**

- `ops-admin` password (persistent, never rotated) visible in every operator-role bootstrap
- OpenBao client tokens (1h TTL, but renewable) and app tokens (authentik, awx) logged as plaintext
- Anyone with read access to `/var/log/kubernetes/audit.log` or S3 archival bucket can replay all app credentials
- **CVSS: 8.4 (High)** — network-adjacent if S3 bucket is internet-accessible

**Remediation (Phase 1 — Done)**

Downgrade audit policy for `pods/exec` in `openbao` namespace and `secrets` write-body cluster-wide from `RequestResponse` to `Metadata`. The hotfix stops new entries; Phase 2 refactors the exec pattern.

**Remediation (Phase 2 — In Progress)**

Move credentials from argv to stdin:
1. `password={{ ops_admin_password }}` → `bao login -method=userpass username=ops-admin -` (password on stdin)
2. `BAO_TOKEN=…` → `VAULT_TOKEN=…` set inside a single `kubectl exec` shell that logs in, does work, and exits (token lives in memory only)
3. Secret patch bodies → use `kubernetes.core.k8s` with `stringData` instead of string interpolation in `-p '{"stringData": …}'` JSON

**Files to refactor (Phase 2):**
- `roles/common/app-admin-facts/tasks/main.yml` — 10 lines
- `roles/common/dmf-born-inventory/tasks/main.yml` — 20 lines
- `roles/stack/operator/authentik/tasks/main.yml` — 50 lines (3 exec calls + 1 password cached)
- `roles/stack/operator/awx-integration/tasks/main.yml` — 80 lines (4 exec calls + 1 password cached)
- `roles/stack/operator/cms/tasks/main.yml` — 30 lines
- `roles/stack/operator/forgejo-bootstrap/tasks/main.yml` — 40 lines
- `roles/stack/operator/netbox/tasks/main.yml` — 40 lines
- `playbooks/696-cms-authentik-api.yml` — 60 lines (3 exec calls + 1 Secret patch)
- `playbooks/697-cms-awx-token.yml` — 100 lines (6 exec calls + Secret patch)
- `playbooks/vertical-orchestration/110-eso-secret-rotation.yml` — 50 lines

**Effort:** 4–6 hours to refactor all 10 callers; sequential once one caller is proven. Write one caller, test, then template for the rest.

**Priority:** HIGH (Phase 1 done; Phase 2 needed before audit log reaches production archival)

---

### N-2: HIGH — Root Token Revoke Can Silently Fail

**Status:** Pending

**Problem Statement**

`roles/stack/operator/openbao/tasks/main.yml:1186-1188` revokes the root token at end of bootstrap:

```yaml
- name: Revoke root token (post-bootstrap security gate)
  ansible.builtin.uri:
    url: "{{ _openbao_api_addr }}/v1/auth/token/revoke-self"
    method: POST
    headers:
      X-Vault-Token: "{{ _openbao_root_token }}"
    status_code: [204, 400]
  when:
    - _openbao_initialized | bool
    - openbao_dispose_root_token | default(true) | bool
  no_log: true
  register: _openbao_token_revoke
```

The `status_code: [204, 400]` accepts both success (204) and "already revoked / bad token" (400). But there's no assertion that revocation actually happened. If the OpenBao API is temporarily unreachable, or if the token is already revoked (idempotent rerun), or if there's a transient OpenBao error, the playbook proceeds without checking.

**Location:** `k3s-lab-bootstrap/roles/stack/operator/openbao/tasks/main.yml:1186-1200`

**Impact**

- If revocation fails silently and the root token is NOT revoked, the super-admin token remains usable
- Subsequent reruns of the playbook don't detect the failure and don't retry
- Unknown to operator whether break-glass is actually in place

**Remediation**

Add assertion after revoke task:

```yaml
- name: Assert root token revocation succeeded
  ansible.builtin.assert:
    that:
      - _openbao_token_revoke.status == 204
    fail_msg: "Root token revoke failed or already spent. Verify OpenBao is healthy and root token is actually revoked. Details: {{ _openbao_token_revoke }}"
  when: openbao_dispose_root_token | default(true) | bool
```

**Effort:** 5 minutes (add 2 tasks)

**Priority:** HIGH (root-token disposal is a security gate; failures should be explicit)

---

### N-3: MEDIUM — Root Token Briefly Persisted in /tmp Before Redaction

**Status:** Pending

**Problem Statement**

`roles/stack/operator/openbao/tasks/main.yml:308-316` writes the root token (and Shamir shares) to a temporary file before redacting it in the break-glass JSON at `:1159-1166`:

```yaml
- name: Write break-glass JSON to temp location
  copy:
    content: |
      {
        "root_token": "{{ _openbao_root_token }}",
        "shamir_keys": {{ _openbao_shamir_shares | to_json }}
      }
    dest: /tmp/openbao-keys-temp.json
```

If the playbook crashes between write (line 308) and redaction (line 1159), the root token persists unencrypted on the node's `/tmp` filesystem. `/tmp` is often mounted with `noexec`, but it is readable by root and may survive reboot if the system crashes.

**Location:** `k3s-lab-bootstrap/roles/stack/operator/openbao/tasks/main.yml:308-316` (write) and `:1159-1166` (redaction)

**Impact**

- Root token accessible to any process running as root on the control node
- Persists for the duration of the playbook run (up to 30+ minutes)
- Unencrypted and unaudited

**Remediation**

Use a temporary file with restricted permissions and a cleanup handler:

```yaml
- name: Create temporary secure scratch directory
  ansible.builtin.tempfile:
    state: directory
    prefix: openbao-keys-
    suffix: -{{ inventory_hostname }}
  register: _openbao_temp_dir
  no_log: true

- name: Write break-glass to secure temp location
  copy:
    content: |
      {
        "root_token": "{{ _openbao_root_token }}",
        "shamir_keys": {{ _openbao_shamir_shares | to_json }}
      }
    dest: "{{ _openbao_temp_dir.path }}/keys.json"
    mode: '0600'
  no_log: true

- name: Redact root token from break-glass JSON
  copy:
    content: |
      {
        "created_at": "{{ ansible_date_time.iso8601 }}",
        "shamir_keys": {{ _openbao_shamir_shares | to_json }},
        "threshold": 3,
        "total_shares": {{ _openbao_shamir_shares | length }},
        "note": "Root token disposed post-bootstrap. Use Shamir shares to unseal."
      }
    dest: <secure-store>/openbao-breakglass/hetzner-lab/openbao-keys-automation.json
    mode: '0600'
  no_log: true

- name: Clean up secure temp directory
  ansible.builtin.file:
    path: "{{ _openbao_temp_dir.path }}"
    state: absent
  when: _openbao_temp_dir is defined
```

**Effort:** 15 minutes (3 task edits + 1 cleanup task)

**Priority:** MEDIUM (window is brief; mitigated by fact that token is revoked immediately after)

---

### N-4: MEDIUM — Service Passwords Cached in /tmp and Never Rotated

**Status:** Pending

**Problem Statement**

Several operator roles use Ansible's `lookup('password', '/tmp/…')` to generate or store passwords:

```yaml
- set_fact:
    _authentik_admin_password: "{{ lookup('password', '/tmp/authentik-admin-password length=20') }}"
```

This caches the password in `/tmp` and never rotates it. The file persists across playbook runs, so if the role is re-run, it reuses the same password. If the password needs to change (e.g., due to a compromise or rotation policy), the playbook doesn't detect that the file is stale and doesn't generate a new one.

**Locations:**
- `roles/stack/operator/authentik/tasks/main.yml` — authentik admin password
- `roles/stack/operator/awx-integration/tasks/main.yml` — awx service user password
- Similar patterns in NetBox, Forgejo, CMS roles

**Impact**

- Service passwords unencrypted in `/tmp` across reboots
- Password persists in `/tmp` longer than session lifetime if operator forgets to clean up
- No way to rotate password without manually deleting `/tmp/…` file
- **CVSS: 5.5 (Medium)** — local read of unencrypted password

**Remediation**

1. Generate passwords using Ansible's `password` module or `community.general.random_string` and pass directly to OpenBao on first run.
2. Store persistent passwords in OpenBao, not `/tmp`.
3. On subsequent runs, read from OpenBao instead of `/tmp`.

Example:

```yaml
- name: Generate authentik admin password (first run only)
  ansible.builtin.set_fact:
    _authentik_admin_password: "{{ lookup('community.general.random_string', length=20, special=false) }}"
  no_log: true
  when: _authentik_first_run | default(false) | bool

- name: Persist to OpenBao
  ansible.builtin.uri:
    url: "{{ _openbao_api_addr }}/v1/secret/data/apps/authentik/bootstrap"
    method: POST
    headers:
      X-Vault-Token: "{{ _cms_authentik_openbao_client_token }}"
    body_format: json
    body:
      data:
        admin_password: "{{ _authentik_admin_password }}"
    status_code: [200, 201]
  no_log: true
  when: _authentik_first_run | default(false) | bool

- name: Read from OpenBao on subsequent runs
  ansible.builtin.set_fact:
    _authentik_admin_password: "{{ lookup('hashi_vault', 'secret=secret/data/apps/authentik/bootstrap:admin_password') }}"
  no_log: true
  when: not _authentik_first_run | default(false) | bool
```

**Effort:** 1 hour per operator role (5 callers = 5 hours total). Template one, copy to rest.

**Priority:** MEDIUM (requires OpenBao client-token pattern from N-1 Phase 2; good to do in parallel)

---

### N-5: MEDIUM — ESO Smoke Test Leaks Decoded Secret to Debug Output

**Status:** Pending

**Problem Statement**

`roles/base/external-secrets/tasks/main.yml:xxx` (lines to be confirmed) runs a smoke test that fetches a test secret from OpenBao and verifies it was synced to K8s:

```yaml
- name: Verify ESO can fetch secret
  ansible.builtin.debug:
    msg: "Secret synced successfully: {{ _test_secret }}"
```

The `debug` module prints the full decoded secret value. If the playbook is logged (e.g., to a file or sent to a logging system), the secret is in plaintext in the logs.

**Location:** `roles/base/external-secrets/tasks/main.yml` (smoke-test debug task)

**Impact**

- Decoded secret visible in Ansible playbook logs
- If logs are forwarded to a central logging system or stored, the secret is persistent
- **CVSS: 4.3 (Medium)** — local log read

**Remediation**

Add `no_log: true` to the debug task and/or verify the secret was synced by checking its presence without printing its value:

```yaml
- name: Verify ESO can fetch secret
  kubernetes.core.k8s_info:
    kubeconfig: /etc/rancher/k3s/k3s.yaml
    kind: Secret
    name: eso-test-secret
    namespace: external-secrets
  register: _eso_test_result
  failed_when:
    - _eso_test_result.resources | length == 0
  no_log: true

- name: Log success without printing secret value
  ansible.builtin.debug:
    msg: "Secret synced successfully ({{ _eso_test_result.resources[0].metadata.uid }})"
  no_log: false
```

**Effort:** 10 minutes (edit 1 task)

**Priority:** MEDIUM (low-severity leak, easy fix)

---

### N-6: MEDIUM — NetworkPolicy Only on openbao Namespace; Other Namespaces Wide-Open

**Status:** Partial; pending expansion

**Problem Statement**

Issue #4 promised "NetworkPolicy boundary around secrets infrastructure" and deployed `default-deny` + `allow-from-eso-and-peers` rules. However, the implementation only covers the `openbao` namespace. The original scope included `authentik`, `dmf-cms`, `awx`, `external-secrets`, `monitoring`, and `prometheus` — all of which are still wide-open to lateral movement.

**Locations:**
- `roles/base/network-policies/tasks/main.yml` — only creates policies for `openbao`
- Missing: `authentik`, `dmf-cms`, `awx`, `external-secrets`, `monitoring`, `prometheus`

**Impact**

- Compromised pod in `default` can reach `authentik`, `awx`, `dmf-cms`, etc.
- No egress restrictions on monitoring tools (prometheus, loki, promtail can reach anywhere)
- **CVSS: 7.1 (High)** — network-adjacent lateral movement unblocked

**Remediation**

Expand the network-policies role to deploy `default-deny` + `allow-*` rules for all seven namespaces. Sequence the allow rules carefully:

```yaml
- name: Deploy default-deny to all critical namespaces
  kubernetes.core.k8s:
    kubeconfig: /etc/rancher/k3s/k3s.yaml
    state: present
    definition:
      apiVersion: networking.k8s.io/v1
      kind: NetworkPolicy
      metadata:
        name: default-deny-ingress
        namespace: "{{ item }}"
      spec:
        podSelector: {}
        policyTypes:
          - Ingress
  loop:
    - authentik
    - dmf-cms
    - awx
    - external-secrets
    - monitoring
    - prometheus
    - openbao

- name: Deploy allow-rules for authentik
  kubernetes.core.k8s:
    kubeconfig: /etc/rancher/k3s/k3s.yaml
    state: present
    definition:
      apiVersion: networking.k8s.io/v1
      kind: NetworkPolicy
      metadata:
        name: allow-ingress-from-dmf-cms
        namespace: authentik
      spec:
        podSelector:
          matchLabels:
            app.kubernetes.io/name: authentik
        policyTypes:
          - Ingress
        ingress:
          - from:
              - namespaceSelector:
                  matchLabels:
                    kubernetes.io/metadata.name: dmf-cms
            ports:
              - protocol: TCP
                port: 9000  # Authentik API
          - from:
              - namespaceSelector:
                  matchLabels:
                    kubernetes.io/metadata.name: kube-system
            ports:
              - protocol: TCP
                port: 9000  # k3s system probes
```

Repeat for awx, dmf-cms, external-secrets. Define egress rules for monitoring (prometheus → all/9090, loki ← promtail).

**Effort:** 2 hours (define 6 namespace pairs; template across)

**Priority:** MEDIUM-HIGH (scope gap from Issue #4; medium effort, high impact)

---

### N-7: MEDIUM — Authentik API Token Has No Expiry

**Status:** Pending

**Problem Statement**

`playbooks/696-cms-authentik-api.yml:846` creates an Authentik API token with `expiring=False`:

```python
token = Token.objects.create(
  user=user,
  intent="api",
  description="dmf-cms",
  expiring=False,  # UPDATE: Set expiry later
)
```

The comment says "UPDATE: Set expiry later" but the update was never done. Tokens with `expiring=False` have unlimited lifetime. If the token is compromised, it remains valid indefinitely.

**Location:** `playbooks/696-cms-authentik-api.yml:846` + the corresponding token-read path in `roles/stack/operator/authentik/tasks/main.yml`

**Impact**

- Compromised token valid forever (unless manually revoked)
- No automatic rotation window
- **CVSS: 5.4 (Medium)** — persistent credential with no TTL

**Remediation**

Set an expiry on token creation and implement a rotation playbook:

```python
token = Token.objects.create(
  user=user,
  intent="api",
  description="dmf-cms",
  expiring=True,
  expires=utc_now() + timedelta(days=90),  # 90-day expiry, rotate quarterly
)
```

Then add a quarterly rotation playbook (e.g., `playbooks/696-cms-authentik-api-rotation.yml`) that:
1. Revokes old token
2. Creates new token with 90-day expiry
3. Persists to OpenBao and K8s Secret

**Effort:** 1 hour (edit token-creation code + add rotation playbook)

**Priority:** MEDIUM (easy fix; mitigated by fact that token is only used by DMF Console)

---

### N-8: MEDIUM — Zero Container Security Hardening

**Status:** Pending

**Problem Statement**

None of the Kubernetes Deployments / Pods in this repo have container security contexts. No `runAsNonRoot`, no `allowPrivilegeEscalation: false`, no `readOnlyRootFilesystem: true`, no `securityContext.capabilities.drop`.

**Locations (sample):**
- `roles/stack/operator/openbao/tasks/main.yml` — OpenBao Helm values (no `securityContext`)
- `roles/stack/operator/authentik/tasks/main.yml` — Authentik Helm values (no `securityContext`)
- `roles/stack/operator/awx-integration/tasks/main.yml` — AWX Operator Helm values (no `securityContext`)
- All app Helm charts deployed via `kubernetes.core.k8s`

**Impact**

- Containers run as root (if image doesn't set USER)
- Privilege escalation possible even inside container
- Container can write to root filesystem (tmpfs, container image, logs)
- **CVSS: 5.3 (Medium)** — in-container privilege escalation + lateral movement

**Remediation**

Add `securityContext` to all Helm values and raw K8s manifests:

```yaml
spec:
  securityContext:
    runAsNonRoot: true
    runAsUser: 1000
    fsGroup: 1000
    capabilities:
      drop:
        - ALL
    readOnlyRootFilesystem: true  # if app supports it; tmpfs for writable paths
  containers:
  - name: openbao
    image: ...
    securityContext:
      allowPrivilegeEscalation: false
      capabilities:
        drop:
          - ALL
      readOnlyRootFilesystem: true
    volumeMounts:
    - name: tmp
      mountPath: /tmp
    - name: data
      mountPath: /openbao/data
  volumes:
  - name: tmp
    emptyDir: {}
  - name: data
    persistentVolumeClaim:
      claimName: openbao-data
```

Test each app to verify it doesn't crash when `readOnlyRootFilesystem: true`.

**Effort:** 2–3 hours (10 apps × 10–15 min each; test each one)

**Priority:** MEDIUM (hardens container boundary; low overhead)

---

### N-9: LOW-MEDIUM — Landing Page Uses Rolling Image Tags

**Status:** Pending

**Problem Statement**

`roles/landing-page/tasks/main.yml` deploys the landing page with sidecar containers using rolling tags:

```yaml
containers:
- name: curl-helper
  image: curlimages/curl:latest
- name: nginx-proxy
  image: nginx:alpine
```

Rolling tags (`latest`, `alpine`) are not pinned to a digest. The image can change on every container creation, introducing supply-chain risk and making debugging difficult.

**Location:** `roles/landing-page/tasks/main.yml` (sidecar image definitions)

**Impact**

- Image changes without version control
- CVE in upstream image appears automatically (good for patching; bad if image is compromised)
- Difficult to reproduce issues or rollback
- **CVSS: 3.9 (Low)** — supply-chain risk, but images are not security-critical

**Remediation**

Pin images to specific tags and compute digests:

```bash
# Find the digest for a specific tag
docker pull curlimages/curl:7.92.0
docker inspect curlimages/curl:7.92.0 | jq .[].RepoDigests

# Result: curlimages/curl@sha256:abcd1234…
```

Update the manifests:

```yaml
containers:
- name: curl-helper
  image: curlimages/curl:7.92.0@sha256:abcd1234…
- name: nginx-proxy
  image: nginx:1.24-alpine@sha256:efgh5678…
```

**Effort:** 30 minutes (find 2 images, update 2 references)

**Priority:** LOW-MEDIUM (supply-chain best practice; low effort)

---

### N-10: LOW — Three validate_certs=false Without Justification

**Status:** Pending

**Problem Statement**

Three tasks disable HTTPS certificate validation:

```yaml
- name: Test DMF Console connectivity
  uri:
    url: https://dmf-cms.lab/
    validate_certs: false
```

Certificate validation is disabled for convenience, but without a comment explaining why. If the justification is "self-signed cert," the right fix is to provide the CA bundle, not to skip validation.

**Locations:**
- `roles/common/dmf-born-inventory/tasks/main.yml:xxx` — DMF console connectivity test
- `roles/stack/operator/awx-integration/tasks/main.yml:xxx` — AWX OIDC endpoint test
- `roles/stack/operator/cms/tasks/main.yml:xxx` — CMS smoke test

**Impact**

- Open to MITM attacks during bootstrap (low-probability, but high-impact)
- Playbook doesn't fail if certificate is invalid (may hide configuration errors)
- **CVSS: 3.4 (Low)** — local network only; requires MITM

**Remediation**

For each task, either:
1. **If self-signed:** Provide the CA bundle and set `validate_certs` to the path:
   ```yaml
   validate_certs: /etc/ssl/certs/openbao-ca.pem
   ```
2. **If prod cert:** Ensure the certificate is trusted by the system and leave `validate_certs: true` (default)
3. **If testing only:** Add a comment explaining why:
   ```yaml
   validate_certs: false  # Development only; self-signed cert used in lab
   ```

**Effort:** 30 minutes (identify CA for each, update 3 tasks)

**Priority:** LOW (development environment; low risk)

---

### N-11: LOW — JSON Interpolation in kubectl patch Fragile to Special Characters

**Status:** Pending

**Problem Statement**

Several playbooks use string interpolation in `kubectl patch -p '{"stringData": {"key": "{{ value }}"}}' JSON:

```yaml
- name: Persist DMF Console runtime secret
  ansible.builtin.command:
    argv:
      - kubectl
      - patch
      - secret
      - dmf-cms-runtime
      - -p
      - '{"stringData":{"authentikApiToken":"{{ authentik_api_token }}"}}'
```

If the token contains special characters (quotes, backslashes, etc.), the JSON becomes invalid and the patch fails. The right approach is to use the `kubernetes.core.k8s` module with a YAML definition, which handles escaping automatically.

**Locations:**
- `playbooks/696-cms-authentik-api.yml:xxx` — authentikApiToken patch
- `playbooks/697-cms-awx-token.yml:xxx` — awxApiToken patch

**Impact**

- Playbook fails if credential contains `"`, `\`, or other JSON-special characters
- Manual debugging required to identify the issue
- **CVSS: 1.0 (Info/Low)** — DoS for that specific credential, but not a direct security issue

**Remediation**

Use `kubernetes.core.k8s` with YAML definition:

```yaml
- name: Persist DMF Console runtime secret
  kubernetes.core.k8s:
    kubeconfig: /etc/rancher/k3s/k3s.yaml
    state: present
    definition:
      apiVersion: v1
      kind: Secret
      metadata:
        name: dmf-cms-runtime
        namespace: dmf-cms
      type: Opaque
      stringData:
        authentikApiToken: "{{ authentik_api_token }}"
        awxApiToken: "{{ awx_api_token }}"
  no_log: true
```

**Effort:** 30 minutes (refactor 2 patch calls)

**Priority:** LOW (edge case; easy fix)

---

### N-12: INFO — Rendered OpenBao Config in /tmp Never Used

**Status:** Pending (informational)

**Problem Statement**

`roles/stack/operator/openbao/tasks/main.yml` renders the OpenBao configuration template to `/tmp/openbao-config.hcl` but never uses it. The Helm chart contains the configuration inline.

**Location:** `roles/stack/operator/openbao/tasks/main.yml:xxx` (lines to be confirmed; task creates `/tmp/openbao-config.hcl`)

**Impact**

- Unused file in `/tmp` (minor clutter)
- Potential confusion about where config is actually stored
- No security impact

**Remediation**

Either:
1. **Delete the render task** if the Helm chart is the source of truth.
2. **Or:** Use the file to mount config via ConfigMap into the pod, and document why the two-source pattern exists.

**Effort:** 5 minutes (delete task or add comment)

**Priority:** INFO (no security impact; cleanup item)

---

## Summary of All Issues

| Priority | Issue # | Problem | Status | Est. Effort | File(s) |
|----------|---------|---------|--------|------------|---------|
| CRITICAL | #1 | OpenBao TLS disabled | Done | 2-3 hrs | openbao/tasks + cert-manager |
| CRITICAL | #2 | Root token persisted | Done | 1-2 hrs | openbao/tasks + break-glass JSON |
| CRITICAL | #3 | No RBAC on kubectl exec | Done | 1 hr | 696-cms-authentik-api.yml + SA/RoleBinding |
| CRITICAL | N-1 | Audit log leaks credentials | Phase 1 Done; Phase 2 In Progress | 4-6 hrs | 10+ operator roles + audit policy |
| HIGH | #4 | No NetworkPolicy | Done (partial) | 2 hrs | network-policies/ + 6 more namespaces |
| HIGH | #5 | No audit logging | Done | 1-2 hrs | k3s audit policy + archival |
| HIGH | #6 | No AppRole rotation | Done | 1-2 hrs | 110-eso-secret-rotation + openbao |
| HIGH | #7 | AWX admin creds | Done | 1 hr | 697-cms-awx-token + awx-integration |
| HIGH | N-2 | Root token revoke can fail silently | Pending | 5 min | openbao/tasks (1 task) |
| HIGH | N-6 | NetworkPolicy incomplete scope | Pending | 2 hrs | network-policies (expand to 6 more namespaces) |
| MEDIUM | N-3 | Root token in /tmp before redaction | Pending | 15 min | openbao/tasks (temp file handling) |
| MEDIUM | N-4 | Service passwords in /tmp, never rotated | Pending | 5 hrs | 5 operator roles (move to OpenBao) |
| MEDIUM | N-5 | ESO smoke test leaks secret | Pending | 10 min | external-secrets/tasks (1 task) |
| MEDIUM | N-7 | Authentik token no expiry | Pending | 1 hr | 696-cms-authentik-api (token creation + rotation playbook) |
| MEDIUM | N-8 | Zero container hardening | Pending | 2-3 hrs | All app Helm values + manifests (10+ apps) |
| LOW-MEDIUM | N-9 | Landing page rolling image tags | Pending | 30 min | landing-page/tasks (2 images) |
| LOW | N-10 | validate_certs=false without justification | Pending | 30 min | 3 tasks (add CA bundle or comment) |
| LOW | N-11 | JSON interpolation fragile to special chars | Pending | 30 min | 696, 697 playbooks (use kubernetes.core.k8s) |
| INFO | N-12 | Rendered config in /tmp unused | Pending | 5 min | openbao/tasks (delete or document) |

---

## Recommended Remediation Order

**All issues in Phase 1–7 are now complete and rolled out (as of 2026-05-01).** The following sequencing applies to remaining findings (N-1 Phase 2 and N-2 through N-12).

### Immediate (This Week)

**High-impact, low-effort fixes that close critical gaps:**

1. **N-2** (5 min) — Add assertion to root token revoke, ensure it fails loud if revocation doesn't complete
2. **N-5** (10 min) — Remove secret value from ESO smoke-test debug output (add `no_log: true`)
3. **N-12** (5 min) — Delete or document the unused `/tmp/openbao-config.hcl` render task
4. **N-10** (30 min) — Add CA bundle validation or document why `validate_certs: false` for 3 tasks
5. **N-11** (30 min) — Replace `kubectl patch -p '{"stringData": …}'` JSON with `kubernetes.core.k8s` module (2 tasks)

**Cumulative effort:** ~1.5 hours. **Impact:** Close root-token-revoke gap, eliminate ESO secret leak, normalize cert validation.

### This Sprint (Next 2–3 Days)

**N-1 Phase 2 refactor — move credentials from argv to stdin:**

6. **N-1 Phase 2 (4–6 hrs)** — Refactor 10 operator roles to pass `ops-admin` password via stdin and client tokens via `~/.vault-token` inside the exec shell
   - Start with `roles/common/app-admin-facts/tasks/main.yml` as a template (smallest)
   - Template to `dmf-born-inventory`, then the 5 operator roles (authentik, awx-integration, cms, forgejo-bootstrap, netbox)
   - Verify each refactored role still deploys cleanly on a test cluster
   - Finally, refactor the two playbooks (696, 697) that do DMF Console token creation
   - Last: refactor the rotation playbook (110-eso-secret-rotation.yml)

**Rationale:** N-1 Phase 2 is the highest-impact remaining fix. Once complete, credentials stop leaking to the audit log. Doing this early prevents the audit log from growing with more leaked credentials.

### Next Week

**Medium-effort, important-scope fixes:**

7. **N-4** (5 hrs) — Migrate service passwords from `/tmp/` caching to OpenBao storage
   - Can be done in parallel with N-1 Phase 2 or immediately after
   - Move `lookup('password', '/tmp/…')` → generate once, persist to OpenBao, read on subsequent runs
   - Applies to authentik, awx-integration, netbox, forgejo-bootstrap, cms

8. **N-6** (2 hrs) — Expand NetworkPolicy to all 7 namespaces (not just openbao)
   - Deploy default-deny + allow-rules for authentik, dmf-cms, awx, external-secrets, monitoring, prometheus
   - Test ingress/egress restrictions using temporary test pods

9. **N-3** (15 min) — Use `tempfile` module + cleanup handler for root-token temp file
   - Low risk; mitigated by fact that token is revoked at end of playbook

### Following Week

**Nice-to-have hardening:**

10. **N-7** (1 hr) — Add 90-day expiry to Authentik API token and implement rotation playbook

11. **N-8** (2–3 hrs) — Add container security contexts to all Helm values
    - `runAsNonRoot: true`, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`
    - Test each app for compatibility with read-only filesystem
    - Coordinate with app teams if any app needs writable paths (use emptyDir tmpfs)

12. **N-9** (30 min) — Pin landing-page sidecar images to digest-locked tags

### Testing & Validation Across All Phases

- After each phase, run a fresh cluster deployment (`bin/run-playbook.sh hetzner-lab site.yml`) and verify all apps come up healthy
- Run the audit-log tests: verify credentials are NOT in `/var/log/kubernetes/audit.log` after Phase 2 completes
- Run the NetworkPolicy tests: verify lateral movement is blocked after Phase 3 completes
- For container hardening: test each app with `readOnlyRootFilesystem: true` to find writable paths

### Dependency Chain

```
N-2 (5 min, standalone)
N-5 (10 min, standalone)
N-12 (5 min, standalone)
N-10 (30 min, standalone)
N-11 (30 min, standalone)
   ↓ (these are in parallel)
N-1 Phase 2 (4–6 hrs)  ← depends on audit policy from Phase 1 (already done)
N-4 (5 hrs, in parallel with N-1 Phase 2)
   ↓
N-6 (2 hrs, standalone after above)
N-3 (15 min, standalone)
   ↓
N-7 (1 hr, standalone)
N-8 (2–3 hrs, standalone)
N-9 (30 min, standalone)
```

**Critical path:** N-1 Phase 2 (4–6 hrs) blocks N-4. Everything else is parallelizable. **Estimated total effort to clear all 19 issues: 18–22 hours of development + 6–10 hours of testing across the team. Recommend spreading Phase 1–2 over 3–4 days, Phase 3 over 2 weeks.**

---

## Contact & Escalation

- **Security Team:** Contact for root cause post-mortems and incident response
- **Operator:** see private inventory for the on-call operator handle
- **Documentation Owner:** DevSecOps

---

**Document Version:** 1.0  
**Last Updated:** 2026-05-01  
**Review Cadence:** Quarterly
