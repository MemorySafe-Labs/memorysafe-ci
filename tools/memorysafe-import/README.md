# memorysafe-import

Moves the memories Claude Code keeps about you into your MemorySafe account.

```
uvx memorysafe-import
```

## What it reads

Claude Code writes small Markdown files to `~/.claude/projects/<project folder>/memory/`
(or under `$CLAUDE_CONFIG_DIR` when that is set). Each one becomes one memory: the text of
the file, with its category taken from the file's `type` (`user` is personal, `feedback` is a
preference, `project` is project, anything else is other) and its date from the file's
`modified` line, or else from the file's own modification time. `MEMORY.md`, the index, is
never read. A file over 64 KB is skipped and listed, and text over 10,000 characters is cut
at a paragraph end.

Nothing is changed or deleted on your computer.

## What it holds back

It refuses what MemorySafe's automatic capture refuses: email addresses, phone numbers,
street addresses, card and government ID numbers, credentials and similar. Each held-back
file is listed by name with the reason, never with the text that triggered it.

## Entering the code

The tool prints a short code. Open the address it prints: it signs you in to your MemorySafe
account and fills the code in. You see how many memories came from which computer before you
link it, so link only a code you made yourself, just now. The terminal
then shows which account is linked and asks `y/N` before anything is sent.

## `--out`

`--out FILE` writes what would be imported to `FILE` as a MemorySafe export instead of
sending it anywhere, so you can read it first. The file holds your memory text in plain JSON:
delete it when you are done.
