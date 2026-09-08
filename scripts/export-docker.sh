#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)
cd "$PROJECT_DIR"
command -v docker >/dev/null 2>&1 || { echo "需要 Docker；请先构建镜像。" >&2; exit 1; }
[ ! -L dist ] || { echo "dist 不能是符号链接。" >&2; exit 1; }
mkdir -p dist
ARCHIVE="$PROJECT_DIR/dist/reimbursement-system-linux-amd64.tar.gz"
[ ! -e "$ARCHIVE" ] && [ ! -L "$ARCHIVE" ] || { echo "镜像归档已存在，请先改名保留旧版本。" >&2; exit 1; }
PLATFORM=$(docker image inspect --format '{{.Os}}/{{.Architecture}}' reimbursement-system:latest)
[ "$PLATFORM" = linux/amd64 ] || { echo "镜像必须是 linux/amd64，当前为 $PLATFORM。" >&2; exit 1; }
EXPORT_DIR=$(mktemp -d "$PROJECT_DIR/dist/.export.XXXXXX")
cleanup() { rm -f -- "$EXPORT_DIR/image.tar" "$EXPORT_DIR/image.tar.gz"; rmdir -- "$EXPORT_DIR"; }
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
docker save --output "$EXPORT_DIR/image.tar" reimbursement-system:latest
gzip "$EXPORT_DIR/image.tar"
ln "$EXPORT_DIR/image.tar.gz" "$ARCHIVE"
printf '%s\n' "$ARCHIVE"
