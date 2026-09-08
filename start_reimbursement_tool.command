#!/bin/sh
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
cd "$SCRIPT_DIR"
exec sh "$SCRIPT_DIR/start_reimbursement_tool.sh" --open-browser
