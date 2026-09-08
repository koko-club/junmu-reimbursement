#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)
cd "$PROJECT_DIR"
[ -f compose.yaml ] || { echo "找不到 compose.yaml，请保留 scripts 目录结构。" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "需要 Docker Compose 插件。" >&2; exit 1; }

restart_service() {
    backup_status=$?
    trap - EXIT HUP INT TERM
    if ! docker compose up -d --wait --wait-timeout 120 reimbursement; then
        echo "服务重启或健康检查失败，请执行 docker compose logs reimbursement。" >&2
        backup_status=1
    fi
    exit "$backup_status"
}
trap restart_service EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
docker compose stop reimbursement
docker compose run --rm --no-deps -T reimbursement python backup.py --data-dir /data --backup-dir /backups
