# Cluster Ready Gate

The cluster ready gate is a shared Ansible role that waits for core cluster services before deploying applications. It prevents race conditions on fresh cluster boots or after reboots when apps might start before DNS, ingress, or storage are available.

## Overview

Without a readiness gate, apps can start too early and fail in confusing ways (missing DNS, missing ingress, storage not attached). This role ensures deployments are calmer and more predictable by waiting for infrastructure dependencies first.

## Checks Performed

The role waits for these conditions:

| Check | Description |
|-------|-------------|
| Nodes | All cluster nodes report `Ready=True` |
| CoreDNS | CoreDNS deployment is ready in `kube-system` |
| Traefik | Traefik deployment is ready in `kube-system` |
| Longhorn CSIDriver | `driver.longhorn.io` CSIDriver exists (if Longhorn installed) |
| Longhorn DaemonSet | `longhorn-csi-plugin` DaemonSet is fully ready (if Longhorn installed) |
| Longhorn StorageClass | `longhorn` StorageClass exists (optional) |

All checks are read-only and do not rely on hostPath mounts, making them safe on hardened clusters.

## Usage

Include the role at the start of application playbooks:

```yaml
roles:
  - ../roles/cluster-ready
  - ../roles/<app>
```

The following playbooks use this role:

- `playbooks/22-landing-page.yml`
- `playbooks/25-prometheus.yml`
- `playbooks/26-loki.yml`
- `playbooks/27-grafana.yml`
- `playbooks/28-promtail.yml`
- `playbooks/30-netbox.yml`
- `playbooks/31-forgejo.yml`
- `playbooks/40-netbox-sot.yml`
- `playbooks/41-forgejo-bootstrap.yml`
- `playbooks/32-librenms.yml`
- `playbooks/35-awx.yml`

## Configuration

Defaults are in `roles/cluster-ready/defaults/main.yml`:

| Variable | Default | Description |
|----------|---------|-------------|
| `cluster_ready_retries` | `30` | Number of retry attempts |
| `cluster_ready_delay` | `10` | Seconds between retries |
| `cluster_ready_wait_for_longhorn` | `true` | Wait for Longhorn CSI if installed |
| `cluster_ready_wait_for_storageclass` | `true` | Wait for Longhorn StorageClass |

Override these in inventory or group_vars if needed.
