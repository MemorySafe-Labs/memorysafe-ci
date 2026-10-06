# MemorySafe for ChatGPT — private beta

This is the first real MemorySafe connector. It exposes seven controlled actions:

- Remember a durable fact, preference, decision, project detail, or task.
- Find relevant stored memories.
- Forget one selected memory.
- Show a real memory-health and governed-storage comparison.
- Render the visual MemorySafe dashboard directly inside ChatGPT.
- Turn opt-in automatic capture on or off.
- Automatically capture one concise, durable, non-sensitive fact when the mode is on.

The connector stores data in `data/memorysafe.sqlite3` on this computer. It does not read ChatGPT's built-in memory or copy full conversation history. Automatic mode is off by default, works only in chats where MemorySafe is selected, and stores only concise candidates passed to the connector—not entire conversations.

## Private beta diagnostics

MemorySafe Doctor inspects the local installation without reading memory text or changing the system:

```bash
memorysafe doctor
memorysafe doctor --json
```

The JSON form is intended for a user's own troubleshooting agent. It reports service health, database integrity and counts, configuration validity, disk space, and bounded log sizes.

After an explicit user request, MemorySafe can create a sanitized local support bundle:

```bash
memorysafe support-bundle
```

The ZIP contains the doctor report and limited sanitized error signatures. It excludes memory contents, conversation history, runtime keys, and complete tunnel identifiers. It is never uploaded automatically. The local dashboard also exposes this action as **Report a problem**.

The installed **MemorySafe Beta** desktop icon opens the real-data local dashboard at `http://127.0.0.1:8765/dashboard`. That page is bound to this Mac only. The same dashboard component is also attached to the `memorysafe_dashboard` MCP tool for compatible ChatGPT surfaces reached through the authorized Secure MCP Tunnel; no public memory URL is created.

## Current status

The local MCP server is implemented and testable. To use it from ChatGPT, connect it through ChatGPT Developer mode and an OpenAI Secure MCP Tunnel. The tunnel keeps the local server private and requires a tunnel ID plus a runtime API key created in the OpenAI Platform; never paste that key into a chat.

## Run locally

```bash
./scripts/start_memorysafe.sh
```

For a fresh installation:

```bash
./scripts/setup.sh
```

## Connect the private beta to ChatGPT

1. In ChatGPT, open Settings → Security and login → Developer mode.
2. In OpenAI Platform tunnel settings, create a tunnel associated with the same ChatGPT workspace.
3. Run `tunnel-client` using the command from Platform. Point its stdio MCP command to the absolute `scripts/start_memorysafe.sh` path in this folder.
4. In ChatGPT Plugins, select + → Tunnel, choose the tunnel, create the MemorySafe connection, and review the seven discovered tools.
5. Start a new chat, enable MemorySafe from the tools menu, and say: “Turn on MemorySafe automatic mode.”

## Privacy boundary

This beta is single-user and local. The MCP server has no public listener and no code that sends memories to third-party services. ChatGPT can reach it only while the separately authorized Secure MCP Tunnel client is running. Automatic capture rejects likely secrets, financial/government identifiers, contact details, precise addresses, and medical/health information; those categories require an explicit manual request and remain subject to the normal safety rules.
