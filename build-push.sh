#!/usr/bin/env bash
# Buduje i (opcjonalnie) pushuje wszystkie obrazy na Docker Hub.
#
# Użycie:
#   ./build-push.sh <twoj_login>              # build + push, tag latest
#   ./build-push.sh <twoj_login> 1.0.0        # build + push, tag 1.0.0
#   PUSH=0 ./build-push.sh <twoj_login>       # tylko build, bez push
set -euo pipefail

DOCKERHUB_USER="${1:?Podaj login Docker Hub, np.: ./build-push.sh jankowalski}"
TAG="${2:-latest}"
PUSH="${PUSH:-1}"

export IMAGE_PREFIX="${DOCKERHUB_USER}/"
export TAG

echo "==> Budowanie obrazów (IMAGE_PREFIX=$IMAGE_PREFIX, TAG=$TAG)"

# Trzy serwisy compose (web, scheduler, worker-controller)
docker compose build

# Transkryber — budowany osobno (uruchamiany przez docker run, nie compose)
docker build \
  -f transcriber/Dockerfile \
  -t "${IMAGE_PREFIX}podcast-transcriber:${TAG}" \
  .

if [[ "$PUSH" == "1" ]]; then
  echo "==> Pushowanie na Docker Hub"
  docker compose push
  docker push "${IMAGE_PREFIX}podcast-transcriber:${TAG}"
  echo ""
  echo "Gotowe. Obrazy dostępne jako:"
  for svc in web scheduler worker-controller transcriber; do
    echo "  ${IMAGE_PREFIX}podcast-${svc}:${TAG}"
  done
else
  echo "Gotowe (PUSH=0, pominięto push)."
fi
