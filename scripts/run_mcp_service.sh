#!/bin/zsh
set -eu

SCRIPT_DIR="${0:A:h}"
INSTALL_ROOT="${MEMORYSAFE_INSTALL_ROOT:-${SCRIPT_DIR:h}}"
export MEMORYSAFE_INSTALL_ROOT="$INSTALL_ROOT"
export MEMORYSAFE_DB_PATH="${MEMORYSAFE_DB_PATH:-$INSTALL_ROOT/data/memorysafe.sqlite3}"
export PYTHONPATH="$INSTALL_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$INSTALL_ROOT/runtime-state/cache}"

exec "$INSTALL_ROOT/.venv/bin/python" -m memorysafe_chatgpt.server
