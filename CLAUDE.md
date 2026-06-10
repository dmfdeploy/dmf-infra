# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## DMF Platform context — read first

This repo is a component of the **DMF Platform**, an umbrella workspace
checked out alongside this repo. Operators set `$DMFDEPLOY_UMBRELLA` to its
local path. Cross-cutting state (status, decisions, plans, skills) lives
there, not here.

Before any non-trivial change in this repo:

```bash
cd "$DMFDEPLOY_UMBRELLA"
git fetch && git pull
bin/generate-status.sh --no-fetch    # refreshes STATUS.md
```

Then read in order:
1. `dmfdeploy/STATUS.md` — what's happening across all repos right now
2. `dmfdeploy/CLAUDE.md` — full boot ritual + workspace map
3. `dmfdeploy/docs/decisions/INDEX.md` — ADRs applicable to your task
4. The most recent file under `dmfdeploy/docs/handoffs/`

For cluster ops, secrets, or dmf-cms releases, also read §0 Secrets Discipline
of the relevant skill in `dmfdeploy/.claude/skills/`.

If you change cross-repo state, update the `<!-- HUMAN-START -->` section of
`dmfdeploy/STATUS.md` before ending the session.

---

## Repository Model

This repo (`dmf-infra`) contains only generic, environment-agnostic playbooks and roles.
Environment-specific configuration (node IPs, ingress settings, passwords) lives in a separate
**private** repo — typically named `dmf-env`. Never commit real IPs or secrets to this repo.

> **ADR-0025 (landed 2026-05-19):**
> A custom AWX Execution Environment build pipeline lives at
> `k3s-lab-bootstrap/ee/` (ansible-builder config); `playbooks/630-zot-seed-platform.yml`
> builds the EE image and pushes it to cluster-internal Zot. The same EE
> image is consumed by the in-cluster ansible runner pod
> (`roles/stack/operator/ansible-runner/`) and by AWX-spawned media catalog
> launchers. See
> `dmfdeploy/docs/plans/DMF Cluster-Internal Ansible Execution and Catalog Helm Pivot Plan 2026-05-19.md`
> and ADR-0025.

```bash
# Run playbooks through the environment wrapper
cd ../dmf-env
bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/site.yml
```

The repo follows the **EBU DMF Reference Architecture V2.0** (2026-04-15):
six horizontal layers + four cross-cutting verticals + six lifecycle stages.
See `dmfdeploy/docs/architecture/DMF EBU Mapping (2026-04-25).md` for the canonical
playbook ↔ layer/vertical map.

## Secrets

Secrets are exported from OpenBao by `dmf-env/bin/run-playbook.sh` into a temp vars file at
runtime. Generic repo code should keep using `vault_*` variables, but should not assume
`community.hashi_vault` runs inside Ansible.

## Running kubectl commands

Run directly on the control node, or export the kubeconfig locally:

```bash
# On the control node
sudo k3s kubectl get pods -A

# Or export kubeconfig from the control node and use locally
export KUBECONFIG=~/.kube/k3s.yaml
kubectl get pods -A
```

## Running Helm commands

```bash
helm get values prometheus -n monitoring
helm uninstall awx-operator -n awx
```

## Landing page quick hints

- Generator script: `k3s-lab-bootstrap/roles/landing-page/templates/generate-html.sh.j2`
- Custom logos: add files to `roles/landing-page/files/logos/`, register in the `landing-page-assets`
  ConfigMap task, and set `landing_page_logo_url: "/assets/<filename>"` in your inventory vars.
- IngressRoute must match `PathPrefix(/assets)` for logo URLs to work.
- ConfigMap changes do not auto-rollout:
  `sudo k3s kubectl -n default rollout restart deploy/landing-page`

## Running Ansible playbooks

```bash
# From dmf-env with the wrapper
bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/playbooks/<playbook>.yml
```

## Token updates in NetBox SoT playbook

- `playbooks/691-netbox-sot.yml` creates the admin token via `kubectl exec` into the NetBox pod
  (basic auth to `/users/tokens/` returns 403).
  NetBox v4 uses v2 tokens: the full token is `TOKEN_PREFIX + key + "." + token_secret`,
  and API calls must use `Authorization: Bearer <token>`. The DB only stores the short
  key, so the full token must be captured at creation time and written back to the environment’s
  OpenBao secret path.

## Forgejo bootstrap playbook

- `playbooks/692-forgejo-bootstrap.yml` creates the admin token via API using
  `forgejo_admin_username` / `forgejo_admin_password`. Store persistent tokens back in OpenBao
  rather than reintroducing ansible-vault files.

## Service account naming convention

- Service users: `<system>-svc` (e.g., `awx-svc`, `librenms-svc`, `forgejo-svc`)
- NetBox groups: `<system>-readonly` or `<system>-writer`
- Tokens: `<system>-token-<purpose>`
- Forgejo hosts automation/config repos for AWX and app integrations, owned by `forgejo-svc`.

## Running commands inside containers

- Some images lack `rg`; use `grep` inside pods if needed.
- `lnms` must not run as root:
  `sudo k3s kubectl exec -n librenms deploy/librenms-frontend -- /bin/sh -c "su -s /bin/sh librenms -c '/opt/librenms/lnms <command>'"`
- ConfigMap changes (nginx proxy config) do not auto-restart; rerun the playbook.

## Cluster readiness gate (use in new app playbooks)

The shared role `roles/cluster-ready` waits for nodes, CoreDNS, Traefik, and Longhorn CSI.
Include it before app roles to avoid race conditions on startup:

```yaml
roles:
  - ../roles/cluster-ready
  - ../roles/<app>
```

## Networking note

Avoid absolute URLs and hardcoded IPs in configs. Prefer relative paths or derive URLs from
`external_base_url`, with `metallb_vip` only as a legacy fallback.

---

## Project Overview

k3s lab infrastructure bootstrap using Ansible. Provisions a 3-node k3s cluster on Debian 12
with environment-selected ingress, Longhorn distributed storage, and a full monitoring/automation
stack.

## Common Commands

```bash
# Full build (default — calls lifecycle-provision)
bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/site.yml

# A single lifecycle stage
bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/lifecycle-provision.yml
bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/lifecycle-operate.yml
bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/lifecycle-finalise.yml

# Targeted re-run of a single layer/vertical playbook
bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/playbooks/300-k3s.yml
bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/playbooks/vertical-monitoring/100-prometheus.yml
bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/playbooks/vertical-security/100-openbao.yml

# Tag-based selection (layerN, vertical-X, lifecycle-Y, plus functional tags)
ansible-playbook site.yml --tags layer3
ansible-playbook site.yml --tags vertical-monitoring
ansible-playbook site.yml --tags openbao,eso

# Verify cluster state (run on control node)
sudo k3s kubectl get nodes
sudo k3s kubectl get pods -A
sudo k3s kubectl get svc -A
```

## Architecture

```
k3s-lab-bootstrap/
├── ansible.cfg                    # Ansible settings (no default inventory)
└── inventories/
    └── example/
        ├── hosts.ini              # Template — copy to dmf-env
        └── group_vars/all/
            ├── main.yml           # All variables, fully documented
            └── main.yml           # Ingress, storage, and generic env settings
```

Environment-specific inventory lives in a separate private repo (e.g., `dmf-env`):

```
dmf-env/inventories/<env-name>/
├── hosts.ini
└── group_vars/all/
    ├── main.yml             # Real ingress, sizing, URLs
    └── openbao_secrets.yml  # OpenBao metadata (no secret_id in git)
```

**Cluster topology:**
- `k3s-node-01` — bootstraps the cluster (`--cluster-init`), runs etcd
- `k3s-node-02`, `k3s-node-03` — join as additional control plane + etcd nodes

**Key variables (set in `<inventory>/group_vars/all/main.yml`):**
- `cluster_ingress_mode` — ingress provider path (`cloud-native`, `metallb-*`, `nodeport-only`)
- `external_base_url` — stable external entry point used by apps
- `k3s_private_interface` — explicit private NIC used for flannel and node IP binding
- `longhorn_default_replica_count` — Longhorn replica count (2 for a 3-node lab)
- `k3s_control_node` — hostname of the first control node

## Web Interfaces

All services are exposed via Traefik on the environment-selected entry point:

| Service | URL | Credentials |
|---------|-----|-------------|
| Landing Page | `<external_base_url>/` | — |
| Grafana | `<external_base_url>/grafana` | admin / (OpenBao) |
| Prometheus | `<external_base_url>/prometheus` | — |
| NetBox | `<external_base_url>/netbox` | admin / (OpenBao) |
| AWX | `<external_base_url>/awx` | admin / (OpenBao) |
| LibreNMS | `<external_base_url>/librenms` | admin / (OpenBao) |
| Forgejo | `<external_base_url>/forgejo` | dev / (OpenBao) |

The landing page provides a dynamic portal to all deployed applications.

## Monitoring Stack

- **Prometheus** — Metrics collection and storage
  - Retention: 6 hours / 2GB TSDB (WAL needs ~300MB additional headroom)
  - WAL compacts every 2 hours; size PVC accordingly
  - Scrapes: node-exporter, kube-state-metrics, cAdvisor, apiserver

- **Grafana** — Visualization
  - Dashboards: [grafana-dashboards-kubernetes](https://github.com/dotdc/grafana-dashboards-kubernetes)
  - Adapted for single-cluster k3s (cluster/job variables removed)

- **Loki** — Log aggregation
- **Promtail** — Log shipping to Loki

## Design Principles

- All configuration in code — no manual cluster modifications
- Environment-specific config always in a separate private repo
- Playbooks numbered for clear execution order
- Secrets sourced from OpenBao outside Ansible runtime
- Roles are idempotent — safe to rerun

---

## Troubleshooting Guide

### Prometheus Storage Full / "No Data" in Grafana

**Symptoms:** Grafana dashboards show "No data", Prometheus pod in CrashLoopBackOff.

**Root Cause:** `retentionSize` controls TSDB blocks only — NOT the WAL. The WAL grows
to 150–300MB before compaction (every 2 hours). A 1Gi PVC fills up.

**Fix:**

```bash
sudo k3s kubectl delete pvc prometheus-server -n monitoring --wait=false
sudo k3s kubectl patch pvc prometheus-server -n monitoring -p '{"metadata":{"finalizers":null}}'
ansible-playbook playbooks/vertical-monitoring/100-prometheus.yml
```

**Storage sizing:**
- 300MB TSDB blocks (retentionSize)
- 300MB WAL headroom
- 400MB safety buffer
- → 5Gi PVC recommended for headroom

### Helm Stuck in "pending-upgrade" State

**Symptoms:** `helm upgrade` fails with "another operation is in progress".

```bash
helm rollback <release-name> <revision> -n <namespace>
```

### PVC Stuck in Terminating State

```bash
sudo k3s kubectl patch pvc <pvc-name> -n <namespace> -p '{"metadata":{"finalizers":null}}'
```

### Longhorn RWO vs RWX Volumes

**Problem:** "Multi-Attach error" when a pod reschedules to a different node.

- **RWO (ReadWriteOnce):** one node at a time
- **RWX (ReadWriteMany):** multiple nodes — use for Deployments that may reschedule

```yaml
accessMode: ReadWriteMany
storageClass: longhorn
```

Affected workloads in this lab: NetBox media, AWX projects.

### AWX PostgreSQL Permission Denied on Longhorn

**Symptoms:** AWX PostgreSQL pod fails with "Permission denied" on Longhorn volume.

**Cause:** PostgreSQL runs as UID 26; Longhorn volumes are created root-owned.

**Fix:** Add to AWX CR spec:

```yaml
postgres_data_volume_init: true
```

### Traefik IngressRoute vs Kubernetes Ingress

- **Traefik IngressRoute (CRD):** most apps (Grafana, Prometheus, NetBox, AWX)
- **Kubernetes Ingress:** Forgejo (standard resource)

The landing page script discovers both types to build the app portal.

### Checking Service Health

```bash
# All pod status
sudo k3s kubectl get pods -A

# PVC status
sudo k3s kubectl get pvc -A

# Longhorn volumes
sudo k3s kubectl get volumes.longhorn.io -n longhorn-system

# Prometheus storage usage
sudo k3s kubectl exec -n monitoring deploy/prometheus-server -- df -h /data

# Service logs
sudo k3s kubectl logs -n <namespace> deploy/<deployment>
```
