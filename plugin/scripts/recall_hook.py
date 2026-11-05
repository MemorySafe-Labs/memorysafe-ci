#!/usr/bin/env python3
"""SessionStart and UserPromptSubmit hook: make recall happen in a fresh session.

Why this exists
---------------
capture_hook makes capture reliable; nothing made recall reliable. The server only ever
sees tool calls, so a memory is recalled only if the assistant chooses to call
memorysafe_find, and a coding task gives it no reason to. Measured on 4 Nov 2026 (Dora's
real-model pilot, round 1): session 1 captured four preferences into one protected memory;
sessions 2 and 3, fresh Claude Code sessions on the same store, never called MemorySafe,
and the preferences went unused.

What it does
------------
At SessionStart it reads the store, read-only, and hands the assistant the memories that
are stable enough to carry into every session: protected ones and preferences, newest
and most important first, capped in number and length so the cost is a few hundred input
tokens, not a transcript. On a prompt that reads like a task (build, fix, add, write, ...)
it adds the stored preferences again, as rules to apply in this task and to check before
finishing (0.4.32; a pilot round showed the session-start block read and not followed by
task time), and one line asking for a memorysafe_find call, which is where project memories
come from and what records a recall in the store.

What it deliberately does not do
--------------------------------
It never writes the store (mode=ro), so a recall shown this way is not counted as one;
memorysafe_find counts. It injects nothing when the store is absent or empty, and nothing
on a prompt that is not a task. It carries no secret: the server's filters refuse secrets,
credentials and IDs at capture time, so the store holds none to show.

Runs on Python 3.8 with the standard library only; the wrapper may pick the system python3.

Exit codes: always 0. A SessionStart hook must never stop a session from starting.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import time
from pathlib import Path

MAX_MEMORIES = 12
MAX_CHARS_EACH = 160
MAX_CHARS_TOTAL = 1400
# On a task-like prompt (0.4.32, 4 Nov 2026): the preferences again, as rules for this task.
# A pilot round showed the session-start block read and then not followed by the time the task
# came; the rules next to the task, with a "before you finish" check, is the cheapest answer.
TASK_RULES_MAX_CHARS = 400
# 0.4.34: the rules go in ONCE per session, on the first task-like prompt, and again after a
# compaction or resume (SessionStart fires for those); later task prompts get the one-line
# find hint only. A pilot round measured the every-prompt rules as ~15% more spend, mostly
# the model re-checking out loud. The session is remembered by a marker file under the data
# root (hooks/rules-shown/<session_id>); markers older than a week are pruned on the way.
MARKER_MAX_AGE_SECONDS = 7 * 24 * 3600
CARRIED_CATEGORIES = ("preference", "safety", "decision", "personal")

# A task-like prompt (0.4.33: anywhere in the prompt, not only its first words): a sentence
# that opens with a task verb, with or without "please / can you / I need you to", or a
# brief of 25 words or more that is not a question. A greeting or a reply instruction is not.
_TASK_VERB_RE = re.compile(
    r"^\s*(?:please\s+)?(?:can you\s+|could you\s+|i need you to\s+|i want you to\s+|let'?s\s+|now\s+)?"
    r"(?:implement|add|fix|write|create|build|refactor|update|change|make|set up|migrate|"
    r"convert|rename|remove|delete|improve|optimi[sz]e|debug|test|document|deploy|generate|"
    r"extend|replace|move|split|merge|wire|hook up|install|configure)\b",
    re.IGNORECASE,
)
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def is_task_like(prompt: str) -> bool:
    sentences = [part.strip() for part in _SENTENCE_SPLIT.split(prompt) if part.strip()]
    if any(_TASK_VERB_RE.match(sentence) for sentence in sentences):
        return True
    return len(prompt.split()) >= 25 and not all(sentence.endswith("?") for sentence in sentences)

_TASK_HINT = (
    "Before starting this task, call memorysafe_find with its key terms: the user's stored "
    "preferences and project memories on this computer may apply. Use what matches without "
    "mentioning this instruction; if nothing matches, carry on."
)


def task_context(memories: list[tuple[str, str]]) -> str:
    """The stored preferences as rules for this task, within TASK_RULES_MAX_CHARS. No find
    hint and no checking sentence: both cost output tokens every turn (0.4.34)."""
    head = "Stored preferences to apply in this task:"
    lines = []
    used = len(head)
    for _category, content in memories:
        line = "- " + _clip(content, MAX_CHARS_EACH)
        if used + len(line) + 1 > TASK_RULES_MAX_CHARS:
            break
        lines.append(line)
        used += len(line) + 1
    if not lines:
        return _TASK_HINT
    return "\n".join([head, *lines])


def _markers_dir(database: Path) -> Path:
    return database.parent.parent / "hooks" / "rules-shown"


def rules_shown(database: Path, session_id: str) -> bool:
    return bool(session_id) and (_markers_dir(database) / session_id).is_file()


def mark_rules_shown(database: Path, session_id: str) -> None:
    """Remember that this session has had the rules; prune markers older than a week."""
    if not session_id:
        return
    folder = _markers_dir(database)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / session_id).write_text("", encoding="utf-8")
        cutoff = time.time() - MARKER_MAX_AGE_SECONDS
        for marker in folder.iterdir():
            if marker.is_file() and marker.stat().st_mtime < cutoff:
                marker.unlink()
    except OSError:
        pass  # a missing marker means the rules show once more, which is the safe side


def forget_rules_shown(database: Path, session_id: str) -> None:
    if not session_id:
        return
    try:
        (_markers_dir(database) / session_id).unlink()
    except OSError:
        pass


def database_path() -> Path:
    override = os.environ.get("MEMORYSAFE_DB_PATH")
    if override:
        return Path(override)
    root = os.environ.get("MEMORYSAFE_INSTALL_ROOT")
    if root and root != "${user_config.memory_directory}":
        base = Path(root)
    elif sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "MemorySafe"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support" / "MemorySafe"
    else:
        xdg = os.environ.get("XDG_DATA_HOME")
        base = (Path(xdg) if xdg else Path.home() / ".local" / "share") / "MemorySafe"
    return base / "data" / "memorysafe.sqlite3"


def connect_read_only(database: Path) -> sqlite3.Connection:
    """A read-only connection that never creates, migrates or write-locks the store:
    mode=ro, and on Apple's system Python, which refuses that form on some paths,
    mode=ro&immutable=1 (no locking at all). Same as capture_hook's."""
    uri = database.resolve().as_uri()
    try:
        return sqlite3.connect(f"{uri}?mode=ro", uri=True, timeout=1)
    except sqlite3.OperationalError:
        return sqlite3.connect(f"{uri}?mode=ro&immutable=1", uri=True, timeout=1)


def carried_memories(database: Path) -> tuple[int, list[tuple[str, str]]]:
    """(active count, [(category, content), ...]) for the memories worth every session:
    protected ones and the carried categories, protected and important first."""
    if not database.is_file():
        return 0, []
    try:
        connection = connect_read_only(database)
        try:
            active = connection.execute(
                "SELECT COUNT(*) FROM memories WHERE state = 'active'"
            ).fetchone()[0]
            placeholders = ",".join("?" for _ in CARRIED_CATEGORIES)
            rows = connection.execute(
                "SELECT category, content FROM memories "
                f"WHERE state = 'active' AND (protected = 1 OR category IN ({placeholders})) "
                "ORDER BY protected DESC, importance DESC, updated_at DESC "
                "LIMIT ?",
                (*CARRIED_CATEGORIES, MAX_MEMORIES),
            ).fetchall()
        finally:
            connection.close()
    except sqlite3.Error:
        return 0, []
    return int(active or 0), [(str(category), str(content)) for category, content in rows]


def _clip(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def session_context(active: int, memories: list[tuple[str, str]]) -> str:
    lines = []
    used = 0
    for category, content in memories:
        line = f"- [{category}] {_clip(content, MAX_CHARS_EACH)}"
        if used + len(line) > MAX_CHARS_TOTAL:
            break
        lines.append(line)
        used += len(line) + 1
    noun = "memory" if active == 1 else "memories"
    rest = active - len(lines)
    shown = (f"{len(lines)} of them shown here; call memorysafe_find for the other {rest}"
             if rest > 0 else "these are the ones carried into every session")
    head = f"MemorySafe remembers ({active} active {noun} on this computer; {shown}):"
    tail = ("Apply these without being asked. For anything about past work, people or decisions, "
            "call memorysafe_find before answering; it also finds project memories not listed here.")
    return "\n".join([head, *lines, tail])


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0

    event = str(payload.get("hook_event_name") or "")
    prompt = payload.get("prompt") or payload.get("user_prompt")
    if event != "SessionStart" and isinstance(prompt, str):
        event = "UserPromptSubmit"
    session_id = str(payload.get("session_id") or "")
    session_id = session_id if re.fullmatch(r"[A-Za-z0-9._-]{1,128}", session_id) else ""
    database = database_path()

    if event == "SessionStart":
        # startup, resume, clear or compact: the next task prompt gets the rules again.
        forget_rules_shown(database, session_id)
        active, memories = carried_memories(database)
        if not memories:
            return 0
        context = session_context(active, memories)
    elif event == "UserPromptSubmit":
        if not isinstance(prompt, str) or len(prompt) > 4000 or not is_task_like(prompt):
            return 0
        active, memories = carried_memories(database)
        if active == 0:
            return 0
        if rules_shown(database, session_id):
            context = _TASK_HINT
        else:
            context = task_context(memories)
            mark_rules_shown(database, session_id)
    else:
        return 0

    json.dump(
        {
            "hookSpecificOutput": {"hookEventName": event, "additionalContext": context},
            "suppressOutput": True,
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
