# NetBox Deployment Notes

This document describes the challenges encountered deploying NetBox on k3s with Longhorn storage and Traefik ingress, and how they were resolved.

## Overview

NetBox v4.5.0 is deployed through a local wrapper chart at
`k3s-lab-bootstrap/charts/netbox/` that pins the upstream
[netbox-community Helm chart](https://github.com/netbox-community/netbox-chart)
(v7.3.0) as a vendored subchart. Env-specific values come from
`roles/stack/operator/netbox/templates/values.yml.j2` and are passed to Helm at
install time by the role. See "Wrapper chart layout" below for how to change
the pinned version.

The deployment required solving several issues related to:

1. Outdated container image tags
2. Redis/Valkey authentication
3. Longhorn RWX volume support
4. URL path handling with Traefik
5. Static asset routing
6. API_TOKEN_PEPPERS requirement (NetBox v4.x)

## Upgrade History

- **Initial deployment**: bootc/netbox chart v4.1.1 with NetBox v3.x
- **Current deployment**: netbox-community chart v7.3.0 with NetBox v4.5.0

## Issues and Solutions

### 1. Bitnami Image Tags Not Found

**Problem:** Bitnami has deprecated version-specific tags from their public Docker Hub registry. Tags like `bitnami/postgresql:15` or `bitnami/valkey:8.0` no longer exist.

**Solution:** Use the `latest` tag for Bitnami images:

```yaml
postgresql:
  image:
    registry: docker.io
    repository: bitnami/postgresql
    tag: latest

valkey:
  image:
    registry: docker.io
    repository: bitnami/valkey
    tag: latest
```

**Note:** As of August 2025, Bitnami evolved their public catalog to offer only hardened images under the Bitnami Secure Images initiative, removing non-latest tags.

**Related commits:**
- `0ced50e` - Add bitnami image overrides with latest tag
- `0f8efce` - Use valkey:latest tag - Bitnami removed version tags

### 2. Redis to Valkey Migration

**Problem:** NetBox v4.x chart uses Valkey (Redis fork) instead of Redis. The configuration structure changed.

**Solution:** Update values to use Valkey configuration:

```yaml
# Old (bootc chart with Redis)
redis:
  enabled: true
  auth:
    enabled: true
    password: <netbox-redis-password>

# New (netbox-community chart with Valkey)
valkey:
  enabled: true
  auth:
    enabled: true
    password: <netbox-valkey-password>
  master:
    persistence:
      enabled: true
      storageClass: longhorn
      size: 1Gi
  replica:
    replicaCount: 0
```

**Related commit:** `88e8ee2` - Upgrade NetBox to v4.5.0 via netbox-community chart 7.3.0

### 3. Longhorn RWX Volume Issues

**Problem:** The `netbox-media` PVC needs to be shared between `netbox` and `netbox-worker` pods. Using `ReadWriteOnce` caused `Multi-Attach` errors.

**How Longhorn RWX Works:**
- Longhorn creates a `share-manager` pod that runs an NFSv4 server
- Requires `nfs-common` package on all nodes (installed by default on Debian 12)
- More details: https://longhorn.io/docs/1.9.1/nodes-and-volumes/volumes/rwx-volumes/

**Solution:** Use `ReadWriteMany` access mode:

```yaml
persistence:
  enabled: true
  storageClass: longhorn
  size: 1Gi
  accessMode: ReadWriteMany
```

**Related commits:**
- `80febcb` - Use ReadWriteMany for NetBox media volume
- `fdb5842` - Fix NetBox RWX volume and valkey image tag

### 4. Readiness/Liveness Probe Failures

**Problem:** Both the bootc and netbox-community charts have a bug constructing probe paths when `basePath` is set. With `basePath: /netbox`, the probe path becomes `//netboxlogin/` instead of `/netbox/login/`.

**Solution:** Disable the probes entirely:

```yaml
basePath: /netbox

startupProbe:
  enabled: false
readinessProbe:
  enabled: false
livenessProbe:
  enabled: false
```

**Related commits:**
- `9fff366` - Use basePath with disabled probes for NetBox
- `d830047` - Disable probes - netbox-community chart has same basePath bug

### 5. Static Asset Routing

**Problem:** NetBox with `basePath: /netbox` generates URLs like `/netbox/static/...` in HTML, but the application serves static files at `/static/` (without the prefix).

**Solution:** Use Traefik StripPrefix middleware specifically for the static path:

```yaml
# Middleware to strip /netbox prefix
apiVersion: traefik.io/v1alpha1
kind: Middleware
metadata:
  name: netbox-stripprefix
spec:
  stripPrefix:
    prefixes:
      - /netbox

# IngressRoute with two routes:
routes:
  - match: "PathPrefix(`/netbox/static`)"
    middlewares:
      - name: netbox-stripprefix
    services:
      - name: netbox
        port: 80
  - match: "PathPrefix(`/netbox`)"
    services:
      - name: netbox
        port: 80
```

**Related commit:** `a5a0a35` - Add StripPrefix for NetBox static assets

### 6. API_TOKEN_PEPPERS Requirement (NetBox v4.x)

**Problem:** NetBox v4.5 introduced v2 API tokens which require cryptographic peppers. Without `API_TOKEN_PEPPERS` defined, NetBox logs:
```
API_TOKEN_PEPPERS is not defined. v2 API tokens cannot be used.
```

If defined but too short:
```
Invalid pepper 1: Pepper must be at least 50 characters in length.
```

**Solution:** Create a Kubernetes Secret with the pepper configuration and reference it via `extraConfig`:

```yaml
# Secret with API_TOKEN_PEPPERS
apiVersion: v1
kind: Secret
metadata:
  name: netbox-extra-config
  namespace: netbox
type: Opaque
stringData:
  extra.yaml: |
    API_TOKEN_PEPPERS:
      1: "<random-base64-string-at-least-50-chars>"

# In values.yml
extraConfig:
  - secret:
      secretName: netbox-extra-config
```

Generate a suitable pepper with: `openssl rand -base64 48`

**Related commits:**
- `88f3616` - Add API_TOKEN_PEPPERS config for NetBox v4.x API tokens
- `1325047` - Fix API_TOKEN_PEPPERS - use 64-char random base64 string

### 7. OpenBao-backed Runtime Secret

**Problem:** NetBox DB and Valkey credentials were being generated on each playbook run. That drifted from the already-persisted PostgreSQL volume and caused `password authentication failed for user "netbox"` after a redeploy.

**Solution:** Persist the NetBox runtime credentials in OpenBao and read them back during the role run:

```yaml
netbox_runtime_secret_path: secret/apps/netbox/runtime
```

The role now reads `db_password`, `valkey_password`, and `api_token_pepper` from that path and writes them back if missing.

**Related change:** NetBox now uses a stable OpenBao runtime secret instead of `lookup('password', '/dev/null ...')` for DB and Valkey credentials.

## Final Configuration (NetBox v4.5.0)

### values.yml.j2

```yaml
---
# NetBox v4.5.0 via netbox-community chart 7.3.0

superuser:
  name: admin
  email: admin@example.com
  password: <vault_netbox_superuser_password>

# Base path for running behind reverse proxy
basePath: /netbox

# Extra config for API_TOKEN_PEPPERS (required for v2 API tokens)
extraConfig:
  - secret:
      secretName: netbox-extra-config

# Disable probes - chart has bug constructing basePath in probe URLs
startupProbe:
  enabled: false
readinessProbe:
  enabled: false
livenessProbe:
  enabled: false

# Media persistence (shared between netbox and worker - needs RWX)
persistence:
  enabled: true
  storageClass: longhorn
  size: 1Gi
  accessMode: ReadWriteMany

# PostgreSQL subchart
postgresql:
  enabled: true
  auth:
    username: netbox
    database: netbox
    password: <netbox-db-password>
    postgresPassword: <netbox-db-password>
  primary:
    persistence:
      enabled: true
      storageClass: longhorn
      size: 1Gi

# Valkey (Redis fork) subchart
valkey:
  enabled: true
  image:
    registry: docker.io
    repository: bitnami/valkey
    tag: latest
  auth:
    enabled: true
    password: <netbox-valkey-password>
  master:
    persistence:
      enabled: true
      storageClass: longhorn
      size: 1Gi
  replica:
    replicaCount: 0
```

### Traefik IngressRoute

Two routes are required:
1. `/netbox/static` - Uses StripPrefix middleware to serve static files
2. `/netbox` - Direct routing (NetBox handles basePath internally)

## Lessons Learned

1. **Bitnami images now only offer `latest` tag** - Version-specific tags were removed from the public catalog in 2025.

2. **NetBox v4.x uses Valkey instead of Redis** - Valkey is a Redis fork with compatible API. Update your configuration accordingly.

3. **API_TOKEN_PEPPERS is required for v2 API tokens** - Must be at least 50 characters. Generate with `openssl rand -base64 48`.

4. **RWX volumes need special handling** - Longhorn RWX requires NFSv4 and creates share-manager pods. Clean up namespaces completely between failed deployments.

5. **basePath probe bug exists in multiple charts** - Both bootc and netbox-community charts have this bug. Disable probes as a workaround.

6. **basePath and static assets are separate concerns** - An application may generate URLs with basePath but serve static files without it. Traefik middleware can bridge this gap.

7. **Wrapper chart values must be nested under the dependency name** - The local wrapper chart is named `netbox` and vendors an upstream dependency also named `netbox`. Role-rendered values must therefore be written under a top-level `netbox:` key. If values are rendered at the YAML root, the upstream dependency ignores them, which causes symptoms such as:
   - `BASE_PATH` rendered as `""` instead of `"/netbox"`
   - `netbox-media` PVC rendered as `ReadWriteOnce` instead of `ReadWriteMany`
   - worker pod `Multi-Attach` failures because the shared media volume is not RWX

8. **`kubernetes.core.helm wait` hid NetBox progress and stalled the playbook** - On the live Hetzner rollout (`2026-04-17`), the Helm task sat inside `wait: true` even though the useful readiness signals were the media PVC plus the `netbox` and `netbox-worker` Deployments. The role now uses `wait: false` and explicit `k8s_info` readiness checks instead.

9. **Persisted PostgreSQL data must keep the same OpenBao-backed password across redeploys** - The chart creates a PostgreSQL StatefulSet with a persistent volume. If a failed install leaves the DB volume behind and the next install uses a different `vault_netbox_db_password`, NetBox and its worker fail with `password authentication failed for user "netbox"`. The fix is operational, not chart-level:
   - keep `vault_netbox_db_password` stable for an existing NetBox database
   - only rotate it with a deliberate DB credential change
   - for a disposable failed rollout, wipe the namespace/PVs and reinstall cleanly

10. **NetBox's Authentik callback is `/oauth/complete/oidc/`** - The login button goes through `/oauth/login/oidc/`, but the authorize request sends `redirect_uri=https://netbox.dmf.example.com/oauth/complete/oidc/`. Authentik must register that exact callback, or it will reject the request with a redirect URI error.

## Wrapper chart layout

```
k3s-lab-bootstrap/
├── charts/netbox/
│   ├── Chart.yaml          # pins netbox-community/netbox 7.3.0
│   ├── Chart.lock          # committed
│   ├── values.yaml         # empty override (all env values come from Ansible)
│   ├── README.md
│   └── charts/
│       └── netbox-7.3.0.tgz   # vendored subchart (committed for offline install)
└── roles/stack/operator/netbox/
    ├── defaults/main.yml   # netbox_chart_stage_path (/tmp/netbox-chart on target)
    ├── tasks/main.yml      # copies charts/netbox/ to target, renders values, helm install
    └── templates/values.yml.j2
```

**Why wrap?**

- Version pinned in a reviewable file (`Chart.yaml`).
- `helm lint` and `helm template` work in CI without reaching the upstream repo.
- Installs work offline (flypack) because the subchart is vendored.
- Matches the pattern every other stack component will follow.

**How to change the pinned version:**

```bash
cd k3s-lab-bootstrap/charts/netbox
# edit Chart.yaml dependency version
helm dependency update
git add Chart.yaml Chart.lock charts/netbox-*.tgz
```

**Runtime flow (role `stack/operator/netbox`):**

1. Copies `charts/netbox/` from the controller to `netbox_chart_stage_path`
   (default `/tmp/netbox-chart`) on the target node.
2. Renders `values.yml.j2` to `/tmp/netbox-values.yml` on the target.
3. Runs `kubernetes.core.helm` with `chart_ref: {{ netbox_chart_stage_path }}`.
4. Waits explicitly for:
   - PVC `netbox-media` to become `Bound`
   - Deployment `netbox` to report ready replicas
   - Deployment `netbox-worker` to report ready replicas

## 2026-04-17 Hetzner rollout notes

This was the first live validation of the wrapper-chart version on the Hetzner ARM cluster.

### Failure 1: values were rendered at the wrong level

Observed symptoms:

- `helm get manifest netbox` showed `BASE_PATH: ""`
- `netbox-media` rendered as `ReadWriteOnce`
- `netbox-worker` hit a `Multi-Attach` error because the media PVC was not RWX

Root cause:

- `roles/stack/operator/netbox/templates/values.yml.j2` rendered overrides at the YAML root.
- The wrapper chart vendors `netbox-community/netbox` as a dependency named `netbox`, so all overrides had to be nested under top-level key `netbox:`.

Fix:

- nest the entire rendered values document under `netbox:`

### Failure 2: Helm wait stalled before ingress tasks

Observed symptoms:

- the playbook appeared hung in `Deploy NetBox via Helm`
- the useful readiness state had to be checked manually in-cluster

Fix:

- replace `wait: true` with `wait: false`
- add explicit waits for `netbox-media`, `netbox`, and `netbox-worker`

### Failure 3: stale PostgreSQL volume reused old credentials

Observed symptoms after the chart fix:

- `netbox` and `netbox-worker` both failed with:

```text
django.db.utils.OperationalError: connection failed: ... FATAL:  password authentication failed for user "netbox"
```

Root cause:

- a previous failed install had left PostgreSQL data behind
- the fresh reinstall used a different `vault_netbox_db_password`
- the chart created a new Kubernetes secret, but PostgreSQL still had the old on-disk credential state

Resolution used on the live cluster:

- delete the `netbox` namespace completely
- confirm the released PVs are gone
- reinstall from a clean namespace

Verification from the successful run:

- `helm get manifest netbox` showed `BASE_PATH: "/netbox"`
- `netbox-media` rendered with `ReadWriteMany`
- pods healthy:
  - `netbox`
  - `netbox-worker`
  - `netbox-postgresql-0`
  - `netbox-valkey-primary-0`

### Failure 4: Traefik CRDs cannot reference cross-namespace TLS secrets or redirect middleware

Observed symptoms after NetBox itself was healthy:

- `https://dmf.example.com/netbox` and `https://dmf.example.com/netbox/` returned `502`
- Traefik logged:

```text
Error configuring TLS: secret netbox/lab-<env>-tls does not exist
Failed to create middleware keys: middleware kube-system/global-https-redirect is not in the IngressRoute namespace netbox
```

Root cause:

- the cluster-wide certificate secret existed in `kube-system`, but the NetBox `IngressRoute` referenced it as if it were in the `netbox` namespace
- the HTTP redirect route referenced `global-https-redirect` across namespaces, which Traefik CRDs rejected

Fix:

- make the redirect middleware local to the `netbox` namespace
- use `tls: {}` on the NetBox `IngressRoute` so Traefik serves the cluster default `TLSStore` certificate instead of looking for a namespace-local copy
- delete and recreate the `IngressRoute` objects once, because `kubernetes.core.k8s state=present` merged the old object and left the stale `secretName` behind

Verification:

- from inside the cluster, `http://10.43.255.159/netbox/` returned `308` to HTTPS
- from a cluster node against the Hetzner LB IP with `Host: dmf.example.com`:
  - `/netbox` returned `308`
  - `/netbox/` returned `200`

### Broader follow-up

This Traefik CRD issue is not unique to NetBox. During the same live rollout, Traefik logs showed
the same pattern on other path-based roles:

- HTTPS `IngressRoute` objects referenced `lab-<env>-tls` as though the secret existed in
  the application namespace
- HTTP redirect routes referenced `kube-system/global-https-redirect` across namespaces

NetBox was fixed first because it was the blocking app under active rollout. The next cleanup pass
should normalize the same middleware/TLSStore pattern across Prometheus, Grafana, Loki, and any
other Traefik-CRD roles before more platform playbooks are added.

## Access

- URL: `http://<metallb_vip>/netbox/`
- Default credentials: `admin` / `<vault_netbox_superuser_password>`
- Version: NetBox 4.5.0
