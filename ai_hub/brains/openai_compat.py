"""OpenAI and other servers with OpenAI-style chat completions (``/v1/chat/completions``); also
how the hub lists any provider's models, Ollama's included (``/v1/models``)."""

from __future__ import annotations

import json
from typing import Any, Sequence

import httpx

from ai_hub.attachments import Part
from ai_hub.brains import Step, ToolCall, text_only
from ai_hub.calls import Result
from ai_hub.config import Provider
from ai_hub.registry import Tool

TIMEOUT = 300.0
# Ollama models that handle tool calls well, best first (the "coder" builds often lack a tool template).
_TOOL_MODELS = ("qwen3", "qwen2.5", "llama3.1", "llama3.2", "mistral", "gpt-oss")


async def models(provider: Provider) -> list[str]:
    """The models the provider offers ([] when it doesn't answer)."""
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get(f"{provider.base_url}/models", headers=_auth(provider))
            r.raise_for_status()
            return sorted(m["id"] for m in r.json().get("data", []) if m.get("id"))
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        return []


async def pick_model(provider: Provider, role: str) -> str:
    """The provider's model for a role nobody chose a model for."""
    available = [m for m in await models(provider) if "embed" not in m]
    if not available:
        raise LookupError(f"{provider.name} has no models (for Ollama: ollama pull qwen2.5:7b)")
    if role == "coder":
        coders = [m for m in available if "coder" in m]
        if coders:
            return coders[0]
    for family in _TOOL_MODELS:
        for m in available:
            if m.startswith(family) and "coder" not in m:
                return m
    return available[0]


def _auth(provider: Provider) -> dict[str, str]:
    return {"Authorization": f"Bearer {provider.api_key}"} if provider.api_key else {}


def _function(t: Tool) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": t.public_name, "description": f"{t.description}\n\nApp: {t.app}", "parameters": t.input_schema},
    }


class OpenAICompatConversation:
    sees_images = True
    reads_pdf = False

    def __init__(self, provider: Provider, model: str, system: str) -> None:
        self.provider = provider
        self.model = model
        self.label = f"{'Ollama' if provider.local else provider.name} ({model})"
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system}]

    def add_user(self, text: str, parts: Sequence[Part] = ()) -> None:
        if not parts:
            self.messages.append({"role": "user", "content": text})
            return
        content: list[dict[str, Any]] = []
        for p in parts:  # the files first, what the person asks after them
            if p.image is not None:
                content.append({"type": "image_url", "image_url": {"url": f"data:{p.image[0]};base64,{p.image[1]}"}})
            elif p.text:
                content.append({"type": "text", "text": p.text})
        if text:
            content.append({"type": "text", "text": text})
        self.messages.append({"role": "user", "content": content})

    async def step(self, tools: list[Tool]) -> Step:
        body: dict[str, Any] = {"model": self.model, "messages": self.messages}
        if tools:
            body["tools"] = [_function(t) for t in tools]
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(f"{self.provider.base_url}/chat/completions", json=body, headers=_auth(self.provider))
        if r.status_code >= 400:
            raise RuntimeError(f"{self.label}: HTTP {r.status_code}: {r.text[:300]}")
        message = r.json()["choices"][0]["message"]
        self.messages.append({k: v for k, v in message.items() if k in ("role", "content", "tool_calls")})
        calls = []
        for c in message.get("tool_calls") or []:
            raw = c.get("function", {}).get("arguments") or "{}"
            try:
                args = json.loads(raw) if isinstance(raw, str) else dict(raw)
            except ValueError:
                args = {}
            calls.append(ToolCall(str(c.get("id") or c["function"]["name"]), c["function"]["name"], args))
        return Step(text=message.get("content") or "", calls=calls, model=self.model)

    def add_results(self, results: list[tuple[ToolCall, Result]]) -> None:
        for call, result in results:
            self.messages.append({"role": "tool", "tool_call_id": call.id, "content": text_only(result)})
