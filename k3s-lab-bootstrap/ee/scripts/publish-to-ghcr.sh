#!/usr/bin/env bash
# publish-to-ghcr.sh — push the DMF AWX EE image to GHCR.
#
# Thin wrapper around the umbrella's bin/publish-image-to-ghcr.sh. The
# umbrella script handles secrets (token via stdin, isolated DOCKER_CONFIG
# with cleanup trap, never argv).
#
# Usage:
#
#   # From macOS Keychain:
#   security find-generic-password -s "ghcr.io" -a "<github-username>" -w \
#     | GHCR_USER="<github-username>" \
#       ~/repos/dmfdeploy/dmf-infra/k3s-lab-bootstrap/ee/scripts/publish-to-ghcr.sh
#
#   # Interactive:
#   ~/repos/dmfdeploy/dmf-infra/k3s-lab-bootstrap/ee/scripts/publish-to-ghcr.sh
#
# Env knobs:
#   GHCR_USER         GitHub username (default: prompt)
#   GHCR_NAMESPACE    GHCR namespace (default: dmfdeploy)
#   IMAGE_TAG         Tag (default: 0.1.0)
#   SOURCE_REGISTRY   Local registry prefix
#                     (default: registry.dmf.example.com/dmf — matches scripts/build.sh)

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# scripts/ → ee/ → k3s-lab-bootstrap/ → dmf-infra/ → dmfdeploy/
UMBRELLA_DIR="$(cd "$SCRIPT_DIR/../../../.." && pwd)"

GHCR_NAMESPACE="${GHCR_NAMESPACE:-dmfdeploy}"
IMAGE_TAG="${IMAGE_TAG:-0.1.0}"
SOURCE_REGISTRY="${SOURCE_REGISTRY:-registry.dmf.example.com/dmf}"

exec "${UMBRELLA_DIR}/bin/publish-image-to-ghcr.sh" \
  "${SOURCE_REGISTRY}/awx-ee:${IMAGE_TAG}" \
  "ghcr.io/${GHCR_NAMESPACE}/awx-ee:${IMAGE_TAG}"
