# Repository Strategy and Lab Development Workflow

**Status:** Planning — pre-implementation reference  
**Date:** 2026-04-16  
**Context:** Original lab is gone. New lab being built on Hetzner ARM64. This document
defines the repository model, development workflow, and implementation phases before
any changes are made.

---

## 1. The Problem

The current repo cannot be made public as-is. Lab-specific content is baked in throughout:

- Real node IPs in `inventories/dev/hosts.ini`
- Hardcoded VIPs, external URLs, and subnet ranges in `group_vars/all/main.yml`
- Default password (`<admin-password>`) in role defaults, README, CLAUDE.md, and docs
- Tailscale SSH address in `CLAUDE.md`
- Lab-specific Forgejo push URL in `CLAUDE.md`
- `vault.yml` (encrypted but still lab-specific secrets) committed to the repo
- `ansible.cfg` hardcoding `inventories/dev` as default inventory

The goal is a clean separation: generic shareable playbooks on GitHub, site-specific
config kept private on Forgejo.

---

## 2. Two-Repo Model

```
github: lkirc/dmf-infra          ← public, generic
forgejo: <your-username>/dmf-env ← private, never leaves Forgejo
```

### `dmf-infra` (GitHub — public)

Contains only things that are environment-agnostic:

- All playbooks (`playbooks/`)
- All roles (`roles/`)
- Generic documentation (`docs/`)
- Example inventory (`inventories/example/`) with placeholder values and comments
- No real IPs, no passwords, no vault.yml, no site-specific URLs

### `dmf-env` (Forgejo — private)

Contains only site-specific config:

```
inventories/
  hetzner-arm/
    hosts.ini                    # real node IPs
    group_vars/all/
      main.yml                   # real VIPs, sizing, URLs
      vault.yml                  # encrypted secrets (never in dmf-infra)
```

Playbooks are run with explicit inventory path:

```bash
ansible-playbook \
  -i ../dmf-env/inventories/hetzner-arm \
  playbooks/30-netbox.yml \
  --vault-password-file ~/.vault_pass
```

`ansible.cfg` in `dmf-infra` sets no default inventory — the operator always
provides it explicitly or via `ANSIBLE_INVENTORY`.

---

## 3. What Must Be Genericified in `dmf-infra`

The following changes make `dmf-infra` public-safe. **None of these are
implemented yet** — this section is the work backlog for Phase 1.

### `inventories/`

| Action | File |
|--------|------|
| Delete | `inventories/dev/hosts.ini` |
| Delete | `inventories/dev/group_vars/all/main.yml` |
| Delete | `inventories/dev/group_vars/all/vault.yml` |
| Create | `inventories/example/hosts.ini` — documented template |
| Create | `inventories/example/group_vars/all/main.yml` — all vars with comments |
| Create | `inventories/example/group_vars/all/vault.yml.example` — structure only, no values |

### `ansible.cfg`

Remove `inventory = inventories/dev/hosts.ini` default. Add comment requiring
explicit `-i` flag or `ANSIBLE_INVENTORY` env var.

### `playbooks/10-k3s.yml`

Hardcoded `k3s_server_url: "https://<control-node-ip>:6443"` must become
`k3s_server_url: "https://{{ hostvars[k3s_control_node].ansible_host }}:6443"`.

### Role defaults (`roles/*/defaults/main.yml`)

Replace `<admin-password>` defaults with empty strings or `changeme` + a comment.
Affected roles: `awx`, `netbox`, `forgejo-bootstrap`, `netbox-sot`, `awx-integration`.

### `roles/landing-page/defaults/main.yml`

`landing_page_repo_url: "https://forgejo.<cluster-domain>/<org>/dmf-infra"` is lab-specific.
Replace with a generic default or remove the hardcoded path.

### `CLAUDE.md`

Rewrite as generic operational guidance. Remove:
- `ssh dev@<node-ip>` (Tailscale or public IP)
- `<admin-password>` password references
- `git push https://<user>:<token>@forgejo.<cluster-domain>/...` (lab-specific push URL)
- `<vip-address>` service access table

Replace with variable-based examples and a pointer to `dmf-env` for
environment-specific values.

### `README.md`

- Remove `<node-ips>` node IPs and `<vip-address>` VIP from examples
- Remove `<admin-password>` from vault setup instructions
- Add section documenting the two-repo model and how to provide an inventory

### Docs with lab-specific IPs

- `docs/integration-sot-interim-report.md` — add header marking it as a historical
  development log (curl examples contain `<vip-address>`)
- Other docs: replace specific IPs with `<metallb_vip>` placeholders

---

## 4. Target Infrastructure: Hetzner ARM64

**Three-node k3s cluster on Hetzner Cloud:**

```
3× CAX21  — 4 vCPU (Ampere Altra) / 8 GB RAM / 80 GB NVMe
1× Private Network (free) — 10.0.0.0/24 for inter-node traffic
1× Floating IP (~€3.81/mo) — MetalLB VIP / external access point

Total: ~€28/month
Datacenter: Falkenstein or Nuremberg (same location, all three nodes)
OS: Debian 12 (bookworm) ARM64
```

### Node layout

| Node | Private IP | Role |
|------|-----------|------|
| k3s-node-01 | 10.0.0.1 | k3s control + etcd |
| k3s-node-02 | 10.0.0.2 | k3s control + etcd |
| k3s-node-03 | 10.0.0.3 | k3s control + etcd |
| Floating IP | assigned to node-01 | MetalLB VIP / Traefik entry |

### Key differences from the previous home lab

| Concern | Home lab | Hetzner ARM |
|---------|----------|-------------|
| MetalLB | L2 mode, LAN ARP | Floating IP assigned to active node |
| Storage | Longhorn replica=2 | Longhorn replica=2 (unchanged) |
| k3s node binding | LAN interface | `--node-ip` = private network IP |
| Inventory IPs | `<old-lan-ips>` | 10.0.0.x (private) + public IPs for SSH |
| `external_base_url` | `<old-vip>` | Floating IP public address |
| SSH entry | `<old-tailscale-ip>` | Public IP of node-01 (or Tailscale if added) |

### Changes required in `dmf-env/inventories/hetzner-arm/`

`hosts.ini` — SSH via public IPs, k3s traffic via private IPs:
```ini
[k3s]
k3s-node-01 ansible_host=<node1-public-ip> k3s_node_ip=10.0.0.1 ansible_user=debian
k3s-node-02 ansible_host=<node2-public-ip> k3s_node_ip=10.0.0.2 ansible_user=debian
k3s-node-03 ansible_host=<node3-public-ip> k3s_node_ip=10.0.0.3 ansible_user=debian

[k3s_control]
k3s-node-01
```

`group_vars/all/main.yml` — key differences from the old dev inventory:
```yaml
metallb_pool_cidr: "<floating-ip>/32"
metallb_vip: "<floating-ip>"
external_base_url: "http://<floating-ip>"
longhorn_default_replica_count: "2"
k3s_node_ip_var: "k3s_node_ip"   # tells k3s playbook to bind to private interface
```

### `playbooks/10-k3s.yml` changes for VPS networking

k3s must bind to the private network interface, not the public one. The install
command needs `--node-ip {{ k3s_node_ip }}` and `--advertise-address {{ k3s_node_ip }}`
per node. This change goes into `dmf-infra` as a generic variable (`k3s_node_ip`
defaults to `ansible_host` if not set — backwards compatible).

---

## 5. Development Workflow

### Repository relationship

```
github: lkirc/dmf-infra  (upstream, generic, public)
         ↑  periodic PR (clean, generic commits only)
         ↓  fork at project start
forgejo: <your-username>/dmf-infra   (lab fork, active development)
```

`dmf-env` is never pushed to GitHub. It lives only on Forgejo.

### Branch model on Forgejo

```
feature/xyz  ──→  develop  ──→  main
    ↓                ↓            ↓
dry-run only    auto-apply    stable /
(--check)       to cluster    tagged
```

| Branch | Trigger | Action |
|--------|---------|--------|
| `feature/*` | push | lint + syntax-check + `--check --diff` |
| `develop` | push | lint + syntax-check + `--check` then auto-apply |
| `main` | manual promote | tag + apply (considered stable) |

New work always starts on a `feature/` branch. Tested with `--check` first.
Merged to `develop` for live apply. Promoted to `main` when stable.

### Keeping GitHub in sync

When a change on `feature/` or `develop` is genuinely generic (no lab-specific
assumptions, no IPs, no credentials), it is cherry-picked or PR'd to
`github: lkirc/dmf-infra`. GitHub main should always be a clean subset of
the Forgejo development history.

The rule: **if it would need editing before going to GitHub, it stays on Forgejo.**

---

## 6. Bootstrap Sequence (Day 0 → Day 2)

The cluster doesn't exist yet, so Forgejo can't host repos during initial setup.
The bootstrap is a three-phase process.

### Day 0 — Provision infrastructure (hcloud CLI from local Mac)

```bash
# Install hcloud
brew install hcloud

# Authenticate
hcloud context create k3s-lab   # prompts for API token

# Create private network
hcloud network create --name k3s-private --ip-range 10.0.0.0/24
hcloud network add-subnet k3s-private --type server --network-zone eu-central \
  --ip-range 10.0.0.0/24

# Create nodes
for i in 1 2 3; do
  hcloud server create \
    --name k3s-node-0${i} \
    --type cax21 \
    --image debian-12 \
    --location fsn1 \
    --network k3s-private \
    --ssh-key <your-key-name>
done

# Create and assign floating IP
hcloud floating-ip create --type ipv4 --home-location fsn1 --name k3s-vip
hcloud floating-ip assign k3s-vip k3s-node-01
```

### Day 1 — Bootstrap the cluster (Ansible from local Mac)

```bash
# Clone both repos locally
git clone git@github.com:lkirc/dmf-infra.git
git clone forgejo:<your-username>/dmf-env.git   # once created on Forgejo

# Populate dmf-env with real node IPs from hcloud output
# hcloud server list → fill in hosts.ini and main.yml

# Run playbooks in order, pointing at hetzner-arm inventory
cd dmf-infra/k3s-lab-bootstrap
export ANSIBLE_INVENTORY=../../dmf-env/inventories/hetzner-arm

ansible-playbook playbooks/00-baseline.yml
ansible-playbook playbooks/10-k3s.yml
ansible-playbook playbooks/15-metallb.yml
ansible-playbook playbooks/20-longhorn.yml
ansible-playbook playbooks/22-landing-page.yml
# ... continue through 42
```

### Day 2 — Hand off to Forgejo (once Forgejo is deployed via playbook 31)

```bash
# Push dmf-env to the newly deployed Forgejo instance
cd dmf-env
git remote add forgejo git@forgejo-lab:<your-username>/dmf-env.git
git push forgejo main

# Push dmf-infra fork to Forgejo
cd dmf-infra
git remote add forgejo git@forgejo-lab:<your-username>/dmf-infra.git
git push forgejo --all

# From this point, all day-2 operations run through Forgejo Actions
```

After Day 2, the local Mac is no longer needed for deployments. Forgejo Actions
drives everything.

---

## 7. Forgejo Actions CI Pipeline

Pipeline lives in `dmf-infra` at `.forgejo/workflows/deploy.yml`.

### Pipeline design

```
push to feature/*
  └─ job: validate
       ansible-lint
       ansible-playbook --syntax-check
       ansible-playbook --check --diff   (dry run, no apply)

push to develop
  └─ job: validate (same as above)
  └─ job: apply (depends on validate)
       ansible-playbook [changed playbooks] --diff
       smoke tests (HTTP checks against VIP)

push to main (via PR from develop)
  └─ job: validate
  └─ job: apply (manual approval gate)
  └─ job: tag release
```

### Runner

A self-hosted Forgejo Actions runner deployed on k3s-node-01. It has:
- Access to `dmf-env` inventory (mounted as a Kubernetes secret or
  checked out from Forgejo private repo)
- SSH key for ansible to reach the nodes
- `~/.vault_pass` mounted from a Kubernetes secret

### Secret injection for Actions

Secrets needed by the runner (vault password, SSH key) are stored as
Kubernetes secrets in the `forgejo` namespace and mounted into the
runner pod — they never appear in the `dmf-env` repo as plaintext.

---

## 8. Implementation Phases

Work is ordered so each phase delivers something usable before the next begins.

### Phase 1 — Genericify `dmf-infra`
*Goal: repo safe to push public on GitHub*

- [ ] Replace `inventories/dev/` with `inventories/example/` (placeholder template)
- [ ] Remove `vault.yml` from repo (add to `.gitignore`)
- [ ] Fix `playbooks/10-k3s.yml` — derive k3s server URL from inventory variable
- [ ] Strip hardcoded passwords from all role defaults
- [ ] Rewrite `CLAUDE.md` — remove lab-specific IPs, passwords, Forgejo URL
- [ ] Update `README.md` — document two-repo model, remove specific IPs
- [ ] Update `ansible.cfg` — remove hardcoded default inventory
- [ ] Fix `roles/landing-page/defaults/main.yml` — remove hardcoded repo URL
- [ ] Mark `docs/integration-sot-interim-report.md` as historical
- [ ] Replace `<ip>` references in docs with `<metallb_vip>` placeholders

### Phase 2 — Create `dmf-env`
*Goal: private config repo ready for Hetzner ARM*

- [ ] Create `dmf-env` repo on Forgejo (private)
- [ ] Write `inventories/hetzner-arm/hosts.ini` template (IPs filled after provisioning)
- [ ] Write `inventories/hetzner-arm/group_vars/all/main.yml` (Hetzner-specific values)
- [ ] Write `inventories/hetzner-arm/group_vars/all/vault.yml` (new secrets, encrypted)
- [ ] Add `.gitignore` (never commit unencrypted vault)

### Phase 3 — Provision Hetzner nodes
*Goal: three CAX21 ARM64 nodes running Debian 12*

- [ ] Install `hcloud` CLI (`brew install hcloud`)
- [ ] Create Hetzner API token (read+write)
- [ ] Create private network (10.0.0.0/24)
- [ ] Provision 3× CAX21 in same datacenter
- [ ] Assign floating IP to node-01
- [ ] Fill real IPs into `dmf-env` inventory
- [ ] Verify SSH access to all three nodes

### Phase 4 — Bootstrap the cluster
*Goal: full stack running on Hetzner ARM*

- [ ] Run playbooks 00-42 targeting `hetzner-arm` inventory
- [ ] Verify all services accessible via floating IP
- [ ] Validate AWX inventory sync from NetBox
- [ ] Run smoke tests against all service endpoints

### Phase 5 — Configure Forgejo Actions
*Goal: CI/CD pipeline operational*

- [ ] Deploy Forgejo Actions runner on k3s-node-01
- [ ] Write `.forgejo/workflows/deploy.yml`
- [ ] Store secrets (vault password, SSH key) as Kubernetes secrets
- [ ] Test pipeline on a `feature/` branch (dry run)
- [ ] Test apply on `develop` branch

### Phase 6 — Publish to GitHub
*Goal: clean public repo on github.com/lkirc/dmf-infra*

- [ ] Review all files for any remaining lab-specific content
- [ ] PR clean generic version to GitHub main
- [ ] Verify nothing sensitive in git history (check with `git log -p`)
- [ ] Add GitHub Actions for basic lint CI (optional)

---

## 9. Open Decisions

These need a decision before or during implementation:

| Decision | Options | Notes |
|----------|---------|-------|
| Forgejo Actions runner type | Docker executor vs shell | Docker is cleaner; shell is simpler |
| Vault password in CI | K8s secret mount vs Hetzner secret | K8s secret is self-contained |
| `dmf-env` initial location | Local only → push to Forgejo after Day 1 | Bootstrap chicken-and-egg |
| GitHub sync cadence | Manual PR vs automated mirror | Manual gives review gate |
| Tailscale on new lab | Yes / No | Useful for SSH but adds a dependency |

---

*This document is the pre-implementation reference. Update it as phases complete.*
