#!/usr/bin/env bash
# build.sh — build the DMF AWX EE image via ansible-builder.
#
# Operator runs this on their workstation (Colima docker-build profile).
# See ../README.md for the full operator workflow.
#
# Env knobs:
#   IMAGE_VERSION    semver tag for the output image (default: 0.1.0)
#   VCS_REF          short commit SHA stamped into the OCI revision label
#                    (default: derived from dmf-infra HEAD)
#   DOCKER_HOST      Colima docker-build socket (default: docker-build profile)
#   LOCAL_TAG        local image tag produced by the build
#                    (default: registry.dmf.example.com/dmf/awx-ee:$IMAGE_VERSION
#                    — placeholder; scripts/publish-to-ghcr.sh retags for GHCR)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EE_DIR="$(dirname "$SCRIPT_DIR")"
# EE_DIR = .../dmf-infra/k3s-lab-bootstrap/ee
# repo root = parent of k3s-lab-bootstrap
REPO_ROOT="$(cd "$EE_DIR/../.." && pwd)"

IMAGE_VERSION="${IMAGE_VERSION:-0.1.0}"
VCS_REF="${VCS_REF:-$(git -C "$REPO_ROOT" rev-parse --short HEAD 2>/dev/null || echo unknown)}"
DOCKER_HOST="${DOCKER_HOST:-unix://$HOME/.colima/docker-build/docker.sock}"
LOCAL_TAG="${LOCAL_TAG:-registry.dmf.example.com/dmf/awx-ee:${IMAGE_VERSION}}"

# Preflight ---------------------------------------------------------------

if ! command -v ansible-builder >/dev/null; then
  cat >&2 <<'ERR'
ERROR: ansible-builder not installed.

  Install with one of:
    pip install 'ansible-builder>=3.0'
    uv tool install ansible-builder
    pipx install ansible-builder

Then re-run this script.
ERR
  exit 1
fi

export DOCKER_HOST
if ! docker info >/dev/null 2>&1; then
  echo "ERROR: docker daemon at \$DOCKER_HOST=$DOCKER_HOST is not reachable." >&2
  echo "       Is the Colima docker-build profile running? (colima start docker-build)" >&2
  exit 1
fi

# Build --------------------------------------------------------------------

cd "$EE_DIR"

echo "=== Building DMF AWX EE ==="
echo "  IMAGE_VERSION = $IMAGE_VERSION"
echo "  VCS_REF       = $VCS_REF"
echo "  LOCAL_TAG     = $LOCAL_TAG"
echo "  DOCKER_HOST   = $DOCKER_HOST"
echo ""

ansible-builder build \
  --tag "$LOCAL_TAG" \
  --container-runtime docker \
  --build-arg "IMAGE_VERSION=${IMAGE_VERSION}" \
  --build-arg "VCS_REF=${VCS_REF}" \
  --prune-images \
  -v 2

# Report -------------------------------------------------------------------

echo ""
echo "=== Build complete ==="
echo "  $LOCAL_TAG"
docker image inspect "$LOCAL_TAG" --format '  arch:   {{.Architecture}}/{{.Os}}
  size:   {{.Size}} bytes
  labels: (org.opencontainers.image.*)'

echo ""
echo "Inspect labels (sanity check):"
echo "  DOCKER_HOST=$DOCKER_HOST docker image inspect $LOCAL_TAG \\"
echo "    --format '{{json .Config.Labels}}' | python3 -m json.tool"

echo ""
echo "Next: publish to GHCR via scripts/publish-to-ghcr.sh"
