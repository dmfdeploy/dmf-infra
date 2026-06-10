# DMF Platform Plan (v0.1 draft)

**Date:** 2026-04-30  
**Status:** Experiment phase — testing architectural assumptions before commitment  
**Version:** v0.1 draft — iterate and evolve  

---

## Vision & Goals

The **DMF Platform** is a unified digital media framework for production broadcast environments. It aims to:

1. **(i) Credibility & career signal** — Establish expertise in EBU/broadcast digital infrastructure and media function orchestration
2. **(ii) Working lab platform** — Provide a fully automated, reference k3s infrastructure for testing media workflows
3. **(iii) Open-source product** — Build and release a reusable, documented media-platform-on-k3s as OSS

In **experiment phase** (current), we are de-risking architectural decisions before locking in. The focus is on falsifying thesis-killers and validating interfaces, not on operational hardening.

---

## Architecture — EBU DMF Reference V2.0

The platform follows the **EBU DMF Reference Architecture V2.0** (2026-04-15), which defines:

### Six Horizontal Layers

1. **Layer 1 — Infrastructure**: Compute, network, storage provisioning (Hetzner cloud, Cloudflare DNS, networking)
2. **Layer 2 — Host Platform**: OS baseline, hardening, host services (Debian 12, openssh, node-exporter)
3. **Layer 3 — Container Platform**: Kubernetes, ingress, persistent storage, registry (k3s, Traefik, Longhorn, Zot)
4. **Layer 4 — Media Exchange**: NMOS device discovery, EBU LIST 2110 packet media, PTP time sync
5. **Layer 5 — Media Functions**: Media processing workloads, adapters, live transcoding
6. **Layer 6 — Application & UI**: Operator console, policy engine, reporting (dmf-cms, NetBox, AWX, Grafana)

### Four Cross-Cutting Verticals

- **Security**: Authentication (Authentik), authorization (RBAC), secrets management (OpenBao), token lifecycle
- **Monitoring**: Metrics (Prometheus), logs (Loki), alerts (Alertmanager), dashboards (Grafana)
- **Orchestration**: Job execution (AWX), workflow scheduling, state reconciliation (ESO)
- **Control**: Runbooks, drills, change control workflows, audit trails

### Six Lifecycle Stages

1. **Design**: Resource Profile manifest (YAML), capacity planning
2. **Plan**: Dry-run, cost estimation, change preview
3. **Provision**: IaaS provisioning (Terraform), node bootstrap
4. **Configure**: k3s deployment, service mesh, initial state
5. **Operate**: Health checks, scaling, workflow execution, runbooks
6. **Finalise & Review**: Teardown, post-mortem, archival

---

## Two-Repo Model

### Public Repo: `dmf-infra` (generic, environment-agnostic)

Contains:
- Ansible playbooks and roles for all layers and verticals
- Helm charts and manifests (Traefik, NetBox, AWX, Forgejo, OpenBao, Authentik, monitoring stack)
- Documentation (layer playbooks, troubleshooting, design decisions)
- Example inventory templates

**No real IPs, FQDNs, passwords, or site-specific configuration.**

Playbook structure follows EBU numbering:
```
playbooks/
├── 200–299: Layer 2 (Host Platform)
├── 300–399: Layer 3 (Container Platform)
├── 400–499: Layer 4 (Media Exchange) — in dmf-media
├── 500–599: Layer 5 (Media Functions) — in dmf-media
├── 600–699: Layer 6 (Application & UI)
└── vertical-*/: Cross-cutting verticals
```

### Private Repo: `dmf-env` (site-specific, per-environment)

Contains:
- Inventory manifests (Resource Profiles) defining target environments
- Ansible inventories (hosts.ini, group_vars)
- Terraform state, tfvars, overrides
- OpenBao AppRole metadata (role_id in git, secret_id in secure keychain)
- Bin wrappers that export secrets at runtime

**All real IPs, FQDNs, passwords, and site-specific decisions live here.**

Each environment (e.g., `hetzner-arm`, `flypack-01`) has:
```
inventories/<env>/
├── hosts.ini
└── group_vars/all/
    ├── main.yml          # Ingress mode, URLs, storage sizing
    └── openbao_secrets.yml # AppRole role_id + secret metadata
```

---

## Multi-Cluster Federated Design

The platform spans three k3s clusters, each with a distinct role:

### 1. `dmf-infra` — Primary Operating Cluster

- **Layers**: 2 (Host Platform) + 3 (Container Platform) + 6 (Application & UI, non-media)
- **Verticals**: Security (Authentik, token rotation), Orchestration (AWX), Monitoring (Prometheus, Grafana, LibreNMS), Control (runbooks, drills)
- **Services**: NetBox SoT, AWX automation, Grafana dashboards, Prometheus, Loki, Forgejo (internal repo), landing page
- **Auth**: Per-cluster Authentik instance (currently); federated to dmf-central in production
- **Inventory**: Lives in private `dmf-env` repo

### 2. `dmf-central` — Deploy-Once Hub (Scaffold)

- **Layers**: 2 + 3 + part of 6
- **Purpose**: Central identity provider (Authentik federation anchor), secrets vault (OpenBao), artifact registry (Zot OIDC)
- **Design**: Single deployment, cross-cluster federation point
- **Status**: Placeholder (3-commit scaffold); not yet wired to downstream clusters
- **Hypothesis to test**: Authentik federation across clusters works cleanly with passkey enrollment

### 3. `dmf-media` — Media Domain Scaffold

- **Layers**: 4 (Media Exchange) + 5 (Media Functions) + part of 3 (media-specific registry, PTP)
- **Services**: NMOS IS-04/05 registry, EBU LIST 2110 packetized media, PTP daemon, NetBox media plugin
- **Status**: Placeholder (3-commit scaffold); no NMOS deployed yet
- **Hypothesis to test**: NMOS + LIST 2110 deploy cleanly on commodity k3s without custom resource controllers

---

## Console & Data Flow: dmf-cms

The operator-facing console (`dmf-cms`) is a **FastAPI + Jinja2 + HTMX + SSE** application running on Layer 6.

### Navigation Structure

- **Overview**: Platform summary, app catalog, onboarding
- **Facility**: Inventory and topology (cluster nodes, endpoints, storage summary, certificate status)
- **Workflows**: Approved AWX jobs (stack verify, endpoint checks, registration dry-runs, remediation)
- **Monitoring**: Alert rollups, probe health, Prometheus target state, certificate expiry, node/PVC risk
- **Changes**: Open PRs, CI state, deploy verification, change history
- **Admin**: Backend connectivity, OIDC status, service account health, feature flags, app contract registry
- **Settings**: Invitation/passkey enrollment via Authentik

### App Contract Model

Applications are declared via **YAML app-contract** — a static fixture defining:
- Key, display name, lane (public/private)
- Summary and deep-links
- Source and lifecycle stage

Currently loaded from fixture; will evolve to live backend discovery (Layer 4/5 NMOS, Layer 6 Forgejo repos).

### Data Integration Points

- **NetBox API**: Cluster topology, facility metadata, IP assignments
- **AWX API**: Job templates, execution results, RBAC
- **Prometheus API**: Alert state, target health, metrics
- **NMOS IS-04/05 API** (Layer 4): Device discovery, sender/receiver registration
- **Authentik OIDC**: User auth, passkey enrollment, federated IdP

---

## Commit Gate: Experiment → Commitment Transition

Currently in **experiment phase** (no commitment). Transition to **commitment phase** when:

> **GATE:** dmf-cms release-1 first vertical slice (Workflows) is running **end-to-end against real backends** (AWX job execution) **AND** one NMOS IS-04/05 registry is deployed in `dmf-media` and running for 24+ hours.

When the gate closes:
- Freeze foundational architecture (two-repo model, layer/vertical taxonomy, multi-cluster design)
- Move forward with operational hardening (Prometheus alerts, NetBox backups, token rotation, breakglass access)
- Write `docs/architectural-commitments-v1.md` documenting what we've locked in and why

**Estimated timeline**: 1–2 weeks with focused effort.

---

## Experiment Phase: Three De-Risking Moves

Until the gate closes, work focuses on falsifying architectural assumptions, not polishing surfaces.

### Move 1: NMOS IS-04/05 Spike in `dmf-media` (Highest Risk)

**Thesis to validate**: NMOS + EBU LIST 2110 can deploy cleanly on commodity k3s with standard resources.

**Scope**: Deploy one NMOS registry (e.g., BBC's `nmos-cpp` or EBU's reference), wire it into NetBox media plugin, register one mock sender/receiver, run for 24 hours.

**What we'll learn**:
- Whether Layer 4 architecture survives contact with the actual protocol
- What custom resource shapes we actually need
- Whether the EBU V2.0 taxonomy holds under media-domain playbooks

**Effort**: ~1 day | **Blocker for gate**: YES

### Move 2: dmf-cms Vertical Slice to Real AWX (Highest Leverage)

**Thesis to validate**: dmf-cms data model, AWX integration, NetBox-to-inventory pipeline actually work end-to-end.

**Scope**: Wire dmf-cms Workflows page to a real AWX job template execution against NetBox-derived inventory. Incidentally closes April P0 item (AWX loop never closes).

**What this falsifies**:
- The dmf-cms `app-contract` model survives a real backend
- NetBox's SoT schema provides enough data to drive AWX inventory the way we'd commit to
- Runtime auth composition (Authentik OIDC → AWX RBAC → NetBox token) actually works

**Effort**: ~2–3 hours | **Blocker for gate**: YES

**Side effect**: Closes April P0 item without extra work.

### Move 3: Commit the DMF Platform Plan (Visibility)

**Thesis to validate**: Architecture is visible to outsiders and future-you.

**Scope**: This document. Commit to `dmf-infra/docs/dmf-platform-plan.md` (canonical location every README already points to).

**What this unblocks**:
- Goal (i): Credible artifact for broadcast domain (visible, versioned, iterable)
- Goal (iii): OSS foundation (can't ship what you can't show)
- Future-you: Can iterate on the plan as a tracked artifact, not a phantom path

**Effort**: ~30 min | **Blocker for gate**: NO (but required for credibility/OSS goals)

---

## What NOT to Do in Experiment Phase

**Don't add Prometheus alerts yet.** You'll pick SLO names and alert thresholds you'll regret once you've actually operated the system. Alerts are reversible later but create soft lock-in (dashboards, runbooks, on-call expectations).

**Don't back up NetBox PostgreSQL yet.** Schema is still being reshaped. Backup what you've decided to commit to, not what you're still experimenting with.

**Don't fold dmf-central or dmf-media back into dmf-infra.** Keeping them as scaffolds is cheap; re-splitting later is expensive. They exist as reservations for the theses Moves 1 and 2 will test.

**Don't reorganise the EBU layering yet.** Find the first playbook that genuinely breaks the taxonomy in practice (likely something in Move 1's NMOS work), then decide whether to evolve the taxonomy.

---

## Breaking Down the Approach: Single Sentence

You're in the right phase doing the wrong type of work in it.

**Experimentation should produce falsifying evidence, not polished surfaces.** Moves 1 and 2 will teach you more about whether the architecture survives contact with reality than a month of console-shell polish or playbook reorganisation. Do those three moves, lock in the gate, then harden the committed architecture.

---

## Repository Structure

```
dmf-infra/                       ← public, generic (this repo)
├── k3s-lab-bootstrap/
│   ├── site.yml                     ← top-level entry
│   ├── lifecycle-*.yml              ← EBU lifecycle stages
│   ├── playbooks/
│   │   ├── 200–299/*.yml            ← Layer 2
│   │   ├── 300–399/*.yml            ← Layer 3
│   │   ├── 600–699/*.yml            ← Layer 6
│   │   └── vertical-*/*.yml         ← Verticals
│   ├── roles/
│   │   ├── base/                    ← Layers 2–3, core verticals
│   │   ├── stack/operator/          ← Layer 6 apps
│   │   ├── modules/                 ← Vertical extensions
│   │   └── common/                  ← Shared utilities
│   ├── charts/                      ← Helm charts
│   ├── inventories/example/         ← Template (copy to dmf-env)
│   └── docs/
│       ├── dmf-platform-plan.md     ← this file
│       ├── authentik.md
│       ├── awx.md
│       ├── netbox-deployment-notes.md
│       ├── landing-page.md
│       └── ...
└── ...

dmf-env/                         ← private, site-specific
├── inventories/
│   └── hetzner-arm/                 ← active environment
│       ├── hosts.ini
│       └── group_vars/all/
│           ├── main.yml
│           └── openbao_secrets.yml
├── terraform/
│   └── hetzner-arm/
├── bin/
│   ├── run-playbook.sh
│   ├── tf-apply.sh
│   └── ...
└── ...

dmf-central/                         ← scaffold (in progress)
└── (central IdP, secrets, registry roles — not yet wired)

dmf-media/                       ← scaffold (in progress)
└── (NMOS, LIST 2110, PTP roles — not yet deployed)
```

---

## Next Steps

1. **Execute Move 1** (NMOS spike): Validate Layer 4 deployability
2. **Execute Move 2** (dmf-cms slice): Validate data flow and architecture
3. **Close the commit gate**: Lock in architectural decisions
4. **Transition to commitment phase**: Begin operational hardening (alerts, backups, token rotation)

---

## Related Documentation

- **EBU DMF Reference Architecture V2.0** — Layer/vertical/lifecycle definitions
- **[authentik.md](authentik.md)** — Passwordless bootstrap and passkey enrollment
- **[awx.md](awx.md)** — AWX subpath routing and integration
- **[netbox-deployment-notes.md](netbox-deployment-notes.md)** — Troubleshooting and field notes
- **[landing-page.md](landing-page.md)** — Dynamic app portal architecture
- **[integration-sot.md](integration-sot.md)** — NetBox → AWX integration
- **[CLAUDE.md](../CLAUDE.md)** — Operational guidance and common commands
