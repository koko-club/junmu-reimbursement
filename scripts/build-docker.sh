#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)
cd "$PROJECT_DIR"
. scripts/release-vars.sh
command -v docker >/dev/null 2>&1 || { echo "需要已启动的 Docker 和 buildx；可先运行 sh scripts/package-synology.sh --source 交付源码。" >&2; exit 1; }
docker buildx build --platform linux/amd64 --load --build-arg APP_VERSION=$VERSION --tag "$IMAGE_TAG" .
