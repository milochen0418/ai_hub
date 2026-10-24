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

### Files

People can give the assistant files with what they type: drop them anywhere on the chat, pick
them with the paperclip, or paste a picture (a screenshot, say) into the message box. They go
with the next message, and the hub turns each into what models take, text and pictures
(`ai_hub/attachments.py`):

| File | What the AI gets |
|---|---|
| Picture (JPEG, PNG, WebP, GIF, HEIC, …) | the picture, turned upright, at most 1568 px on its long side |
| Video | its length, and 8 frames across it (or across a span) with their times; not its sound |
| PDF | each page's text and a picture of the page, 4 pages at a time; Claude (with a key) reads the PDF itself, up to 100 pages and 32 MB |
| Text, code, data (CSV, JSON, SVG, STEP, …) | the text (UTF-8, UTF-16 or Big5), 40,000 characters at a time |
| Word, Excel, PowerPoint (.docx, .xlsx, .pptx) | their text, in the order of the pages, sheets and slides |
| Anything else, sound included | its name, type and size |

The models get the first part of each file with the message (the first 4 pages of a PDF, the
first 40,000 characters of a text); the Claude Code on your computer opens the rest as it needs
it (see below). Models that can't see pictures, such as Ollama's qwen2.5, get only the text, and
the chat says so: for pictures, set the role to Claude, an OpenAI model, or an Ollama model that
sees them (Ollama lists `vision` among its capabilities, e.g. qwen2.5vl).

Files are kept on the hub (in `AI_HUB_DATA`) for 7 days, up to 2 GB each
(`AI_HUB_MAX_ATTACHMENT_MB`). Like what is typed, they go to the model's company when the role
uses Claude or OpenAI with a key; use the `local` role for files that must stay here. Pictures
go through Pillow, PDFs through pdfium (`pypdfium2`), videos through the ffmpeg that
`imageio-ffmpeg` brings, so the container needs nothing else installed.

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
   tools, the channel's `reply` and `attachment` (the files attached to its messages), and web
   search and fetching pages (`WebSearch`, `WebFetch`), so it can look things up before it works
   ("find what the inside of the Sagrada Família looks like, then build it"), and nothing else: no
   files on your computer, no shell. A web page can try to steer what it
   does; at worst that is a change in the app it works in, and it is told to treat pages as
   information only. To keep it off the web, start it with `--tools ""` in `start.sh`.
4. Under Models, set a role (`smart`, say) to "Claude Code on my computer". The assistant then
   says when that Claude Code isn't running.

One message is worked on at a time; the next waits for the answer to the last.

Files attached to a message reach it as a list (the ids in the `files` attribute of its
`<channel>` tag). It opens each with the channel's `attachment` tool, which fetches from the hub
what an AI is shown of the file (see Files above), a part at a time: more pages of a PDF, a span
of a video, the rest of a long text. It opens only the files of its own messages, and the chat
shows each one it opens. A session started before the `attachment` tool gets the message without
the files, and the chat says to restart it: copy `claude_channel/bridge.mjs` into the session
folder (or run `setup.sh` again) and restart `start.sh`.

While it works, the dialog shows what it does: under the messages, a line says what it is doing now
("thinking (max effort) · 12 min", "searching the web: …", "reading …", "using cad_set_code",
"waiting for you in its terminal: …", "finishing earlier work") and how long it has been on the
message; the chat lists its web searches, the pages it reads and the apps' tools it uses, with the
short notes it writes in between. The dialog waits for as long as it works, and stops waiting when
it answers, ends its turn without answering (the dialog then shows what it last wrote in its
terminal), fails, or is closed; **New chat** stops waiting at once (its next message goes to the
session when the turn ends). This comes from the session's
[hooks](https://code.claude.com/docs/en/hooks), which `setup.sh` writes into
`.claude/settings.local.json` of the session folder: each pipes its event to `curl`, which posts it
to `/api/session/activity` (Claude Code's own HTTP hooks may reach only loopback and public
addresses, not a hub on the local network). They apply only to the Claude Code that `start.sh` runs
there, and change nothing in it when the hub can't be reached. Set up before they existed, it shows
only the apps' tools, and the dialog gives up after 20 minutes without one: run `setup.sh` again
(with a new token) and restart the session.

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
