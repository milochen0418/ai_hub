"""Claude, through the Anthropic SDK (Messages API).

The conversation keeps Claude's own content blocks and sends them back unchanged, thinking
blocks included, as the API asks. Requests opt into the server-side fallback
(``fallbacks="default"``): when Claude declines, the API itself retries on the model Anthropic
recommends for that kind of decline. A picture a tool returns goes to Claude as an image, so it
sees what it built; so do the pictures of the files the person attaches, and a PDF goes as a
document.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Sequence

import anthropic

from ai_hub.attachments import Part
from ai_hub.brains import Step, ToolCall
from ai_hub.calls import Result
from ai_hub.config import Provider
from ai_hub.registry import Tool

MAX_TOKENS = 16000
EFFORT = "high"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Blocks the model produced internally: after a fallback, those before it aren't sent back.
_INTERNAL = {"thinking", "redacted_thinking", "tool_use"}


def _tool(t: Tool) -> dict[str, Any]:
    return {"name": t.public_name, "description": f"{t.description}\n\nApp: {t.app}", "input_schema": t.input_schema}


def _block(part: Part) -> dict[str, Any]:
    if part.image is not None:
        mime, data = part.image
        return {"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}}
    if part.pdf:
        return {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": part.pdf}}
    return {"type": "text", "text": part.text}


def _echo(content: list[Any]) -> list[Any]:
    """The assistant's content to send back: as it came, except that after a mid-output
    fallback the declined model's internal blocks before the switch are left out."""
    last = max((i for i, b in enumerate(content) if b.type == "fallback"), default=-1)
    if last < 0:
        return list(content)
    return [b for i, b in enumerate(content) if i > last or (i < last and b.type not in _INTERNAL)]


class ClaudeConversation:
    sees_images = True
    reads_pdf = True

    def __init__(self, provider: Provider, model: str, system: str) -> None:
        self.client = anthropic.AsyncAnthropic(api_key=provider.api_key)
        self.model = model
        self.label = f"Claude ({model})"
        self.system = system
        self.messages: list[dict[str, Any]] = []

    def add_user(self, text: str, parts: Sequence[Part] = ()) -> None:
        if not parts:
            self.messages.append({"role": "user", "content": text})
            return
        # The files first, what the person asks after them.
        content = [_block(p) for p in parts] + ([{"type": "text", "text": text}] if text else [])
        self.messages.append({"role": "user", "content": content})

    async def step(self, tools: list[Tool]) -> Step:
        extra: dict[str, Any] = {"tools": [_tool(t) for t in tools]} if tools else {}
        while True:
            response = await self.client.beta.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=self.system,
                messages=self.messages,
                output_config={"effort": EFFORT},
                betas=[FALLBACK_BETA],
                fallbacks="default",
                **extra,
            )
            if response.stop_reason == "refusal":
                # Declined, fallback included: nothing to keep from this turn.
                return Step(refused=True, model=response.model)
            self.messages.append({"role": "assistant", "content": _echo(response.content)})
            if response.stop_reason == "pause_turn":
                continue
            break
        text = "\n".join(b.text for b in response.content if b.type == "text").strip()
        calls = [ToolCall(b.id, b.name, dict(b.input or {})) for b in response.content if b.type == "tool_use"]
        return Step(text=text, calls=calls, model=response.model)

    def add_results(self, results: list[tuple[ToolCall, Result]]) -> None:
        blocks = []
        for call, result in results:
            if result.ok and result.images:
                content: Any = [{"type": "text", "text": result.text()}] if result.data is not None else []
                content += [{"type": "image", "source": {"type": "base64", "media_type": mime, "data": data}} for mime, data in result.images]
            else:
                content = result.text()
            blocks.append({"type": "tool_result", "tool_use_id": call.id, "content": content, "is_error": not result.ok})
        # All results of one turn go back in one message.
        self.messages.append({"role": "user", "content": blocks})


async def stream_text(provider: Provider, model: str, system: str, messages: list[dict[str, str]], max_tokens: int, effort: str, answer: Any) -> AsyncIterator[str]:
    """One answer for an app (``ai_hub.brains.answer``), as Claude writes it; sets
    ``answer.stop``. The rules are cached between an app's turns (they repeat)."""
    client = anthropic.AsyncAnthropic(api_key=provider.api_key)
    extra: dict[str, Any] = {"system": system} if system else {}
    async with client.beta.messages.stream(
        model=model,
        max_tokens=max_tokens,
        messages=messages,
        output_config={"effort": effort},
        cache_control={"type": "ephemeral"},
        betas=[FALLBACK_BETA],
        fallbacks="default",
        **extra,
    ) as stream:
        async for text in stream.text_stream:
            yield text
        final = await stream.get_final_message()
    answer.stop = final.stop_reason if final.stop_reason in ("refusal", "max_tokens") else "end_turn"
