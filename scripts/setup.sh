#!/bin/sh
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PLUGIN_DIR=$(dirname "$SCRIPT_DIR")
PYTHON_SOURCE=${PYTHON_SOURCE:-python3}

"$PYTHON_SOURCE" -m venv "$PLUGIN_DIR/.venv"
"$PLUGIN_DIR/.venv/bin/python" -m pip install --upgrade pip setuptools wheel
"$PLUGIN_DIR/.venv/bin/python" -m pip install --prefer-binary "cryptography>=48.0.1,<50"
"$PLUGIN_DIR/.venv/bin/python" -m pip install --editable "$PLUGIN_DIR"
mkdir -p "$PLUGIN_DIR/runtime-state/cache"
XDG_CACHE_HOME="$PLUGIN_DIR/runtime-state/cache" \
  "$PLUGIN_DIR/.venv/bin/python" -c "import tiktoken; tiktoken.get_encoding('o200k_base')"

echo "MemorySafe is installed in its private beta workspace."
