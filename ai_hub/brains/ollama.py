"""Ollama, through its own chat API (``/api/chat``).

Its OpenAI-style endpoint can't set the context size, so a model there runs with Ollama's
default of 4096 tokens: an app's guide and tools fill most of that, and after a few steps Ollama
drops the start of the chat (the person's request too) and cuts the model's answers short. Here
every request asks for ``NUM_CTX`` tokens (Ollama caps it at what the model supports).

Pictures of attached files go only to models that can see (Ollama says so: ``vision``); the
others get the files' text.
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncIterator, Sequence

import httpx

from ai_hub.attachments import Part
from ai_hub.brains import Step, ToolCall, text_only, user_text
from ai_hub.brains.openai_compat import TIMEOUT, _function
from ai_hub.calls import Result
from ai_hub.config import Provider
from ai_hub.registry import Tool

NUM_CTX = int(os.environ.get("AI_HUB_OLLAMA_NUM_CTX") or 16384)


async def sees_images(provider: Provider, model: str) -> bool:
    """Whether the model takes pictures (False when Ollama doesn't say)."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.post(provider.base_url.removesuffix("/v1") + "/api/show", json={"model": model})
            r.raise_for_status()
            return "vision" in (r.json().get("capabilities") or [])
    except (httpx.HTTPError, ValueError, AttributeError):
        return False


class OllamaConversation:
    reads_pdf = False

    def __init__(self, provider: Provider, model: str, system: str, vision: bool = False) -> None:
        self.url = provider.base_url.removesuffix("/v1") + "/api/chat"
        self.model = model
        self.label = f"Ollama ({model})"
        self.sees_images = vision
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system}]

    def add_user(self, text: str, parts: Sequence[Part] = ()) -> None:
        message: dict[str, Any] = {"role": "user", "content": user_text(text, parts, self.sees_images)}
        images = [p.image[1] for p in parts if p.image is not None]
        if images and self.sees_images:
            message["images"] = images
        self.messages.append(message)

    async def step(self, tools: list[Tool]) -> Step:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": self.messages,
            "stream": False,
            "options": {"num_ctx": NUM_CTX},
        }
        if tools:
            body["tools"] = [_function(t) for t in tools]
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(self.url, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"{self.label}: HTTP {r.status_code}: {r.text[:300]}")
        message = r.json()["message"]
        # Thinking models (qwen3, gpt-oss) want their thinking back with their tool calls.
        self.messages.append({k: v for k, v in message.items() if k in ("role", "content", "thinking", "tool_calls")})
        calls = []
        for i, c in enumerate(message.get("tool_calls") or []):
            name = c["function"]["name"]
            args = c["function"].get("arguments") or {}
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except ValueError:
                    args = {}
            calls.append(ToolCall(str(c.get("id") or f"{name}-{i}"), name, dict(args)))
        return Step(text=message.get("content") or "", calls=calls, model=self.model)

    def add_results(self, results: list[tuple[ToolCall, Result]]) -> None:
        for call, result in results:
            self.messages.append({"role": "tool", "tool_name": call.name, "content": text_only(result)})


async def stream_text(provider: Provider, model: str, system: str, messages: list[dict[str, str]], max_tokens: int, effort: str, answer: Any) -> AsyncIterator[str]:
    """One answer for an app (``ai_hub.brains.answer``), as the model writes it; sets
    ``answer.stop``. ``effort`` means nothing to Ollama."""
    body: dict[str, Any] = {
        "model": model,
        "messages": ([{"role": "system", "content": system}] if system else []) + list(messages),
        "stream": True,
        "keep_alive": "30m",
        "options": {"num_ctx": NUM_CTX, "num_predict": max_tokens},
    }
    url = provider.base_url.removesuffix("/v1") + "/api/chat"
    async with httpx.AsyncClient(timeout=httpx.Timeout(TIMEOUT, connect=5.0)) as client:
        async with client.stream("POST", url, json=body) as r:
            if r.status_code >= 400:
                raise RuntimeError(f"HTTP {r.status_code}: {(await r.aread())[:300]!r}")
            async for line in r.aiter_lines():
                if not line.strip():
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if data.get("error"):
                    raise RuntimeError(str(data["error"]))
                piece = (data.get("message") or {}).get("content") or ""
                if piece:
                    yield piece
                if data.get("done"):
                    answer.stop = "max_tokens" if data.get("done_reason") == "length" else "end_turn"
                    break
