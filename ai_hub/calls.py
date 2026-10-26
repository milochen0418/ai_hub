"""Calls an app's tool for a person: the one way into the apps, for MCP and the assistant alike.

The call goes to the app's backend over the Docker network with a short-lived token signed
with the platform's DDNS_AUTH_SECRET (``reflex_ddns_auth.tools.call_token``), naming the person
the AI acts for. The hub keeps the last calls in memory for its page.

A tool answers with JSON, a picture (``reflex_ddns_auth.tools.image``), or JSON with pictures in
it (CAD Roam's ``{"picture": image(...), "light_here": 0.04, ...}``, or a list of them): the
pictures travel to the AI as pictures, each left in the words as "(picture N)".
"""

from __future__ import annotations

import collections
import dataclasses
import json
import time
from typing import Any

import httpx
from reflex_ddns_auth.tools import IMAGE_KEY, call_token

from ai_hub.registry import Tool

TIMEOUT = 240.0  # a build may take its minute, a tool may type for a while


@dataclasses.dataclass
class Result:
    ok: bool
    data: Any = None  # the tool's JSON result, its pictures taken out (None: it was one picture)
    error: str = ""
    images: list[tuple[str, str]] = dataclasses.field(default_factory=list)  # (mime, base64), in order

    @property
    def image(self) -> tuple[str, str] | None:
        """The first picture, if any."""
        return self.images[0] if self.images else None

    def text(self) -> str:
        """The result as text for an AI (pictures as notes: they travel apart)."""
        if not self.ok:
            return f"Error: {self.error}"
        if self.data is None and self.images:
            return "(a picture, attached)" if len(self.images) == 1 else f"({len(self.images)} pictures, attached)"
        return self.data if isinstance(self.data, str) else json.dumps(self.data, ensure_ascii=False)

    def brief(self, limit: int = 300) -> str:
        """What came back, in short, for the chat."""
        if self.ok and self.images:
            return "a picture" if len(self.images) == 1 else f"{len(self.images)} pictures"
        return self.text()[:limit]


def take_pictures(data: Any) -> tuple[Any, list[tuple[str, str]]]:
    """A tool's result with its pictures taken out, wherever they are in it (each one left as
    "(picture N)"), and the pictures; a result that is one picture leaves None."""
    images: list[tuple[str, str]] = []

    def out(value: Any) -> Any:
        if isinstance(value, dict):
            if value.get(IMAGE_KEY):
                images.append((str(value.get("mime") or "image/png"), str(value.get("data") or "")))
                return f"(picture {len(images)})"
            return {k: out(v) for k, v in value.items()}
        if isinstance(value, list):
            return [out(v) for v in value]
        return value

    rest = out(data)
    return (None if isinstance(data, dict) and data.get(IMAGE_KEY) else rest), images


@dataclasses.dataclass(frozen=True)
class Entry:
    at: float
    via: str  # "mcp" or "assistant"
    user: str
    app: str
    tool: str
    ok: bool
    ms: int
    error: str


LOG: collections.deque[Entry] = collections.deque(maxlen=200)


async def call(tool: Tool, args: dict[str, Any], user: str, via: str) -> Result:
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            r = await client.post(
                f"{tool.backend}/_ddns_tool/call/{tool.name}",
                json={"args": args or {}},
                headers={"Authorization": f"Bearer {call_token(tool.app, user)}"},
            )
        body = r.json()
        if body.get("ok"):
            data, images = take_pictures(body.get("result"))
            result = Result(True, data, images=images)
        else:
            result = Result(False, error=str(body.get("error") or f"HTTP {r.status_code}"))
    except (httpx.HTTPError, ValueError, PermissionError) as e:
        result = Result(False, error=f"the app {tool.app} did not answer: {e}")
    LOG.appendleft(
        Entry(time.time(), via, user, tool.app, tool.name, result.ok, int((time.monotonic() - started) * 1000), result.error)
    )
    return result
