#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$SCRIPT_DIR"

PYTHON_BIN=""
PORTABLE_PYTHON="$SCRIPT_DIR/runtime/python/bin/python3"
for candidate in \
  "$PORTABLE_PYTHON" \
  "/Users/koko/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/bin/python3" \
  "/Users/koko/.cache/codex-runtimes/codex-primary-runtime/bin/python3" \
  "$(command -v python3 || true)"; do
  [ -n "$candidate" ] || continue
  [ -x "$candidate" ] || continue
  if [ "$candidate" = "$PORTABLE_PYTHON" ]; then
    if PYTHONHOME="$SCRIPT_DIR/runtime/python" "$candidate" -c 'import openpyxl, PIL' >/dev/null 2>&1; then
      PYTHON_BIN="$candidate"
      export PYTHONHOME="$SCRIPT_DIR/runtime/python"
      break
    fi
  elif "$candidate" -c 'import openpyxl, PIL' >/dev/null 2>&1; then
    PYTHON_BIN="$candidate"
    break
  fi
done
if [ -z "$PYTHON_BIN" ]; then
  echo "找不到可用的 Python 3（需要 openpyxl 和 Pillow）。请先安装依赖：python3 -m pip install -r requirements.txt" >&2
  exit 1
fi

if [ -d "$SCRIPT_DIR/vendor" ]; then
  export PYTHONPATH="$SCRIPT_DIR/vendor${PYTHONPATH:+:$PYTHONPATH}"
fi

OPEN_BROWSER=0
if [ "${1:-}" = "--open-browser" ]; then OPEN_BROWSER=1; fi

"$PYTHON_BIN" app.py | while IFS= read -r line; do
  printf '%s\n' "$line"
  case "$line" in
    http://*)
      if [ "$OPEN_BROWSER" = 1 ] && command -v open >/dev/null 2>&1; then
        open "$line" >/dev/null 2>&1 || true
        OPEN_BROWSER=0
      fi
      ;;
  esac
done
