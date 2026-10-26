"""The hub's page state (HubState), the assistant's (AssistantState), the talking AI's
(TalkState), and the owner's mark (OwnerState) that they read."""

from __future__ import annotations

import asyncio
import collections
import time
import uuid
from typing import Any

import reflex as rx
from reflex_ddns_auth.intent import IntentPage

from ai_hub import agent, attachments, calls, config, registry, session, talk
from ai_hub.brains import openai_compat

CLAUDE_CHOICES = ["anthropic/claude-opus-5-5", "anthropic/claude-sonnet-5-5", "anthropic/claude-haiku-5-5"]
SESSION_CHOICE = f"{config.SESSION}/"
MIN_PASSCODE = 6
# The assistant's uploads: files dropped anywhere on the chat (or pasted into its box), and the
# ones picked with its paperclip button.
DROP_ID = "assistant-files"
PICK_ID = "assistant-pick"

# The assistant's conversations, by chat id (a Chat holds clients and model state, not state vars).
_CHATS: dict[str, agent.Chat] = {}
# Uploads under way in this process, by tab (client token): one cut off by a restart of the hub
# must not leave its tab waiting for it.
_UPLOADS: collections.Counter[str] = collections.Counter()


class OwnerState(rx.State):
    """Marks the owner's browser once it gave the passcode (``config.owner_key``)."""

    owner_key: str = rx.LocalStorage(name="ai_hub_owner", sync=True)


def _uses_session(role: str) -> bool:
    try:
        return config.resolve(role)[0].kind == "session"
    except LookupError:
        return False


class HubState(rx.State):
    apps: list[dict[str, Any]] = []
    tokens: list[dict[str, str]] = []
    token_name: str = ""
    new_token: str = ""
    roles: dict[str, str] = {}
    choices: list[str] = []
    providers: list[dict[str, str]] = []
    log: list[dict[str, str]] = []
    passcode: str = ""
    new_passcode: str = ""
    locked: bool = False
    has_passcode: bool = False
    passcode_from_env: bool = False
    session_token: str = ""
    session_ready: bool = False
    session_running: bool = False
    _unlocked: bool = False
    message: str = ""
    anthropic_key: str = ""
    openai_key: str = ""
    ollama_url: str = ""

    @rx.var
    def hub_url(self) -> str:
        return self.router.headers.origin or ""

    @rx.var
    def mcp_url(self) -> str:
        origin = self.router.headers.origin or ""
        return f"{origin}/api/mcp" if origin else "/api/mcp"

    @rx.event
    def set_token_name(self, value: str):
        self.token_name = value

    @rx.event
    def set_passcode(self, value: str):
        self.passcode = value

    @rx.event
    def set_new_passcode(self, value: str):
        self.new_passcode = value

    @rx.event
    def set_anthropic_key(self, value: str):
        self.anthropic_key = value

    @rx.event
    def set_openai_key(self, value: str):
        self.openai_key = value

    @rx.event
    def set_ollama_url(self, value: str):
        self.ollama_url = value

    async def _allowed(self) -> bool:
        """Whether this browser may change the settings: no passcode yet, or it gave it."""
        if not config.has_passcode() or self._unlocked:
            return True
        owner = await self.get_state(OwnerState)
        return config.is_owner(owner.owner_key)

    async def _mark_owner(self) -> None:
        self._unlocked = True
        owner = await self.get_state(OwnerState)
        owner.owner_key = config.owner_key()

    @rx.event
    async def unlock(self):
        if config.check_passcode(self.passcode):
            await self._mark_owner()
            self.message = ""
        else:
            self.message = "Wrong passcode."
        self.passcode = ""
        return HubState.load

    @rx.event
    async def save_passcode(self):
        if config.passcode_from_env() or not await self._allowed():
            return
        if len(self.new_passcode) < MIN_PASSCODE:
            self.message = f"The passcode needs at least {MIN_PASSCODE} characters."
            return
        config.set_passcode(self.new_passcode)
        self.new_passcode = ""
        await self._mark_owner()
        self.message = "Passcode saved: this browser counts as yours."
        return HubState.load

    @rx.event
    async def load(self):
        tools = await registry.tools(refresh=True)
        apps: dict[str, list[dict[str, str]]] = {}
        for t in tools:
            apps.setdefault(t.app, []).append(
                {"name": t.public_name, "title": t.title, "kind": t.kind, "description": t.description.split("\n")[0]}
            )
        self.apps = [{"app": a, "tools": ts} for a, ts in sorted(apps.items())]
        self.locked = not await self._allowed()
        self.has_passcode = config.has_passcode()
        self.passcode_from_env = config.passcode_from_env()
        self.tokens = [
            {"id": t["id"], "name": t["name"], "created": time.strftime("%Y-%m-%d %H:%M", time.localtime(t["created"]))}
            for t in config.tokens()
        ]
        self.roles = config.roles()
        known = config.providers()
        self.session_ready = config.SESSION in known
        self.session_running = session.running()
        ollama_models = await openai_compat.models(known["ollama"])
        choices = [f"ollama/{m}" for m in ollama_models if "embed" not in m]
        if "anthropic" in known:
            choices = CLAUDE_CHOICES + choices
        if "openai" in known:
            choices += [f"openai/{m}" for m in await openai_compat.models(known["openai"]) if m.startswith(("gpt", "o"))]
        if self.session_ready:
            choices = [SESSION_CHOICE] + choices
        self.choices = ["ollama/"] + choices
        self.providers = [
            {
                "name": "Ollama (on this machine)",
                "status": f"{len(ollama_models)} models at {known['ollama'].base_url.removesuffix('/v1')}"
                if ollama_models
                else f"not reachable at {known['ollama'].base_url.removesuffix('/v1')}",
            },
            {"name": "Claude (Anthropic)", "status": "key set" if "anthropic" in known else "no key"},
            {"name": "OpenAI", "status": "key set" if "openai" in known else "no key"},
            {
                "name": "Claude Code on my computer",
                "status": ("running" if self.session_running else "set up, not running")
                if self.session_ready
                else "not set up",
            },
        ]
        self.log = [
            {
                "at": time.strftime("%H:%M:%S", time.localtime(e.at)),
                "via": e.via,
                "user": e.user,
                "tool": f"{e.app} · {e.tool}",
                "result": "ok" if e.ok else f"error: {e.error[:80]}",
                "ms": str(e.ms),
            }
            for e in list(calls.LOG)[:30]
        ]

    @rx.event
    async def make_token(self):
        if not await self._allowed():
            return
        name = self.token_name.strip()
        if not name:
            self.message = "Give the token a name first (e.g. Claude Code on my Mac)."
            return
        self.new_token = config.create_token(name)
        self.token_name = ""
        self.message = ""
        return HubState.load

    @rx.event
    async def delete_token(self, token_id: str):
        if not await self._allowed():
            return
        config.delete_token(token_id)
        self.new_token = ""
        return HubState.load

    @rx.event
    async def make_session_token(self):
        """The token for the Claude Code on the owner's computer (only with a passcode: it is
        what keeps other people off the owner's Claude plan)."""
        if not config.has_passcode() or not await self._allowed():
            return
        self.session_token = config.make_session_token()
        return HubState.load

    @rx.event
    async def forget_session(self):
        if not await self._allowed():
            return
        token_id = config.session_token_id()
        if token_id:
            config.delete_token(token_id)
        for role, target in config.roles().items():
            if target.startswith(SESSION_CHOICE):
                config.set_role(role, "")
        self.session_token = ""
        return HubState.load

    @rx.event
    async def set_role(self, role: str, target: str):
        if not await self._allowed():
            return
        config.set_role(role, "" if target == "ollama/" and role != "local" else target)
        return HubState.load

    @rx.event
    async def save_keys(self):
        if not await self._allowed():
            return
        if self.anthropic_key.strip():
            config.set_provider("anthropic", api_key=self.anthropic_key.strip())
        if self.openai_key.strip():
            config.set_provider("openai", api_key=self.openai_key.strip())
        if self.ollama_url.strip():
            config.set_provider("ollama", url=self.ollama_url.strip())
        self.anthropic_key = self.openai_key = self.ollama_url = ""
        self.message = "Saved."
        return HubState.load

    @rx.event
    async def forget_key(self, provider: str):
        if not await self._allowed():
            return
        config.set_provider(provider, api_key="")
        return HubState.load


class AssistantState(rx.State):
    messages: list[dict[str, str]] = []
    draft: str = ""
    busy: bool = False
    # While busy: how it goes, e.g. "Claude Code is searching the web: “…” · 6 s" and, on a second
    # line, "14 min on this message · 9 steps".
    status: str = ""
    role: str = "smart"
    app: str = ""
    room: str = ""
    model: str = ""
    ask_passcode: bool = False
    passcode: str = ""
    passcode_note: str = ""
    # Files for the next message, each {"id", "name", "details", "thumb"} (ai_hub.attachments),
    # and how their upload goes ("" when none is on the way).
    files: list[dict[str, str]] = []
    uploading: str = ""
    _chat_id: str = ""

    @rx.event
    def set_draft(self, value: str):
        self.draft = value

    @rx.event
    def upload_progress(self, progress: dict):
        if _UPLOADS[self.router.session.client_token]:  # not a late one, after the upload ended
            self.uploading = f"Uploading… {int(float(progress.get('progress') or 0) * 100)}%"

    @rx.event(background=True)
    async def attach(self, chunks: rx.UploadChunkIterator):
        """Files dropped, pasted or picked: kept on the hub as they arrive, for the next message."""
        tab = self.router.session.client_token
        _UPLOADS[tab] += 1
        async with self:
            self.uploading = self.uploading or "Uploading…"
        incoming: attachments.Incoming | None = None
        name = ""
        try:
            async for chunk in chunks:
                # A file's pieces come in order, each with its place in the file: 0 begins the next.
                if incoming is None or chunk.filename != name or (chunk.offset == 0 and incoming.size):
                    if incoming is not None:
                        await self._keep(incoming)
                    name = chunk.filename
                    incoming = attachments.Incoming(chunk.filename, chunk.content_type)
                incoming.write(chunk.data)
            if incoming is not None:
                await self._keep(incoming)
                incoming = None
        except Exception as e:  # noqa: BLE001 — the person sees what went wrong
            if incoming is not None:
                incoming.discard()
            async with self:
                self.messages.append({"kind": "error", "text": f"{name or 'The file'} didn't arrive: {e}"})
        finally:
            _UPLOADS[tab] -= 1
            if _UPLOADS[tab] <= 0:
                del _UPLOADS[tab]
                async with self:
                    self.uploading = ""

    async def _keep(self, incoming: attachments.Incoming) -> None:
        att = await asyncio.to_thread(incoming.finish)
        async with self:
            self.files.append({"id": att.id, "name": att.name, "details": att.details(), "thumb": att.thumb})

    @rx.event
    def remove_file(self, file_id: str):
        if any(f["id"] == file_id for f in self.files):
            self.files = [f for f in self.files if f["id"] != file_id]
            attachments.remove(file_id)

    @rx.event
    def set_passcode(self, value: str):
        self.passcode = value

    @rx.event
    async def set_role(self, value: str):
        if value in config.ROLES and value != self.role:
            self.role = value
            await self._new_chat()

    async def _new_chat(self) -> None:
        chat = _CHATS.pop(self._chat_id, None)
        if chat is not None:
            # Claude Code on the owner's computer may still be on it: stop waiting for it there.
            await session.drop(chat.id)
        self._chat_id = ""
        self.messages = []
        self.model = ""
        self.ask_passcode = False
        self._unstick()

    def _unstick(self) -> None:
        """A chat or an upload cut off by a restart of the hub (its task gone with the process)
        left this tab busy for good: free it."""
        if self.busy and self._chat_id not in _CHATS:
            self.busy, self.status = False, ""
        if self.uploading and not _UPLOADS[self.router.session.client_token]:
            self.uploading = ""

    @rx.event
    async def new_chat(self):
        await self._new_chat()

    @rx.event
    async def on_load(self):
        """The page: /assistant?in_app=cad&room=default&role=smart (or app=cad)."""
        query = self.router.url.query_parameters
        await self._open_scope(dict(query))

    @rx.event
    async def open_intent(self, params: dict):
        """Opened from another app as a dialog (the ai.assist intent)."""
        await self._open_scope(params)

    async def _open_scope(self, params: dict) -> None:
        app = str(params.get("in_app") or params.get("app") or "")
        room = str(params.get("room") or "")
        role = str(params.get("role") or self.role)
        if (app, room) != (self.app, self.room):
            await self._new_chat()
        self._unstick()
        self.app, self.room = app, room
        if role in config.ROLES:
            self.role = role

    @rx.event
    async def unlock(self):
        """The hub's passcode, asked before a message goes to the owner's own Claude Code."""
        if config.check_passcode(self.passcode):
            owner = await self.get_state(OwnerState)
            owner.owner_key = config.owner_key()
            self.ask_passcode, self.passcode, self.passcode_note = False, "", ""
            return AssistantState.send
        self.passcode, self.passcode_note = "", "Wrong passcode."

    @rx.event(background=True)
    async def send(self, form_data: dict | None = None):
        """The form's text and the files attached (or, after the passcode, the message kept while
        it was asked)."""
        async with self:
            if form_data is not None:
                self.draft = str(form_data.get("draft") or "")
            text = self.draft.strip()
            if not (text or self.files) or self.busy or self.uploading:
                return
            if _uses_session(self.role):
                if not config.has_passcode():
                    self.messages.append({"kind": "error", "text": "Claude Code on the owner's computer needs a hub passcode first (on the AI Hub page)."})
                    return
                owner = await self.get_state(OwnerState)
                if not config.is_owner(owner.owner_key):
                    self.ask_passcode = True
                    return
            self.draft = ""
            self.busy = True
            files = []
            for f in self.files:
                att = attachments.get(f["id"])
                if att is None:
                    self.messages.append({"kind": "error", "text": f"{f['name']} is no longer on the hub (files are kept {attachments.KEEP_DAYS} days): attach it again."})
                    continue
                files.append(att)
                self.messages.append({"kind": "file", "text": f["name"], "details": f["details"], "thumb": f["thumb"]})
            self.files = []
            if not (text or files):
                self.busy = False
                return
            if text:
                self.messages.append({"kind": "user", "text": text})
            if not self._chat_id or self._chat_id not in _CHATS:
                self._chat_id = uuid.uuid4().hex
            chat_id, role, scope = self._chat_id, self.role, agent.Scope(self.app, self.room)
        chat = _CHATS.get(chat_id)
        if chat is None:
            chat = _CHATS[chat_id] = agent.Chat(role, scope, user="assistant")

        async def show(event: dict) -> None:
            async with self:
                kind = event["type"]
                if kind == "model":
                    self.model = event["text"]
                    chat.user = event["text"]
                elif kind == "text":
                    self.messages.append({"kind": "assistant", "text": event["text"]})
                elif kind == "tool":
                    self.messages.append({"kind": "tool", "text": event["name"]})
                elif kind in ("web", "note", "info"):
                    self.messages.append({"kind": kind, "text": event["text"]})
                elif kind == "result":
                    self.messages.append({"kind": "result" if event["ok"] else "error", "text": event["text"]})
                elif kind == "error":
                    self.messages.append({"kind": "error", "text": event["text"]})
                elif kind == "status":
                    self.status = event["text"]

        try:
            await chat.send(text, show, files)
        except Exception as e:  # noqa: BLE001 — the person sees what went wrong
            await show({"type": "error", "text": f"{type(e).__name__}: {e}"})
        finally:
            async with self:
                self.busy = False
                self.status = ""

    @rx.event
    def close_dialog(self):
        return IntentPage.cancel


class TalkState(rx.State):
    """The talking AI (the ``ai.talk`` intent, or /talk): what was said, and what to say next.

    The page's own code (assets/talk.js) hears and speaks: what it hears it types into the form
    and sends, with what the host page shows now (``context``, asked of the host through the
    intent bridge); what the model answers lands in ``speech`` (a new ``seq`` each time) and the
    page reads it out with the mood the reply began with.
    """

    messages: list[dict[str, str]] = []
    busy: bool = False
    status: str = ""
    role: str = "fast"
    app: str = ""
    room: str = ""
    lang: str = ""
    model: str = ""
    # The reply to read out: {"seq", "text", "mood"}.
    speech: dict[str, str] = {}
    ask_passcode: bool = False
    passcode: str = ""
    passcode_note: str = ""
    _chat_id: str = ""
    _seq: int = 0
    # Said while the model was still answering: goes next, as one message.
    _queued: list[str] = []
    # Kept while the passcode is asked.
    _held: str = ""
    _held_context: str = ""

    @rx.event
    def set_passcode(self, value: str):
        self.passcode = value

    @rx.event
    async def set_role(self, value: str):
        if value in config.ROLES and value != self.role:
            self.role = value
            await self._new_chat()

    async def _new_chat(self) -> None:
        chat = _CHATS.pop(self._chat_id, None)
        if chat is not None:
            await session.drop(chat.id)
        self._chat_id = ""
        self.messages = []
        self.model = ""
        self.ask_passcode = False
        self._queued = []
        self._unstick()

    def _unstick(self) -> None:
        if self.busy and self._chat_id not in _CHATS:
            self.busy, self.status = False, ""

    @rx.event
    async def new_chat(self):
        await self._new_chat()

    @rx.event
    async def on_load(self):
        """The page: /talk?in_app=cad-roam&room=<id>&lang=zh-TW&role=fast."""
        await self._open_scope(dict(self.router.url.query_parameters))

    @rx.event
    async def open_intent(self, params: dict):
        """Opened from another app as a dialog (the ai.talk intent)."""
        await self._open_scope(params)

    async def _open_scope(self, params: dict) -> None:
        app = str(params.get("in_app") or params.get("app") or "")
        room = str(params.get("room") or "")
        role = str(params.get("role") or self.role)
        if (app, room) != (self.app, self.room):
            await self._new_chat()
        self._unstick()
        self.app, self.room = app, room
        self.lang = str(params.get("lang") or self.lang or "")
        if role in config.ROLES:
            self.role = role
        talk.start()

    @rx.event
    async def unlock(self):
        """The hub's passcode, asked before a message goes to the owner's own Claude Code."""
        if config.check_passcode(self.passcode):
            owner = await self.get_state(OwnerState)
            owner.owner_key = config.owner_key()
            self.ask_passcode, self.passcode, self.passcode_note = False, "", ""
            held, context = self._held, self._held_context
            self._held = self._held_context = ""
            return TalkState.send({"draft": held, "context": context})
        self.passcode, self.passcode_note = "", "Wrong passcode."

    def _say(self, text: str, mood: str) -> None:
        self._seq += 1
        self.speech = {"seq": str(self._seq), "text": text, "mood": mood}
        self.messages.append({"kind": "assistant", "text": text, "mood": mood})

    @rx.event(background=True)
    async def send(self, form_data: dict | None = None):
        """The form's words (typed, or heard by the page) and what the host page shows now."""
        async with self:
            form = form_data or {}
            text = str(form.get("draft") or "").strip()
            context = talk.context_text(form.get("context") or "")
            if not text:
                return
            if _uses_session(self.role):
                if not config.has_passcode():
                    self.messages.append({"kind": "error", "text": "Claude Code on the owner's computer needs a hub passcode first (on the AI Hub page)."})
                    return
                owner = await self.get_state(OwnerState)
                if not config.is_owner(owner.owner_key):
                    self._held, self._held_context = text, form.get("context") or ""
                    self.ask_passcode = True
                    return
            self.messages.append({"kind": "user", "text": text})
            if self.busy:
                self._queued.append(text)
                return
            self.busy = True
            self.status = ""
            if not self._chat_id or self._chat_id not in _CHATS:
                self._chat_id = uuid.uuid4().hex
            chat_id, role, scope = self._chat_id, self.role, agent.Scope(self.app, self.room, self.lang)
        chat = _CHATS.get(chat_id)
        if chat is None:
            chat = _CHATS[chat_id] = agent.Chat(role, scope, user="companion", manner="talk")

        async def show(event: dict) -> None:
            async with self:
                kind = event["type"]
                if kind == "model":
                    self.model = event["text"]
                    chat.user = event["text"]
                elif kind == "text":
                    mood, words = talk.mood_of(event["text"])
                    if words:
                        self._say(talk.traditional(words, self.lang), mood)
                elif kind == "tool":
                    self.messages.append({"kind": "tool", "text": event["name"]})
                elif kind in ("web", "note", "info"):
                    self.messages.append({"kind": kind, "text": event["text"]})
                elif kind == "result":
                    self.messages.append({"kind": "result" if event["ok"] else "error", "text": event["text"]})
                elif kind == "error":
                    self.messages.append({"kind": "error", "text": event["text"]})
                elif kind == "status":
                    self.status = event["text"]

        while True:
            try:
                await chat.send(text, show, (), context=context)
            except Exception as e:  # noqa: BLE001 — the person sees what went wrong
                await show({"type": "error", "text": f"{type(e).__name__}: {e}"})
            async with self:
                if self._queued and self._chat_id == chat_id:
                    text, self._queued = " ".join(self._queued), []
                    continue
                self.busy = False
                self.status = ""
                return

    @rx.event
    def close_dialog(self):
        return IntentPage.cancel
