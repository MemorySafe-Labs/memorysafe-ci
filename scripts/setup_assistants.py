#!/usr/bin/env python3
"""Configure MemorySafe for whichever assistants are actually on this computer.

The beta shipped three files and a section headed "Which file do I need?", which made
the tester answer a question the machine can answer for itself. Someone who only uses
Codex was still handed Claude instructions, and someone who only uses Claude had to
work out that the ChatGPT installer was not for them.

Nothing here fails because an assistant is absent. Absent is the normal case.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

try:
    HOME = Path.home()
except RuntimeError:
    # Path.home() raises when it cannot resolve a home directory (no HOME and no passwd
    # entry on POSIX; no USERPROFILE and no HOMEDRIVE+HOMEPATH on Windows). Computing it
    # at import time meant importing this module at all -- for any reason, on any
    # machine -- could raise before a single line of its own logic ran. This module's
    # only Windows caller was retired along with scripts/install_windows.py, and its
    # remaining caller (install_macos.py) is macOS-only, so this branch is unreached
    # today; caught anyway so a future import is never a trap. Functions that need a
    # real HOME still fail if this ever fires, same as before -- just at first use
    # instead of at import.
    HOME = None

PLUGIN_HINTS = {
    "codex": (
        "install the plugin: codex plugin marketplace add MemorySafe-Labs/memorysafe-plugin, "
        "then codex plugin add memorysafe@memorysafe"
    ),
    "claude_code": (
        "install the plugin: claude plugin marketplace add MemorySafe-Labs/memorysafe-plugin, "
        "then claude plugin install memorysafe@memorysafe"
    ),
}


def configure_windows_stdio() -> None:
    """Windows consoles default to cp1252; setup messages use Unicode punctuation."""

    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

def canonical_install_root() -> Path:
    if sys.platform == "win32":
        # `or`, not a .get() default: the latter evaluates HOME / "AppData" / "Local"
        # unconditionally, which raises TypeError outright now that HOME can be None
        # (see the module-level try/except above) -- even on the overwhelming majority
        # of real Windows machines where LOCALAPPDATA is set and that fallback is
        # never used.
        base = Path(os.environ.get("LOCALAPPDATA") or (HOME / "AppData" / "Local"))
        return base / "MemorySafe"
    if sys.platform == "darwin":
        return HOME / "Library" / "Application Support" / "MemorySafe"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else HOME / ".local" / "share"
    return base / "MemorySafe"


def canonical_database() -> Path:
    return canonical_install_root() / "data" / "memorysafe.sqlite3"


def detect() -> dict[str, bool]:
    if sys.platform == "win32":
        # `or`, not .get() defaults -- see canonical_install_root() above.
        local = Path(os.environ.get("LOCALAPPDATA") or (HOME / "AppData" / "Local"))
        roaming = Path(os.environ.get("APPDATA") or (HOME / "AppData" / "Roaming"))
        # Claude Desktop also ships as a packaged (MSIX) app, and that build keeps its
        # data under AppData\Local\Packages\Claude_<publisher-hash>\LocalCache\Roaming\Claude
        # while still calling it %APPDATA%\Claude internally. None of the four paths
        # below match it, so a beta user with a working Claude Desktop was told
        # "not installed - skipped" and had to sideload the .mcpb by hand.
        packaged = sorted((local / "Packages").glob("Claude_*/LocalCache/Roaming/Claude"))
        claude_desktop = any(
            path.exists()
            for path in (
                local / "Claude",
                roaming / "Claude",
                local / "Programs" / "Claude",
                local / "AnthropicClaude",
                *packaged,
            )
        )
        return {
            "claude_desktop": claude_desktop,
            "claude_code": (HOME / ".claude.json").is_file() or bool(shutil.which("claude")),
            "codex": (HOME / ".codex").is_dir() or bool(shutil.which("codex")),
            # The ChatGPT.com tunnel is macOS-only.
            "chatgpt_web": False,
        }
    if sys.platform.startswith("linux"):
        return {
            "claude_desktop": (HOME / ".config" / "Claude").is_dir()
            or (HOME / ".config" / "claude-desktop").is_dir()
            or bool(shutil.which("claude-desktop")),
            "claude_code": (HOME / ".claude.json").is_file() or bool(shutil.which("claude")),
            "codex": (HOME / ".codex").is_dir() or bool(shutil.which("codex")),
            "chatgpt_web": False,
        }
    return {
        "claude_desktop": Path("/Applications/Claude.app").exists()
        or (HOME / "Library" / "Application Support" / "Claude").is_dir(),
        "claude_code": (HOME / ".claude.json").is_file() or bool(shutil.which("claude")),
        # Codex is local and speaks MCP over stdio, exactly like Claude Code.
        "codex": (HOME / ".codex").is_dir() or bool(shutil.which("codex")),
        # ChatGPT the website cannot reach this Mac, so it is the only surface that
        # needs the tunnel and a runtime key.
        "chatgpt_web": Path("/Applications/ChatGPT.app").exists(),
    }


def _mcp_launch(install_root: Path) -> dict[str, object]:
    """Stdio launch for Claude Code and Codex. Windows cannot run the .sh launcher."""

    if sys.platform == "win32":
        script = install_root / "scripts" / "start.cmd"
        return {
            "command": "cmd.exe",
            "args": ["/d", "/s", "/c", f'"{script}"'],
        }
    return {
        "command": str(install_root / "scripts" / "run_mcp_service.sh"),
        "args": [],
    }


def configure_codex(install_root: Path, database: Path) -> str:
    """Register MemorySafe with Codex over stdio — no tunnel, no key, no account."""

    config_path = HOME / ".codex" / "config.toml"
    if not config_path.parent.is_dir():
        return "not installed — skipped"

    launch = _mcp_launch(install_root)
    command = str(launch["command"])
    args = list(launch["args"])
    command_line = f"command = {json.dumps(command)}"
    args_line = "args = [" + ", ".join(json.dumps(item) for item in args) + "]"
    section = "\n".join((
        "",
        "[mcp_servers.memorysafe]",
        command_line,
        args_line,
        "startup_timeout_sec = 120.0",
        "",
        "[mcp_servers.memorysafe.env]",
        # json.dumps, exactly like command_line and args_line above, because a TOML
        # basic string escapes the same way JSON does. Interpolating a path raw was
        # fine on macOS and produced invalid TOML on Windows: a drive path starts with
        # \U, which is an escape, so Codex could not parse its own config at all and
        # started with no servers. A machine here carried that broken block from a
        # 0.3.x install until 21 September.
        f'MEMORYSAFE_INSTALL_ROOT = {json.dumps(str(install_root))}',
        f'MEMORYSAFE_DB_PATH = {json.dumps(str(database))}',
        f'MEMORYSAFE_STATE_DIR = {json.dumps(str(install_root / "runtime-state"))}',
        "",
    ))

    try:
        existing = config_path.read_text() if config_path.is_file() else ""
    except OSError as error:
        return f"left alone — could not read config.toml ({error})"

    if "[mcp_servers.memorysafe]" in existing:
        return "already configured — nothing changed"

    if config_path.is_file():
        try:
            shutil.copy2(config_path, config_path.with_suffix(".toml.memorysafe-backup"))
        except OSError:
            return "left alone — could not back up config.toml first"

    # Append rather than regenerate: the file is the user's, and parsing then rewriting
    # would lose their comments and ordering.
    temporary = config_path.with_suffix(".toml.memorysafe-tmp")
    try:
        temporary.write_text(existing + section)
        os.replace(temporary, config_path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        return f"left alone — could not write config.toml ({error})"
    return "configured automatically — restart Codex"


def configure_claude_code(install_root: Path, database: Path) -> str:
    """Register the MCP server in ~/.claude.json.

    This is the one that can be finished without the user doing anything: it is a plain
    JSON file, not another application's private format.
    """

    # Refuse to record a path that will not survive. A build or test run from a
    # temporary directory would otherwise write /tmp/... into the user's Claude
    # configuration, and the server would fail to start once that directory was gone —
    # with nothing to indicate why.
    # Only ever point the user's Claude at the real install. Guarding just /tmp was not
    # enough: a test install into a differently named folder under Application Support
    # rewrote the live config to a sandbox that was about to be deleted. Anything that
    # is not the canonical location is a test, and a test must not edit user config.
    canonical_root = canonical_install_root()
    if install_root.resolve() != canonical_root.resolve():
        return (
            f"skipped — {install_root} is not the standard install location, "
            "so the Claude configuration was left untouched"
        )

    config_path = HOME / ".claude.json"
    try:
        config = json.loads(config_path.read_text()) if config_path.is_file() else {}
    except json.JSONDecodeError:
        return "left alone — ~/.claude.json is not valid JSON, so it was not touched"

    # This file holds the user's entire Claude Code configuration, not just ours.
    # Back it up on every run, not only the first: the useful backup is the one taken
    # immediately before the change that broke something.
    if config_path.is_file():
        backup = config_path.with_suffix(".json.memorysafe-backup")
        try:
            shutil.copy2(config_path, backup)
        except OSError:
            return "left alone — could not back up ~/.claude.json first"

    servers = config.setdefault("mcpServers", {})
    existing = servers.get("memorysafe") or {}
    launch = _mcp_launch(install_root)
    entry = {
        "command": str(launch["command"]),
        "args": list(launch["args"]),
        "env": {
            **(existing.get("env") or {}),  # keep anything the user set deliberately
            "MEMORYSAFE_INSTALL_ROOT": str(install_root),
            "MEMORYSAFE_DB_PATH": str(database),
            "MEMORYSAFE_STATE_DIR": str(install_root / "runtime-state"),
        },
    }
    if existing == entry:
        return "already configured — nothing changed"
    servers["memorysafe"] = entry

    # Write via a temporary file in the same directory and rename over the original.
    # A partial write here would not lose MemorySafe's settings, it would lose all of
    # the user's, and a rename on the same filesystem is atomic.
    temporary = config_path.with_suffix(".json.memorysafe-tmp")
    try:
        temporary.write_text(json.dumps(config, indent=2))
        os.replace(temporary, config_path)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        return f"left alone — could not write ~/.claude.json ({error})"
    return "configured automatically — restart Claude Code"


def _find_desktop_extension(source_root: Path) -> Path | None:
    # The installer runs from a payload inside the .app, while the .mcpb sits in the
    # download folder beside it. Looking in one fixed place found nothing whenever the
    # app had been copied or run from a temporary unpack, so search the plausible
    # layouts instead of assuming one.
    roots = [source_root, source_root.parent, source_root.parent.parent]
    app = next((parent for parent in source_root.parents if parent.suffix == ".app"), None)
    if app is not None:
        roots += [app.parent, app.parent.parent]
    for root in roots:
        try:
            bundle = next((root / "claude").glob("*.mcpb"), None) or next(root.glob("*.mcpb"), None)
        except OSError:
            continue
        if bundle:
            return bundle
    return None


def reveal_claude_desktop_extension(source_root: Path) -> str:
    """Point Claude Desktop at the .mcpb that already ships in the download.

    Copying it onto the Desktop made testers lose it under screenshots, and writing
    into Claude's own extensions folder is another application's internal layout.
    Opening the file with Claude is the supported install path (the .mcpb document
    type). Finder reveal is the fallback when Claude is not installed, which this
    function is not called for.
    """

    bundle = _find_desktop_extension(source_root)
    if bundle is None:
        return (
            "extension not found beside the installer — install it from the download's "
            "claude folder: Settings > Extensions > Install Extension"
        )

    # Never copy onto the Desktop. The file already lives next to the installer.
    if sys.platform == "darwin":
        try:
            subprocess.run(
                ["/usr/bin/open", "-R", str(bundle)],
                check=False,
                capture_output=True,
            )
        except OSError:
            pass
        if Path("/Applications/Claude.app").exists():
            try:
                subprocess.run(
                    ["/usr/bin/open", "-a", "Claude", str(bundle)],
                    check=False,
                    capture_output=True,
                )
            except OSError:
                pass
            return (
                f"{bundle.name} — Claude should prompt to install it. If not: Settings > "
                "Extensions > Install Extension and pick that file. First chat can take "
                "a minute while a private runtime is built."
            )
    if sys.platform == "win32":
        try:
            os.startfile(bundle)  # type: ignore[attr-defined]
            return (
                f"{bundle.name} — Windows should open it with Claude Desktop. If not: "
                "Settings > Extensions > Install Extension and pick that file. First "
                "chat can take a minute while a private runtime is built."
            )
        except OSError:
            pass
    return (
        f"{bundle.name} is in {bundle.parent} — Claude Desktop > Settings > Extensions "
        "→ Install Extension"
    )


def main() -> int:
    configure_windows_stdio()
    # The caller's value wins over the environment: the installer knows where it just
    # installed, and an inherited variable may describe something else entirely.
    argv = list(sys.argv[1:])
    override = None
    if "--install-root" in argv:
        index = argv.index("--install-root")
        override = argv[index + 1] if index + 1 < len(argv) else None
        del argv[index : index + 2]
    desktop_only = "--desktop-only" in argv
    if desktop_only:
        # The macOS installer passes this. Claude Code and Codex install MemorySafe as a
        # plugin now, and registering it here as well would list every tool twice.
        argv.remove("--desktop-only")
    # canonical_install_root() must not run as a .get() default: that evaluates it
    # unconditionally, even for a caller who passed --install-root or set
    # MEMORYSAFE_INSTALL_ROOT and whose value was always going to win over it.
    if override:
        install_root = Path(override)
    elif os.environ.get("MEMORYSAFE_INSTALL_ROOT"):
        install_root = Path(os.environ["MEMORYSAFE_INSTALL_ROOT"])
    else:
        install_root = canonical_install_root()
    source_root = Path(argv[0]) if argv else install_root
    database = canonical_database()
    found = detect()

    print("MemorySafe setup\n")
    print(f"  All assistants share one memory file:\n    {database}\n")

    if not any(found.values()):
        print("  No supported assistant found on this computer.")
        print("  MemorySafe works with Claude Desktop, Claude Code, Codex, and ChatGPT.")
        print("  Install one of them, then run this again.")
        return 1

    if found["codex"]:
        status = PLUGIN_HINTS["codex"] if desktop_only else configure_codex(install_root, database)
        print(f"  Codex             {status}")
    if found.get("chatgpt_web"):
        print("  ChatGPT (website) optional — needs a runtime key; open the setup window")
    if found["claude_code"]:
        status = PLUGIN_HINTS["claude_code"] if desktop_only else configure_claude_code(install_root, database)
        print(f"  Claude Code       {status}")
    if found["claude_desktop"]:
        print(f"  Claude Desktop    {reveal_claude_desktop_extension(source_root)}")

    for name, label in (
        ("claude_desktop", "Claude Desktop"),
        ("claude_code", "Claude Code"),
        ("codex", "Codex"),
    ):
        if not found[name]:
            print(f"  {label:17s} not installed — skipped")

    # A notification rather than a dialog: nothing here needs a click, and the one
    # actionable item (restarting Claude Code) is easy to miss in terminal output.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from notify import notify

        ready = [label for key, label in
                 (("codex", "Codex"), ("claude_code", "Claude Code"),
                  ("claude_desktop", "Claude Desktop")) if found[key]]
        todo = []
        if found["claude_code"] and not desktop_only:
            todo.append("restart Claude Code")
        if found["claude_desktop"]:
            todo.append("confirm the Claude Desktop extension install")
        message = f"Set up for {', '.join(ready)}. Automatic capture stays off until you turn it on."
        if todo:
            message += " Next: " + " and ".join(todo) + "."
        notify("MemorySafe is ready", message)
    except Exception:
        pass

    print("\n  Automatic capture is off until you turn it on from the dashboard or by asking.")
    print("  Check anything at any time:  python3 memorysafe_selfcheck.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
