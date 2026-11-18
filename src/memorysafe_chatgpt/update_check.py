"""Tell people when a newer MemorySafe has been released.

Why this exists
---------------
The plugin does not update itself, and until now nothing told a person on this computer
that a newer version existed: someone who installed 0.5.0 stayed on it until they happened
to read the release page.

What it does
------------
At most once a week, one process on this computer asks GitHub where the latest release of
the public plugin repository is. The request is a HEAD for

    https://github.com/MemorySafe-Labs/memorysafe-plugin/releases/latest

and the answer is a redirect whose address ends in the version (".../releases/tag/v0.5.4").
The redirect is not followed and no page is downloaded. The version, and when it was asked
for, are kept in the state folder (update-check.json). Everything else here only reads
that file: the session-start note, the dashboard and `memorysafe doctor` make no call.

What the request carries
------------------------
The address above and a fixed User-Agent ("memorysafe-update-check"). No query string, no
body, no cookie, no installed version, no device id, no platform, nothing from the store.
GitHub sees what any web server sees: the address the request came from. Only digits and
dots are ever taken from the answer, so nothing a server sends can become text an
assistant reads.

Once a week, counted on attempts
--------------------------------
The time of an attempt is written before the request is sent, and a failed attempt counts:
a computer that was offline asks again a week later, not at the next session. "At most
once a week" is then true whatever the network does.

Switching it off
----------------
MEMORYSAFE_UPDATE_CHECK=0 (or false, no, off), or the switch on the dashboard, which writes
update-preference.json. When it is off no request is made and no notice is shown.

Runs on Python 3.8 with the standard library only: the session-start hook and the bootstrap
proxy import it on whatever python3 the machine has.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Tuple

from .bootstrap_catalog import VERSION

PUBLIC_REPOSITORY = "MemorySafe-Labs/memorysafe-plugin"
LATEST_RELEASE_URL = f"https://github.com/{PUBLIC_REPOSITORY}/releases/latest"
MCPB_NAME = "memorysafe-claude-desktop.mcpb"
MCPB_URL = f"{LATEST_RELEASE_URL}/download/{MCPB_NAME}"
USER_AGENT = "memorysafe-update-check"
TIMEOUT_SECONDS = 3.0

CHECK_INTERVAL_SECONDS = 7 * 24 * 60 * 60
# The session-start note carries the line once a day per assistant, not at every session:
# someone who opens ten sessions a day and has said "not now" is not asked ten times.
NOTICE_INTERVAL_SECONDS = 24 * 60 * 60
# How often a long-running server looks at whether a week has passed. Looking is local.
WAKE_INTERVAL_SECONDS = 6 * 60 * 60
# A stamp further ahead than this is a clock that was wrong when it was written, not a
# reason to stay silent until the clock catches up.
_CLOCK_SLACK_SECONDS = 24 * 60 * 60

ENV_SWITCH = "MEMORYSAFE_UPDATE_CHECK"
CACHE_FILE = "update-check.json"
PREFERENCE_FILE = "update-preference.json"
NOTIFIED_FILE = "update-notified.json"
LOCK_FILE = "update-check.lock"
_LOCK_STALE_SECONDS = 5 * 60

CLAUDE_CODE = "claude-code"
CODEX = "codex"
CLAUDE_DESKTOP = "claude-desktop"

_OFF_WORDS = frozenset({"0", "false", "no", "off"})
_VERSION_RE = re.compile(r"\d{1,4}(?:\.\d{1,4}){1,3}\Z")
_TAG_RE = re.compile(r"/releases/tag/v?(\d{1,4}(?:\.\d{1,4}){1,3})/?\Z")

Fetch = Callable[[], Optional[str]]


# -- the switch -----------------------------------------------------------------------------------


def switched_off_by_environment(env: Optional[Mapping[str, str]] = None) -> bool:
    value = (os.environ if env is None else env).get(ENV_SWITCH)
    return isinstance(value, str) and value.strip().lower() in _OFF_WORDS


def preference(state_dir: Path) -> bool:
    """What the dashboard switch says. On unless it was switched off there."""

    return _read(Path(state_dir) / PREFERENCE_FILE).get("enabled") is not False


def set_preference(state_dir: Path, enabled: bool) -> None:
    _write(Path(state_dir) / PREFERENCE_FILE, {"enabled": bool(enabled), "decided_at": _iso(time.time())})


def enabled(state_dir: Path, env: Optional[Mapping[str, str]] = None) -> bool:
    return not switched_off_by_environment(env) and preference(state_dir)


# -- versions -------------------------------------------------------------------------------------


def version_key(version: object) -> Optional[Tuple[int, ...]]:
    """"0.5.4" -> (0, 5, 4). None for anything that is not dotted integers."""

    if not isinstance(version, str) or not _VERSION_RE.match(version):
        return None
    return tuple(int(part) for part in version.split("."))


def is_newer(latest: object, installed: object) -> bool:
    latest_key, installed_key = version_key(latest), version_key(installed)
    if latest_key is None or installed_key is None:
        return False
    return latest_key > installed_key


# -- the one request ------------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *arguments: Any, **keywords: Any) -> None:
        return None


def version_from_location(location: object) -> Optional[str]:
    """The version a release address ends in. Digits and dots only, or None."""

    if not isinstance(location, str) or len(location) > 300:
        return None
    match = _TAG_RE.search(location.split("?", 1)[0].split("#", 1)[0])
    return match.group(1) if match else None


def fetch_latest(url: str = LATEST_RELEASE_URL, timeout: float = TIMEOUT_SECONDS) -> Optional[str]:
    """Ask where the latest release is. None when it cannot be learned, for any reason.

    A HEAD that follows no redirect: the answer wanted is the redirect's own address. The
    opener is built here with no cookie handling; it does honour HTTPS_PROXY, as the
    first-start downloads do."""

    request = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    try:
        response = urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout)
    except urllib.error.HTTPError as answer:
        try:
            if answer.code in (301, 302, 303, 307, 308):
                return version_from_location(answer.headers.get("Location"))
            return None
        finally:
            answer.close()
    except Exception:  # noqa: BLE001 - offline, blocked, bad certificate: all mean "not known"
        return None
    # Answered without redirecting: there is no release address to read.
    response.close()
    return None


# -- the record -----------------------------------------------------------------------------------


def _read(path: Path) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(json.dumps(dict(payload), indent=2, sort_keys=True) + "\n")
        os.replace(temporary, str(path))
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _iso(moment: float) -> str:
    return datetime.fromtimestamp(moment, timezone.utc).isoformat(timespec="seconds")


def _seconds(stamp: object) -> Optional[float]:
    if not isinstance(stamp, str):
        return None
    try:
        return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _within(stamp: object, interval: float, now: float) -> bool:
    """Whether `stamp` is a time less than `interval` before `now`."""

    then = _seconds(stamp)
    if then is None or then > now + _CLOCK_SLACK_SECONDS:
        return False
    return now - then < interval


def read_cache(state_dir: Path) -> Dict[str, Any]:
    return _read(Path(state_dir) / CACHE_FILE)


def due(state_dir: Path, now: Optional[float] = None) -> bool:
    """Whether a week has passed since the last attempt."""

    moment = time.time() if now is None else now
    return not _within(read_cache(state_dir).get("attempted_at"), CHECK_INTERVAL_SECONDS, moment)


def _take_lock(state_dir: Path, now: float) -> bool:
    """One process at a time. Claude Desktop, Claude Code and Codex each run a server on the
    same state folder and may all start in the same second."""

    lock = Path(state_dir) / LOCK_FILE
    for _ in range(2):
        try:
            os.close(os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return True
        except FileExistsError:
            try:
                if now - lock.stat().st_mtime < _LOCK_STALE_SECONDS:
                    return False
                lock.unlink()
            except OSError:
                return False
        except OSError:
            return False
    return False


def refresh(
    state_dir: Path,
    now: Optional[float] = None,
    fetch: Optional[Fetch] = None,
    env: Optional[Mapping[str, str]] = None,
) -> bool:
    """Ask for the latest version if it is switched on and a week has passed.

    Returns whether a request was made. Never raises: a version check must not be the
    reason anything else fails."""

    state_dir = Path(state_dir)
    moment = time.time() if now is None else now
    try:
        if not enabled(state_dir, env) or not due(state_dir, moment):
            return False
        state_dir.mkdir(parents=True, exist_ok=True)
        if not _take_lock(state_dir, moment):
            return False
        try:
            # Another process may have asked between the look above and the lock.
            if not due(state_dir, moment):
                return False
            record = read_cache(state_dir)
            record["attempted_at"] = _iso(moment)
            # Written before the request: if it cannot be written, no request is made,
            # because nothing would stop the next start from making another.
            _write(state_dir / CACHE_FILE, record)
            latest = (fetch or fetch_latest)()
            if version_key(latest) is not None:
                record["latest"] = latest
                record["checked_at"] = _iso(moment)
                _write(state_dir / CACHE_FILE, record)
            return True
        finally:
            try:
                (state_dir / LOCK_FILE).unlink()
            except OSError:
                pass
    except Exception:  # noqa: BLE001
        return False


def run_in_background(state_dir: Path) -> None:
    """Start the weekly check beside a long-running server, off its path.

    A daemon thread, so it never holds the server open. It looks every few hours whether
    a week has passed, because Claude Desktop can leave one server running for weeks."""

    import threading

    def loop() -> None:
        while True:
            refresh(state_dir)
            time.sleep(WAKE_INTERVAL_SECONDS)

    threading.Thread(target=loop, name="memorysafe-update-check", daemon=True).start()


# -- what is shown --------------------------------------------------------------------------------


def available(state_dir: Path, installed: str = VERSION, env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """The newer version on record, or None: switched off, never checked, or up to date."""

    state_dir = Path(state_dir)
    if not enabled(state_dir, env):
        return None
    latest = read_cache(state_dir).get("latest")
    return latest if is_newer(latest, installed) else None


def detect_host(env: Optional[Mapping[str, str]] = None, plugin_path: Optional[Path] = None) -> Optional[str]:
    """Which assistant this copy of the plugin was installed into, when that can be told.

    Claude Desktop names itself in the extension's environment. Claude Code and Codex keep
    their plugins under ~/.claude and ~/.codex, so the folder this file runs from says which."""

    environment = os.environ if env is None else env
    if environment.get("MEMORYSAFE_HOST") == CLAUDE_DESKTOP:
        return CLAUDE_DESKTOP
    try:
        parts = set((plugin_path or Path(__file__)).resolve().parts)
    except OSError:
        return None
    if ".codex" in parts:
        return CODEX
    if ".claude" in parts:
        return CLAUDE_CODE
    if "Claude Extensions" in parts:
        return CLAUDE_DESKTOP
    return None


_CLAUDE_CODE_STEPS = "`claude plugin marketplace update memorysafe` then `claude plugin update memorysafe@memorysafe`"
_CODEX_STEPS = "`codex plugin marketplace upgrade memorysafe` then `codex plugin add memorysafe@memorysafe`"
_DESKTOP_STEPS = (
    f"download {MCPB_URL} and install it in Claude Desktop under "
    "Settings > Extensions > Advanced settings > Install Extension"
)


def steps(host: Optional[str]) -> str:
    """The update steps for one assistant, or for all three when it is not known which."""

    if host == CLAUDE_CODE:
        return f"run {_CLAUDE_CODE_STEPS}, and then they restart Claude Code"
    if host == CODEX:
        return f"run {_CODEX_STEPS}, and then they restart Codex"
    if host == CLAUDE_DESKTOP:
        return f"they {_DESKTOP_STEPS}, then restart Claude Desktop (you cannot do this for them)"
    return (
        f"in Claude Code run {_CLAUDE_CODE_STEPS}; in Codex run {_CODEX_STEPS}; for Claude Desktop "
        f"they {_DESKTOP_STEPS}; then they restart that assistant"
    )


def assistant_line(latest: str, host: Optional[str], installed: str = VERSION) -> str:
    """The one line an assistant is given. It asks first; it never updates unasked."""

    return (
        f"MemorySafe update: version {latest} is available and this computer has {installed}. "
        f"Tell the person once and ask before updating; do not update unasked. If they agree, "
        f"{steps(host)}. Memories and settings carry over."
    )


def assistant_notice(
    state_dir: Path,
    host: Optional[str],
    installed: str = VERSION,
    env: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """The line for an assistant, or None when there is nothing to say."""

    latest = available(state_dir, installed, env)
    return assistant_line(latest, host, installed) if latest else None


def session_notice(
    state_dir: Path,
    host: Optional[str],
    now: Optional[float] = None,
    installed: str = VERSION,
    env: Optional[Mapping[str, str]] = None,
) -> Optional[str]:
    """The line for the session-start note: at most once a day for each assistant.

    Remembering that it was said is a write to the state folder, never to the store. If it
    cannot be written the line is still given; said twice is better than never."""

    state_dir = Path(state_dir)
    latest = available(state_dir, installed, env)
    if not latest:
        return None
    moment = time.time() if now is None else now
    key = f"{host or 'assistant'}:{latest}"
    said = _read(state_dir / NOTIFIED_FILE)
    if _within(said.get(key), NOTICE_INTERVAL_SECONDS, moment):
        return None
    try:
        # Only the newest version's stamps are kept, so the file does not grow.
        kept = {name: stamp for name, stamp in said.items() if name.endswith(f":{latest}")}
        kept[key] = _iso(moment)
        _write(state_dir / NOTIFIED_FILE, kept)
    except OSError:
        pass
    return assistant_line(latest, host, installed)


def status(state_dir: Path, installed: str = VERSION, env: Optional[Mapping[str, str]] = None) -> Dict[str, Any]:
    """What the dashboard and the doctor show. Reads the record; makes no request."""

    state_dir = Path(state_dir)
    record = read_cache(state_dir)
    by_environment = switched_off_by_environment(env)
    on = not by_environment and preference(state_dir)
    latest = record.get("latest") if version_key(record.get("latest")) is not None else None
    newer = bool(on and is_newer(latest, installed))
    return {
        "enabled": on,
        "switched_off_by_environment": by_environment,
        "installed": installed,
        "latest": latest if on else None,
        "update_available": newer,
        "last_checked_at": record.get("checked_at") if isinstance(record.get("checked_at"), str) else None,
        "release_url": LATEST_RELEASE_URL,
        "steps": person_steps() if newer else [],
    }


def person_steps() -> list:
    """The same steps, said to the person, one entry per assistant."""

    return [
        {"assistant": "Claude Code", "text": "In a terminal: claude plugin marketplace update memorysafe, "
         "then claude plugin update memorysafe@memorysafe. Restart Claude Code."},
        {"assistant": "Codex", "text": "In a terminal: codex plugin marketplace upgrade memorysafe, "
         "then codex plugin add memorysafe@memorysafe. Restart Codex."},
        {"assistant": "Claude Desktop", "text": f"Download {MCPB_URL} and install it under "
         "Settings > Extensions > Advanced settings > Install Extension. Restart Claude Desktop."},
    ]


def summary(state: Mapping[str, Any]) -> str:
    """One plain sentence about `status()`, for the doctor."""

    if state.get("switched_off_by_environment"):
        return f"The weekly check for a new version is switched off by {ENV_SWITCH}. No request is made."
    if not state.get("enabled"):
        return "The weekly check for a new version is switched off on the dashboard. No request is made."
    if state.get("update_available"):
        return (
            f"MemorySafe {state['latest']} is available; this computer has {state['installed']}. "
            f"Ask your assistant to update MemorySafe, or see {LATEST_RELEASE_URL}."
        )
    if state.get("last_checked_at"):
        return f"MemorySafe {state['installed']} is the newest version (checked {str(state['last_checked_at'])[:10]})."
    return "No check for a new version has finished yet. It runs once a week when an assistant starts MemorySafe."

