# AI Hub

One door between AI and the apps of a [re_ddns](https://github.com/milochen0418/re_ddns) host
(the Agentic App Store). Apps only say what an AI may do in them; which AI does it is the
hub's business.

> Important: Before working on this project, read [AGENTS.md](AGENTS.md) for required workflows and tooling expectations.

```
 Claude Code / Codex / Cursor …            the assistant (a page, or a dialog in any app)
            │  https://ai.reflex-ddns.com/api/mcp        │  Claude · Ollama · OpenAI
            ▼                                              ▼
 ┌─ AI Hub ────────────────────────────────────────────────────────────
 │  tools of every installed app (found through re-ddns), tokens, models, a log
 └─────────────────────────────────────────────────────────────────────
            │  POST <app backend>/_ddns_tool/call/<tool>   (Docker network, signed)
            ▼
 apps built on reflex_ddns_auth.tools   (e.g. Codoc in CAD: cad.set_code, cad.run_preview, …)
```

## For agents: one MCP address

On the hub's page, make a token (named after who will use it: it is how the AI shows up in the
apps), then for Claude Code:

```bash
claude mcp add --transport http apps https://ai.reflex-ddns.com/api/mcp \
  --header "Authorization: Bearer <token>"
```

Any other MCP client: an HTTP (Streamable HTTP) server at that address with the header
`Authorization: Bearer <token>`. The agent then sees the tools of every installed app; a newly
installed app's tools show up by themselves. If the client reports a certificate error, it
doesn't trust the re-ddns CA yet: for Node-based clients such as Claude Code, point
`NODE_EXTRA_CA_CERTS` at it (`https://home.reflex-ddns.com/api/ca.pem`).

Agents that run in a vendor's cloud (claude.ai and Claude Desktop custom connectors, ChatGPT)
connect from there and can't reach a LAN-only host; agents on your machine can.

## For people: the assistant

`https://ai.reflex-ddns.com/assistant`, or from inside an app: the app opens the hub's
`ai.assist` intent as a dialog, scoped to itself (one button; Codoc in CAD's **AI** button):

```python
Intent.start(None, "ai.assist", keep_alive=True, single=True, in_app="cad", room=CadState.current_room_id)
```

The assistant runs the loop the models themselves don't: it gives the model the app's tools,
calls the ones the model asks for (for the person, under the model's name), hands back the
results, and goes on until the model is done. Opened from an app, it first reads the app's
`<app>.guide` tool, so even a small local model starts out knowing the app's rules, and a tool
that takes a `room` works in the room it was opened in when the model leaves `room` out.

## For app developers: offer tools

```python
from reflex_ddns_auth.tools import tool, image
from reflex_ddns_auth import rooms

@tool(app, "cad.run_preview", kind="write", params={"room": "The room."})
async def run_preview(ctx, room: str = "default") -> dict:
    """Builds the room's model and shows it in every tab of the room."""
    async with rooms.change(CadState, room) as cad:   # everyone in the room sees the change
        ...
    return {"ok": True}
```

The app then serves `/_ddns_tool/manifest` and `/_ddns_tool/call/<name>` on its backend; the
hub finds them through re-ddns. Calls carry a short-lived JWT signed with the platform's
`DDNS_AUTH_SECRET`; anything else is refused. `kind` is `read`, `write` or `danger` (shown to
agents as MCP tool annotations). See `reflex_ddns_auth/tools.py` and `reflex_ddns_auth/rooms.py`.

## Models

Roles are what the assistant asks for; each is set to `<provider>/<model>` on the hub's page:

| Role | Default | For |
|---|---|---|
| `smart` | Claude (`claude-opus-5-5`) with a key, else Ollama | the assistant |
| `fast` | Ollama | quick answers |
| `local` | Ollama (must stay local) | data that must not leave this machine |
| `coder` | as `smart` | writing code |

- **Ollama** (always): the Docker host's (`http://host.docker.internal:11434`), or `OLLAMA_URL`.
  Left on "pick a model", a model that handles tool calls is chosen (qwen3, qwen2.5, llama3.1, …).
  The hub uses Ollama's own chat API and asks for a 16k-token context (`AI_HUB_OLLAMA_NUM_CTX`):
  at Ollama's default of 4096 an app's guide and tools leave almost no room, and Ollama quietly
  drops the start of the chat. A 7B model manages simple tasks; bigger ones go further.
- **Claude**: through the Anthropic SDK (Messages API); its content blocks go back unchanged,
  thinking included, and requests opt into the server-side fallback (`fallbacks="default"`),
  so a declined request is retried by the API on the model Anthropic recommends for it.
- **OpenAI** and other OpenAI-compatible servers.
- **Claude Code on my computer**, for the owner's own development: see below.

Keys are set on the page (or `ANTHROPIC_API_KEY` / `OPENAI_API_KEY`) and kept in the hub's data
(`AI_HUB_DATA`, default `~/.local/share/ai_hub`, a volume on the App Store). Once the hub has a
passcode (`ADMIN_PASSCODE`, or one set on its page), making tokens and changing models needs it;
a browser that gave it is remembered as the owner's. With a Claude or OpenAI key, what the
assistant works on goes to that company; use the `local` role for data that must stay here.

## Claude Code on my computer (for your own development)

While you build apps, the assistant can hand what you type to the Claude Code you run on your
own computer, signed in to your own Claude plan: it does the work with the apps' tools, each step
shows in the dialog, and its answer comes back there. The hub only passes messages on and never
holds your Claude login (`ai_hub/session.py`; the other end is [`claude_channel/`](claude_channel/),
a [Claude Code channel](https://code.claude.com/docs/en/channels-reference), a research preview).

**For your own development and testing only.** Your Claude plan is for you: the assistant asks for
the hub's passcode before anything goes to your Claude Code, so don't give it to other people. A
company, a product, or anyone else uses an API key instead ("Claude" above), as
[Anthropic's rules](https://code.claude.com/docs/en/legal-and-compliance) require for apps.

1. On the hub's page, give the hub a passcode and press **Make the connection token**.
2. On your computer, once, in this checkout (Node.js 22.15 or later):
   `./claude_channel/setup.sh https://ai.reflex-ddns.com <token>`. It installs the channel in
   `~/.local/share/ai_hub/claude-session/` with an `mcp.json` that only you can read.
3. Each time you want it, start Claude Code with the channel, in a terminal (VS Code's is fine):
   `~/.local/share/ai_hub/claude-session/start.sh`. The first time, choose "Yes, I trust this
   folder"; each time, "I am using this for local development". The session gets the apps'
   tools, the channel's `reply`, and web search and fetching pages (`WebSearch`, `WebFetch`), so
   it can look things up before it works ("find what the inside of the Sagrada Família looks like,
   then build it"), and nothing else: no files, no shell. A web page can try to steer what it
   does; at worst that is a change in the app it works in, and it is told to treat pages as
   information only. To keep it off the web, start it with `--tools ""` in `start.sh`.
4. Under Models, set a role (`smart`, say) to "Claude Code on my computer". The assistant then
   says when that Claude Code isn't running.

One message is worked on at a time; the next waits for the answer to the last.

## Running it

```bash
poetry env use python3.11
poetry install
poetry run ./reflex_rerun.sh
```

Outside the platform, tell it where the apps are and give it the platform secret the apps use:

```bash
DDNS_AUTH_SECRET=... AI_HUB_APPS="cad=http://localhost:8001" poetry run reflex run
```

Tests: `poetry run pytest`.
