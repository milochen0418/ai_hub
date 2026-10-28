"""The AI models the hub talks to, behind one small interface for the assistant's loop.

A ``Conversation`` keeps a chat in its provider's own format, so nothing a provider needs back
is lost between turns (Claude's thinking blocks, for one): ``add_user`` a message (with what the
model is shown of its files, ``ai_hub.attachments``), ``step`` once (the model answers and may
ask for tools), ``add_results`` of the tools it asked for, and step again. ``conversation(role)``
picks the provider and model the role is set to (``ai_hub.config``).

- ``ollama``: Ollama, through its own chat API.
- ``openai_compat``: OpenAI and anything else with OpenAI-style chat completions.
- ``claude``: Claude, through the Anthropic SDK.

For the apps (``ai_hub.ask``), which bring their own rules and keep their own conversations,
``answer(role, ...)`` streams one answer without tools: an ``Answer`` with the model's label,
its text as it comes, and how it stopped.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any, AsyncIterator, Protocol, Sequence

from ai_hub import config
from ai_hub.attachments import Part
from ai_hub.calls import Result
from ai_hub.registry import Tool

logger = logging.getLogger(__name__)


@dataclasses.dataclass(frozen=True)
class ToolCall:
    id: str
    name: str  # the tool's public name
    args: dict[str, Any]


@dataclasses.dataclass
class Step:
    text: str = ""
    calls: list[ToolCall] = dataclasses.field(default_factory=list)
    refused: bool = False  # the model declined (and no fallback took over)
    model: str = ""


class Conversation(Protocol):
    label: str  # e.g. "Claude (claude-opus-5-5)"
    sees_images: bool  # pictures reach the model (else it gets only the text of the parts)
    reads_pdf: bool  # it takes a PDF as it is (``Part.pdf``)

    def add_user(self, text: str, parts: Sequence[Part] = ()) -> None: ...

    async def step(self, tools: list[Tool]) -> Step: ...

    def add_results(self, results: list[tuple[ToolCall, Result]]) -> None: ...


def text_only(result: Result) -> str:
    """A tool's result for a conversation that takes no pictures (only Claude's does)."""
    if result.ok and result.images:
        note = "(The tool returned a picture, but pictures can't be shown to you here.)"
        if len(result.images) > 1:
            note = f"(The tool returned {len(result.images)} pictures, but pictures can't be shown to you here.)"
        return note if result.data is None else f"{result.text()}\n{note}"
    return result.text()


def user_text(text: str, parts: Sequence[Part], sees_images: bool) -> str:
    """The message as one text: the text of its files' parts, then what the person wrote (and,
    for a model that can't see them, a note on the pictures left out)."""
    texts = [p.text for p in parts if p.text]
    left_out = 0 if sees_images else sum(1 for p in parts if p.image is not None)
    if left_out:
        texts.append(f"({left_out} picture{'s' if left_out > 1 else ''} of these files can't be shown to you here.)")
    return "\n\n".join([*texts, text]).strip()


async def conversation(role: str, system: str) -> Conversation:
    """A new conversation with the model ``role`` is set to."""
    provider, model = config.resolve(role)
    if provider.kind == "anthropic":
        from ai_hub.brains.claude import ClaudeConversation

        return ClaudeConversation(provider, model or config.CLAUDE_MODEL, system)
    from ai_hub.brains.openai_compat import OpenAICompatConversation, pick_model

    model = model or await pick_model(provider, role)
    if provider.kind == "ollama":
        from ai_hub.brains.ollama import OllamaConversation, sees_images

        return OllamaConversation(provider, model, system, await sees_images(provider, model))
    return OpenAICompatConversation(provider, model, system)


# ------------------------------------------------------------ one answer for an app


@dataclasses.dataclass
class Answer:
    label: str  # e.g. "Claude (claude-opus-5-5)"
    text: AsyncIterator[str]  # the answer as it is written
    stop: str = "end_turn"  # "end_turn", "max_tokens" or "refusal", once the text is done


async def describe(role: str, app: str | None = None, user: str = "") -> tuple[config.Provider, str, str]:
    """``(provider, model, label)`` for a role: what would answer it now. For an app (``app``:
    its name, "" when unknown; None is the hub's own assistant), the model chosen for that app's
    role on the hub's page comes first (``config.app_target``; for ``local``, only one on this
    machine). The Claude Code on the owner's computer answers an app's ``smart`` and ``coder``
    asks when a role is set to it (the hub's owner decided so for everyone using the app); a
    quick role on it gets the role's default model instead (Claude with a key, else Ollama).
    ``user`` is who asks, for the log."""
    provider, model = config.resolve(role)
    if app is not None:
        chosen = config.app_target(app, role) if app else ""
        if chosen:
            try:
                provider, model = config.resolve(chosen, for_role=role)
            except LookupError as e:  # its key is gone, or it would take local data away: the role's own model, as if nothing was chosen
                logger.warning("The model chosen for %s's %s can't answer (%s): using the role's", app, role, e)
        if provider.kind == "session" and role in config.ROLES and role not in config.SESSION_ROLES:
            provider, model = config.resolve(config.default_target(role))
    if provider.kind == "anthropic":
        model = model or config.CLAUDE_MODEL
        return provider, model, f"Claude ({model})"
    if provider.kind == "session":
        from ai_hub.session import LABEL

        return provider, "", LABEL
    from ai_hub.brains.openai_compat import pick_model

    model = model or await pick_model(provider, role)
    return provider, model, f"{'Ollama' if provider.local else provider.name} ({model})"


async def answer(role: str, system: str, messages: list[dict[str, str]], max_tokens: int, effort: str, app: str = "", user: str = "") -> Answer:
    """One streamed answer, with the app's own rules and conversation, from the model the role
    is set to (or the one chosen for ``app``'s role on the hub's page), the Claude Code on the
    owner's computer included."""
    provider, model, label = await describe(role, app, user)
    out = Answer(label, text=_nothing())
    if provider.kind == "session":
        out.text = _session_text(system, messages, app, out)
        return out
    if provider.kind == "anthropic":
        from ai_hub.brains.claude import stream_text
    elif provider.kind == "ollama":
        from ai_hub.brains.ollama import stream_text
    else:
        from ai_hub.brains.openai_compat import stream_text
    out.text = stream_text(provider, model, system, messages, max_tokens, effort, out)
    return out


async def _nothing() -> AsyncIterator[str]:
    return
    yield


def as_one_message(system: str, messages: list[dict[str, str]], app: str) -> str:
    """An app's rules and conversation as one message for the Claude Code on the owner's
    computer, which takes a message at a time and answers through its channel."""
    parts = []
    if system:
        parts.append(f"[Rules for this answer, from the app {app or 'unknown'!r}; answer with the channel's reply as always]\n{system}")
    earlier = messages[:-1]
    if earlier:
        turns = "\n\n".join(("The person: " if m["role"] == "user" else "You: ") + m["content"] for m in earlier)
        parts.append(f"[Earlier in this conversation]\n{turns}")
    parts.append(f"[The person now says]\n{messages[-1]['content']}" if earlier or system else messages[-1]["content"])
    return "\n\n".join(parts)


async def _session_text(system: str, messages: list[dict[str, str]], app: str, out: Answer) -> AsyncIterator[str]:
    """The owner's Claude Code's answer to an app, whole (it doesn't stream)."""
    import secrets

    from ai_hub import session

    said: list[str] = []
    errors: list[str] = []

    async def on_event(event: dict[str, Any]) -> None:
        if event.get("type") == "text" and event.get("text"):
            said.append(str(event["text"]))
        elif event.get("type") == "error":
            errors.append(str(event.get("text") or "it failed"))

    await session.ask(f"app-{secrets.token_hex(3)}", as_one_message(system, messages, app), app, "", on_event)
    if errors and not said:
        raise RuntimeError(errors[-1])
    if said:
        yield "\n\n".join(said)
