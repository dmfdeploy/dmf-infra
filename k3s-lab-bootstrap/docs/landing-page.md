# Landing Page

The landing page provides a static open-source project showcase at the cluster's root URL (`/`) with live cluster stats from Prometheus.

## Features

- **OSS Project Showcase**: Displays cards for every open-source project powering the DMF Platform, with logo, license, description, and link to the project site.
- **Live Cluster Stats**: CPU, RAM, and Disk meters powered by Prometheus queries, refreshing every 15 seconds.
- **Static content**: All OSS project data is embedded at deploy time — no Kubernetes API queries needed.

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                     Landing Page Pod                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────┐    ┌─────────────────────────────────┐    │
│  │  Init Container │    │         Main Containers         │    │
│  │  (generate-html)│    │                                 │    │
│  │                 │    │  ┌───────────┐  ┌───────────┐  │    │
│  │  Renders HTML   │───▶│  │   nginx   │  │regenerator│  │    │
│  │  + stats.json   │    │  │  (serve)  │  │ (60s loop)│  │    │
│  │                 │    │  └───────────┘  └───────────┘  │    │
│  └─────────────────┘    │       │               │        │    │
│                         └───────┼───────────────┼────────┘    │
│                                 │               │              │
│                         ┌───────▼───────────────▼────────┐    │
│                         │      Shared Volume (/html)      │    │
│                         │   index.html  +  stats.json     │    │
│                         └─────────────────────────────────┘    │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

## Components

### Generator Script

The `generate-html.sh` script:
1. Reads the embedded OSS project data from `/scripts/oss-projects.sh` (mounted from a ConfigMap).
2. Queries Prometheus for cluster stats (CPU, RAM, Disk, nodes/pods/namespaces).
3. Generates:
   - `index.html` (the OSS showcase landing page)
   - `stats.json` (cluster stats for dynamic meter refresh)

### Deployment Structure

- **Init container**: Generates initial HTML on pod startup.
- **nginx container**: Serves the static HTML from `/usr/share/nginx/html`.
- **regenerator sidecar**: Re-runs the generator every 60 seconds (configurable) and writes updated `stats.json`.

Static assets (e.g. logos) are copied from an assets ConfigMap into `/html/assets` so nginx can serve them.

## Configuration

### Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `landing_page_namespace` | `default` | Namespace for landing page resources |
| `landing_page_title` | `DMF Platform` | Title displayed on the page |
| `landing_page_tagline` | `Open Source Infrastructure Lab` | Subtitle under the title |
| `landing_page_logo_url` | `` (empty) | Logo URL. Set to a path under `/assets/` if you add a logo file to the assets ConfigMap. |
| `landing_page_repo_url` | `` (empty) | External repository URL for the "clone or fork" link |
| `landing_page_prometheus_url` | `http://prometheus-server.monitoring.svc.cluster.local/prometheus` | Prometheus endpoint for cluster stats |
| `landing_page_brand_bg` | `#0b1c2c` | Page background color |
| `landing_page_brand_bg_alt` | `#12263a` | Surface color for cards/header |
| `landing_page_brand_accent` | `#f58220` | Accent color |
| `landing_page_brand_text` | `#f5f7fa` | Primary text color |
| `landing_page_brand_muted` | `#9fb2c7` | Muted text color |

### Cluster Stats Block (Header)

The right side of the header includes a compact stats block (CPU/RAM/Disk meters + total counts).

**Source of data (Prometheus):**
- CPU usage: `sum(rate(node_cpu_seconds_total{mode!="idle"}[5m]))`
- CPU capacity: `sum(machine_cpu_cores)`
- Memory used: `sum(node_memory_MemTotal_bytes - node_memory_MemAvailable_bytes)`
- Memory capacity: `sum(node_memory_MemTotal_bytes)`
- Disk used: `sum(kubelet_volume_stats_used_bytes)`
- Disk capacity: `sum(kubelet_volume_stats_capacity_bytes)`
- Nodes count: `count(kube_node_info)`
- Pods count: `sum(kube_pod_status_phase{phase="Running"})`
- Namespaces count: `count(kube_namespace_status_phase{phase="Active"}) or count(kube_namespace_labels)`

If any query returns no data, the UI falls back to `n/a`.

### Adding / Removing OSS Projects

Edit `templates/oss-projects.sh.j2`. Each line follows the format:

```
NAME|LOGO_URL|LICENSE|HOMEPAGE|DESCRIPTION
```

Blank lines are skipped. The file is sourced as a shell variable `OSS_PROJECTS` inside the generator script. After changing the template, restart the deployment:

```bash
kubectl rollout restart deployment/landing-page -n default
kubectl rollout status deployment/landing-page -n default
```

### Adding a Logo

Place the logo file in the `landing-page-assets` ConfigMap and set:

```yaml
landing_page_logo_url: "/assets/logo.svg"
landing_page_logo_alt: "My Org"
```

## Deployment

```bash
ansible-playbook playbooks/600-landing-page.yml --ask-vault-pass
```

## Troubleshooting

### Check generated HTML

```bash
kubectl exec -n default deployment/landing-page -c nginx -- cat /usr/share/nginx/html/index.html
```

### Check generator logs

```bash
# Init container logs (only available briefly after pod start)
kubectl logs -n default deployment/landing-page -c generate-html

# Regenerator logs
kubectl logs -n default deployment/landing-page -c regenerator
```

### Force regeneration

Delete the pod to trigger the init container:

```bash
kubectl delete pod -l app=landing-page -n default
```

### Picked-up changes (ConfigMap gotchas)

ConfigMap updates do not automatically roll the deployment. If you change the generator script, OSS data, or assets, restart the deployment:

```bash
kubectl rollout restart deployment/landing-page -n default
kubectl rollout status deployment/landing-page -n default
```

### /assets not working (logo 404)

The IngressRoute must include `PathPrefix(/assets)` so Traefik routes logo requests to the landing page service.

### Read-only mount error for assets

Mounting a ConfigMap directly into `/usr/share/nginx/html/assets` fails because the parent path is read-only. The fix is to mount the assets at `/assets-src` and copy them into `/html/assets` (the shared writable volume) from the generator.
