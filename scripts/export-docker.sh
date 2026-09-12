#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)
cd "$PROJECT_DIR"
. scripts/release-vars.sh
command -v docker >/dev/null 2>&1 || { echo "需要 Docker；请先构建镜像。" >&2; exit 1; }
[ ! -L dist ] || { echo "dist 不能是符号链接。" >&2; exit 1; }
mkdir -p dist
ARCHIVE="$PROJECT_DIR/dist/$IMAGE_REPO-$VERSION-linux-amd64.tar.gz"
[ ! -e "$ARCHIVE" ] && [ ! -L "$ARCHIVE" ] || { echo "镜像归档已存在，请先改名保留旧版本。" >&2; exit 1; }
EXPORT_DIR=$(mktemp -d "$PROJECT_DIR/dist/.export.XXXXXX")
cleanup() { rm -f -- "$EXPORT_DIR/platform" "$EXPORT_DIR/version" "$EXPORT_DIR/image.tar" "$EXPORT_DIR/image.tar.gz"; rmdir -- "$EXPORT_DIR"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
docker image inspect --format '{{.Os}}/{{.Architecture}}' "$IMAGE_TAG" > "$EXPORT_DIR/platform"
PLATFORM=$(cat "$EXPORT_DIR/platform")
[ "$PLATFORM" = linux/amd64 ] || { echo "镜像必须是 linux/amd64，当前为 ${PLATFORM}。" >&2; exit 1; }
docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.version" }}' "$IMAGE_TAG" > "$EXPORT_DIR/version"
IMAGE_VERSION=$(cat "$EXPORT_DIR/version")
[ "$IMAGE_VERSION" = "$VERSION" ] || {
    echo "镜像 OCI 版本标签必须为 ${VERSION}，当前为 ${IMAGE_VERSION}。" >&2
    exit 1
}
docker save --output "$EXPORT_DIR/image.tar" "$IMAGE_TAG"
gzip "$EXPORT_DIR/image.tar"
ln "$EXPORT_DIR/image.tar.gz" "$ARCHIVE"
printf '%s\n' "$ARCHIVE"
