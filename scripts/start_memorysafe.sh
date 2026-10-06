#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PLUGIN_DIR=$(dirname "$SCRIPT_DIR")
PYTHON_BIN="$PLUGIN_DIR/.venv/bin/python"

if [ ! -x "$PYTHON_BIN" ]; then
  echo "MemorySafe is not installed yet. Run scripts/setup.sh first." >&2
  exit 1
fi

export MEMORYSAFE_DB_PATH="${MEMORYSAFE_DB_PATH:-$PLUGIN_DIR/data/memorysafe.sqlite3}"
# The bundled desktop Python intentionally ignores hidden .pth files. Set the
# source path explicitly so the editable beta package always resolves.
export PYTHONPATH="$PLUGIN_DIR/src${PYTHONPATH:+:$PYTHONPATH}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$PLUGIN_DIR/runtime-state/cache}"
exec "$PYTHON_BIN" -m memorysafe_chatgpt.server
