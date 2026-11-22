#!/bin/zsh
set -u

CONTENTS_DIR="${0:A:h:h}"
PAYLOAD="$CONTENTS_DIR/Resources/payload"
LOG_DIR="$HOME/Library/Logs/MemorySafe Beta"
LOG_FILE="$LOG_DIR/installer.log"
mkdir -p "$LOG_DIR"

/usr/bin/osascript -e 'display notification "Installation started. This may take a minute." with title "MemorySafe Beta"' >/dev/null 2>&1 || true

PYTHON_EXECUTABLE=""
for candidate in \
  /usr/local/bin/python3 \
  /opt/homebrew/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/Current/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/3.14/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/3.13/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/3.12/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/3.11/bin/python3 \
  /Library/Frameworks/Python.framework/Versions/3.10/bin/python3 \
  /usr/bin/python3; do
  if [[ -x "$candidate" ]] && "$candidate" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
    PYTHON_EXECUTABLE="$candidate"
    break
  fi
done

if [[ -z "$PYTHON_EXECUTABLE" ]]; then
  print -r -- "MemorySafe Beta needs Python 3.10 or newer." >>"$LOG_FILE"
  /usr/bin/osascript -e 'display dialog "MemorySafe needs a compatible Python runtime. Please contact contact@memorysafe.ca for help." buttons {"OK"} default button "OK" with icon stop with title "MemorySafe Beta"' >/dev/null 2>&1 || true
  exit 1
fi

if "$PYTHON_EXECUTABLE" "$PAYLOAD/scripts/install_macos.py" --source-root "$PAYLOAD" --python "$PYTHON_EXECUTABLE" >>"$LOG_FILE" 2>&1; then
  /usr/bin/osascript -e 'display dialog "MemorySafe Beta is ready. The setup screen is opening now." buttons {"Continue"} default button "Continue" with title "MemorySafe Beta"' >/dev/null 2>&1 || true
  /usr/bin/open 'http://127.0.0.1:8765/'
  exit 0
fi

/usr/bin/osascript -e 'display dialog "MemorySafe could not finish the installation. The error report is in Library/Logs/MemorySafe Beta/installer.log." buttons {"OK"} default button "OK" with icon stop with title "MemorySafe Beta"' >/dev/null 2>&1 || true
exit 1
