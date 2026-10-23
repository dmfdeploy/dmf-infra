# dmf-infra

[![CI](https://github.com/dmfdeploy/dmf-infra/actions/workflows/ci.yml/badge.svg)](https://github.com/dmfdeploy/dmf-infra/actions/workflows/ci.yml)

Generic Ansible playbooks and roles that build the k3s infrastructure layer and its cross-cutting services: identity, secrets, inventory, monitoring and automation.

Part of [dmfdeploy](https://github.com/dmfdeploy/dmfdeploy), an independent open prototype that explores the ideas of the EBU Dynamic Media Facility (DMF) on commodity infrastructure. Status: prototype v0.1, single ARM64 node (`sandbox-single-node` profile).

> **New to the project vocabulary?** See the [DMF Glossary](https://github.com/dmfdeploy/dmfdeploy/blob/main/docs/GLOSSARY.md) for project-coined terms (umbrella, component repo, provider, …).

> **Note: prototype sizing**
>
> This is a prototype, so the defaults aim to show the stack working
> under light load. The example inventory sets:
> - **Prometheus**: 50Gi volume, 30 days of retention, capped at 40 GB
> - **Loki**: 50Gi volume, 720 hours of retention
> - **Grafana and PostgreSQL**: 1Gi volumes
> - **Longhorn replicas**: 2, used only by the optional three-node layout
>
> Adjust the sizes and retention values in your inventory to match your node.

## How this repository fits in

`dmf-infra` is one of the public component repos of the **dmfdeploy platform**
(`github.com/dmfdeploy/`). It contains **only generic, environment-agnostic
playbooks and roles**, with no real IPs, hostnames, or secrets. Its companion
[`dmf-env`](https://github.com/dmfdeploy/dmf-env) holds the generic environment
provisioning + bootstrap tooling (wrapper scripts, OpenTofu roots/modules). Per
ADR-0035, **all per-environment state** (inventory, secrets bundle, SSH keys,
OpenTofu state) is **operator-local** under `~/.dmfdeploy/envs/<env>/` and is
never committed to any repo.

Playbooks are typically run through the environment wrapper so secrets are exported from OpenBao
before Ansible starts:

```bash
cd ../dmf-env
bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/site.yml
```

The repo layout follows the **EBU DMF Reference Architecture V2.0** (2026-04-15).
It has six horizontal layers (Infrastructure, Host Platform, Container Platform, Media
Exchange, Media Functions, Application & UI) plus four cross-cutting verticals
(Orchestration, Control, Monitoring, Security) and a six-stage Media Workload
lifecycle (Design / Plan / Provision / Configure / Operate / Finalise & Review).
See `dmfdeploy/docs/architecture/DMF EBU Mapping (2026-04-25).md` for the canonical
playbook ↔ layer/vertical map.

## Cluster Overview

The v0.1 profile runs everything on one node. The diagram shows the optional
three-node lab layout, which the roles also support. It is not part of the v0.1
profile.

```
┌───────────────────────────────────────────────────────────────────┐
│                      Cluster Network                              │
├───────────────────────────────────────────────────────────────────┤
│                                                                   │
│   ┌────────────────┐  ┌────────────────┐  ┌────────────────┐      │
│   │  k3s-node-01   │  │  k3s-node-02   │  │  k3s-node-03   │      │
│   │ <node1-ip>     │  │ <node2-ip>     │  │ <node3-ip>     │      │
│   │ control + etcd │  │ control + etcd │  │ control + etcd │      │
│   └───────┬────────┘  └───────┬────────┘  └───────┬────────┘      │
│           └───────────────────┼───────────────────┘               │
│                               │                                   │
│                      ┌────────┴─────────────┐                     │
│                      │ Environment-Selected │                     │
│                      │   Ingress Provider   │                     │
│                      └────────┬─────────────┘                     │
│                               │                                   │
│                 cloud LB | MetalLB | NodePort-only                │
│                               │                                   │
│                      ┌────────┴────────┐                          │
│                      │     Traefik     │                          │
│                      │ Ingress Control │                          │
│                      └────────┬────────┘                          │
│                               │                                   │
│    /awx  /forgejo  /grafana  /librenms  /netbox  /prometheus      │
│                                                                   │
│                         Landing Page (/)                          │
│                                                                   │
└───────────────────────────────────────────────────────────────────┘
```

## Requirements

### Hardware

- **One node** for the v0.1 `sandbox-single-node` profile (VM or bare metal):
  - 4+ CPU cores
  - 8+ GB RAM
  - 80+ GB disk
- Tested on Hetzner CAX21 (ARM64 / Ampere Altra) and x86_64 VMs
- Optional: three such nodes with network connectivity between them, for the
  three-node lab layout

### Software

- **Debian 12 (Bookworm)** on all nodes, the documented target. The playbooks
  also handle Debian 13 (Trixie), but they do not check the OS version.
- **Ansible 2.14+** on your control machine
- **SSH key-based authentication** to all nodes

## Quick Start

### 1. Prepare Your Nodes

On each Debian node (repeat for each node in the three-node layout):

```bash
# Set the hostname (a different name on each node)
sudo hostnamectl set-hostname k3s-node-01

# Enable passwordless sudo for your user
sudo visudo
# Add: <your-user> ALL=(ALL) NOPASSWD: ALL
```

### 2. Set Up SSH Keys

From your control machine:

```bash
ssh-keygen -t ed25519 -C "k3s-lab"
ssh-copy-id <your-user>@<node1-ip>
# Three-node layout only:
# ssh-copy-id <your-user>@<node2-ip>
# ssh-copy-id <your-user>@<node3-ip>
```

### 3. Install Ansible

```bash
# Debian/Ubuntu
sudo apt update && sudo apt install -y ansible git

# Or using pip
pip install ansible
```

### 4. Create Your Inventory Repo

Copy the example inventory to a private repo (`dmf-env`):

```bash
cp -r k3s-lab-bootstrap/inventories/example dmf-env/inventories/<env-name>

# Edit with your real IPs and settings
nano dmf-env/inventories/<env-name>/hosts.ini
nano dmf-env/inventories/<env-name>/group_vars/all/main.yml

# Configure OpenBao metadata in the private env repo and store the AppRole
# SecretID in macOS keychain. The wrapper exports a temp vars file at runtime.
```

### 5. Deploy the Cluster

```bash
cd ../dmf-env

# Full build: walks every EBU layer and weaves in cross-cutting verticals
bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/site.yml

# Or invoke a single lifecycle stage:
bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/lifecycle-provision.yml   # Provision + Configure
bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/lifecycle-operate.yml     # Operate verify
bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/lifecycle-finalise.yml    # Teardown

# Or a single layer/vertical playbook:
bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/playbooks/300-k3s.yml
bin/run-playbook.sh <env-name> ../dmf-infra/k3s-lab-bootstrap/playbooks/vertical-security/100-openbao.yml
```

For the full per-layer playbook list and run order, see
`dmf-env/DEPLOYMENT.md` and
`dmfdeploy/docs/architecture/DMF EBU Mapping (2026-04-25).md`.

### 6. Verify

```bash
# SSH to control node
ssh <user>@<node1-ip>

sudo k3s kubectl get nodes
sudo k3s kubectl get pods -A
```

## Accessing Services

All services are exposed via Traefik at the environment-selected external entry point defined by
`external_base_url`:

| Service | URL | Credentials |
|---------|-----|-------------|
| Landing Page | `<external_base_url>/` | None |
| AWX | `<external_base_url>/awx` | admin / (OpenBao) |
| Forgejo | `<external_base_url>/forgejo` | dev / (OpenBao) |
| Grafana | `<external_base_url>/grafana` | admin / (OpenBao) |
| LibreNMS | `<external_base_url>/librenms` | admin / (OpenBao) |
| Prometheus | `<external_base_url>/prometheus` | None |
| Loki | `<external_base_url>/loki` (log API, no web UI) | None |
| NetBox | `<external_base_url>/netbox` | admin / (OpenBao) |

The **Landing Page** automatically discovers and displays links to all deployed applications.

## Project Structure

```
k3s-lab-bootstrap/
├── ansible.cfg                      # Ansible configuration (no default inventory)
├── requirements.yml                 # Galaxy collection/role requirements
├── site.yml                         # Top-level entry: calls lifecycle-provision
├── lifecycle-provision.yml          # EBU Provision (full build)
├── lifecycle-configure.yml          # EBU Configure stage
├── lifecycle-operate.yml            # EBU Operate stage (verify, drills)
├── lifecycle-finalise.yml           # EBU Finalise & Review (teardown)
├── bootstrap-*.yml                  # From-scratch bootstrap chain (pre-/post-seed,
│                                    #   configure, verify) — driven by dmf-env / dmf-init
├── inventories/
│   └── example/                     # Template inventory; real envs are operator-local
├── playbooks/
│   ├── 200-baseline.yml … 219-*     # Layer 2 — Host Platform: baseline, harden, verify
│   ├── 300-k3s.yml … 339-*          # Layer 3 — Container Platform (k3s, ingress, TLS, storage, registry)
│   ├── 600-landing-page.yml … 699-* # Layer 6 — Application & UI (NetBox, Forgejo, AWX, dmf-cms, integration glue)
│   ├── vertical-security/           # OpenBao, Authentik, breakglass-verify
│   ├── vertical-monitoring/         # Prometheus, Loki, Grafana, Promtail, LibreNMS
│   ├── vertical-orchestration/      # ESO (External Secrets Operator)
│   ├── vertical-resilience/         # Resilience drills / recovery runbooks
│   └── lifecycle/                   # Stack verify + teardown bodies
├── roles/
│   ├── base/                        # Layers 2/3 + verticals (k3s, harden, ingress, longhorn, prometheus base, …)
│   ├── stack/operator/              # Layer 6 + verticals (NetBox, Forgejo, AWX, OpenBao, Authentik, …)
│   ├── stack/standalone/            # Layer 6 alternate (Flypack profile)
│   ├── modules/infra-monitoring/    # Vertical-monitoring extension (LibreNMS, …)
│   ├── modules/advanced/            # Vertical-orchestration extension (ArgoCD, federation)
│   └── common/                      # Utilities used across layers
├── charts/                          # Helm charts vendored/used by playbooks
├── ee/                              # AWX Execution Environment build (ansible-builder)
├── providers/                       # Per-provider helpers
├── tests/                           # Test scaffolding
└── docs/                            # Additional documentation
```

Layer 4 (Media Exchange) and Layer 5 (Media Functions) live in the sibling
repo `dmf-media`. See `dmfdeploy/docs/architecture/DMF EBU Mapping (2026-04-25).md`
for the canonical mapping.

## Configuration

Edit `<inventory>/group_vars/all/main.yml` in your private `dmf-env` repo:

```yaml
# Select an ingress mode per environment:
# cloud-native | metallb-bgp | metallb-l2 | nodeport-only
cluster_ingress_mode: cloud-native
external_base_url: "http://dmf.example.com"

# Longhorn replica count
longhorn_default_replica_count: "2"

# Storage sizes (adjust to match your node)
prometheus_storage_size: "50Gi"
grafana_storage_size: "1Gi"
```

## Documentation

- **[Authentik](k3s-lab-bootstrap/docs/authentik.md)**: Passwordless bootstrap and passkey enrollment
- **[AWX](k3s-lab-bootstrap/docs/awx.md)**: AWX `/awx` subpath routing and Traefik config
- **[Forgejo](k3s-lab-bootstrap/docs/forgejo.md)**: Forgejo `/forgejo` deployment and subpath routing
- **[Integration SoT](k3s-lab-bootstrap/docs/integration-sot.md)**: NetBox → AWX integration overview
- **[Landing Page](k3s-lab-bootstrap/docs/landing-page.md)**: Dynamic landing page architecture
- **[LibreNMS](k3s-lab-bootstrap/docs/librenms.md)**: LibreNMS deployment and `/librenms` routing
- **[NetBox Deployment Notes](k3s-lab-bootstrap/docs/netbox-deployment-notes.md)**: Troubleshooting guide

## Troubleshooting

```bash
sudo k3s kubectl get pods -A
sudo k3s kubectl describe pod <pod-name> -n <namespace>
sudo k3s kubectl logs <pod-name> -n <namespace>
sudo k3s kubectl get volumes.longhorn.io -n longhorn-system
sudo k3s kubectl get pvc -A
sudo k3s kubectl get ipaddresspool -n metallb-system
```

For more detail, see the documents in `k3s-lab-bootstrap/docs/`.

## Contributing

1. Fork the repository
2. Create a feature branch (`git checkout -b feature/my-feature`)
3. Make your changes (keep them environment-agnostic, with no real IPs or passwords)
4. Test with `--check --diff` before applying
5. Submit a pull request

### Guidelines

- No hardcoded IPs, passwords, or site-specific URLs in playbooks or roles
- Follow existing numbering conventions for new playbooks
- Document complex deployments (see `docs/netbox-deployment-notes.md` as an example)
- Keep secrets out of git: use the environment repo’s OpenBao-backed secret path
- Ensure playbooks are idempotent

## Repository layout note

`AGENTS.md`, `CLAUDE.md` and `QWEN.md` hold instructions for AI coding agents
that help maintain this repository. You do not need them to use it.

## License

Apache License, Version 2.0: see [LICENSE](LICENSE).
Third-party components are listed in [NOTICE](NOTICE).

## Acknowledgments

- [k3s](https://k3s.io/): Lightweight Kubernetes
- [Longhorn](https://longhorn.io/): Cloud-native distributed storage
- [MetalLB](https://metallb.universe.tf/): Bare metal load balancer
- [Traefik](https://traefik.io/): Cloud-native ingress controller
- [NetBox](https://netbox.dev/): Network source of truth
- [AWX](https://github.com/ansible/awx): Ansible automation platform
