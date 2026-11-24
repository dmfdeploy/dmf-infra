# DMF AWX Execution Environment

Build context for the shared AWX EE image consumed by:

- the in-cluster ansible runner pod (bootstrap-configure 69x chain; see
  `../roles/stack/operator/ansible-runner/`)
- AWX-spawned media catalog launchers (`media-*` JTs)

Per [ADR-0025](https://github.com/dmfdeploy/dmfdeploy/blob/main/docs/decisions/0025-ansible-in-cluster-pods-and-catalog-helm.md)
and the [convergence plan](https://github.com/dmfdeploy/dmfdeploy/blob/45eb326e973b10e0b52deef290981ceac9bcf5bd/docs/plans/DMF%20Cluster-Internal%20Ansible%20Execution%20and%20Catalog%20Helm%20Pivot%20Plan%202026-05-19.md).

## Image flow

```
quay.io/fedora/fedora-minimal:43@sha256:<index digest>   (upstream base)
            │
            │   ansible-builder build (scripts/build.sh)
            │   + ansible-core from ../requirements-ansible-core.txt
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

## Versions

The table describes image 0.2.0 as built on 2026-11-24 (linux/arm64).

| Item | Value | Where it is set |
|---|---|---|
| ansible-core | 2.21.6 | `../requirements-ansible-core.txt`, the only place |
| Python | 3.14.10 (Fedora 43 `python3`) | The base image; ansible-core 2.21 supports 3.12 to 3.14 |
| ansible-runner | 2.4.3 | `execution-environment.yml` |
| Base image | `quay.io/fedora/fedora-minimal:43` | `execution-environment.yml` |
| Base index digest | `sha256:119588c03182271a8980aba57b4dfd924fb3e8d9bf9a90ae68a5c8c3964d9790` | `execution-environment.yml` (resolved 2026-11-24) |

The ansible-core pin is one line. `scripts/build.sh` checks it against
`^ansible-core==[0-9]+\.[0-9]+\.[0-9]+$`, copies only the files the spec
references (`requirements.yml`, `requirements.txt`, `bindep.txt`) to a temporary
directory, writes the spec there with the version filled in, builds there, and
removes the copy on exit. A stray or ignored file in this directory, such as a
`context/` directory, cannot enter the build. The checked-in spec keeps a placeholder, and
`../tests/test_ee_ansible_core_pin.py` fails if anyone fills it in by hand.

## Files

| Path | Purpose |
|---|---|
| `execution-environment.yml` | ansible-builder v3 spec (base + deps + labels). Holds a placeholder for the ansible-core pin |
| `../requirements-ansible-core.txt` | The ansible-core pin: one line, `ansible-core==X.Y.Z` |
| `requirements.yml` | Ansible Galaxy collections (kubernetes.core, community.general, ansible.posix, community.docker, netbox.netbox) |
| `requirements.txt` | Python packages (kubernetes, openshift, jsonpatch, pynetbox, jmespath) |
| `bindep.txt` | System packages (helm, git, ca-certificates, skopeo) |
| `scripts/build.sh` | Checks the pin, renders the spec and its inputs into a temporary directory, and wraps `ansible-builder build` with the right `DOCKER_HOST` and the commit SHA for the revision label |
| `scripts/publish-to-ghcr.sh` | Operator-side push to `ghcr.io/dmfdeploy/awx-ee` (token via stdin, isolated DOCKER_CONFIG) |
| `../playbooks/630-zot-seed-platform.yml` | Cluster-side mirror from GHCR digest into cluster-internal Zot |

## Operator workflow

### Prerequisites

- macOS with Colima (`colima start docker-build`) — produces arm64 images.
- Docker CLI installed.
- `ansible-builder` >= 3.0: `pip install 'ansible-builder>=3.0'` (recommended in a venv or via `uv tool install ansible-builder`).
- A GitHub Personal Access Token with `write:packages` scope, stored in macOS Keychain (or any password manager that can pipe to stdin).

### Build

For an image you intend to publish, build from a clean checkout of the commit
you want to release (for example `git worktree add --detach <dir> <commit>`),
not from a working tree with local changes. `build.sh` stamps the full commit
SHA of that checkout into the `org.opencontainers.image.revision` label and
stops if it cannot find one.

```bash
cd <clean checkout>/k3s-lab-bootstrap/ee
scripts/build.sh    # the version is DEFAULT_IMAGE_VERSION in build.sh
```

Output: a local image tagged with the version that `scripts/build.sh --print-version`
prints, at arm64/linux, about 195 MB. The first build pulls the Fedora minimal base; later rebuilds reuse
cached layers.

Inspect labels:

```bash
IMG=registry.dmf.example.com/dmf/awx-ee:$(scripts/build.sh --print-version)
DOCKER_HOST=unix://$HOME/.colima/docker-build/docker.sock \
  docker image inspect $IMG \
    --format '{{json .Config.Labels}}' | python3 -m json.tool
```

Check a pin file without building:

```bash
scripts/build.sh --check-pin ../requirements-ansible-core.txt
```

### Verify the local image

Run these against the local tag. Each one should pass.

```bash
export DOCKER_HOST=unix://$HOME/.colima/docker-build/docker.sock
IMG=registry.dmf.example.com/dmf/awx-ee:$(scripts/build.sh --print-version)
docker run --rm $IMG ansible --version        # core equals the pin; note the Python
docker run --rm $IMG ansible-runner --version
docker run --rm $IMG ansible-galaxy collection list   # equals requirements.yml
docker run --rm $IMG pip check
docker run --rm $IMG bash -c 'helm version && git --version && skopeo --version'
# One example of the OpenBao fake-cluster tests: mount the repository's
# k3s-lab-bootstrap directory and run a single playbook. The full suite is every
# playbook the ansible-openbao job in .github/workflows/ci.yml runs; run each
# one the same way (the CI job lists them and their shard arguments).
docker run --rm -v "$PWD/..":/work -w /work -e HOME=/tmp/home --entrypoint bash $IMG -c \
  'mkdir -p /tmp/home; PATH=/work/tests/fixtures/fake-kubectl:$PATH \
   ANSIBLE_COLLECTIONS_PATH=/work/tests/fixtures/fake-collections:/usr/share/ansible/collections \
   ansible-playbook tests/test-openbao-capabilities.yml'
```

Record the local image ID and the revision label:

```bash
docker image inspect --format '{{.Id}}' $IMG    # config digest of the tested image
docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' $IMG
```

The revision label must equal the full SHA of the commit you are releasing.

### Publish, mirror, switch and rollback

Slice 1 changes no consumer: nothing in the repository points at this image yet.
Slice 3 (the switch to the shared tag variable) adds the publish, remote-verify,
mirror, switch and rollback steps.

## Version bumps

1. Decide a new semver (for example `0.3.0` for a collection or core change).
2. To change ansible-core, edit the one line in `../requirements-ansible-core.txt`.
   Check that every collection in `requirements.yml` supports the new core
   (`requires_ansible` in the collection's `meta/runtime.yml`).
3. Set `DEFAULT_IMAGE_VERSION` in `scripts/build.sh` to the new version, and
   change the pin if needed. Commit both. `scripts/build.sh --print-version`
   prints the new version, so the commands in this file follow it.
4. From a clean checkout of that commit, run `scripts/build.sh`, then verify the
   local image (see "Verify the local image"). The build refuses to run when
   `ee/` or the pin file has uncommitted changes, or when `IMAGE_VERSION` or
   `LOCAL_TAG` is set in the environment.
5. Publishing, mirroring and switching consumers to the new tag follow the steps
   that slice 3 adds.

## Yearly Fedora refresh

Fedora supports each release for about 13 months, and the base is pinned by
digest, so a new Fedora security update does not reach the image on its own.
Refresh the base once a year, and sooner when a fix matters.

1. Resolve the index digest of the new tag, for example
   `skopeo inspect --raw docker://quay.io/fedora/fedora-minimal:<N> | shasum -a 256`.
   Confirm with `skopeo manifest-digest` and check that the index lists the
   architectures you build.
2. Update `images.base_image.name` in `execution-environment.yml` and the
   date in its comment, and the `org.opencontainers.image.base.name` label.
3. Check the Fedora `python3` version against the ansible-core support table
   at https://docs.ansible.com/projects/ansible/latest/reference_appendices/release_and_maintenance.html.
   If Fedora moves to a Python that the pinned core does not support, move the
   core pin first.
4. Check that every package in `bindep.txt` still resolves, then build and verify as a new version. Slice 3 adds publishing.
5. Update the versions table above.

## Known constraints

### ansible-core support for Python

ansible-core 2.21 supports Python 3.12 to 3.14 as a controller and
3.9 to 3.14 on targets. Fedora minimal 43 ships Python 3.14, which is in
range. ansible-runner 2.4.3 does not list 3.14 in its classifiers but runs on it
(checked in the 0.2.0 build). ansible-core 2.22 needs Python 3.13 or later.
When you move the core pin to a new minor release, check the Python range
before you build.

### Collections must support the pinned core

Every collection in `requirements.yml` must support the ansible-core pin. The
collection versions are exact and unchanged from the 0.1.x images, so the
0.2.0 checks confirm they load on core 2.21: `ansible-doc` loads every module
the playbooks use. When you bump a collection, check its `requires_ansible`
in `meta/runtime.yml` before you pin a newer major release.

### The AWX pod security context was not read

The live AWX pod security context was not read, because AWX was down. The
UID-and-HOME checks used a plain container run with an arbitrary UID, group 0
and `HOME` set, which is not the AWX container group's real pod spec.
Acceptance for the security context moves to slice 3's real AWX job proof. Do
not treat it as verified.

### A writable HOME is required

As in 0.1.1, the image sets no `HOME` that a random UID can write. A pod that
runs as an arbitrary UID with group 0 and no `HOME` fails with
"Unable to create local directories '/.ansible/tmp'". Set `HOME=/runner` (or
any writable path) on the pod.

### Containerfile ARG / LABEL substitution

The OCI labels in `additional_build_steps.append_final` use Containerfile
`ARG`/`${VAR}` substitution. `scripts/build.sh` passes the image version
and the commit SHA via `--build-arg`. The pattern works because each `ARG`
declaration precedes the matching `LABEL` in the same final stage.

Verified embedded in the 0.2.0 build:

```
"org.opencontainers.image.revision": "<full 40-character commit sha>"
"org.opencontainers.image.version":  "0.2.0"
```

If labels show literal `${VAR}` strings instead of substituted values,
the `ARG` declaration is in the wrong stage; check that the relevant
`ARG VAR=...` line in `additional_build_steps.append_final` comes
**before** the `LABEL ... = "${VAR}"` line.

## Why this lives in `dmf-infra` and not `dmf-runbooks`

This EE is consumed by **infrastructure** plays (the bootstrap-configure
69x chain). Catalog launcher playbooks live in `dmf-runbooks` but
*also* consume this EE — they reference the same image. The build
context lives wherever the canonical consumer's role lives; in this
case, the runner-pod role at `roles/stack/operator/ansible-runner/`
is the load-bearing consumer, so the EE belongs in dmf-infra.

## See also

- [ADR-0025](https://github.com/dmfdeploy/dmfdeploy/blob/main/docs/decisions/0025-ansible-in-cluster-pods-and-catalog-helm.md)
- [Convergence plan §5 Lane A](https://github.com/dmfdeploy/dmfdeploy/blob/45eb326e973b10e0b52deef290981ceac9bcf5bd/docs/plans/DMF%20Cluster-Internal%20Ansible%20Execution%20and%20Catalog%20Helm%20Pivot%20Plan%202026-05-19.md)
- [Public registry plan §3.2 + §11](https://github.com/dmfdeploy/dmfdeploy/blob/45eb326e973b10e0b52deef290981ceac9bcf5bd/docs/plans/DMF%20Public%20Container%20Registry%20Publishing%20Plan%202026-05-19.md)
- [Runner-pod plan (Lane C)](https://github.com/dmfdeploy/dmfdeploy/blob/45eb326e973b10e0b52deef290981ceac9bcf5bd/docs/plans/DMF%20In-Cluster%20Ansible%20Runner%20Pod%20Implementation%20Plan%202026-05-14.md)
- ADR-0007 (secrets never in argv)
- ADR-0010 (`bin/run-playbook.sh` is the sanctioned ansible entry)
