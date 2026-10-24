"""The hub's page and the assistant's chat."""

from __future__ import annotations

import reflex as rx
from reflex.event import EventSpec

from ai_hub import attachments, config
from ai_hub.states import DROP_ID, PICK_ID, SESSION_CHOICE, AssistantState, HubState

CARD = "bg-white border border-slate-200 rounded-xl p-5 shadow-sm flex flex-col gap-3"
H2 = "text-base font-semibold text-slate-800"
MUTED = "text-sm text-slate-500"
BUTTON = "px-3 py-1.5 rounded-lg bg-emerald-600 hover:bg-emerald-500 text-white text-sm font-medium disabled:opacity-50"
SUBTLE_BUTTON = "px-2 py-1 rounded-md bg-slate-100 hover:bg-slate-200 text-slate-700 text-xs font-medium"
INPUT = "px-3 py-1.5 rounded-lg border border-slate-300 text-sm focus:outline-none focus:border-emerald-500"
CODE = "block whitespace-pre-wrap break-all bg-slate-900 text-emerald-200 text-xs rounded-lg p-3 font-mono"
NOTICE = "flex flex-col gap-2 border border-amber-200 bg-amber-50 rounded-lg p-3"
SESSION_LABEL = "Claude Code on my computer (development only)"
SESSION_DIR = "~/.local/share/ai_hub/claude-session"
RULES_URL = "https://code.claude.com/docs/en/legal-and-compliance"


def kind_badge(kind: rx.Var) -> rx.Component:
    return rx.el.span(
        kind,
        class_name=rx.match(
            kind,
            ("read", "text-[10px] px-1.5 py-0.5 rounded bg-sky-100 text-sky-700"),
            ("danger", "text-[10px] px-1.5 py-0.5 rounded bg-red-100 text-red-700"),
            "text-[10px] px-1.5 py-0.5 rounded bg-amber-100 text-amber-700",
        ),
    )


def unlock_form() -> rx.Component:
    return rx.el.div(
        rx.el.p("Making tokens and changing models needs the hub's passcode.", class_name=MUTED),
        rx.el.div(
            rx.el.input(
                type="password",
                placeholder="Passcode",
                value=HubState.passcode,
                on_change=HubState.set_passcode,
                class_name=INPUT,
            ),
            rx.el.button("Unlock", on_click=HubState.unlock, class_name=BUTTON),
            class_name="flex gap-2",
        ),
    )


def connect_card() -> rx.Component:
    claude_code = (
        "claude mcp add --transport http apps "
        + HubState.mcp_url
        + ' --header "Authorization: Bearer '
        + HubState.new_token
        + '"'
    )
    return rx.el.section(
        rx.el.h2("Connect an agent", class_name=H2),
        rx.el.p(
            "Claude Code, Codex, Cursor and other agents reach every app's tools through one MCP address:",
            class_name=MUTED,
        ),
        rx.el.code(HubState.mcp_url, class_name=CODE),
        rx.cond(
            HubState.locked,
            unlock_form(),
            rx.el.div(
                rx.el.input(
                    placeholder="Who will use it, e.g. Claude Code on my Mac",
                    value=HubState.token_name,
                    on_change=HubState.set_token_name,
                    class_name=INPUT + " flex-1",
                ),
                rx.el.button("Make a token", on_click=HubState.make_token, class_name=BUTTON),
                class_name="flex gap-2",
            ),
        ),
        rx.cond(
            HubState.new_token != "",
            rx.el.div(
                rx.el.p("The token is shown only now. For Claude Code, run:", class_name="text-sm text-slate-700"),
                rx.el.code(claude_code, class_name=CODE),
                rx.el.p(
                    "If Claude Code reports a certificate error, point NODE_EXTRA_CA_CERTS at the "
                    "re-ddns CA (https://home.reflex-ddns.com/api/ca.pem). Other agents: an HTTP MCP "
                    "server at the address above, with the header Authorization: Bearer <token>.",
                    class_name="text-xs text-slate-500",
                ),
                class_name="flex flex-col gap-2 border border-emerald-200 bg-emerald-50 rounded-lg p-3",
            ),
        ),
        rx.cond(
            HubState.tokens.length() > 0,
            rx.el.div(
                rx.foreach(
                    HubState.tokens,
                    lambda t: rx.el.div(
                        rx.el.span(t["name"], class_name="text-sm text-slate-800 flex-1"),
                        rx.el.span(t["created"], class_name="text-xs text-slate-400"),
                        rx.cond(
                            HubState.locked,
                            rx.fragment(),
                            rx.el.button("Delete", on_click=HubState.delete_token(t["id"]), class_name=SUBTLE_BUTTON),
                        ),
                        class_name="flex items-center gap-3 py-1 border-b border-slate-100",
                    ),
                ),
            ),
        ),
        class_name=CARD,
    )


def apps_card() -> rx.Component:
    return rx.el.section(
        rx.el.div(
            rx.el.h2("Apps and their tools", class_name=H2),
            rx.el.button("Refresh", on_click=HubState.load, class_name=SUBTLE_BUTTON),
            class_name="flex items-center justify-between",
        ),
        rx.cond(
            HubState.apps.length() == 0,
            rx.el.p(
                "No app offers tools yet. Apps built on reflex_ddns_auth.tools show theirs here once installed.",
                class_name=MUTED,
            ),
            rx.foreach(
                HubState.apps,
                lambda a: rx.el.div(
                    rx.el.div(a["app"], class_name="text-sm font-semibold text-slate-700"),
                    rx.foreach(
                        a["tools"].to(list[dict[str, str]]),
                        lambda t: rx.el.div(
                            rx.el.code(t["name"], class_name="text-xs text-slate-800"),
                            kind_badge(t["kind"]),
                            rx.el.span(t["description"], class_name="text-xs text-slate-500 truncate"),
                            class_name="flex items-center gap-2 pl-3",
                        ),
                    ),
                    class_name="flex flex-col gap-1",
                ),
            ),
        ),
        class_name=CARD,
    )


def role_row(role: str) -> rx.Component:
    return rx.el.div(
        rx.el.div(
            rx.el.span(role, class_name="text-sm font-medium text-slate-800"),
            rx.el.span(config.ROLE_HELP[role], class_name="text-xs text-slate-500"),
            class_name="flex flex-col w-56",
        ),
        rx.el.select(
            rx.foreach(
                HubState.choices,
                lambda c: rx.el.option(
                    rx.match(c, ("ollama/", "Ollama: pick a model"), (SESSION_CHOICE, SESSION_LABEL), c),
                    value=c,
                ),
            ),
            value=HubState.roles[role],
            on_change=lambda v: HubState.set_role(role, v),
            disabled=HubState.locked,
            class_name=INPUT + " flex-1",
        ),
        class_name="flex items-center gap-3",
    )


def models_card() -> rx.Component:
    return rx.el.section(
        rx.el.h2("Models", class_name=H2),
        rx.foreach(
            HubState.providers,
            lambda p: rx.el.div(
                rx.el.span(p["name"], class_name="text-sm text-slate-700 w-56"),
                rx.el.span(p["status"], class_name="text-sm text-slate-500"),
                class_name="flex gap-3",
            ),
        ),
        rx.el.div(*[role_row(r) for r in config.ROLES], class_name="flex flex-col gap-2 pt-2"),
        rx.cond(
            HubState.locked,
            rx.fragment(),
            rx.el.div(
                rx.el.input(
                    type="password",
                    placeholder="Anthropic API key (Claude)",
                    value=HubState.anthropic_key,
                    on_change=HubState.set_anthropic_key,
                    class_name=INPUT,
                ),
                rx.el.input(
                    type="password",
                    placeholder="OpenAI API key",
                    value=HubState.openai_key,
                    on_change=HubState.set_openai_key,
                    class_name=INPUT,
                ),
                rx.el.input(
                    placeholder="Ollama address, e.g. http://host.docker.internal:11434",
                    value=HubState.ollama_url,
                    on_change=HubState.set_ollama_url,
                    class_name=INPUT,
                ),
                rx.el.div(
                    rx.el.button("Save", on_click=HubState.save_keys, class_name=BUTTON),
                    rx.el.button("Forget the Claude key", on_click=HubState.forget_key("anthropic"), class_name=SUBTLE_BUTTON),
                    rx.el.button("Forget the OpenAI key", on_click=HubState.forget_key("openai"), class_name=SUBTLE_BUTTON),
                    class_name="flex gap-2 items-center",
                ),
                rx.el.p(
                    "Keys stay in the hub's settings (only this container reads them). With a Claude key, "
                    "what the assistant works on goes to Anthropic; use the local role for data that must stay here.",
                    class_name="text-xs text-slate-500",
                ),
                class_name="grid gap-2 pt-2",
            ),
        ),
        class_name=CARD,
    )


def passcode_form(label: str, button: str) -> rx.Component:
    return rx.el.div(
        rx.el.p(label, class_name="text-sm text-slate-700"),
        rx.el.div(
            rx.el.input(
                type="password",
                placeholder="At least 6 characters",
                value=HubState.new_passcode,
                on_change=HubState.set_new_passcode,
                class_name=INPUT + " flex-1",
            ),
            rx.el.button(button, on_click=HubState.save_passcode, class_name=SUBTLE_BUTTON),
            class_name="flex gap-2 items-center",
        ),
        class_name="flex flex-col gap-1",
    )


def session_steps() -> rx.Component:
    setup = "./claude_channel/setup.sh " + HubState.hub_url + " " + HubState.session_token
    return rx.el.div(
        rx.el.p("The token is shown only now. On your computer:", class_name="text-sm text-slate-700"),
        rx.el.p("1. Once, in your ai_hub checkout (needs Node.js 22.15 or later):", class_name="text-sm text-slate-700"),
        rx.el.code(setup, class_name=CODE),
        rx.el.p(
            "2. Each time you want it, start Claude Code with the hub's channel, in a terminal (VS Code's is fine). "
            "The first time, choose “Yes, I trust this folder”; each time, “I am using this for local development”:",
            class_name="text-sm text-slate-700",
        ),
        rx.el.code(f"{SESSION_DIR}/start.sh", class_name=CODE),
        rx.el.p(
            "3. Under Models, set a role (smart, say) to “" + SESSION_LABEL + "”.",
            class_name="text-sm text-slate-700",
        ),
        class_name="flex flex-col gap-2 border border-emerald-200 bg-emerald-50 rounded-lg p-3",
    )


def session_card() -> rx.Component:
    return rx.el.section(
        rx.el.h2("Claude Code on my computer (for development)", class_name=H2),
        rx.el.p(
            "The assistant can hand what you type to the Claude Code you run on your own computer, signed in "
            "to your own Claude plan: it looks things up on the web when the task needs it, does the work "
            "with the apps' tools, and its answers show in the assistant. The hub only passes the messages "
            "on; it never holds your Claude login.",
            class_name=MUTED,
        ),
        rx.el.div(
            rx.el.p(
                "For your own development and testing only. Only you may use it, so the assistant asks for "
                "this hub's passcode first: don't let other people use it. For a company, a product, or "
                "anyone else, use an API key instead (Claude API, above).",
                class_name="text-sm text-amber-900",
            ),
            rx.el.a("Anthropic's rules for Claude logins and API keys", href=RULES_URL, target="_blank", class_name="text-xs text-amber-800 underline"),
            class_name=NOTICE,
        ),
        rx.cond(
            ~HubState.has_passcode,
            passcode_form("First give this hub a passcode (only you should know it):", "Set the passcode"),
            rx.cond(
                HubState.locked,
                unlock_form(),
                rx.el.div(
                    rx.el.p(
                        rx.cond(
                            HubState.session_ready,
                            rx.cond(HubState.session_running, "Running.", "Set up; not running now."),
                            "Not set up.",
                        ),
                        class_name="text-sm text-slate-700",
                    ),
                    rx.cond(HubState.session_token != "", session_steps()),
                    rx.el.div(
                        rx.el.button(
                            rx.cond(HubState.session_ready, "Make a new connection token", "Make the connection token"),
                            on_click=HubState.make_session_token,
                            class_name=BUTTON,
                        ),
                        rx.cond(
                            HubState.session_ready,
                            rx.el.button("Disconnect", on_click=HubState.forget_session, class_name=SUBTLE_BUTTON),
                        ),
                        class_name="flex gap-2 items-center",
                    ),
                    rx.cond(~HubState.passcode_from_env, passcode_form("Change the passcode:", "Change")),
                    class_name="flex flex-col gap-3",
                ),
            ),
        ),
        class_name=CARD,
    )


def log_card() -> rx.Component:
    return rx.el.section(
        rx.el.h2("Recent tool calls", class_name=H2),
        rx.cond(
            HubState.log.length() == 0,
            rx.el.p("None yet.", class_name=MUTED),
            rx.el.div(
                rx.foreach(
                    HubState.log,
                    lambda e: rx.el.div(
                        rx.el.span(e["at"], class_name="text-slate-400 w-16"),
                        rx.el.span(e["via"], class_name="text-slate-500 w-20"),
                        rx.el.span(e["user"], class_name="text-slate-700 w-48 truncate"),
                        rx.el.span(e["tool"], class_name="text-slate-800 flex-1 truncate"),
                        rx.el.span(e["result"], class_name="text-slate-600 w-48 truncate"),
                        rx.el.span(e["ms"] + " ms", class_name="text-slate-400 w-20 text-right"),
                        class_name="flex gap-2 text-xs font-mono py-0.5 border-b border-slate-100",
                    ),
                ),
            ),
        ),
        class_name=CARD,
    )


def hub_page() -> rx.Component:
    return rx.el.main(
        rx.el.header(
            rx.el.div(
                rx.el.h1("AI Hub", class_name="text-2xl font-bold text-slate-900"),
                rx.el.p(
                    "One door between AI and the apps: agents and the assistant use the apps' tools here.",
                    class_name=MUTED,
                ),
            ),
            rx.el.a("Open the assistant →", href="/assistant", class_name=BUTTON),
            class_name="flex items-center justify-between",
        ),
        rx.cond(HubState.message != "", rx.el.p(HubState.message, class_name="text-sm text-amber-700")),
        connect_card(),
        apps_card(),
        models_card(),
        session_card(),
        log_card(),
        class_name="max-w-4xl mx-auto p-6 flex flex-col gap-5 bg-slate-50 min-h-screen",
    )


def file_picture(f: rx.Var) -> rx.Component:
    """A file's thumbnail, or a plain mark for files without one."""
    return rx.cond(
        f["thumb"] != "",
        rx.el.img(src=f["thumb"], alt="", class_name="h-9 w-9 object-cover rounded shrink-0"),
        rx.el.div(rx.icon("file-text", size=18), class_name="h-9 w-9 flex items-center justify-center rounded bg-slate-100 text-slate-500 shrink-0"),
    )


def file_label(name: rx.Var, details: rx.Var) -> rx.Component:
    return rx.el.div(
        rx.el.span(name, class_name="text-xs font-medium text-slate-700 truncate"),
        rx.el.span(details, class_name="text-[11px] text-slate-500 truncate"),
        class_name="flex flex-col min-w-0",
    )


def file_chip(f: rx.Var) -> rx.Component:
    """A file waiting to go with the next message."""
    return rx.el.div(
        file_picture(f),
        file_label(f["name"], f["details"]),
        rx.el.button(
            "×",
            type="button",
            title="Remove",
            on_click=AssistantState.remove_file(f["id"]),
            class_name="text-slate-400 hover:text-slate-700 text-base leading-none px-1",
        ),
        class_name="flex items-center gap-2 max-w-[260px] bg-white border border-slate-200 rounded-lg p-1 pr-1.5",
    )


def message_row(m: rx.Var) -> rx.Component:
    # shrink-0: once the list scrolls, flex would squeeze the truncated rows to nothing.
    return rx.match(
        m["kind"],
        ("user", rx.el.div(m["text"], class_name="shrink-0 self-end max-w-[80%] bg-emerald-600 text-white rounded-2xl px-3 py-2 text-sm whitespace-pre-wrap")),
        # A file that went with the person's message.
        (
            "file",
            rx.el.div(
                file_picture(m),
                file_label(m["text"], m["details"]),
                class_name="shrink-0 self-end flex items-center gap-2 max-w-[80%] bg-emerald-50 border border-emerald-200 rounded-xl p-1 pr-3",
            ),
        ),
        ("assistant", rx.el.div(m["text"], class_name="shrink-0 self-start max-w-[85%] bg-white border border-slate-200 rounded-2xl px-3 py-2 text-sm whitespace-pre-wrap")),
        ("tool", rx.el.div("⚙ " + m["text"], class_name="shrink-0 self-start text-xs font-mono text-slate-500 pl-2")),
        # What Claude Code on the owner's computer does besides the apps' tools (ai_hub.session).
        ("web", rx.el.div("🌐 " + m["text"], class_name="shrink-0 self-start text-xs text-sky-700 pl-2 truncate max-w-[85%]")),
        ("note", rx.el.div(m["text"], class_name="shrink-0 self-start max-w-[85%] text-xs italic text-slate-500 border-l-2 border-slate-300 pl-2 whitespace-pre-wrap")),
        ("info", rx.el.div(m["text"], class_name="shrink-0 self-start text-xs text-slate-600 pl-2")),
        ("result", rx.el.div("✓ " + m["text"], class_name="shrink-0 self-start text-xs font-mono text-emerald-700 pl-4 truncate max-w-[85%]")),
        rx.el.div("✗ " + m["text"], class_name="shrink-0 self-start text-xs font-mono text-red-600 pl-4 whitespace-pre-wrap max-w-[85%]"),
    )


def passcode_prompt() -> rx.Component:
    return rx.el.div(
        rx.el.p(
            "This goes to the hub owner's own Claude Code, on their own Claude plan, for their development "
            "only. Enter the hub's passcode:",
            class_name="text-sm text-amber-900",
        ),
        rx.el.div(
            rx.el.input(
                type="password",
                placeholder="Passcode",
                value=AssistantState.passcode,
                on_change=AssistantState.set_passcode,
                id="assistant-passcode",
                class_name=INPUT + " flex-1",
            ),
            rx.el.button("Unlock and send", on_click=AssistantState.unlock, class_name=BUTTON),
            class_name="flex gap-2",
        ),
        rx.cond(
            AssistantState.passcode_note != "",
            rx.el.p(AssistantState.passcode_note, class_name="text-xs text-red-600"),
        ),
        class_name=NOTICE,
    )


def _attach(upload_id: str) -> EventSpec:
    """Streams the files of an upload area to the hub as they are dropped or picked."""
    return AssistantState.attach(rx.upload_files_chunk(upload_id=upload_id, on_upload_progress=AssistantState.upload_progress))


def pick_button() -> rx.Component:
    """The paperclip: picks files (an upload area that only clicks; drops go to the chat's)."""
    return rx.upload.root(
        rx.el.button(
            rx.icon("paperclip", size=16),
            type="button",
            title="Attach files: pictures, videos, PDFs, documents (or drop them on the chat, or paste a picture)",
            class_name="h-[38px] px-2 rounded-lg border border-slate-300 bg-white hover:bg-slate-100 text-slate-600",
        ),
        id=PICK_ID,
        no_drag=True,
        max_size=attachments.MAX_BYTES,
        on_drop=_attach(PICK_ID),
        class_name="shrink-0",
    )


def pending_files() -> rx.Component:
    return rx.cond(
        (AssistantState.files.length() > 0) | (AssistantState.uploading != ""),
        rx.el.div(
            rx.foreach(AssistantState.files, file_chip),
            rx.cond(AssistantState.uploading != "", rx.el.span(AssistantState.uploading, class_name="text-xs text-slate-500 self-center")),
            id="assistant-pending",
            class_name="flex flex-wrap gap-2",
        ),
    )


def chat_view(dialog: bool = False) -> rx.Component:
    # All of it is the drop area for files (it doesn't open the file picker on a click: the
    # paperclip does).
    return rx.upload.root(
        rx.el.div(
            rx.el.div(
                rx.el.span("Assistant", class_name="font-semibold text-slate-800"),
                rx.cond(
                    AssistantState.app != "",
                    rx.el.span(
                        " · " + AssistantState.app + rx.cond(AssistantState.room != "", " / " + AssistantState.room, ""),
                        class_name="text-sm text-slate-500",
                    ),
                ),
                rx.cond(AssistantState.model != "", rx.el.span(" · " + AssistantState.model, class_name="text-xs text-slate-400")),
            ),
            rx.el.div(
                rx.el.select(
                    *[rx.el.option(r, value=r) for r in config.ROLES],
                    value=AssistantState.role,
                    on_change=AssistantState.set_role,
                    class_name=INPUT + " py-1",
                ),
                rx.el.button("New chat", on_click=AssistantState.new_chat, class_name=SUBTLE_BUTTON),
                class_name="flex gap-2 items-center",
            ),
            # In a dialog, the host's own buttons (⧉ – ✕) sit in the top right corner.
            class_name="flex items-center justify-between" + (" pr-28" if dialog else ""),
        ),
        rx.el.div(
            rx.cond(
                AssistantState.messages.length() == 0,
                rx.el.div(
                    rx.el.p("Tell me what to do in the apps, e.g. “draw a small table with four legs”."),
                    rx.el.p("Files help too: drop pictures, videos, PDFs or documents here, or paste a picture."),
                    class_name="text-sm text-slate-400 self-center text-center pt-8 flex flex-col gap-1",
                ),
            ),
            rx.foreach(AssistantState.messages, message_row),
            rx.cond(
                AssistantState.busy,
                rx.el.div(
                    rx.el.span(class_name="h-2 w-2 mt-1 rounded-full bg-emerald-500 animate-pulse shrink-0"),
                    rx.el.span(rx.cond(AssistantState.status != "", AssistantState.status, "…"), class_name="whitespace-pre-wrap"),
                    id="assistant-status",
                    class_name="shrink-0 self-start flex items-start gap-2 text-xs text-slate-500 pl-2",
                ),
            ),
            class_name="flex flex-col gap-2 overflow-y-auto flex-1 min-h-[240px] py-2",
            id="assistant-messages",
        ),
        rx.cond(AssistantState.ask_passcode, passcode_prompt()),
        pending_files(),
        rx.el.form(
            pick_button(),
            rx.el.textarea(
                placeholder="Message the assistant (Enter sends, Shift+Enter breaks the line)",
                # Not bound to the state: a value echoed back while typing breaks IME composition.
                name="draft",
                rows=2,
                id="assistant-input",
                class_name=INPUT + " flex-1 resize-none",
            ),
            rx.el.button("Send", type="submit", disabled=AssistantState.busy | (AssistantState.uploading != ""), class_name=BUTTON),
            on_submit=AssistantState.send,
            reset_on_submit=True,
            class_name="flex gap-2 items-end",
        ),
        rx.script(
            """
if (!window.__assistantChat) {
  window.__assistantChat = true;
  document.addEventListener('keydown', (e) => {
    const el = document.getElementById('assistant-input');
    if (e.target === el && e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      // While the assistant works, Send is off: keep what was typed rather than clear it.
      const send = el.form && el.form.querySelector('button[type="submit"]');
      if (el.form && !(send && send.disabled)) el.form.requestSubmit();
    }
  });
  // A picture pasted into the box (a screenshot, say) is attached as if it were dropped. Pasted
  // text stays text, even when the clipboard also holds a picture of it (as Word's does).
  document.addEventListener('paste', (e) => {
    const el = document.getElementById('assistant-input');
    const data = e.clipboardData;
    if (e.target !== el || !data || !data.files.length || data.getData('text/plain')) return;
    const input = document.querySelector('#""" + DROP_ID + """ > input[type=file]');
    if (!input) return;
    e.preventDefault();
    const files = new DataTransfer();
    Array.from(data.files).forEach((f) => files.items.add(f));
    input.files = files.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
  });
  // New messages scroll into view, unless the person scrolled up to read.
  let height = 0, atEnd = true;
  setInterval(() => {
    const list = document.getElementById('assistant-messages');
    if (!list) return;
    if (list.scrollHeight !== height) {
      if (atEnd) list.scrollTop = list.scrollHeight;
      height = list.scrollHeight;
    }
    atEnd = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
  }, 250);
}
"""
        ),
        id=DROP_ID,
        no_click=True,
        no_keyboard=True,
        max_size=attachments.MAX_BYTES,
        on_drop=_attach(DROP_ID),
        drag_active_style={"outline": "2px dashed #10b981", "outline_offset": "-2px", "border_radius": "12px", "background": "#ecfdf5"},
        class_name="flex flex-col gap-3 h-full",
    )


def assistant_page() -> rx.Component:
    return rx.el.main(
        rx.el.a("← AI Hub", href="/", class_name="text-sm text-slate-500"),
        chat_view(),
        class_name="max-w-3xl mx-auto p-6 flex flex-col gap-3 bg-slate-50 h-screen",
    )


def assistant_dialog() -> rx.Component:
    # A fixed height: the dialog takes the height its content reports, so the content can't
    # take the dialog's (the host keeps the dialog within the window).
    return rx.el.div(chat_view(dialog=True), class_name="p-4 bg-slate-50 h-[560px] flex flex-col")
