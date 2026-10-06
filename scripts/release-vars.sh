#!/bin/sh
set -eu

# When sourced, POSIX sh does not expose the sourced file path. Prefer the
# caller's script path, then fall back to the current project directory.
# VERSION format example: v1.0.0
SOURCE_PATH=${0:-}
PROJECT_DIR=
case "$SOURCE_PATH" in
    */*)
        SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SOURCE_PATH")" 2>/dev/null && pwd -P) || SCRIPT_DIR=
        if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/../VERSION" ]; then
            PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)
        fi
        ;;
esac
if [ -z "$PROJECT_DIR" ] && [ -f "$(pwd -P)/VERSION" ]; then
    PROJECT_DIR=$(pwd -P)
fi
[ -n "$PROJECT_DIR" ] && [ -f "$PROJECT_DIR/VERSION" ] || {
    echo "无法定位项目根目录或 VERSION 文件。" >&2
    exit 1
}

VERSION=$(awk '
    NR == 1 && $0 ~ /^v[0-9]+\.[0-9]+\.[0-9]+$/ { value = $0; next }
    { invalid = 1 }
    END { if (invalid || value == "") exit 1; print value }
' "$PROJECT_DIR/VERSION") || {
    echo "VERSION 必须严格为单行 v数字.数字.数字。" >&2
    exit 1
}

IMAGE_REPO=junmu-reimbursement
IMAGE_TAG="$IMAGE_REPO:$VERSION"
CONTAINER_NAME="$IMAGE_REPO-$VERSION"
export VERSION IMAGE_REPO IMAGE_TAG CONTAINER_NAME
