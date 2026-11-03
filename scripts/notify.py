#!/usr/bin/env python3
"""Tell the user something without interrupting them.

The installer only ever spoke through blocking dialogs, and only during installation.
After that MemorySafe was silent — including when both background services were dying
on launch and being restarted forever by launchd. That ran for eleven days on the
developer's own Mac without a single signal.

Notifications, not dialogs: a dialog demands attention for good news, and there is
nobody at the keyboard when a service fails at login.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

STATE = Path.home() / "Library" / "Application Support" / "MemorySafe" / "runtime-state"


def notify(title: str, message: str, once_key: str | None = None, hours: float = 24.0) -> bool:
    """Post a notification. With once_key, do not repeat it within `hours`.

    A crash loop restarts every 30 seconds; a notification per restart would be its own
    failure, so repeats are suppressed.
    """

    if once_key:
        stamp = STATE / "notified" / f"{once_key}.stamp"
        now = time.time()
        if stamp.is_file() and now - stamp.stat().st_mtime < hours * 3600:
            return False
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(now))

    safe_title = title.replace('"', "'")
    safe_message = message.replace('"', "'")
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e",
             f'display notification "{safe_message}" with title "{safe_title}"'],
            check=False, capture_output=True, timeout=10,
        )
    except Exception:
        return False  # never let telling someone something become the failure
    return True


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("usage: notify.py TITLE MESSAGE [once_key]")
        raise SystemExit(2)
    notify(sys.argv[1], sys.argv[2], sys.argv[3] if len(sys.argv) > 3 else None)
