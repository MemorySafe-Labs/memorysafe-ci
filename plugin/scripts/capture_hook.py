#!/usr/bin/env python3
"""UserPromptSubmit hook: make MemorySafe auto-capture fire reliably.

Why this exists
---------------
An MCP server only ever sees tool calls, never the conversation. So automatic
capture can only happen if the assistant *chooses* to call the capture tool --
and measured over two weeks of daily use, it chose to exactly once.

This hook removes the choice from chance without removing it from judgement:
the harness runs this on every prompt (deterministic), and when the message
looks like it carries a durable user-stated fact, it injects an instruction
telling the assistant to offer the candidate. Detection is mechanical here;
extraction stays with the model, which is the part models are actually good at.

One exception, decided 4 Nov 2026 by the product owner after a real-model pilot in
which, in two rounds, the model ignored the capture hint and saved nothing: a PLAINLY STATED
PREFERENCE ("always use Black", "I prefer pytest", "from now on, no new
dependencies") is saved by this hook itself, without the model in the loop. It is
the one shape a pattern can judge, and it is the shape the product promises to
keep. The write goes through the same door as the tool: capture_policy.skip_reason
(secrets, contacts, IDs, health, length) and MemoryStore.remember with
source="automatic" (dedupe, merge, importance), so nothing this hook saves could
not have been saved by memorysafe_auto_capture. Everything else in a message is
still the model's to judge, through the hint below. The store code is imported
from the plugin's own src/ with the runtime Python; where that import fails
(system Python without the runtime), the hook falls back to the hint.

Gated on automatic mode
-----------------------
The hook ships inside the Claude Code and Codex plugins, so it runs for people who never
turned automatic mode on. After the cheap pattern match it reads the automatic_mode
setting from the store, read-only, and stays silent unless the mode is on: someone who
never opted in sees no extra tool calls and pays no extra tokens.

Runs on Python 3.8 with the standard library only; the wrapper may pick the system python3.

Exit codes: always 0. A capture hint is never worth interrupting a turn for.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from pathlib import Path

# First-person statements of durable fact or preference. Tuned to fire on the
# shapes the capture tool actually accepts (user-stated, durable, concise) and
# to stay quiet on the far more common case: questions and task instructions.
_FACT_PATTERNS = (
    r"\bI (?:prefer|like|hate|always|never|usually|tend to|generally)\b",
    r"\bI(?:'m| am) (?:working on|building|based in|allergic to|used to)\b",
    r"\bmy (?:name|role|title|team|company|timezone|preference|goal|deadline|stack)\b",
    r"\b(?:we|our team) (?:use|prefer|decided|agreed|always|never)\b",
    r"\b(?:remember|keep in mind|note) that\b",
    r"\bcall me\b",
    r"\bfrom now on\b",
    r"\bgoing forward\b",
)
_FACT_RE = re.compile("|".join(_FACT_PATTERNS), re.IGNORECASE)

# An explicit request is handled by memorysafe_remember, not auto-capture, and
# the assistant already reacts to it. Staying silent avoids double-prompting.
_EXPLICIT_RE = re.compile(r"\bremember (?:this|that i|me)\b", re.IGNORECASE)

# 0.4.33 (4 Nov 2026): what the hook saves on its own is decided by a small score, not by
# sentence openings. A pilot round showed the openings missed plain declarative facts ("The
# settings file is app.settings.toml at the repo root; never create config.* files.", "All
# user-facing strings go through t() from app/i18n.py, British spelling.") and saved two
# lines of a one-off instruction ("Reply with only the word OK. Do not use any tools.").
#
# Vetoes first: anything addressed to this reply or this turn, anything about tools, a
# question, a task ask (a task verb with a specific thing and nothing general). Then points
# for what makes a sentence LASTING: a generaliser (all, every, always, never, only, by
# default, ...), a declarative statement about the project ("The X is Y", "All X go through
# Y"), a first-person durable preference, a rule ("never create ...", "do not add ..."), a
# working verb with a tool and no specific thing. Two points save; the write still goes
# through capture_policy and the store's governance.
_ONE_OFF_RE = re.compile(
    r"\b(?:reply|respond|answer|say|tell me|show me|give me|send me|output|print|explain|"
    r"summari[sz]e|describe|only the word|this time|for now|right now|in this (?:reply|answer|"
    r"message|response|turn)|below|above|the following|first,|next,|then,|step \d|tools?)\b",
    re.IGNORECASE,
)
_TASK_VERB_RE = re.compile(
    r"^\s*(?:please\s+)?(?:can you\s+|could you\s+|i need you to\s+|i want you to\s+|let'?s\s+)?"
    r"(?:add|create|implement|fix|build|write|make|update|change|remove|delete|rename|refactor|"
    r"migrate|deploy|generate|test|debug|format|style|run|convert|improve|optimi[sz]e|document|"
    r"set up|use|keep|name|indent|put|place)\b",
    re.IGNORECASE,
)
_SPECIFIC_RE = re.compile(r"\b(?:the|this|that|these|those|a|an)\b", re.IGNORECASE)
_GENERALISER_RE = re.compile(
    r"\b(?:all|every|everything|everywhere|anything|any|always|never|only|by default|whenever|"
    r"from now on|going forward|as a rule|plain|standard|no new|in this (?:project|repo|repository|codebase))\b",
    re.IGNORECASE,
)
_DECLARATIVE_RE = re.compile(
    r"^\s*(?:the|our|all|every|each|this project'?s?|in this (?:project|repo|repository|codebase),?)\s+"
    r"[\w\s/.-]{1,60}?\b(?:is|are|lives?|goes?|go through|uses?|belongs?|must|should|has|have|needs?|comes? from|"
    r"stays?|runs? on)\b",
    re.IGNORECASE,
)
_FIRST_PERSON_RE = re.compile(
    r"^\s*(?:i|we|our team|my team)\s+(?:prefer|always|never|usually|use|want|like|work with|write|format|run)\b",
    re.IGNORECASE,
)
_RULE_RE = re.compile(r"\b(?:never|do not|don'?t|always|no)\s+(?:create|add|use|install|write|commit|touch|edit|new)\b", re.IGNORECASE)
_WORK_VERB_RE = re.compile(
    r"^\s*(?:please\s+)?(?:always\s+|never\s+)?(?:format|style|write|use|keep|name|indent|put|place|run|"
    r"prefer|avoid|follow|stick to|stick with|lint|test|document|commit|wrap|sort|import|organi[sz]e|structure|"
    r"type|declare|return|log|handle|validate|escape|quote|cite|speak|call|address|refer to)\b",
    re.IGNORECASE,
)
_TOOL_PHRASE_RE = re.compile(r"\b(?:with|using|via|as)\s+(?:plain\s+)?[A-Za-z][\w.+-]*", re.IGNORECASE)
_BARE_CHOICE_RE = re.compile(r"^\s*(?:please\s+)?(?:use|prefer|avoid|stick to|stick with)\s+\S+", re.IGNORECASE)
LASTING_THRESHOLD = 2
PREFERENCE_MAX_CHARS = 200
# 0.4.35 (4 Nov 2026): a message that states many lasting facts, a project brief pasted
# once, is saved whole, up to this many sentences; the score and capture_policy stay the
# gate. The earlier cap of three per message made a 25-fact brief save three.
MAX_PREFERENCES_PER_MESSAGE = 40
MESSAGE_MAX_CHARS = 8000
LISTED_IN_CONFIRMATION = 3
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n+")


def lasting_score(sentence: str) -> int:
    """Points for a sentence being a lasting fact or preference; 0 when a veto applies."""
    if "?" in sentence or _ONE_OFF_RE.search(sentence):
        return 0
    general = bool(_GENERALISER_RE.search(sentence))
    specific = bool(_SPECIFIC_RE.search(sentence))
    declarative = bool(_DECLARATIVE_RE.match(sentence))
    if _TASK_VERB_RE.match(sentence) and specific and not general and not declarative:
        return 0  # "Add a footer under the list": a task, the model's to judge
    score = 0
    if general:
        score += 2
    if declarative:
        score += 2
    if _FIRST_PERSON_RE.match(sentence):
        score += 2
    if _RULE_RE.search(sentence):
        score += 1
    if _WORK_VERB_RE.match(sentence) and not specific and _TOOL_PHRASE_RE.search(sentence):
        score += 1
    if _BARE_CHOICE_RE.match(sentence) and not specific and len(sentence.split()) <= 6:
        score += 2
    return score


def is_clear_preference(sentence: str) -> bool:
    """One sentence, already trimmed: lasting enough to save unjudged."""
    return lasting_score(sentence) >= LASTING_THRESHOLD


_HINT = (
    "The user's message may contain a durable, user-stated fact or preference. "
    "If it does, call memorysafe_auto_capture with one concise fact (at most three "
    "distinct facts). Pass only what the user directly stated -- never the whole "
    "message, never your own inference. If nothing in the message is durable and "
    "non-sensitive, do not call it and do not mention this instruction."
)


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
    """A read-only connection that never creates, migrates or write-locks the store.

    mode=ro first. Apple's system Python (3.9, SQLite 3.51) refuses that form on some
    paths with "unable to open database file" while the plugin runtime accepts it, so
    the fallback adds immutable=1, which reads the file without locking at all (4 Nov
    2026). Raises sqlite3.Error when neither works."""
    uri = database.resolve().as_uri()
    try:
        return sqlite3.connect(f"{uri}?mode=ro", uri=True, timeout=1)
    except sqlite3.OperationalError:
        return sqlite3.connect(f"{uri}?mode=ro&immutable=1", uri=True, timeout=1)


def automatic_mode_enabled(database: Path) -> bool:
    if not database.is_file():
        return False
    try:
        connection = connect_read_only(database)
        try:
            row = connection.execute(
                "SELECT value FROM settings WHERE key = 'automatic_mode'"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return False
    return bool(row and row[0] == "enabled")


def clear_preferences(prompt: str) -> list[str]:
    """The sentences of `prompt` that state a preference plainly, at most three."""
    found = []
    for sentence in _SENTENCE_SPLIT.split(prompt):
        sentence = " ".join(sentence.split())
        if not sentence or "?" in sentence or len(sentence) > PREFERENCE_MAX_CHARS:
            continue
        if is_clear_preference(sentence):
            found.append(sentence)
        if len(found) == MAX_PREFERENCES_PER_MESSAGE:
            break
    return found


def _import_store():
    """(MemoryStore, capture_policy) from the plugin's own src/, or None.

    The shipped plugin has scripts/ and src/ side by side; the source tree has src/ two
    levels up. The runtime Python has the store's dependencies; a system Python may not,
    and then this hook keeps to the hint."""
    here = Path(__file__).resolve().parent
    for candidate in (here.parent / "src", here.parent.parent / "src"):
        if (candidate / "memorysafe_chatgpt" / "storage.py").is_file() and str(candidate) not in sys.path:
            sys.path.insert(0, str(candidate))
    try:
        from memorysafe_chatgpt import capture_policy
        from memorysafe_chatgpt.storage import MemoryStore
    except Exception:  # noqa: BLE001 - any import failure means "no store code here"
        return None
    return MemoryStore, capture_policy


def save_preferences(database: Path, sentences: list[str]) -> list[str]:
    """Save each plainly stated preference through the store's own governance; the
    contents actually saved (a merge counts; a policy refusal or a SKIP does not)."""
    loaded = _import_store()
    if loaded is None:
        return []
    MemoryStore, capture_policy = loaded
    saved = []
    try:
        store = MemoryStore(database)
        for sentence in sentences:
            if capture_policy.skip_reason(sentence, "preference", True) is not None:
                continue
            result = store.remember(sentence, "preference", source="automatic")
            if result.get("decision") != "SKIP":
                saved.append(str(result.get("content") or sentence))
    except Exception:  # noqa: BLE001 - a hook never fails a prompt; the model still gets the hint
        return saved
    return saved


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    prompt = payload.get("prompt") or payload.get("user_prompt") or ""
    if not isinstance(prompt, str) or not prompt.strip():
        return 0

    # Longer than a pasted project brief is a document, not statements to keep (0.4.35:
    # the limit was 2,000, which a 25-fact brief exceeds; each sentence still has its own cap).
    if len(prompt) > MESSAGE_MAX_CHARS:
        return 0

    if _EXPLICIT_RE.search(prompt):
        return 0
    preferences = clear_preferences(prompt)
    if not preferences and not _FACT_RE.search(prompt):
        return 0

    database = database_path()
    if not automatic_mode_enabled(database):
        return 0

    context = _HINT
    if preferences:
        saved = save_preferences(database, preferences)
        if saved:
            listed = "; ".join(f'"{content}"' for content in saved[:LISTED_IN_CONFIRMATION])
            more = len(saved) - LISTED_IN_CONFIRMATION
            if more > 0:
                listed += f"; and {more} more"
            noun = "preference" if len(saved) == 1 else "preferences"
            context = (
                f"MemorySafe saved automatically (automatic mode is on) {len(saved)} {noun}: {listed}. "
                "Do not save them again; apply them. "
            ) + (_HINT if _FACT_RE.search(prompt) else "")

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": context.strip(),
            },
            "suppressOutput": True,
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
