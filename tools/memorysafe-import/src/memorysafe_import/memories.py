"""Read Claude Code's auto-memory files and shape them as a MemorySafe export.

Claude Code keeps one Markdown file per memory under <claude dir>/projects/<folder>/memory/,
with a small frontmatter block. This reads that and nothing else: it never writes, and it
imports nothing from the MemorySafe package, because `uvx` runs it on a machine that has none.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Mapping, Optional

from . import capture_policy

# Claude Code's own types, mapped to the categories a hosted memory can have. `feedback` is how
# the person wants to be worked with, so it is a preference; `reference` is a pointer, not a fact.
CATEGORY_BY_TYPE = {"user": "personal", "feedback": "preference", "project": "project", "reference": "other"}
MAX_CONTENT = 10_000
# A memory file is a few lines. A bigger one is something else that landed in the folder.
MAX_FILE_BYTES = 64 * 1024
SOURCE = "claude-code-import"

_INDEX_NAME = "MEMORY.md"
_SESSION_FILES = 5
_SESSION_BYTES = 1024 * 1024
_CWD = re.compile(r'"cwd":"((?:[^"\\]|\\.)*)"')
_LINK = re.compile(r"\[\[([^\]\n]+)\]\]")
_WORKTREE = re.compile(r"[/\\]\.claude[/\\]worktrees[/\\].*$")


@dataclass
class Memory:
    content: str
    category: str
    stamp: str
    project: Optional[str]
    label: str
    shortened: bool


@dataclass
class Found:
    memories: "list[Memory]" = field(default_factory=list)
    held_back: "list[tuple[str, str]]" = field(default_factory=list)
    unreadable: "list[str]" = field(default_factory=list)
    # Claude Code project folders that yielded at least one memory file.
    folders: int = 0


def claude_root(env: Mapping[str, str] = os.environ) -> Path:
    """The `projects` folder under Claude Code's config directory."""

    configured = env.get("CLAUDE_CONFIG_DIR")
    base = Path(configured) if configured else Path(env.get("HOME") or Path.home()) / ".claude"
    return base / "projects"


def folder_name_for(cwd: Path) -> str:
    """The folder Claude Code uses for sessions started in `cwd`."""

    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def _unquote(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def _split_frontmatter(text: str) -> "tuple[dict[str, str], dict[str, str], str]":
    """(top-level keys, metadata keys, body). Not YAML: just the shape Claude Code writes."""

    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, {}, text
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        # No closing line: nothing says where the header stops, so all of it is the memory.
        return {}, {}, text
    top: "dict[str, str]" = {}
    meta: "dict[str, str]" = {}
    in_meta = False
    for line in lines[1:end]:
        key, sep, value = line.strip().partition(":")
        if not sep:
            continue
        if line.startswith((" ", "\t")) and in_meta:
            meta[key.strip()] = _unquote(value)
            continue
        in_meta = key.strip() == "metadata"
        if not in_meta:
            top[key.strip()] = _unquote(value)
    return top, meta, "\n".join(lines[end + 1:])


def _stamp(modified: Optional[str], mtime: float) -> str:
    if modified:
        try:
            parsed = datetime.fromisoformat(modified.strip().replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()
        except ValueError:
            pass
    return datetime.fromtimestamp(mtime, timezone.utc).replace(microsecond=0).isoformat()


def _cut(text: str) -> "tuple[str, bool]":
    if len(text) <= MAX_CONTENT:
        return text, False
    head = text[:MAX_CONTENT]
    # End on a paragraph so the memory does not stop mid-sentence; a single huge paragraph is cut hard.
    paragraph = head.rfind("\n\n")
    return (head[:paragraph] if paragraph > 0 else head).rstrip(), True


def parse_memory(text: str, mtime: float) -> "tuple[str, str, str, bool]":
    """(content, category, stamp, shortened) for one memory file's text."""

    top, meta, body = _split_frontmatter(text)
    kind = meta.get("type") or top.get("type") or ""
    body = body.strip() or top.get("description", "").strip()
    content, shortened = _cut(_LINK.sub(r"\1", body))
    return content, CATEGORY_BY_TYPE.get(kind, "other"), _stamp(meta.get("modified") or top.get("modified"), mtime), shortened


def project_name(folder: Path) -> str:
    """The project a Claude Code folder belongs to, from the working directory its sessions record."""

    sessions = sorted(folder.glob("*.jsonl"), key=lambda p: p.name)[:_SESSION_FILES]
    for session in sessions:
        try:
            with open(session, "rb") as handle:
                chunk = handle.read(_SESSION_BYTES).decode("utf-8", errors="replace")
        except OSError:
            continue
        match = _CWD.search(chunk)
        if not match:
            continue
        try:
            cwd = json.loads('"' + match.group(1) + '"')
        except ValueError:
            continue
        # A worktree is the same project as its repository, not a project of its own.
        cwd = _WORKTREE.sub("", cwd).rstrip("/\\")
        name = re.split(r"[/\\]", cwd)[-1]
        if name:
            return name
    return folder.name.strip("-") or folder.name


def find_memories(root: Path, only: Optional[str] = None) -> Found:
    found = Found()
    if only is not None:
        folders = [root / only]
    else:
        folders = sorted(p for p in root.iterdir() if p.is_dir()) if root.is_dir() else []
    for folder in folders:
        files = sorted(p for p in (folder / "memory").glob("*.md") if p.name != _INDEX_NAME and p.is_file())
        if not files:
            continue
        found.folders += 1
        project = project_name(folder)
        for path in files:
            try:
                stat = path.stat()
                if stat.st_size > MAX_FILE_BYTES:
                    raise ValueError("too large")
                content, category, stamp, shortened = parse_memory(path.read_bytes().decode("utf-8"), stat.st_mtime)
            except (OSError, ValueError):
                found.unreadable.append(f"{project}/{path.name}")
                continue
            owner = project if category not in ("personal", "preference") else None
            label = f"{owner or 'You'}/{path.name}"
            if not content:
                continue
            shape = capture_policy.classify(content)
            if shape is not None:
                found.held_back.append((label, capture_policy.reason_for(shape)))
                continue
            found.memories.append(Memory(content, category, stamp, owner, label, shortened))
    return found


def to_export(memories: "list[Memory]") -> dict:
    """The `memorysafe-export` v1 shape `memorysafe import` and the account's import page read."""

    items = []
    for memory in memories:
        item = {
            "content": memory.content,
            "category": memory.category,
            "protected": False,
            "source": SOURCE,
            "created_at": memory.stamp,
            "updated_at": memory.stamp,
        }
        if memory.project is not None:
            item["project"] = memory.project
        items.append(item)
    return {
        "format": "memorysafe-export",
        "format_version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "exported_by": "memorysafe-import",
        "memories": items,
    }
