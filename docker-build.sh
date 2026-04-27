#!/usr/bin/env bash
# =============================================================================
# docker-build.sh — Build (and optionally push) Docker image(s)
#
# Usage:
#   ./docker-build.sh [OPTIONS]
#
# Options:
#   -t, --tag TAG          Image tag  (default: latest)
#   -r, --registry URL     Registry prefix, e.g. registry.example.com:5000
#   -p, --push             Push to registry after build (no registry = Docker Hub)
#   --no-cache             Full rebuild without Docker cache
#   --refresh-deps         Re-fetch git-based dependencies only (keep other layers cached)
#   --target TARGET        Which image to build (multi-image projects only, default: all)
#   -h, --help             Show this help and exit
#
# Examples:
#   ./docker-build.sh                                       # build locally
#   ./docker-build.sh -t v1.2.3                             # custom tag
#   ./docker-build.sh -r registry.example.com:5000 -p       # build & push to private registry
#   ./docker-build.sh -p                                    # build & push to Docker Hub
#   ./docker-build.sh --no-cache                            # full rebuild
#   ./docker-build.sh --refresh-deps                        # only re-fetch git deps
#   ./docker-build.sh --target backend                      # build one image (multi-image)
# =============================================================================
set -euo pipefail

# ── Project config ────────────────────────────────────────────────────────────
IMAGES=("hardware-monitor-web . Dockerfile")
declare -A TARGET_MAP

# ── Defaults ──────────────────────────────────────────────────────────────────
TAG="latest"
REGISTRY=""
PUSH=false
NO_CACHE=""
CACHE_BUST=""
TARGET="all"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ── Help ──────────────────────────────────────────────────────────────────────
usage() {
  sed -n '/^# Usage:/,/^# ====/p' "$0" | sed 's/^# \?//'
  if [[ ${#TARGET_MAP[@]} -gt 0 ]]; then
    echo ""
    echo "Available targets: all ${!TARGET_MAP[*]}"
  fi
  exit 0
}

# ── Argument parsing ──────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    -t|--tag)           TAG="$2";      shift 2 ;;
    -r|--registry)      REGISTRY="$2"; shift 2 ;;
    -p|--push)          PUSH=true;     shift   ;;
    --no-cache)         NO_CACHE="--no-cache"; CACHE_BUST="--build-arg CACHE_BUST=$(date +%s)"; shift ;;
    --refresh-deps)     CACHE_BUST="--build-arg CACHE_BUST=$(date +%s)"; shift ;;
    --target)           TARGET="$2";   shift 2 ;;
    -h|--help)          usage ;;
    *)
      echo "Unknown option: $1" >&2
      echo "Run '$0 --help' for usage." >&2
      exit 1
      ;;
  esac
done

REGISTRY="${REGISTRY%/}"

# Validate --target
if [[ "$TARGET" != "all" && ${#TARGET_MAP[@]} -eq 0 ]]; then
  echo "Error: --target is not supported for single-image projects" >&2
  exit 1
fi
if [[ "$TARGET" != "all" && -z "${TARGET_MAP[$TARGET]+x}" ]]; then
  echo "Error: unknown target '$TARGET'. Available: all ${!TARGET_MAP[*]}" >&2
  exit 1
fi

# ── Build function ────────────────────────────────────────────────────────────
build_and_push() {
  local name="$1"
  local context="$2"
  local dockerfile="$3"

  local full_image
  if [[ -n "$REGISTRY" ]]; then
    full_image="${REGISTRY}/${name}:${TAG}"
  else
    full_image="${name}:${TAG}"
  fi

  echo "==> Building ${full_image}"
  # shellcheck disable=SC2086
  docker build \
    ${NO_CACHE} \
    ${CACHE_BUST} \
    -t "${full_image}" \
    -f "${SCRIPT_DIR}/${dockerfile}" \
    "${SCRIPT_DIR}/${context}"

  if [[ "$PUSH" == true ]]; then
    echo "==> Pushing ${full_image}"
    docker push "${full_image}"
  fi

  echo "==> Done: ${full_image}"
}

# ── Main ──────────────────────────────────────────────────────────────────────
for i in "${!IMAGES[@]}"; do
  read -r name context dockerfile <<< "${IMAGES[$i]}"

  # Skip if --target selects a specific image
  if [[ "$TARGET" != "all" && "${TARGET_MAP[$TARGET]}" != "$i" ]]; then
    continue
  fi

  build_and_push "$name" "$context" "$dockerfile"
done
