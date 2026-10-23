#!/bin/zsh
set -eu

SCRIPT_DIR="${0:A:h}"
INSTALL_ROOT="${MEMORYSAFE_INSTALL_ROOT:-${SCRIPT_DIR:h}}"
STATE_DIR="$INSTALL_ROOT/runtime-state"
KEY_FILE="$INSTALL_ROOT/.secrets/tunnel-runtime-key"
TUNNEL_ID_FILE="$STATE_DIR/tunnel-id"
CONSENT_FILE="$STATE_DIR/consent.json"
mkdir -p "$STATE_DIR/logs"
/usr/bin/python3 "$INSTALL_ROOT/scripts/rotate_runtime_log.py" \
  "$STATE_DIR/logs/tunnel.log" --max-bytes $((10 * 1024 * 1024)) --backups 2

PID_FILE="$STATE_DIR/tunnel-service.pid"
HEALTH_FILE="${MEMORYSAFE_HEALTH_URL_FILE:-$STATE_DIR/health/tunnel.url}"
pid_belongs_to_tunnel() {
  local pid="$1"
  local command
  command="$(/bin/ps -p "$pid" -o command= 2>/dev/null || true)"
  [[ "$command" == *"$INSTALL_ROOT/bin/tunnel-client"* || "$command" == *"$INSTALL_ROOT/scripts/run_tunnel_service.sh"* ]]
}

if [[ -s "$PID_FILE" ]]; then
  EXISTING_PID="$(<"$PID_FILE")"
  if [[ "$EXISTING_PID" == <-> ]] && /bin/kill -0 "$EXISTING_PID" 2>/dev/null && pid_belongs_to_tunnel "$EXISTING_PID"; then
    if [[ -s "$HEALTH_FILE" ]]; then
      # A manually-started healthy copy may already own the tunnel. Remain alive as
      # a launchd watcher instead of exiting into a restart loop.
      while /bin/kill -0 "$EXISTING_PID" 2>/dev/null && [[ -s "$HEALTH_FILE" ]]; do
        /bin/sleep 30
      done
    else
      # Only terminate a verified MemorySafe tunnel process. A stale PID file must
      # never be allowed to kill an unrelated process that reused the same number.
      /bin/kill "$EXISTING_PID" 2>/dev/null || true
      for _ in {1..20}; do
        /bin/kill -0 "$EXISTING_PID" 2>/dev/null || break
        /bin/sleep 0.25
      done
    fi
  elif [[ "$EXISTING_PID" == <-> ]]; then
    # Stale or unrelated PID: do not signal it; simply replace the stale record.
    true
  fi
  if [[ "$EXISTING_PID" == <-> ]] && /bin/kill -0 "$EXISTING_PID" 2>/dev/null && pid_belongs_to_tunnel "$EXISTING_PID"; then
    /bin/kill "$EXISTING_PID" 2>/dev/null || true
  fi
fi
print -r -- "$$" >| "$PID_FILE"
/bin/chmod 600 "$PID_FILE"

consent_is_accepted() {
  /usr/bin/python3 - "$CONSENT_FILE" <<'PY'
import json
import sys
from pathlib import Path

try:
    payload = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
except (OSError, ValueError):
    raise SystemExit(1)
raise SystemExit(0 if payload.get("terms_accepted") is True else 1)
PY
}

while [[ ! -s "$KEY_FILE" || ! -s "$TUNNEL_ID_FILE" ]] || ! consent_is_accepted; do
  sleep 2
done

CONFIG_PATH="$(/usr/bin/python3 "$INSTALL_ROOT/scripts/write_runtime_config.py" --install-root "$INSTALL_ROOT")"
exec "$INSTALL_ROOT/bin/tunnel-client" run --config "$CONFIG_PATH"
