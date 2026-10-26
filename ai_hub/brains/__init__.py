"""The AI models the hub talks to, behind one small interface for the assistant's loop.

A ``Conversation`` keeps a chat in its provider's own format, so nothing a provider needs back
is lost between turns (Claude's thinking blocks, for one): ``add_user`` a message (with what the
model is shown of its files, ``ai_hub.attachments``), ``step`` once (the model answers and may
ask for tools), ``add_results`` of the tools it asked for, and step again. ``conversation(role)``
picks the provider and model the role is set to (``ai_hub.config``).

- ``ollama``: Ollama, through its own chat API.
- ``openai_compat``: OpenAI and anything else with OpenAI-style chat completions.
- ``claude``: Claude, through the Anthropic SDK.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Protocol, Sequence

from ai_hub import config
from ai_hub.attachments import Part
from ai_hub.calls import Result
from ai_hub.registry import Tool


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
