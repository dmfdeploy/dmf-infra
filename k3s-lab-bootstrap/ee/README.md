# DMF AWX Execution Environment

Build context for the shared AWX EE image consumed by:

- the in-cluster ansible runner pod (bootstrap-configure 69x chain; see
  `../roles/stack/operator/ansible-runner/`)
- AWX-spawned media catalog launchers (`media-*` JTs)

Per [ADR-0025](../../../docs/decisions/0025-ansible-in-cluster-pods-and-catalog-helm.md)
and the [convergence plan](../../../docs/plans/DMF%20Cluster-Internal%20Ansible%20Execution%20and%20Catalog%20Helm%20Pivot%20Plan%202026-05-19.md).

## Image flow

```
quay.io/ansible/awx-ee:24.6.1  (upstream base)
            │
            │   ansible-builder build (scripts/build.sh)
            │   + DMF requirements.yml / .txt / bindep.txt + OCI labels
            ▼
registry.dmf.example.com/dmf/awx-ee:<tag>   (local Colima build artifact)
            │
            │   scripts/publish-to-ghcr.sh (operator workstation)
            ▼
ghcr.io/dmfdeploy/awx-ee:<tag>              (public source of truth — Lane A)
            │
            │   playbooks/630-zot-seed-platform.yml (Stage 4b; cluster-side)
            ▼
zot.zot.svc.cluster.local:5000/dmf/awx-ee:<tag>  (runtime registry — Lane A consumers)
```

## Files

| Path | Purpose |
|---|---|
| `execution-environment.yml` | ansible-builder v3 spec (base + deps + labels) |
| `requirements.yml` | Ansible Galaxy collections (kubernetes.core, community.general, ansible.posix, community.docker) |
| `requirements.txt` | Python packages (kubernetes, openshift, jsonpatch) |
| `bindep.txt` | System packages (helm, git, ca-certificates) |
| `scripts/build.sh` | Wraps `ansible-builder build` with the right `DOCKER_HOST`, IMAGE_VERSION, VCS_REF |
| `scripts/publish-to-ghcr.sh` | Operator-side push to `ghcr.io/dmfdeploy/awx-ee` (token via stdin, isolated DOCKER_CONFIG) |
| `../playbooks/630-zot-seed-platform.yml` | Cluster-side mirror from GHCR digest into cluster-internal Zot |

## Operator workflow

### Prerequisites

- macOS with Colima (`colima start docker-build`) — produces arm64 images.
- Docker CLI installed.
- `ansible-builder` >= 3.0: `pip install 'ansible-builder>=3.0'` (recommended in a venv or via `uv tool install ansible-builder`).
- A GitHub Personal Access Token with `write:packages` scope, stored in macOS Keychain (or any password manager that can pipe to stdin).

### Build

```bash
cd ~/repos/dmfdeploy/dmf-infra/k3s-lab-bootstrap/ee
IMAGE_VERSION=0.1.0 scripts/build.sh
```

Output: local image `registry.dmf.example.com/dmf/awx-ee:0.1.0` at arm64/linux,
~535 MB content. First build pulls the `awx-ee:24.6.1` base (~1 GB);
subsequent rebuilds reuse cached layers and finish in seconds.

Inspect labels:

```bash
DOCKER_HOST=unix://$HOME/.colima/docker-build/docker.sock \
  docker image inspect registry.dmf.example.com/dmf/awx-ee:0.1.0 \
    --format '{{json .Config.Labels}}' | python3 -m json.tool
```

### Publish to GHCR

```bash
# From macOS Keychain (recommended — see top-level README for Keychain setup):
security find-generic-password -s "ghcr.io" -a "<your-github-username>" -w \
  | GHCR_USER="<your-github-username>" scripts/publish-to-ghcr.sh

# Or interactive (paste token at prompt; won't echo):
scripts/publish-to-ghcr.sh
```

The script:

- Verifies the local image exists.
- Uses isolated `DOCKER_CONFIG` so the GHCR token doesn't bleed into `~/.docker/config.json`.
- Retags to `ghcr.io/dmfdeploy/awx-ee:0.1.0` and pushes.
- Reports the resulting digest.

### Post-publish (GitHub UI, operator action)

1. Open https://github.com/orgs/dmfdeploy/packages.
2. Find the new `awx-ee` package.
3. **Keep package PRIVATE** until in-cluster pull verification passes (post-rebuild). Then promote to Public.
4. Link the package to the source repo (`dmf-infra`) under package Settings.

### Cluster-side mirror (when cluster comes up)

```bash
cd ~/repos/dmfdeploy/dmf-env
bin/run-playbook.sh <env> ../dmf-infra/k3s-lab-bootstrap/playbooks/630-zot-seed-platform.yml
```

This pulls the GHCR image and pushes it into the cluster-internal Zot at
`registry.dmf.example.com/dmf/awx-ee:0.1.0`. Required before any pod
that references the Zot path can start (e.g., the in-cluster ansible
runner Job, or AWX EE pods for catalog launchers).

## Version bumps

1. Decide a new semver (e.g., `0.2.0` for a collection version bump).
2. Update collections/python/system requirements as needed.
3. `IMAGE_VERSION=0.2.0 scripts/build.sh`
4. Smoke-test locally: pull a Galaxy collection inside the new image
   to confirm versions resolve.
5. `IMAGE_TAG=0.2.0 scripts/publish-to-ghcr.sh` — pushes to a new tag.
6. Re-run playbook 630-zot-seed-platform.yml against the cluster to mirror the new tag into Zot.
7. Bump `awx_ee_catalog_tag` and `ansible_runner_image_tag` in role defaults.
8. Re-run 050-ansible-runner.yml + any 69x play to validate.

## Known constraints

### Base image Python 3.9 ↔ ansible-core 2.16+ incompatibility

`quay.io/ansible/awx-ee:24.6.1` ships with Python 3.9 and ansible-core
2.15.x. `ansible-core >= 2.16` requires Python ≥ 3.10. **Do not
re-pin `ansible_core` in `execution-environment.yml`** — the
`dependencies.ansible_core: package_pip: ansible-core>=2.16` directive
asks pip to upgrade the in-image ansible-core, which fails:

```
ERROR: Could not find a version that satisfies the requirement
ansible-core<2.18,>=2.16
```

This trap caught the first build attempt (2026-05-19); fix landed in
`dmf-infra@424a795`.

**To upgrade ansible-core** later: bump the awx-ee base tag in
`execution-environment.yml` to a build that ships with Python ≥ 3.11
(check upstream at https://quay.io/repository/ansible/awx-ee for tags
post-25.x). Then the `ansible_core` pin can be reintroduced if explicit
version assertion is desired.

### Containerfile ARG / LABEL substitution

The OCI labels in `additional_build_steps.append_final` use Containerfile
`ARG`/`${VAR}` substitution. `scripts/build.sh` passes `IMAGE_VERSION`
and `VCS_REF` via `--build-arg`. The pattern works because each `ARG`
declaration precedes the matching `LABEL` in the same final stage.

Verified embedded in the 2026-05-19 build:

```
"org.opencontainers.image.revision": "<short-sha>"
"org.opencontainers.image.version":  "0.1.0"
```

If labels show literal `${VAR}` strings instead of substituted values,
the `ARG` declaration is in the wrong stage; check that the relevant
`ARG VAR=...` line in `additional_build_steps.append_final` comes
**before** the `LABEL ... = "${VAR}"` line.

### Collections must be ansible-core-2.15 compatible

Until the base image bumps Python, every collection in `requirements.yml`
must support ansible-core 2.15. Current pins (kubernetes.core 5.x,
community.general 9.x, ansible.posix 1.x, community.docker 3.x) all do.
When bumping, check the collection's `requires_ansible` in its
`runtime.yml` before pinning to a newer major.

## Why this lives in `dmf-infra` and not `dmf-runbooks`

This EE is consumed by **infrastructure** plays (the bootstrap-configure
69x chain). Catalog launcher playbooks live in `dmf-runbooks` but
*also* consume this EE — they reference the same image. The build
context lives wherever the canonical consumer's role lives; in this
case, the runner-pod role at `roles/stack/operator/ansible-runner/`
is the load-bearing consumer, so the EE belongs in dmf-infra.

## See also

- [ADR-0025](../../../docs/decisions/0025-ansible-in-cluster-pods-and-catalog-helm.md)
- [Convergence plan §5 Lane A](../../../docs/plans/DMF%20Cluster-Internal%20Ansible%20Execution%20and%20Catalog%20Helm%20Pivot%20Plan%202026-05-19.md)
- [Public registry plan §3.2 + §11](../../../docs/plans/DMF%20Public%20Container%20Registry%20Publishing%20Plan%202026-05-19.md)
- [Runner-pod plan (Lane C)](../../../docs/plans/DMF%20In-Cluster%20Ansible%20Runner%20Pod%20Implementation%20Plan%202026-05-14.md)
- ADR-0007 (secrets never in argv)
- ADR-0010 (`bin/run-playbook.sh` is the sanctioned ansible entry)
