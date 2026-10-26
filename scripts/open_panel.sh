#!/bin/zsh
# Open the small MemorySafe window. Called from the Dock app after services start.
set -u
PORT="${MEMORYSAFE_SETUP_PORT:-8765}"
PANEL="http://127.0.0.1:${PORT}/panel"

for _ in {1..40}; do
  if /usr/bin/nc -z 127.0.0.1 "$PORT" >/dev/null 2>&1; then
    break
  fi
  /bin/sleep 0.25
done

for app in "Google Chrome" "Brave Browser" "Microsoft Edge" "Chromium"; do
  if [ -d "/Applications/${app}.app" ]; then
    /usr/bin/open -na "$app" --args --app="$PANEL" --window-size=400,640 --window-position=48,72
    exit 0
  fi
done

/usr/bin/open "$PANEL"
