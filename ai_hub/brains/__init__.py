"""The AI models the hub talks to, behind one small interface for the assistant's loop.

A ``Conversation`` keeps a chat in its provider's own format, so nothing a provider needs back
is lost between turns (Claude's thinking blocks, for one): ``add_user`` a message, ``step``
once (the model answers and may ask for tools), ``add_results`` of the tools it asked for, and
step again. ``conversation(role)`` picks the provider and model the role is set to
(``ai_hub.config``).

- ``ollama``: Ollama, through its own chat API.
- ``openai_compat``: OpenAI and anything else with OpenAI-style chat completions.
- ``claude``: Claude, through the Anthropic SDK.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Protocol

from ai_hub import config
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

    def add_user(self, text: str) -> None: ...

    async def step(self, tools: list[Tool]) -> Step: ...

    def add_results(self, results: list[tuple[ToolCall, Result]]) -> None: ...


def text_only(result: Result) -> str:
    """A tool's result for a conversation that takes no pictures (only Claude's does)."""
    if result.ok and result.image is not None:
        return "(The tool returned a picture, but pictures can't be shown to you here.)"
    return result.text()


async def conversation(role: str, system: str) -> Conversation:
    """A new conversation with the model ``role`` is set to."""
    provider, model = config.resolve(role)
    if provider.kind == "anthropic":
        from ai_hub.brains.claude import ClaudeConversation

        return ClaudeConversation(provider, model or config.CLAUDE_MODEL, system)
    from ai_hub.brains.openai_compat import OpenAICompatConversation, pick_model

    model = model or await pick_model(provider, role)
    if provider.kind == "ollama":
        from ai_hub.brains.ollama import OllamaConversation

        return OllamaConversation(provider, model, system)
    return OpenAICompatConversation(provider, model, system)
