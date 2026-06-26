#!/usr/bin/env bash
set -euo pipefail

IMAGE="${IMAGE:-ghcr.io/yourusername/benz-bot}"
TAG="${TAG:-latest}"

echo "Building ${IMAGE}:${TAG} ..."
docker build -t "${IMAGE}:${TAG}" .

echo "Pushing ${IMAGE}:${TAG} ..."
docker push "${IMAGE}:${TAG}"

echo "Done: ${IMAGE}:${TAG}"
