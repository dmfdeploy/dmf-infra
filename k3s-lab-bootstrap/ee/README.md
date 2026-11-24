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
            │   bin/publish-image-to-ghcr.sh (umbrella checkout, operator workstation)
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
docker image inspect --format '{{.Id}}' $IMG    # the image ID: the config digest, or the index digest on the containerd image store
docker image inspect --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' $IMG
```

The revision label must equal the full SHA of the commit you are releasing.

### Publish, mirror, switch and rollback

Consumers read the image tag from one variable, `awx_ee_image_tag` in
`../vars/awx-ee.yml`. Playbooks 050 (the runner foundation), 630 (the Zot
mirror) and 693 (the AWX integration) load that file with `vars_files`, and
`-e awx_ee_image_tag=<tag>` overrides it for one run. The value is `0.2.0`.

**Architecture.** The image is built and published for arm64 only, as 0.1.1
was: its GHCR index lists one `linux/arm64` image and an attestation. Playbook
630 copies the platform named by `dmf_seed_arch`, which is `dmf_architecture`
from the inventory and falls back to `amd64` when that is unset. The environment
wizard renders `dmf_architecture` from the architecture you choose, and the
sandbox profile renders `arm64`. An install must therefore have
`dmf_architecture: arm64` (that is, `dmf_seed_arch=arm64`) before it seeds this
image. An `amd64` seed fails, because the index has no `amd64` manifest:
`no image found in image index for architecture "amd64", variant "", OS "linux"`.

**Publish** (needs the operator's approval; it pushes to the public registry).
From the umbrella checkout, with the image built by `scripts/build.sh` on the
same Docker host:

```bash
gh auth token | GHCR_USER=<github-user> bin/publish-image-to-ghcr.sh \
  registry.dmf.example.com/dmf/awx-ee:<tag> ghcr.io/dmfdeploy/awx-ee:<tag>
```

The helper reads the token on stdin and keeps it out of argv. Then check the
push in two separate ways.

- **The image you tested is the image GHCR holds.** Compare the arm64 platform
  manifest digest locally and on GHCR. A local image ID is not a portable
  identity (it is the config digest on Docker's classic image store and the
  index digest on the containerd image store, which the Colima `docker-build`
  profile uses), so read the platform manifest from the index. On the build host,
  with the containerd image store, the local index is in the saved image:

  ```bash
  IMG=registry.dmf.example.com/dmf/awx-ee:<tag>
  INDEX=$(docker image inspect --format '{{.Descriptor.digest}}' $IMG)
  docker save $IMG | tar -xOf - blobs/sha256/${INDEX#sha256:} \
    | jq -r '.manifests[] | select(.platform.architecture == "arm64") | .digest'
  docker buildx imagetools inspect ghcr.io/dmfdeploy/awx-ee:<tag> --raw \
    | jq -r '.manifests[] | select(.platform.architecture == "arm64") | .digest'
  ```

  The two lines must be equal. On a host with the classic image store there is
  no index locally, so compare the config digest instead: the local
  `docker image inspect --format '{{.Id}}' $IMG` against
  `skopeo inspect --raw docker://ghcr.io/dmfdeploy/awx-ee@<arm64 manifest digest> | jq -r .config.digest`.
- **The registry serves the index you record.** Take the tag's index digest from
  `docker buildx imagetools inspect ghcr.io/dmfdeploy/awx-ee:<tag> --format '{{.Manifest.Digest}}'`
  and record it in this README. It is the identity for a pull by digest, and it
  is not the platform manifest digest above.

**Mirror.** `630-zot-seed-platform.yml` copies the platform manifest for
`dmf_seed_arch` from GHCR into Zot. Its tag gate skips a tag that Zot already
holds, so a tag that was copied earlier and then re-cut stays as it was; run 630
with `zot_seed_force=true` to copy it again. A new tag has no such entry. Verify
the copy by comparing the arm64 platform manifest digest, not the index digest
(Zot holds the platform manifest only):

```bash
# GHCR: the digest of the arm64 entry in the index
skopeo inspect --raw docker://ghcr.io/dmfdeploy/awx-ee:<tag> \
  | jq -r '.manifests[] | select(.platform.architecture == "arm64") | .digest'
# Zot: the digest of the manifest that the tag resolves to
skopeo inspect --raw docker://<registry-host>/dmf/awx-ee:<tag> | shasum -a 256
```

| Tag | Index digest | arm64 platform manifest digest |
|---|---|---|
| `0.2.0` | `sha256:352e46fa1a72597cf8fa0a2a2c5e5e682f4b7e96fac536b9282b7b76a7d24b94` | `sha256:7b558a7a5e67c5df53c79e8736a44b61d05b93b7af0714736b019295ae308981` |
| `0.1.1` | `sha256:867faa97ed7391befe599ca0ad96fab1f6dfeeddb25b542c79229b6434ee9b51` | `sha256:925d8a35ffabe934091fa063696842583524e40d0a4314c2f1a597d06a42cb3f` |

Zot holds the platform manifest for a seeded tag, so a pod that pulls the tag
from Zot should report that platform manifest digest as its `imageID`; the
rollback check below compares against the platform column, and the live proof
confirms it. Read a tag's platform digest with the GHCR command above.

**Switch.** Change the one line in `../vars/awx-ee.yml`, then run 630 and
`693-awx-integration.yml`, which points AWX at the new tag.

**Rollback.** Do not roll back by editing the line on a checkout: the next
checkout advance loses the edit. Before the final cleanup of
dmfdeploy/dmfdeploy#975 (it removes the ansible-core 2.15 CI job and
compatibility code) lands, revert the commit that switched the tag
on `main` (a revert pull request), advance the Manage checkout to the revert,
wake AWX, rerun 630 and 693, and check that the AWX execution environment object
names `0.1.1` and that one job pod's `imageID` is the 0.1.1 arm64 platform
manifest digest from the table above. After the cleanup
lands, revert the cleanup commit first and then the switch commit, in that
order, and only with the operator's approval. Keep 0.1.1 in GHCR and in Zot for
at least 30 days after dmfdeploy/dmfdeploy#975 closes. The rollback drill is a
separate live acceptance step.

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
   `ee/` or the pin file has uncommitted changes, or when `IMAGE_VERSION`,
   `LOCAL_TAG` or `VCS_REF` is set in the environment.
5. Publish, mirror and switch consumers to the new tag as described under
   "Publish, mirror, switch and rollback": change `awx_ee_image_tag` in
   `../vars/awx-ee.yml`, then rerun 630 and 693.

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
4. Check that every package in `bindep.txt` still resolves, then build and verify as a new version, and publish it as described under
   "Publish, mirror, switch and rollback".
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
