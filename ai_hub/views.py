"""The hub's page and the assistant's chat."""

from __future__ import annotations

from typing import Any

import reflex as rx
from reflex.event import EventSpec

from ai_hub import attachments, config
from ai_hub.states import DROP_ID, PICK_ID, SESSION_CHOICE, AssistantState, HubState, TalkState

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
                HubState.local_choices if role == "local" else HubState.choices,
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


def app_role_row(app: rx.Var, r: rx.Var) -> rx.Component:
    return rx.el.div(
        rx.el.div(
            rx.el.span(r["title"], class_name="text-sm font-medium text-slate-800"),
            rx.el.span(r["note"], class_name="text-xs text-slate-500"),
            class_name="flex flex-col w-64 shrink-0",
        ),
        rx.el.select(
            rx.foreach(
                r["options"].to(list[str]),
                lambda c: rx.el.option(
                    rx.match(c, ("", "Same as the role"), ("ollama/", "Ollama: pick a model"), (SESSION_CHOICE, SESSION_LABEL), c),
                    value=c,
                ),
            ),
            value=r["chosen"],
            on_change=lambda v: HubState.set_app_target(app, r["role"], v),
            disabled=HubState.locked,
            class_name=INPUT + " flex-1",
        ),
        class_name="flex items-center gap-3",
    )


def app_models_card() -> rx.Component:
    """What each app's AI answers with: what the app uses AI for, and a choice per role."""
    return rx.el.section(
        rx.el.h2("What each app's AI uses", class_name=H2),
        rx.el.p(
            "Each app's AI, in the app's own words (reflex_ddns_auth.ai.uses): each asks for a role and gets "
            "the role's model above, unless another one is chosen for it here; everyone using the app gets it. "
            "An app appears once installed, or once it has asked. "
            "Your Claude Code answers smart and coder when set here (not the quick roles: "
            "that Claude Code takes one message at a time); it is your own Claude login, so keep that to your "
            "own development, and give an organization a Claude API key (billed to you) instead. "
            "The local role only takes a model on this machine.",
            class_name=MUTED,
        ),
        rx.cond(
            HubState.app_choices.length() == 0,
            rx.el.p("No app uses the hub's models yet.", class_name=MUTED),
            rx.foreach(
                HubState.app_choices,
                lambda a: rx.el.div(
                    rx.el.div(
                        rx.el.span(a["app"], class_name="text-sm font-semibold text-slate-700"),
                        # An app that says what it uses stays listed; Forget drops one that only asked.
                        rx.cond(
                            a["said"],
                            rx.fragment(),
                            rx.el.button("Forget", on_click=HubState.forget_app(a["app"]), disabled=HubState.locked, class_name=SUBTLE_BUTTON),
                        ),
                        class_name="flex items-center justify-between",
                    ),
                    rx.foreach(a["roles"].to(list[dict[str, Any]]), lambda r: app_role_row(a["app"], r)),
                    class_name="flex flex-col gap-2 pt-2",
                ),
            ),
        ),
        rx.el.h3("Recent asks", class_name="text-sm font-semibold text-slate-700 pt-2"),
        rx.cond(
            HubState.asks.length() == 0,
            rx.el.p("None yet.", class_name=MUTED),
            rx.el.div(
                rx.foreach(
                    HubState.asks,
                    lambda e: rx.el.div(
                        rx.el.span(e["at"], class_name="text-slate-400 w-16"),
                        rx.el.span(e["app"], class_name="text-slate-700 w-24 truncate"),
                        rx.el.span(e["user"], class_name="text-slate-700 w-40 truncate"),
                        rx.el.span(e["role"], class_name="text-slate-800 w-14"),
                        rx.el.span(e["model"], class_name="text-slate-800 flex-1 truncate"),
                        rx.el.span(e["result"], class_name="text-slate-600 w-32 truncate"),
                        rx.el.span(e["ms"] + " ms", class_name="text-slate-400 w-20 text-right"),
                        class_name="flex gap-2 text-xs font-mono py-0.5 border-b border-slate-100",
                    ),
                ),
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
        app_models_card(),
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


def approve_card(m: rx.Var) -> rx.Component:
    """A change the AI asks to make (``ai_hub.approvals``): what it would do, and the answers."""

    def answer(label: str, decision: str, style: str) -> rx.Component:
        return rx.el.button(label, type="button", on_click=AssistantState.decide(m["id"], decision), class_name=style + " approve-" + decision)

    return rx.el.div(
        rx.el.div("The AI wants to make this change: " + m["title"], class_name="text-xs font-semibold text-amber-900"),
        rx.cond(m["text"] != "", rx.el.div(m["text"], class_name="approve-summary text-sm text-slate-900 whitespace-pre-wrap")),
        rx.cond(m["details"] != "", rx.el.pre(m["details"], class_name="approve-details text-xs font-mono text-slate-700 bg-white border border-amber-200 rounded-lg p-2 max-h-56 overflow-auto whitespace-pre-wrap")),
        rx.match(
            m["state"],
            (
                "pending",
                rx.el.div(
                    answer("Accept", "accept", BUTTON),
                    answer("Accept all in this chat", "all", SUBTLE_BUTTON),
                    answer("Reject", "reject", SUBTLE_BUTTON),
                    class_name="flex gap-2 items-center flex-wrap",
                ),
            ),
            ("accept", rx.el.div("✓ Accepted", class_name="approve-state text-xs font-semibold text-emerald-700")),
            ("all", rx.el.div("✓ Accepted (and the rest of this chat)", class_name="approve-state text-xs font-semibold text-emerald-700")),
            ("reject", rx.el.div("✗ Rejected: nothing changed", class_name="approve-state text-xs font-semibold text-red-700")),
            rx.el.div("No answer in time: nothing changed", class_name="approve-state text-xs font-semibold text-slate-600"),
        ),
        class_name="approve-card shrink-0 self-start w-[92%] flex flex-col gap-2 bg-amber-50 border border-amber-300 rounded-2xl px-3 py-2",
    )


def message_row(m: rx.Var) -> rx.Component:
    # shrink-0: once the list scrolls, flex would squeeze the truncated rows to nothing.
    return rx.match(
        m["kind"],
        ("approve", approve_card(m)),
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
                        # A room may say more after "~" (which tab of the app, for its tools).
                        " · " + AssistantState.app + rx.cond(AssistantState.room != "", " / " + AssistantState.room.split("~")[0], ""),
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
            # What the page that opened the assistant shows, kept fresh by the script below.
            rx.el.input(type="hidden", name="context", id="assistant-context"),
            rx.el.button("Send", type="submit", disabled=AssistantState.busy | (AssistantState.uploading != ""), class_name=BUTTON),
            on_submit=AssistantState.send,
            reset_on_submit=True,
            class_name="flex gap-2 items-end",
        ),
        rx.script(
            """
if (!window.__assistantChat) {
  window.__assistantChat = true;
  // Opened from an app (an intent dialog), the assistant asks that page what it shows now (its
  // window.__ddnsContext: the sheet and the selection, say) and sends it with each message.
  const refreshContext = async () => {
    const field = document.getElementById('assistant-context');
    const page = window.__ddnsIntentPage;
    if (!field) return;
    if (!page || !page.ask) { field.value = ''; return; }
    try {
      const data = await page.ask(800);
      field.value = data == null ? '' : JSON.stringify(data);
    } catch (err) { field.value = ''; }
  };
  setInterval(refreshContext, 1500);
  document.addEventListener('keydown', (e) => {
    const el = document.getElementById('assistant-input');
    if (e.target === el && e.key === 'Enter' && !e.shiftKey && !e.isComposing && e.keyCode !== 229) {
      e.preventDefault();
      // While the assistant works, Send is off: keep what was typed rather than clear it.
      const send = el.form && el.form.querySelector('button[type="submit"]');
      if (el.form && !(send && send.disabled)) refreshContext().then(() => el.form.requestSubmit());
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


# ------------------------------------------------------------ the talking AI

TALK_CSS = """
#talk-face { background: #e2e8f0; }
#talk-mic[data-on="1"] { background: #dc2626; color: #fff; border-color: #dc2626; }
#talk-mic[data-hearing="1"] { box-shadow: 0 0 0 4px rgba(220, 38, 38, 0.3); }
#talk-speaker[data-on="0"] { opacity: 0.45; }
#talk-stop { display: none; }
#talk-root[data-speaking="1"] #talk-stop { display: inline-flex; }
/* As a small picture-in-picture window (scaled down by the host): the face and the words, large. */
html[data-intent-view="pip"] .talk-hide { display: none !important; }
html[data-intent-view="pip"] #talk-dialog { height: auto !important; padding: 12px !important; }
html[data-intent-view="pip"] #talk-body { flex-direction: column; }
html[data-intent-view="pip"] #talk-left { width: 100% !important; }
html[data-intent-view="pip"] #talk-face { height: 400px !important; }
html[data-intent-view="pip"] #talk-caption { font-size: 34px !important; line-height: 1.25 !important; max-height: none !important; }
html[data-intent-view="pip"] #talk-hearing { font-size: 26px !important; }
"""

MOOD_MARKS = [("happy", "🙂"), ("laughing", "😄"), ("surprised", "😮"), ("thinking", "🤔"), ("sad", "😢"), ("angry", "😠"), ("wink", "😉")]
ROUND = "h-[38px] w-[38px] shrink-0 rounded-full border border-slate-300 bg-white hover:bg-slate-100 text-slate-700 flex items-center justify-center"


def talk_row(m: rx.Var) -> rx.Component:
    assistant = rx.el.div(
        rx.el.span(rx.match(m["mood"], *MOOD_MARKS, ""), class_name="mr-1"),
        m["text"],
        class_name="shrink-0 self-start max-w-[90%] bg-white border border-slate-200 rounded-2xl px-3 py-2 text-sm whitespace-pre-wrap",
    )
    return rx.match(
        m["kind"],
        ("user", rx.el.div(m["text"], class_name="shrink-0 self-end max-w-[85%] bg-emerald-600 text-white rounded-2xl px-3 py-2 text-sm whitespace-pre-wrap")),
        ("assistant", assistant),
        ("tool", rx.el.div("⚙ " + m["text"], class_name="shrink-0 self-start text-xs font-mono text-slate-500 pl-2")),
        ("web", rx.el.div("🌐 " + m["text"], class_name="shrink-0 self-start text-xs text-sky-700 pl-2 truncate max-w-[90%]")),
        ("note", rx.el.div(m["text"], class_name="shrink-0 self-start max-w-[90%] text-xs italic text-slate-500 border-l-2 border-slate-300 pl-2 whitespace-pre-wrap")),
        ("info", rx.el.div(m["text"], class_name="shrink-0 self-start text-xs text-slate-600 pl-2")),
        ("result", rx.el.div("✓ " + m["text"], class_name="shrink-0 self-start text-xs font-mono text-emerald-700 pl-4 truncate max-w-[90%]")),
        rx.el.div("✗ " + m["text"], class_name="shrink-0 self-start text-xs font-mono text-red-600 pl-4 whitespace-pre-wrap max-w-[90%]"),
    )


def talk_passcode_prompt() -> rx.Component:
    return rx.el.div(
        rx.el.p(
            "This goes to the hub owner's own Claude Code, on their own Claude plan, for their development only. Enter the hub's passcode:",
            class_name="text-sm text-amber-900",
        ),
        rx.el.div(
            rx.el.input(type="password", placeholder="Passcode", value=TalkState.passcode, on_change=TalkState.set_passcode, id="talk-passcode", class_name=INPUT + " flex-1"),
            rx.el.button("Unlock and send", on_click=TalkState.unlock, class_name=BUTTON),
            class_name="flex gap-2",
        ),
        rx.cond(TalkState.passcode_note != "", rx.el.p(TalkState.passcode_note, class_name="text-xs text-red-600")),
        class_name=NOTICE,
    )


def talk_view(dialog: bool = False) -> rx.Component:
    """The talking AI: its face and words on the left, the conversation on the right, the
    microphone, the speaker and a box to type in below. assets/talk.js makes it live."""
    header = rx.el.div(
        rx.el.div(
            rx.el.span("AI", class_name="font-semibold text-slate-800"),
            rx.cond(
                TalkState.app != "",
                rx.el.span(" · " + TalkState.app + rx.cond(TalkState.room != "", " / " + TalkState.room, ""), class_name="text-sm text-slate-500"),
            ),
            rx.cond(TalkState.model != "", rx.el.span(" · " + TalkState.model, class_name="text-xs text-slate-400")),
            class_name="min-w-0 truncate",
        ),
        rx.el.div(
            rx.el.select(
                *[rx.el.option(r, value=r) for r in config.ROLES],
                value=TalkState.role,
                on_change=TalkState.set_role,
                title="Which AI answers (set on the AI Hub page)",
                id="talk-role",
                class_name=INPUT + " py-1",
            ),
            rx.el.select(id="talk-style", title="The face", class_name=INPUT + " py-1"),
            rx.el.button("New chat", on_click=TalkState.new_chat, class_name=SUBTLE_BUTTON),
            class_name="flex gap-2 items-center shrink-0",
        ),
        class_name="talk-hide flex items-center justify-between gap-2" + (" pr-28" if dialog else ""),
    )
    left = rx.el.div(
        rx.el.canvas(id="talk-face", class_name="w-full aspect-square rounded-2xl"),
        rx.el.div(id="talk-caption", class_name="text-[15px] text-slate-800 leading-snug max-h-28 overflow-y-auto whitespace-pre-wrap"),
        rx.el.div(id="talk-hearing", class_name="text-sm text-red-700 italic min-h-[1.25rem]"),
        id="talk-left",
        class_name="w-[232px] shrink-0 flex flex-col gap-2",
    )
    right = rx.el.div(
        rx.cond(
            TalkState.messages.length() == 0,
            rx.el.div(
                rx.el.p("Press the microphone and talk, or type. I can see what your page shows and use its tools."),
                class_name="text-sm text-slate-400 self-center text-center pt-6 px-4",
            ),
        ),
        rx.foreach(TalkState.messages, talk_row),
        rx.cond(
            TalkState.busy,
            rx.el.div(
                rx.el.span(class_name="h-2 w-2 mt-1 rounded-full bg-emerald-500 animate-pulse shrink-0"),
                rx.el.span(rx.cond(TalkState.status != "", TalkState.status, "…"), class_name="whitespace-pre-wrap"),
                id="talk-busy",
                class_name="shrink-0 self-start flex items-start gap-2 text-xs text-slate-500 pl-2",
            ),
        ),
        id="talk-messages",
        class_name="talk-hide flex flex-col gap-2 overflow-y-auto flex-1 min-h-0 py-1",
    )
    form = rx.el.form(
        rx.el.button(rx.icon("mic", size=18), type="button", id="talk-mic", title="Listen to me (hands free)", class_name=ROUND, custom_attrs={"data-on": "0"}),
        rx.el.button(rx.icon("volume-2", size=18), type="button", id="talk-speaker", title="Speak the answers", class_name=ROUND, custom_attrs={"data-on": "1"}),
        rx.el.button(rx.icon("square", size=16), type="button", id="talk-stop", title="Stop talking", class_name=ROUND + " !bg-slate-800 !text-white"),
        rx.el.textarea(
            placeholder="Say something, or type it (Enter sends)",
            name="draft",
            rows=1,
            id="talk-input",
            class_name=INPUT + " flex-1 resize-none",
        ),
        rx.el.input(type="hidden", name="context", id="talk-context"),
        rx.el.button("Send", type="submit", id="talk-send", class_name=BUTTON),
        on_submit=TalkState.send,
        reset_on_submit=True,
        id="talk-form",
        class_name="talk-hide flex gap-2 items-center",
    )
    return rx.el.div(
        rx.el.style(TALK_CSS),
        header,
        rx.el.div(left, right, id="talk-body", class_name="flex gap-3 flex-1 min-h-0"),
        rx.cond(TalkState.ask_passcode, talk_passcode_prompt()),
        rx.el.div(id="talk-status", class_name="talk-hide text-xs text-slate-500 min-h-[1rem]"),
        form,
        # What to read out next (talk.js watches it).
        rx.el.div(id="talk-say", custom_attrs={"data-seq": TalkState.speech["seq"], "data-text": TalkState.speech["text"], "data-mood": TalkState.speech["mood"]}, class_name="hidden"),
        rx.script(src="/avatar_face.js"),
        rx.script(src="/talk.js"),
        id="talk-root",
        custom_attrs={
            "data-backend": rx.get_upload_url(""),
            "data-lang": TalkState.lang,
            "data-app": TalkState.app,
            "data-busy": rx.cond(TalkState.busy, "1", ""),
        },
        class_name="flex flex-col gap-2 h-full",
    )


def talk_page() -> rx.Component:
    return rx.el.main(
        rx.el.a("← AI Hub", href="/", class_name="text-sm text-slate-500"),
        talk_view(),
        class_name="max-w-3xl mx-auto p-6 flex flex-col gap-3 bg-slate-50 h-screen",
    )


def talk_dialog() -> rx.Component:
    return rx.el.div(talk_view(dialog=True), id="talk-dialog", class_name="p-4 bg-slate-50 h-[540px] flex flex-col")
