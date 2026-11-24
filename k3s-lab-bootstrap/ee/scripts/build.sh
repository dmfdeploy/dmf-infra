#!/usr/bin/env bash
# build.sh — build the DMF AWX EE image via ansible-builder.
#
# Operator runs this on their workstation (Colima docker-build profile).
# See ../README.md for the full operator workflow.
#
# The image version is DEFAULT_IMAGE_VERSION below, and the local tag is always
# registry.dmf.example.com/dmf/awx-ee:<DEFAULT_IMAGE_VERSION> (a placeholder
# registry; scripts/publish-to-ghcr.sh retags for GHCR). A build refuses to run
# when IMAGE_VERSION or LOCAL_TAG is set in the environment, so the tag that
# --print-version reports is the tag the build produces.
#
# Env knobs:
#   VCS_REF          full commit SHA stamped into the OCI revision label
#                    (default: git rev-parse HEAD of the source tree; the
#                    build stops if neither is available)
#   DOCKER_HOST      Colima docker-build socket (default: docker-build profile)

# The ansible-core version comes from ../requirements-ansible-core.txt, the one
# place it is set. Builder takes a pip argument, not a file, and reads
# requirements.yml, requirements.txt and bindep.txt relative to the spec. So
# this script checks the line, copies only those three files (STAGED_INPUTS)
# to a temporary directory, writes the spec there with the value filled in, and
# builds there. A build also refuses to run when ee/ or the pin file has
# uncommitted changes, so the revision label names the commit that holds the
# build inputs. Nothing else from ee/ (an ignored context/ directory, an
# untracked file) can enter the build context. The copy is removed on exit.
# The checked-in spec is never changed.
#
# Modes for tests and for checking a pin or version without a build:
#   build.sh --check-pin <pin-file>
#   build.sh --render <pin-file> <ee-dir> <dest-dir>
#   build.sh --print-version

set -euo pipefail

PIN_PATTERN='^ansible-core==[0-9]+\.[0-9]+\.[0-9]+$'
PLACEHOLDER='__ANSIBLE_CORE_PIN__'
# The one place the default image version is set. publish-to-ghcr.sh reads it
# with --print-version.
DEFAULT_IMAGE_VERSION='0.2.0'
# The files execution-environment.yml references, besides itself.
STAGED_INPUTS=(requirements.yml requirements.txt bindep.txt)

# Print the pin from the file, or exit 1 if the file does not hold exactly one
# line of the form ansible-core==X.Y.Z.
read_pin() {
  local file="$1" pin
  if [[ ! -f "$file" ]]; then
    echo "ERROR: pin file $file not found." >&2
    return 1
  fi
  if [[ "$(wc -l <"$file")" -ne 1 ]]; then
    echo "ERROR: $file must hold exactly one line, ansible-core==X.Y.Z." >&2
    return 1
  fi
  pin="$(cat "$file")"
  if ! [[ "$pin" =~ $PIN_PATTERN ]]; then
    echo "ERROR: $file holds an invalid pin; expected ansible-core==X.Y.Z." >&2
    return 1
  fi
  printf '%s\n' "$pin"
}

# Copy the spec's inputs from <ee-dir> to <dest-dir>, with the pin filled the pin from <pin-file> into the copy of
# into the copy of the spec. The spec must hold the placeholder exactly once.
render_spec() {
  local pin_file="$1" ee_dir="$2" dest="$3" pin count name
  pin="$(read_pin "$pin_file")" || return 1
  count="$(grep -c "$PLACEHOLDER" "$ee_dir/execution-environment.yml" || true)"
  if [[ "$count" -ne 1 ]]; then
    echo "ERROR: execution-environment.yml must hold $PLACEHOLDER exactly once; found $count." >&2
    return 1
  fi
  for name in "${STAGED_INPUTS[@]}"; do
    if [[ ! -f "$ee_dir/$name" ]]; then
      echo "ERROR: $ee_dir/$name not found." >&2
      return 1
    fi
  done
  mkdir -p "$dest"
  for name in "${STAGED_INPUTS[@]}"; do
    cp "$ee_dir/$name" "$dest/$name"
  done
  sed "s/$PLACEHOLDER/$pin/" "$ee_dir/execution-environment.yml" >"$dest/execution-environment.yml"
}

case "${1:-}" in
  --check-pin)
    read_pin "${2:?usage: build.sh --check-pin <pin-file>}"
    exit $?
    ;;
  --print-version)
    printf '%s\n' "$DEFAULT_IMAGE_VERSION"
    exit 0
    ;;
  --render)
    render_spec "${2:?usage: build.sh --render <pin-file> <ee-dir> <dest-dir>}" \
      "${3:?missing <ee-dir>}" "${4:?missing <dest-dir>}"
    exit $?
    ;;
esac

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
EE_DIR="$(dirname "$SCRIPT_DIR")"
# EE_DIR = .../dmf-infra/k3s-lab-bootstrap/ee
# repo root = parent of k3s-lab-bootstrap
REPO_ROOT="$(cd "$EE_DIR/../.." && pwd)"

# DEFAULT_IMAGE_VERSION is the only source of the version and the local tag.
for override in IMAGE_VERSION LOCAL_TAG; do
  if [[ -n "${!override+set}" ]]; then
    echo "ERROR: $override is set in the environment; build.sh does not accept it." >&2
    echo "       Change DEFAULT_IMAGE_VERSION in build.sh, commit, and build from that commit." >&2
    exit 1
  fi
done

IMAGE_VERSION="$DEFAULT_IMAGE_VERSION"
LOCAL_TAG="registry.dmf.example.com/dmf/awx-ee:${IMAGE_VERSION}"
VCS_REF="${VCS_REF:-$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || true)}"
if ! [[ "$VCS_REF" =~ ^[0-9a-f]{40}$ ]]; then
  echo "ERROR: VCS_REF must be a full 40-character commit SHA; run from a git checkout or set VCS_REF." >&2
  exit 1
fi
DOCKER_HOST="${DOCKER_HOST:-unix://$HOME/.colima/docker-build/docker.sock}"

PIN_FILE="$EE_DIR/../requirements-ansible-core.txt"

# The revision label must name the commit that holds the build inputs.
if ! DIRTY="$(git -C "$REPO_ROOT" status --porcelain -- "$EE_DIR" "$PIN_FILE")"; then
  echo "ERROR: cannot read the git status of $EE_DIR." >&2
  exit 1
fi
if [[ -n "$DIRTY" ]]; then
  echo "ERROR: ee/ or the pin file has uncommitted changes; commit them and build from that commit:" >&2
  printf '%s\n' "$DIRTY" >&2
  exit 1
fi
ANSIBLE_CORE_PIN="$(read_pin "$PIN_FILE")"

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

STAGE_DIR="$(mktemp -d)"
trap 'rm -rf "$STAGE_DIR"' EXIT
render_spec "$PIN_FILE" "$EE_DIR" "$STAGE_DIR"
cd "$STAGE_DIR"

echo "=== Building DMF AWX EE ==="
echo "  ANSIBLE_CORE  = $ANSIBLE_CORE_PIN"
echo "  IMAGE_VERSION = $IMAGE_VERSION"
echo "  VCS_REF       = $VCS_REF"
echo "  LOCAL_TAG     = $LOCAL_TAG"
echo "  DOCKER_HOST   = $DOCKER_HOST"
echo ""

ansible-builder build \
  --file execution-environment.yml \
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
