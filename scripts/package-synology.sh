#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)
cd "$PROJECT_DIR"

require_regular_files() {
    for PACKAGE_INPUT do
        if [ ! -f "$PACKAGE_INPUT" ] || [ -L "$PACKAGE_INPUT" ]; then
            echo "打包输入必须是非符号链接的普通文件：$PACKAGE_INPUT" >&2
            exit 1
        fi
    done
}

require_regular_files VERSION scripts/release-vars.sh
. scripts/release-vars.sh
case "${1:-}" in
    "")
        PACKAGE_NAME="$IMAGE_REPO-$VERSION-synology"
        ARCHIVE="$PROJECT_DIR/dist/$IMAGE_REPO-$VERSION-synology.tar.gz"
        IMAGE_ARCHIVE="$PROJECT_DIR/dist/$IMAGE_REPO-$VERSION-linux-amd64.tar.gz"
        ;;
    --source)
        PACKAGE_NAME="$IMAGE_REPO-$VERSION-source"
        ARCHIVE="$PROJECT_DIR/dist/$IMAGE_REPO-$VERSION-source.tar.gz"
        ;;
    *) echo "用法: sh scripts/package-synology.sh [--source]" >&2; exit 1 ;;
esac
[ "$#" -le 1 ] || { echo "参数过多。" >&2; exit 1; }
require_regular_files VERSION compose.yaml CHANGELOG.md README_部署说明.md backup.py database.py .env.example \
    scripts/backup.sh scripts/release-vars.sh
[ ! -L dist ] || { echo "dist 不能是符号链接。" >&2; exit 1; }
mkdir -p dist
[ ! -e "$ARCHIVE" ] && [ ! -L "$ARCHIVE" ] || { echo "交付包已存在，请先改名保留旧版本。" >&2; exit 1; }
if [ "${1:-}" = "" ]; then
    [ -f "$IMAGE_ARCHIVE" ] && [ ! -L "$IMAGE_ARCHIVE" ] || { echo "缺少镜像归档；请先 build-docker.sh 和 export-docker.sh，或使用 --source。" >&2; exit 1; }
    gzip -t "$IMAGE_ARCHIVE"
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
cp VERSION compose.yaml CHANGELOG.md README_部署说明.md backup.py database.py "$DESTINATION/"
cp scripts/backup.sh scripts/release-vars.sh "$DESTINATION/scripts/"

if ! awk -v version="$VERSION" '
    /^[[:space:]]*APP_VERSION[[:space:]]*=/ {
        count += 1
        print "APP_VERSION=" version
        next
    }
    { print }
    END { if (count != 1) exit 1 }
' .env.example > "$DESTINATION/.env.example"; then
    echo ".env.example 必须且只能包含一个 APP_VERSION 配置。" >&2
    exit 1
fi

if [ "${1:-}" = "" ]; then
    cp "$IMAGE_ARCHIVE" "$DESTINATION/"
else
    require_regular_files app.py config.py generator.py office.py reimbursements.py security.py sessions.py users.py \
        validation.py web.py config.json requirements.txt requirements-test.txt Dockerfile .dockerignore \
        start_reimbursement_tool.sh start_reimbursement_tool.command \
        scripts/build-docker.sh scripts/export-docker.sh scripts/package-synology.sh
    for SOURCE_DIRECTORY in resources templates static tests; do
        if [ ! -d "$SOURCE_DIRECTORY" ] || [ -L "$SOURCE_DIRECTORY" ]; then
            echo "源码目录必须是非符号链接的目录：$SOURCE_DIRECTORY" >&2
            exit 1
        fi
    done
    cp app.py config.py generator.py office.py reimbursements.py security.py sessions.py users.py validation.py web.py \
        config.json requirements.txt requirements-test.txt Dockerfile .dockerignore \
        start_reimbursement_tool.sh start_reimbursement_tool.command "$DESTINATION/"
    cp scripts/build-docker.sh scripts/export-docker.sh scripts/package-synology.sh "$DESTINATION/scripts/"

    UNSAFE_ENTRIES=$(find resources templates static tests ! -type d ! -type f -print)
    [ -z "$UNSAFE_ENTRIES" ] || {
        echo "源码目录包含符号链接或其他非普通文件，拒绝打包：" >&2
        printf '%s\n' "$UNSAFE_ENTRIES" >&2
        exit 1
    }

    SOURCE_MANIFEST="$PACKAGE_DIR/source-files"
    find resources templates static tests -type f -print > "$SOURCE_MANIFEST"
    while IFS= read -r SOURCE_FILE; do
        case "$SOURCE_FILE" in
            */__pycache__/*|*/.DS_Store|*/._*|*.pyc|*.pyo) continue ;;
            */data/*|data/*|*/backups/*|backups/*|*/generated/*|generated/*|*/runtime/*|runtime/*|\
            */.env|*/.env.*|*/app-secret|*.db|*.db-*|*.sqlite|*.sqlite-*|*.sqlite3|*.sqlite3-*)
                echo "源码目录包含受保护文件，拒绝打包：$SOURCE_FILE" >&2
                exit 1
                ;;
        esac
        case "$SOURCE_FILE" in
            resources/差旅报销单模板.xlsx|\
            templates/admin.html|templates/change-password.html|templates/history.html|templates/index.html|\
            templates/login.html|templates/register.html|templates/setup.html|\
            static/admin.js|static/app.js|static/auth.js|static/calendar.css|static/calendar.js|\
            static/common.js|static/favicon.png|static/history.js|static/icons.svg|static/logo.png|\
            static/mileage-development-qr.jpg|static/styles.css|static/ui-components.css|static/ui-tokens.css|\
            tests/__init__.py|tests/http_helpers.py|tests/test_*.py|tests/fixtures/receipt.png) ;;
            *)
                echo "源码目录包含未批准的交付文件，拒绝打包：$SOURCE_FILE" >&2
                exit 1
                ;;
        esac
        TARGET_FILE="$DESTINATION/$SOURCE_FILE"
        mkdir -p "$(dirname -- "$TARGET_FILE")"
        cp "$SOURCE_FILE" "$TARGET_FILE"
    done < "$SOURCE_MANIFEST"
fi
if tar --help 2>&1 | grep -q -- '--format'; then
    COPYFILE_DISABLE=1 COPY_EXTENDED_ATTRIBUTES_DISABLE=1 \
        tar --format ustar -czf "$PACKAGE_DIR/archive.tar.gz" -C "$PACKAGE_DIR" "$PACKAGE_NAME"
else
    COPYFILE_DISABLE=1 COPY_EXTENDED_ATTRIBUTES_DISABLE=1 \
        tar -czf "$PACKAGE_DIR/archive.tar.gz" -C "$PACKAGE_DIR" "$PACKAGE_NAME"
fi
ln "$PACKAGE_DIR/archive.tar.gz" "$ARCHIVE"
printf '%s\n' "$ARCHIVE"
