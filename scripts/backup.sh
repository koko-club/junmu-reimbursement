#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
PROJECT_DIR=$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd -P)
cd "$PROJECT_DIR"
. scripts/release-vars.sh
[ -f compose.yaml ] || { echo "找不到 compose.yaml，请保留 scripts 目录结构。" >&2; exit 1; }
command -v docker >/dev/null 2>&1 || { echo "需要 Docker Compose 插件。" >&2; exit 1; }

restart_service() {
    backup_status=$?
    trap - EXIT
    # A scheduler cancellation must not interrupt the recovery that follows a stopped backup.
    trap '' HUP INT TERM
    restart_status=1
    restart_attempt=1
    while [ "$restart_attempt" -le 3 ]; do
        if docker compose up -d --wait --wait-timeout 120 junmu-reimbursement; then
            restart_status=0
            break
        fi
        restart_attempt=$((restart_attempt + 1))
    done
    if [ "$restart_status" -ne 0 ]; then
        echo "服务重启或健康检查失败，请执行 docker compose logs junmu-reimbursement。" >&2
        backup_status=1
    fi
    exit "$backup_status"
}
trap restart_service EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
docker compose stop junmu-reimbursement
docker compose run --rm --no-deps -T junmu-reimbursement python backup.py --data-dir /data --backup-dir /backups
