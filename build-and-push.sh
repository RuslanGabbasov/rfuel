#!/usr/bin/env bash
set -euo pipefail

IMAGE="${IMAGE:-ghcr.io/ruslangabbasov/rfuel}"
TAG="${TAG:-latest}"

echo "Building ${IMAGE}:${TAG} ..."
docker build -t "${IMAGE}:${TAG}" .

echo "Pushing ${IMAGE}:${TAG} ..."
docker push "${IMAGE}:${TAG}"

echo "Done: ${IMAGE}:${TAG}"
