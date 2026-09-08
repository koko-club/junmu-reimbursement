#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
cd "$SCRIPT_DIR"

PYTHON_BIN=""
for candidate in \
  "$SCRIPT_DIR/.venv/bin/python" \
  "$(command -v python3.14 || true)" \
  "$(command -v python3.13 || true)" \
  "$(command -v python3.12 || true)" \
  "$(command -v python3 || true)"; do
  [ -n "$candidate" ] || continue
  [ -x "$candidate" ] || continue
  if "$candidate" -c 'import sys; assert sys.version_info >= (3, 12); import openpyxl, PIL' >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done
if [ -z "$PYTHON_BIN" ]; then
  echo "需要 Python 3.12+、openpyxl 和 Pillow。请执行：python3.12 -m venv .venv，然后 .venv/bin/python -m pip install -r requirements.txt" >&2
  exit 1
fi

OPEN_BROWSER=0
case "${1:-}" in
  "") ;;
  --open-browser) OPEN_BROWSER=1 ;;
  *) echo "用法: sh start_reimbursement_tool.sh [--open-browser]" >&2; exit 1 ;;
esac
export APP_PORT="${APP_PORT:-8800}"
if [ "$OPEN_BROWSER" = 1 ] && command -v open >/dev/null 2>&1; then
  "$PYTHON_BIN" -c 'import os, subprocess, time, urllib.request
url = "http://127.0.0.1:" + os.environ["APP_PORT"]
for attempt in range(30):
    try:
        urllib.request.urlopen(url + "/api/health", timeout=1).close()
    except OSError:
        time.sleep(1)
    else:
        subprocess.run(["open", url], check=False)
        break' &
fi
exec "$PYTHON_BIN" app.py
