#!/bin/zsh
set -eu

SCRIPT_DIR="${0:A:h}"
INSTALL_ROOT="${MEMORYSAFE_INSTALL_ROOT:-${SCRIPT_DIR:h}}"
export MEMORYSAFE_INSTALL_ROOT="$INSTALL_ROOT"
export MEMORYSAFE_STATE_DIR="${MEMORYSAFE_STATE_DIR:-$INSTALL_ROOT/runtime-state}"
export MEMORYSAFE_DB_PATH="${MEMORYSAFE_DB_PATH:-$INSTALL_ROOT/data/memorysafe.sqlite3}"
export MEMORYSAFE_RUNTIME_KEY_FILE="${MEMORYSAFE_RUNTIME_KEY_FILE:-$INSTALL_ROOT/.secrets/tunnel-runtime-key}"
export MEMORYSAFE_TUNNEL_ID_FILE="${MEMORYSAFE_TUNNEL_ID_FILE:-$INSTALL_ROOT/runtime-state/tunnel-id}"
export MEMORYSAFE_HEALTH_URL_FILE="${MEMORYSAFE_HEALTH_URL_FILE:-$INSTALL_ROOT/runtime-state/health/tunnel.url}"
export MEMORYSAFE_LEGAL_DIR="${MEMORYSAFE_LEGAL_DIR:-$INSTALL_ROOT/legal}"
export PYTHONPATH="$INSTALL_ROOT/src${PYTHONPATH:+:$PYTHONPATH}"
export XDG_CACHE_HOME="${XDG_CACHE_HOME:-$INSTALL_ROOT/runtime-state/cache}"

/usr/bin/python3 "$INSTALL_ROOT/scripts/rotate_runtime_log.py" \
  "$MEMORYSAFE_STATE_DIR/logs/setup.error.log" --max-bytes $((10 * 1024 * 1024)) --backups 2
/usr/bin/python3 "$INSTALL_ROOT/scripts/rotate_runtime_log.py" \
  "$MEMORYSAFE_STATE_DIR/logs/setup.output.log" --max-bytes $((10 * 1024 * 1024)) --backups 2

PID_FILE="$MEMORYSAFE_STATE_DIR/setup-service.pid"
pid_belongs_to_setup() {
  local pid="$1"
  local command
  command="$(/bin/ps -p "$pid" -o command= 2>/dev/null || true)"
  [[ "$command" == *"$INSTALL_ROOT/scripts/run_setup_service.sh"* || "$command" == *"memorysafe_chatgpt.setup_app"* ]]
}

memorysafe_dashboard_running() {
  /usr/bin/python3 - <<'PY'
import json
import urllib.request

try:
    with urllib.request.urlopen("http://127.0.0.1:8765/api/status", timeout=0.5) as response:
        payload = json.load(response)
except Exception:
    raise SystemExit(1)
raise SystemExit(0 if payload.get("product") == "MemorySafe Beta" else 1)
PY
}

if [[ -s "$PID_FILE" ]]; then
  EXISTING_PID="$(<"$PID_FILE")"
  if [[ "$EXISTING_PID" == <-> ]] && /bin/kill -0 "$EXISTING_PID" 2>/dev/null && pid_belongs_to_setup "$EXISTING_PID"; then
    # A launcher fallback may already be serving the dashboard. Stay alive as a
    # watcher so launchd does not repeatedly restart and flood the error log.
    while /bin/kill -0 "$EXISTING_PID" 2>/dev/null; do
      /bin/sleep 30
    done
  fi
fi

# Claude's portable launcher can also start this same local dashboard without
# owning the setup PID file. If it is healthy, watch it instead of fighting for
# port 8765 and producing an address-in-use restart loop.
while memorysafe_dashboard_running; do
  /bin/sleep 30
done

print -r -- "$$" >| "$PID_FILE"
/bin/chmod 600 "$PID_FILE"

exec "$INSTALL_ROOT/.venv/bin/python" -m memorysafe_chatgpt.setup_app
