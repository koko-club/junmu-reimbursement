#!/bin/zsh
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"
exec "$SCRIPT_DIR/start_reimbursement_tool.sh" --open-browser
