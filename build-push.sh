#!/usr/bin/env bash
# Buduje i wypycha obraz na Docker Hub (linux/amd64 + linux/arm64).
#
#   ./build-push.sh                # tag latest + krotki SHA
#   PUSH=0 ./build-push.sh         # tylko build lokalny
set -euo pipefail

IMAGE="${IMAGE:-kpa90/podcast-transcriber}"
TAG="${1:-latest}"
SHA=$(git rev-parse --short HEAD)
PUSH="${PUSH:-1}"

docker buildx create --use --name multiarch 2>/dev/null || docker buildx use multiarch

echo "==> $IMAGE:$TAG + $IMAGE:$SHA"
if [[ "$PUSH" == "1" ]]; then
  docker buildx build --platform linux/amd64,linux/arm64 \
    -t "$IMAGE:$TAG" -t "$IMAGE:$SHA" --push .
else
  docker buildx build -t "$IMAGE:$TAG" --load .
fi

echo "Gotowe. Rollback: podmien :latest na :$SHA w compose.yaml"
