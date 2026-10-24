"""Calls an app's tool for a person: the one way into the apps, for MCP and the assistant alike.

The call goes to the app's backend over the Docker network with a short-lived token signed
with the platform's DDNS_AUTH_SECRET (``reflex_ddns_auth.tools.call_token``), naming the person
the AI acts for. The hub keeps the last calls in memory for its page.
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
    data: Any = None  # the tool's JSON result
    error: str = ""
    image: tuple[str, str] | None = None  # (mime, base64)

    def text(self) -> str:
        """The result as text for an AI (a picture as a note: it travels apart)."""
        if not self.ok:
            return f"Error: {self.error}"
        if self.image is not None:
            return "(a picture, attached)"
        return self.data if isinstance(self.data, str) else json.dumps(self.data, ensure_ascii=False)


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
            data = body.get("result")
            image = None
            if isinstance(data, dict) and data.get(IMAGE_KEY):
                image = (str(data.get("mime") or "image/png"), str(data.get("data") or ""))
                data = None
            result = Result(True, data, image=image)
        else:
            result = Result(False, error=str(body.get("error") or f"HTTP {r.status_code}"))
    except (httpx.HTTPError, ValueError, PermissionError) as e:
        result = Result(False, error=f"the app {tool.app} did not answer: {e}")
    LOG.appendleft(
        Entry(time.time(), via, user, tool.app, tool.name, result.ok, int((time.monotonic() - started) * 1000), result.error)
    )
    return result
