# MemorySafe Beta 0.4.16 — Installation

MemorySafe keeps durable memories in one local SQLite database on your own computer, with a
private dashboard at `http://127.0.0.1:8765/dashboard`. Claude Code, Claude Desktop and Codex
all share that one database. Nothing is uploaded.

## macOS, Linux and Windows

### One install, every assistant

Install MemorySafe in whichever assistant you use most, using its section below. Then open
<http://127.0.0.1:8765/dashboard>. It finds the other assistants on this computer and asks once
whether to connect them to the same memory. Say yes and it connects them, each through its own
installer, and connects any you install later too. Remove MemorySafe from one of them later and
it stays removed. Claude Desktop asks you to confirm extensions yourself, so for it the
dashboard's **One memory, every assistant** panel shows the one step to take.

From a terminal, `memorysafe connect` shows the same list and changes nothing; add `--apply`
to connect them:

```bash
~/.local/share/MemorySafe/bin/memorysafe connect --apply                   # Linux
~/Library/Application\ Support/MemorySafe/bin/memorysafe connect --apply   # macOS
%LOCALAPPDATA%\MemorySafe\bin\memorysafe.cmd connect --apply                # Windows
```

Or ask your assistant to connect MemorySafe to your other assistants.

### Claude Code

Needs the `claude` command-line tool, which the Claude desktop app does not include. If
`claude --version` doesn't answer in a terminal, install it first:

On macOS and Linux:

```bash
curl -fsSL https://claude.ai/install.sh | bash
```

On Windows, in PowerShell:

```powershell
irm https://claude.ai/install.ps1 | iex
```

On Windows, open a new terminal afterwards. If `claude` still isn't found, the installer's last
lines say how to add it to your PATH. Then:

```bash
claude plugin marketplace add MemorySafe-Labs/memorysafe-plugin
claude plugin install memorysafe@memorysafe
```

Restart Claude Code, run `/mcp`, and confirm `memorysafe` is connected with twelve tools.

### Codex

```bash
codex plugin marketplace add MemorySafe-Labs/memorysafe-plugin
codex plugin add memorysafe@memorysafe
```

Codex asks you to trust the plugin's hook before running it. MemorySafe works without it:
the hook only makes automatic capture fire more reliably once you turn that on.

### Other MCP clients

Cursor, VS Code, Windsurf and anything else that speaks MCP can use the same memory. They
take a command to launch, and MemorySafe keeps one at a fixed path in your data folder,
rewritten on every start so it follows updates:

```
~/.local/share/MemorySafe/bin/memorysafe-mcp                   # Linux
~/Library/Application\ Support/MemorySafe/bin/memorysafe-mcp   # macOS
%LOCALAPPDATA%\MemorySafe\bin\memorysafe-mcp.cmd               # Windows
```

Most clients take a block like this, with the path spelled out in full (on Windows, with
doubled backslashes, as JSON requires):

```json
{
  "mcpServers": {
    "memorysafe": {
      "command": "/Users/you/Library/Application Support/MemorySafe/bin/memorysafe-mcp"
    }
  }
}
```

It needs MemorySafe installed in one of the three assistants above first: the command runs
that plugin's launcher, so it is created the first time one of them starts. These clients
are not in the dashboard's **One memory, every assistant** panel, which only connects
assistants it can install into.

### Claude Desktop

Download
[memorysafe-claude-desktop.mcpb](https://github.com/MemorySafe-Labs/memorysafe-plugin/releases/latest/download/memorysafe-claude-desktop.mcpb),
then in Claude Desktop choose it from **Settings → Extensions → Advanced settings → Install
Extension…**. Leave the data folder empty so Claude Desktop shares the same store. Don't
double-click the file: on some computers Windows opens it in Notepad, and saving it from there
breaks it.

Then restart Claude Desktop and open <http://127.0.0.1:8765/dashboard>. If it loads,
MemorySafe is installed.

### The first start

You do not need Python, on any platform. The first time an assistant starts MemorySafe, it
downloads uv from GitHub, a Python build through uv, hash-pinned packages from PyPI, and the
tokenizer data it counts tokens with, all into the MemorySafe folder. That takes a minute or
two. While it runs, <http://127.0.0.1:8765/dashboard> shows each step, and it turns into your
dashboard when setup finishes. The assistant connects straight away, and if you ask for
something before setup has finished, it tells you which step it is on. After that, nothing
leaves this computer, unless you connect another assistant to MemorySafe: that assistant's
own installer then downloads the plugin from GitHub.

On a network that blocks GitHub, set `MEMORYSAFE_UV` to the full path of a uv you already
have, or `HTTPS_PROXY` to your proxy.

### Updates

Both marketplaces update the plugin on their own. An update rebuilds the runtime only when
MemorySafe's dependencies change.

### Installed MemorySafe by hand before?

The earlier installers registered MemorySafe directly. With the plugin as well, every tool is
listed twice. Ask your assistant to check MemorySafe: it will offer to remove the old
registration and show you the change first. Or, from a terminal:

```bash
~/.local/share/MemorySafe/bin/memorysafe migrate --apply                   # Linux
~/Library/Application\ Support/MemorySafe/bin/memorysafe migrate --apply   # macOS
%LOCALAPPDATA%\MemorySafe\bin\memorysafe.cmd migrate --apply                # Windows
```

Leave off `--apply` to see what it would change. Each file it edits is backed up first. Your
memories are never touched.

### Removing MemorySafe

```bash
~/.local/share/MemorySafe/bin/memorysafe uninstall                  # Linux
~/Library/Application\ Support/MemorySafe/bin/memorysafe uninstall  # macOS
%LOCALAPPDATA%\MemorySafe\bin\memorysafe.cmd uninstall              # Windows
```

It lists everything MemorySafe put on this computer, with sizes, and changes nothing without
`--apply`. Plugins leave through each assistant's own installer; the private runtime, the local
state, the assistants' caches and any hand-written registration go too.

**Your memories stay** unless you add `--purge`. A purge copies the database to your home folder
first and prints where it went. Claude Desktop asks you to confirm what its extensions do, so
removing that one stays with you: **Settings → Extensions → MemorySafe**. Restart each assistant
afterwards.

## If something looks wrong

Ask your assistant: **"Check MemorySafe."** It runs the read-only doctor and walks you through
what it finds. From a terminal, `memorysafe doctor` (at the paths above) gives the same report.

To send a report, use **Report a problem** in the dashboard. It asks what went wrong, saves a
report file to your Downloads folder, and opens an email to contact@memorysafe.ca: attach the
file and press Send. The file holds status, counts, sanitized error signatures and your
description. It contains no memory contents, no conversation history and no keys, and nothing is
sent until you send the email.

## Automatic saving

Automatic mode is opt-in. Ask: **"Turn on MemorySafe automatic mode."** The assistant
recognises and submits each durable candidate itself; MemorySafe never passively copies
conversations. On Windows there's no prompt-time hint nudging the assistant to catch a
candidate on its own yet, so it works best there when you ask for something to be remembered.

## Where your data lives

- macOS — `~/Library/Application Support/MemorySafe/data/memorysafe.sqlite3`
- Linux — `~/.local/share/MemorySafe/data/memorysafe.sqlite3`, or under `$XDG_DATA_HOME/MemorySafe`
- Windows — `%LOCALAPPDATA%\MemorySafe\data\memorysafe.sqlite3`

Do not copy or move the database while MemorySafe is running.

## What changed in 0.4.16

**A calmer dashboard.** The first view is about half as long. The savings comparison, the
most-used list and the governance trail fold under their headings, and the governance heading
still tells you how many items need a decision. Recent memories shows the latest three.

**Fewer false "one step left" rows on the setup page.** Claude Code now reads as connected when it
works through Claude Desktop, and says so. Codex gets its Connect button when the ChatGPT app is
installed, even before Codex has set anything up. The optional ChatGPT steps stay hidden until you
ask for them.

Your memories and settings carry over unchanged.

## What changed in 0.4.15

**Intel Macs install without compiling.** 0.4.14 updated a bundled security library to a version
that has no ready-made package for Intel Macs, so an Intel Mac had to build it from source on
first start, which needs Apple's developer tools and a network connection. Intel Macs now get the
newest version that does have a ready-made package; every other computer keeps the newer one.
Nothing else changes, and your memories and settings carry over unchanged.

## What changed in 0.4.14

**The setup page asks for less.** Claude Code is shown as covered when the Claude desktop app's
extension already reaches it, instead of asking you to install something you do not need. Each
assistant you do need to connect has one Connect button. Codex now gets that button when you
only have the Codex desktop app. The optional ChatGPT steps start hidden behind a link, and
finished steps fold away on your next visit.

**A security update to a bundled library.** The encryption library MemorySafe ships with was
updated to its current version to close five published security alerts. Nothing about your
memories or settings changes. The first start after updating rebuilds MemorySafe's private
runtime, so the setup page may show a short progress screen once.

Your memories and settings carry over unchanged.

## What changed in 0.4.13

**An unclear update no longer overwrites a clear fact.** When a new memory looks like it
might replace an older one but does not say so plainly, MemorySafe keeps both and marks
them for review instead of merging them or retiring the older one. It also keeps two
different systems or events apart when they only share words, and keeps amounts in
different currencies apart.

**Both sides of an open conflict stay in recall until you decide**, even when the optional
capacity limit is on. That limit is a soft target: protected memories and open conflicts
can keep the count above it.

Your memories and settings carry over unchanged.

## What changed in 0.4.12

**Nothing in how MemorySafe behaves.** Your memories, settings and commands are exactly as in
0.4.11, and the update does not rebuild the runtime.

**Every change is now checked on Windows, macOS and Linux before it merges**, including a real
first start on each, where before only a release was. This is the first version published
that way.

## What changed in 0.4.11

**Newer facts win, and you can see when they have not.** A later memory that changes a date,
time, amount or version on the same subject -- or says "moved to", "switched from X to Y" or
"now" -- replaces the older one instead of leaving both in recall. When a conflict is still
open, search marks both memories and shows how confident each one is, and the health score
drops until someone decides. An agent's lower-confidence guess no longer replaces a fact you
stated.

**Undo a wrong update in one step.** `resolve-conflict <id> revert` rejects the newer memory and
brings back the one it replaced. Restoring a forgotten memory reopens any conflict it was in.

**Protect what matters yourself.** A new tool and `memorysafe protect` / `unprotect` commands.
Forgetting a protected memory now asks for confirmation. `forget`, `restore`,
`review-conflicts` and `resolve-conflict` are commands as well as tools.

**Fewer false alarms and no lost text.** Routine notes that differ only in their numbers are no
longer merged into each other, auto-protected, or reported as conflicts. An optional capacity
limit, `MEMORYSAFE_MAX_ACTIVE`, is off unless you set it.

## What changed in 0.4.10

**Fixed: after an update, the dashboard could keep showing the old version.** On Windows the
page at <http://127.0.0.1:8765/dashboard> could go on being served by a previous install
indefinitely -- three versions behind, on the machine where this was found -- so what you saw
there was not what you had. MemorySafe now recognises its own dashboard properly and replaces
an old one when it starts.

**And the doctor says what to do about it.** If an old dashboard is still holding the address,
it now names the process and tells you to restart the assistant that started it, instead of
only reporting that the version is old.

## What changed in 0.4.9

**Fixed: MemorySafe's old registration in Codex could stop Codex loading any tools at all.**
Installs going back to 0.3.x wrote a `[mcp_servers.memorysafe]` block into
`~/.codex/config.toml` with Windows paths that TOML cannot read, so the whole file failed to
parse and Codex started with none of its servers -- not just without MemorySafe. Nothing new
writes that block, and `memorysafe migrate` removes an old one.

**If that entry keeps coming back, close Codex before removing it.** Codex writes
`config.toml` from what it has in memory, so an entry deleted while it is running reappears
the next time it saves. `memorysafe migrate` now says so before you run it.

## What changed in 0.4.8

**Fixed: `memorysafe doctor` could stop with an error instead of printing its report.**
On Windows, saving the output to a file or letting your assistant read it made the command
fail on the first tick it tried to print. `uninstall` had the same fault, and a memory with
an accent in it could stop `find` the same way. All of it prints properly now.

**Fixed: uninstalling could stop partway with "Access is denied".** Windows holds a file for
a moment after it is touched, so the uninstaller now waits and tries again. If something is
genuinely still in use it says so, and says what to do: close your assistants and run it
again, which picks up where it left off.

## What changed in 0.4.7

**You can take MemorySafe off this computer with one command.** `memorysafe uninstall` lists
everything it put here, with sizes, and changes nothing until you add `--apply`. Plugins leave
through each assistant's own installer, and the private runtime, the local state and any
hand-written registration go with them. **Your memories stay** unless you add `--purge`, which
copies the database to your home folder first and tells you where.

**Other MCP clients can share the same memory.** Cursor, VS Code, Windsurf and anything else
that speaks MCP can use `bin/memorysafe-mcp` in your MemorySafe folder as the command to run.
It is rewritten on every start, so it keeps working across updates. See **Other MCP clients**
above.

**The dashboard opens itself, once.** On the first start after an install it shows itself rather
than leaving you to type an address. Once only, never again, and `MEMORYSAFE_OPEN_DASHBOARD=0`
turns it off.

**The Claude Code row says what connecting it is for.** If you have the Claude Desktop extension,
sessions inside the desktop app already reach MemorySafe through it. The dashboard now says so,
and that connecting Claude Code is for using it in a terminal, where the extension cannot reach.

**The Beta Terms of Use and Privacy Policy now describe this product.** They were written for a
ChatGPT-only beta and said every request passed through OpenAI's systems, which was never true of
Claude Code, Claude Desktop or Codex: those reach MemorySafe on your own computer. The documents
now say where your memories actually go, and name Anthropic and OpenAI where content reaches
them. They are version 0.2, so the dashboard asks you to read and accept them again. Nothing
stops working while you do, and your memories are untouched.

**Fixed:** on Windows, an edit to the launcher could silently stop the `memorysafe` command from
being created, because `cmd.exe` cannot reliably find a label in a file with Unix line endings.
`migrate` also missed MemorySafe registrations inside individual projects, so a project could
still load it twice.
