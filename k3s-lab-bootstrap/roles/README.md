# Role layout — EBU DMF mapping

This directory follows the EBU DMF Reference Architecture V2.0 (2026-04-15)
six-layer + four-vertical model. The role tree predates the playbook
renumbering and has not been moved; the table below is the canonical
mapping from role directory → EBU layer/vertical.

See `dmfdeploy/docs/architecture/DMF EBU Mapping (2026-04-25).md` for the full
old↔new playbook map and EBU vocabulary canon.

## Layer mapping

| Role directory | EBU scope |
|---|---|
| `base/k3s/`, `base/cluster-ready/` | Layer 3 — Container Platform (orchestrator) |
| `base/ingress/`, `base/cert-manager/`, `base/tailscale/` | Layer 3 — Container networking + TLS |
| `base/longhorn/` | Layer 3 — Container Platform (persistent storage) |
| `base/harden/` | Layer 2 — Host Platform (OS hardening; EBU §Host Platform §Security) |
| `base/post-bootstrap-verify/` | Layer 3 — Container Platform verify gate |
| `base/prometheus/`, `base/grafana/`, `base/loki/` | Vertical-monitoring |
| `base/external-secrets/` | Vertical-orchestration (ESO) |
| `base/storage-slot/`, `base/lb-slot/` | Layer 3 — pluggable platform slots |
| `stack/operator/openbao/` | Vertical-security (secret store) |
| `stack/operator/authentik/` | Vertical-security (IdP / AAA) |
| `stack/operator/oauth2-proxy/` | Vertical-security (access enforcement) |
| `stack/operator/loki/` | Vertical-monitoring (operator-stack overlay) |
| `stack/operator/netbox/`, `forgejo/`, `awx/`, `cms/` | Layer 6 — Application & UI |
| `stack/operator/netbox-sot/`, `awx-integration/` | Layer 6 — App integration glue |
| `stack/operator/landing-page/` | Layer 6 — public landing portal |
| `stack/operator/event-glue/`, `alert-rules/` | Vertical-monitoring (operator overlays) |
| `stack/standalone/awx-standalone/`, `netbox-standalone/`, `landing-page/` | Layer 6 alternate (Flypack profile) |
| `modules/infra-monitoring/` (LibreNMS et al.) | Vertical-monitoring extension |
| `modules/advanced/` (ArgoCD, federation) | Vertical-orchestration extension |
| `common/app-admin-facts/` | Utilities used across layers/verticals |

## Layer 4 + 5 (Media Exchange, Media Functions)

Not in this repo. They live in `$DMFDEPLOY_UMBRELLA/dmf-media/roles/` (nmos-cpp,
ebu-list, flow-exporters, ptp-monitor, netbox-media-plugin, media-controllers).

## Layer 1 (Infrastructure)

Provider-specific provisioning (Hetzner CCM, hcloud server creation, network
zones) lives in `$DMFDEPLOY_UMBRELLA/dmf-env/bin/provision-nodes.sh` and the
`tasks/hetzner_*.yml` files. From the EBU model's perspective the operator
host invoking those scripts is the Layer-1 orchestrator.
