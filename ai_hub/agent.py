"""The hub's assistant: a model that does a task in the apps with their tools.

Ollama and the model APIs only answer; the assistant gives them the apps' tools and runs the
loop: the model asks for tools, the hub calls them (``ai_hub.calls``, for the person who asked),
hands back the results, and asks again, until the model is done. People see each step in their
apps as it happens. ``Chat`` keeps one conversation going across messages. Files the person
attaches go with their message, as what the model can take of them (``ai_hub.attachments``).

A role set to the Claude Code on the owner's computer is the exception: that is an agent with
a loop of its own, so the message goes to it (``ai_hub.session``) and its steps come back.

A ``Chat`` has a manner: ``assist`` (the assistant: does tasks) or ``talk`` (the talking AI,
``ai_hub.talk``: keeps company by voice, in short spoken sentences with a mood). Either way a
message may come with ``context``, what the person's page shows right now (where they are in a
model, say), which goes to the model with the message.
"""

from __future__ import annotations

import asyncio
import dataclasses
import secrets
from typing import Any, Awaitable, Callable, Sequence

from ai_hub import attachments, brains, calls, config, registry, session

MAX_STEPS = 24

SYSTEM = """\
You are the assistant of the AI Hub on a re_ddns host. You do tasks in people's apps with the \
tools you are given; each tool's name starts with its app's name. Do the task with the tools \
instead of describing it: call them right away, and write text only when the task is done (a \
short summary) or when you need something from the person. What you change, the people using \
the app see at once. Before you use an app's tools for the first time, call its guide tool if \
it has one. When a tool reports an error, fix the cause and try again. Write in the language \
the person writes in."""

TALK_SYSTEM = """\
You are a friendly companion who keeps people company while they use their apps on a re_ddns \
host, talking with them by voice. A message may begin with a bracketed line saying what the \
person's page shows right now (where they are, what they look at): take it as what you both see, \
and don't read it back unless asked. Talk as in a conversation: one to three short, plain \
sentences; no lists, no markdown, no code, no addresses; round numbers. Begin every reply with \
one mood tag in square brackets, then your words: [neutral], [happy], [laughing], [surprised], \
[thinking], [sad], [angry] or [wink]. You have the app's tools: use them when they help (to look \
at something, to find out, to do what the person asks), and say in a few words what you found or \
did, without naming the tools (nobody hears "roam.view": say "let me look"). Before you use an \
app's tools for the first time, call its guide tool if it has one. Answer in the language the \
person speaks."""

MANNERS = {"assist": SYSTEM, "talk": TALK_SYSTEM}

Event = dict[str, Any]


def with_context(text: str, context: str) -> str:
    """The person's message with what their page shows now in front of it."""
    context = " ".join((context or "").split())
    return f"[What the person's page shows now: {context}]\n\n{text}" if context else text


@dataclasses.dataclass
class Scope:
    """Where the assistant was opened: an app (its tools only) and that app's context."""

    app: str = ""
    room: str = ""
    # The language of the person's page (e.g. "zh-TW"), when the app says it.
    lang: str = ""

    def note(self) -> str:
        out = ""
        if self.app:
            where = f" in the room '{self.room}'" if self.room else ""
            hint = f" Pass room='{self.room}' to its tools." if self.room else ""
            out += f"\n\nThe person opened you from the app '{self.app}'{where}.{hint}"
        if self.lang.lower() in ("zh-tw", "zh-hant", "zh-hk"):
            out += "\n\nThe person's page is in Traditional Chinese (繁體中文): write Chinese in Traditional characters."
        elif self.lang:
            out += f"\n\nThe person's page is in the language '{self.lang}'."
        return out

    def fill(self, tool: registry.Tool, args: dict[str, Any]) -> dict[str, Any]:
        """The model's arguments, with this room where the tool takes one and the model left it
        out: a tool's own default room is someone else's."""
        if self.room and "room" in (tool.input_schema.get("properties") or {}) and not args.get("room"):
            return {**args, "room": self.room}
        return args


class Chat:
    def __init__(self, role: str, scope: Scope, user: str, manner: str = "assist") -> None:
        self.id = secrets.token_hex(4)
        self.role = role
        self.scope = scope
        self.user = user
        self.manner = manner if manner in MANNERS else "assist"
        self.conversation: brains.Conversation | None = None

    async def _tools(self) -> list[registry.Tool]:
        found = await registry.tools(refresh=True)
        return [t for t in found if not self.scope.app or t.app == self.scope.app]

    async def _guide(self, tools: list[registry.Tool]) -> str:
        """The app's guide (its ``<app>.guide`` tool), read once up front so that even a small
        model starts out knowing the app's rules."""
        guide = next((t for t in tools if t.name == f"{self.scope.app}.guide"), None)
        if guide is None:
            return ""
        result = await calls.call(guide, {}, self.user, via="assistant")
        if not result.ok:
            return ""
        return f"\n\nThe app's guide (read for you already: no need to call {guide.public_name}):\n{result.text()}"

    async def send(
        self,
        text: str,
        on_event: Callable[[Event], Awaitable[None]],
        files: Sequence[attachments.Attachment] = (),
        context: str = "",
    ) -> None:
        """Works on the person's message (and its files) until the model is done, reporting each step."""
        provider, _ = config.resolve(self.role)
        text = with_context(text, context)
        if provider.kind == "session":
            await session.ask(self.id, text, self.scope.app, self.scope.room, on_event, files)
            return
        tools = await self._tools()
        if self.conversation is None:
            guide = await self._guide(tools) if self.scope.app else ""
            self.conversation = await brains.conversation(self.role, MANNERS[self.manner] + self.scope.note() + guide)
            await on_event({"type": "model", "text": self.conversation.label})
        by_name = {t.public_name: t for t in tools}
        parts: list[attachments.Part] = []
        if files:
            await on_event({"type": "status", "text": "Getting the files ready for the model"})
            parts = await asyncio.to_thread(attachments.parts, files, self.conversation.reads_pdf)
            if not self.conversation.sees_images and any(p.image is not None for p in parts):
                await on_event({
                    "type": "info",
                    "text": f"{self.conversation.label} can't see pictures, so it gets only the files' text. "
                    "For pictures, set the role to a model that sees them on the AI Hub page.",
                })
        self.conversation.add_user(text, parts)
        for _ in range(MAX_STEPS):
            step = await self.conversation.step(tools)
            if step.refused:
                await on_event({"type": "error", "text": "The model declined this request."})
                return
            if step.text:
                await on_event({"type": "text", "text": step.text})
            if not step.calls:
                if not step.text:
                    await on_event({"type": "error", "text": "The model stopped without a reply."})
                return
            results = []
            for call in step.calls:
                tool = by_name.get(call.name)
                args = self.scope.fill(tool, call.args) if tool else call.args
                await on_event({"type": "tool", "name": call.name, "app": tool.app if tool else "", "args": args})
                if tool is None:
                    result = calls.Result(False, error=f"there is no tool named {call.name!r}")
                else:
                    result = await calls.call(tool, args, self.user, via="assistant")
                await on_event(
                    {
                        "type": "result",
                        "name": call.name,
                        "ok": result.ok,
                        "text": result.brief(),
                    }
                )
                results.append((call, result))
            self.conversation.add_results(results)
        await on_event({"type": "error", "text": f"Stopped after {MAX_STEPS} steps."})
