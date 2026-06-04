#!/usr/bin/env bash
# Buduje i (opcjonalnie) pushuje wszystkie obrazy na Docker Hub.
# Multi-platform: linux/arm64 + linux/amd64 przez docker buildx.
#
# Użycie:
#   ./build-push.sh <twoj_login>              # build + push, tag latest
#   ./build-push.sh <twoj_login> 1.0.0        # build + push, tag 1.0.0
#   PUSH=0 ./build-push.sh <twoj_login>       # tylko build lokalny (bez push)
set -euo pipefail

DOCKERHUB_USER="${1:?Podaj login Docker Hub, np.: ./build-push.sh jankowalski}"
TAG="${2:-latest}"
PUSH="${PUSH:-1}"
PLATFORMS="linux/arm64,linux/amd64"

PREFIX="${DOCKERHUB_USER}/"

docker buildx create --use --name multiarch 2>/dev/null || docker buildx use multiarch

echo "==> Budowanie obrazów (PREFIX=$PREFIX, TAG=$TAG, platforms=$PLATFORMS)"

build_image() {
  local name="$1"
  local dockerfile="$2"
  local target="${PREFIX}${name}:${TAG}"
  echo "--> $target"
  if [[ "$PUSH" == "1" ]]; then
    docker buildx build --platform "$PLATFORMS" -f "$dockerfile" -t "$target" --push .
  else
    docker buildx build --platform "$PLATFORMS" -f "$dockerfile" -t "$target" --load . 2>/dev/null \
      || docker buildx build --platform linux/arm64 -f "$dockerfile" -t "$target" --load .
  fi
}

build_image "podcast-web"               "web/Dockerfile"
build_image "podcast-worker-controller" "worker_controller/Dockerfile"
build_image "podcast-transcriber"       "transcriber/Dockerfile"

echo ""
echo "Gotowe. Obrazy:"
for svc in web worker-controller transcriber; do
  echo "  ${PREFIX}podcast-${svc}:${TAG}"
done
