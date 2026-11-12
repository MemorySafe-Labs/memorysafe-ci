"""`memorysafe-import`: show what would move, pair with a MemorySafe account, send it on a `y`."""

from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Callable, Optional

from . import __version__
from .memories import claude_root, find_memories, folder_name_for, to_export
from .pairing import DEFAULT_SERVER, Client, NotSupported, PairingError

POLL_SECONDS = 3
PROGRESS_SECONDS = 2
MAX_BAD_TRIES = 5
# The server's result keys (its COUNTS), in the order it lists them, with the words shown.
RESULT_WORDS = (
    ("stored", "stored"),
    ("merged", "merged"),
    ("replaced_older", "replaced older"),
    ("needs_review", "need review"),
    ("protected", "protected"),
    ("failed", "failed"),
)
_NOT_ALLOWED = re.compile(r"[^A-Za-z0-9 ._-]")


def _host_label() -> str:
    """This computer's name for the server, limited to the characters it accepts: the label is
    shown to the person who links the code, so it is plain text."""
    try:
        name = socket.gethostname().split(".")[0]
    except OSError:
        name = ""
    name = _NOT_ALLOWED.sub("-", name).strip()[:64].strip()
    return name or "this computer"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _transient(error: PairingError) -> bool:
    """A network failure or a server-side (5xx) one: worth trying again. A 4xx is an answer."""
    return error.code == "network" or error.status >= 500


def main(
    argv: Optional[list] = None,
    *,
    stdin=sys.stdin,
    stdout=sys.stdout,
    client_factory: Callable[..., Client] = Client,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
    env=os.environ,
) -> int:
    parser = argparse.ArgumentParser(prog="memorysafe-import", description="Move Claude Code's memory files into MemorySafe.")
    parser.add_argument("--server", default=DEFAULT_SERVER, help="the MemorySafe server (default: %(default)s)")
    parser.add_argument("--only-here", action="store_true", help="only the project of the current folder")
    parser.add_argument("--out", metavar="FILE", help="write the export to FILE instead of sending it")
    parser.add_argument("--version", action="version", version=f"memorysafe-import {__version__}")
    args = parser.parse_args(argv)

    def say(text: str = "", end: str = "\n") -> None:
        stdout.write(text + end)
        stdout.flush()

    server = args.server.rstrip("/")
    page = f"{server}/account/import"
    root = claude_root(env)
    found = find_memories(root, folder_name_for(Path.cwd()) if args.only_here else None)
    kept, held = found.memories, found.held_back

    if not kept and not held:
        say(f"Found no Claude Code memories in {_tilde(root)}.")
        return 0

    say(f"Found {_plural(len(kept) + len(held), 'memory file', 'memory files')} in "
        f"{_plural(found.folders, 'project', 'projects')}.")
    counts = Counter(m.project for m in kept)
    rows = []
    if counts.get(None):
        rows.append(("You (personal, preferences)", counts[None]))
    rows += sorted((name, n) for name, n in counts.items() if name is not None)
    width = max((len(name) for name, _ in rows), default=0) + 3
    for name, n in rows:
        say(f"  {name.ljust(width)}{n:>3}")
    if held:
        say(f"Held back {len(held)} that look like contact details or credentials (they stay on this computer):")
        for label, reason in held:
            say(f"  {label}  {reason}")
    shortened = sum(1 for m in kept if m.shortened)
    if shortened:
        say(f"Shortened {shortened} longer than 10,000 characters.")
    if found.unreadable:
        say(f"Could not read {len(found.unreadable)} files; they were skipped.")

    if not kept:
        say("Everything was held back, so there is nothing to send.")
        return 0

    export = to_export(kept)

    if args.out:
        try:
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            say(f"{args.out} already exists; it was not replaced. Nothing was written.")
            return 1
        except OSError as error:
            say(f"Could not write {args.out}: {error.strerror or error}")
            return 1
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(export, handle, ensure_ascii=False)
        say(f"Wrote {len(kept)} memories to {args.out}. Upload it at {page}.")
        return 0

    client = client_factory(server)
    secret: Optional[str] = None

    def cancel() -> None:
        if secret is None:
            return
        try:
            client.cancel(secret)
        except PairingError:
            pass

    def refused(error: PairingError) -> int:
        say(str(error))
        say(f"Nothing may have been imported; see {page}.")
        return 1

    try:
        started = client.start(_host_label(), len(kept))
    except NotSupported:
        say("This MemorySafe server does not support code import yet; use --out and upload the file at /account/import.")
        return 1
    except PairingError as error:
        say(str(error))
        say("Nothing was sent.")
        return 1
    secret = started.device_secret
    deadline = clock() + started.expires_in

    say()
    say(f"Enter this code at {page}?code={started.code}")
    say(f"Waiting (expires in {max(1, started.expires_in // 60)} minutes)...")

    account = None
    try:
        failures = 0
        while True:
            if clock() >= deadline:
                say("The code expired. Nothing was sent. Run the command again.")
                cancel()
                return 1
            try:
                status, account = client.poll(secret)
            except PairingError as error:
                if not _transient(error):
                    say(str(error))
                    say("Nothing was sent.")
                    cancel()
                    return 1
                sleep(min(POLL_SECONDS * 2**failures, 30))
                failures += 1
                continue
            failures = 0
            if status == "linked":
                break
            if status == "expired":
                say("The code expired. Nothing was sent. Run the command again.")
                return 1
            if status == "refused":
                say("The code was cancelled. Nothing was sent.")
                return 1
            sleep(POLL_SECONDS)

        say()
        say(f"Linked to {account}. Import {len(kept)} memories? [y/N]")
        answer = stdin.readline().strip().lower()
        if answer not in ("y", "yes"):
            cancel()
            say("Nothing was sent.")
            return 1
    except KeyboardInterrupt:
        cancel()
        say()
        say("Nothing was sent.")
        return 130

    try:
        client.upload(secret, export)
    except PairingError as error:
        return refused(error)
    except KeyboardInterrupt:
        say()
        say(f"The upload may have gone through; check {page}.")
        return 130

    last = None
    bad_tries = 0  # consecutive network errors or unreadable states

    def give_up(reason: str) -> int:
        if last is not None:
            say()
        say(reason)
        say(f"The import may still be running on the server; see {page}.")
        return 1

    try:
        while True:
            try:
                info = client.progress(secret)
            except PairingError as error:
                if error.code == "no_import":
                    # The import page already showed the finished import and cleared it.
                    if last is not None:
                        say()
                    say(f"The import finished, or its result was already shown on {page}.")
                    return 0
                if not _transient(error):
                    return give_up(str(error))
                bad_tries += 1
                if bad_tries >= MAX_BAD_TRIES:
                    return give_up(str(error))
                sleep(min(PROGRESS_SECONDS * 2**bad_tries, 30))
                continue
            state = info.get("state")
            result = info.get("result") if isinstance(info.get("result"), dict) else {}
            message = info.get("message") if isinstance(info.get("message"), str) else ""
            if state == "running" and info.get("stalled"):
                # The worker died: the server says so, and the page has the way on.
                if last is not None:
                    say()
                say(message or "The import stopped making progress.")
                say(f"See {page}.")
                return 1
            if state in ("preview", "running"):
                bad_tries = 0
                if state == "running":
                    last = f"Importing... {info.get('done', 0)}/{info.get('total', len(kept))}"
                    say("\r" + last, end="")
            elif state == "done":
                if last is not None:
                    say()
                parts = [f"{label} {result[key]}" for key, label in RESULT_WORDS if result.get(key)]
                text = ", ".join(parts)
                say(text[0].upper() + text[1:] + "." if parts else "Import finished.")
                return 0
            elif state in ("failed", "expired"):
                if last is not None:
                    say()
                say(message or "The import did not finish.")
                say(f"See {page}.")
                return 1
            else:
                bad_tries += 1
                if bad_tries >= MAX_BAD_TRIES:
                    return give_up("The server's answer about the import could not be understood.")
            sleep(PROGRESS_SECONDS)
    except KeyboardInterrupt:
        say()
        say(f"The import continues on the server; follow it at {page}.")
        return 130


def _tilde(path: Path) -> str:
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home):] if text.startswith(home) else text
