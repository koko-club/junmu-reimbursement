#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)
cd "$PROJECT_DIR"
case "${1:-}" in
    "") PACKAGE_NAME=reimbursement-system-synology ;;
    --source) PACKAGE_NAME=reimbursement-system-source ;;
    *) echo "用法: sh scripts/package-synology.sh [--source]" >&2; exit 1 ;;
esac
[ "$#" -le 1 ] || { echo "参数过多。" >&2; exit 1; }
[ ! -L dist ] || { echo "dist 不能是符号链接。" >&2; exit 1; }
mkdir -p dist
ARCHIVE="$PROJECT_DIR/dist/$PACKAGE_NAME.tar.gz"
[ ! -e "$ARCHIVE" ] && [ ! -L "$ARCHIVE" ] || { echo "交付包已存在，请先改名保留旧版本。" >&2; exit 1; }
IMAGE="$PROJECT_DIR/dist/reimbursement-system-linux-amd64.tar.gz"
if [ "$PACKAGE_NAME" = reimbursement-system-synology ]; then
    [ -f "$IMAGE" ] && [ ! -L "$IMAGE" ] || { echo "缺少镜像归档；请先 build-docker.sh 和 export-docker.sh，或使用 --source。" >&2; exit 1; }
    gzip -t "$IMAGE"
fi
PACKAGE_DIR=$(mktemp -d "$PROJECT_DIR/dist/.package.XXXXXX")
cleanup() {
    # This directory is the private mktemp output above, never a caller-supplied target.
    case "$PACKAGE_DIR" in "$PROJECT_DIR"/dist/.package.*) rm -rf -- "$PACKAGE_DIR" ;; esac
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM
DESTINATION="$PACKAGE_DIR/$PACKAGE_NAME"
mkdir -p "$DESTINATION/scripts"
cp compose.yaml .env.example README_部署说明.md backup.py database.py "$DESTINATION/"
cp scripts/backup.sh "$DESTINATION/scripts/"
if [ "$PACKAGE_NAME" = reimbursement-system-synology ]; then
    cp "$IMAGE" "$DESTINATION/"
else
    cp ./*.py config.json requirements.txt requirements-test.txt Dockerfile .dockerignore start_reimbursement_tool.sh start_reimbursement_tool.command "$DESTINATION/"
    cp scripts/build-docker.sh scripts/export-docker.sh scripts/package-synology.sh "$DESTINATION/scripts/"
    cp -R resources templates static tests "$DESTINATION/"
    find "$DESTINATION" -type d -name __pycache__ -prune -exec rm -rf -- {} +
fi
tar -czf "$PACKAGE_DIR/archive.tar.gz" -C "$PACKAGE_DIR" "$PACKAGE_NAME"
ln "$PACKAGE_DIR/archive.tar.gz" "$ARCHIVE"
printf '%s\n' "$ARCHIVE"
