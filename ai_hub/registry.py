"""The installed apps, the tools they offer an AI (``reflex_ddns_auth.tools`` manifests), and
what they ask the hub's models for (``reflex_ddns_auth.ai.uses``).

Apps are found the way re-ddns finds intent providers: its service list
(``$RE_DDNS_API_URL/api/service/list``) gives each app's backend on the Docker network, where
``/_ddns_tool/manifest`` lists the app's tools and, as ``ai_uses``, what it asks each role
for in its own words. ``AI_HUB_APPS`` (``cad=http://localhost:8061,
grid=http://localhost:8062``) replaces the list, for running outside the platform. The answer
is cached for a few seconds, so a newly installed app's tools show up by themselves.

Outside the apps, a tool is known by its *public name*, its name with dots turned into
underscores (``cad.set_code`` -> ``cad_set_code``): MCP clients and model APIs allow only
letters, digits, ``_`` and ``-``.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import os
import re
import time
from typing import Any

import httpx

logger = logging.getLogger(__name__)

MANIFEST_PATH = "/_ddns_tool/manifest"
_CACHE_SECONDS = 10.0
_TIMEOUT = 3.0
_SELF = os.environ.get("SERVICE_SUBDOMAIN", "ai")


@dataclasses.dataclass(frozen=True)
class App:
    name: str  # subdomain
    backend: str  # base URL of its backend


@dataclasses.dataclass(frozen=True)
class Tool:
    app: str
    name: str
    title: str
    description: str
    kind: str  # read | write | danger
    input_schema: dict[str, Any]
    backend: str

    @property
    def public_name(self) -> str:
        return public_name(self.name)


def public_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", name)[:64]


# (when, every app's tools, each app's {role: what it asks it for})
_cache: tuple[float, list[Tool], dict[str, dict[str, str]]] = (0.0, [], {})
_lock = asyncio.Lock()


def _configured_apps() -> list[App] | None:
    spec = os.environ.get("AI_HUB_APPS", "").strip()
    if not spec:
        return None
    apps = []
    for part in spec.split(","):
        name, _, url = part.strip().partition("=")
        if name and url:
            apps.append(App(name.strip(), url.strip().rstrip("/")))
    return apps


async def apps(client: httpx.AsyncClient) -> list[App]:
    configured = _configured_apps()
    if configured is not None:
        return configured
    registry = (os.environ.get("RE_DDNS_API_URL") or "").rstrip("/")
    if not registry:
        return []
    r = await client.get(f"{registry}/api/service/list")
    r.raise_for_status()
    out = []
    for s in r.json():
        if s.get("upstream_host") and s.get("subdomain") and s["subdomain"] != _SELF:
            out.append(App(s["subdomain"], f"http://{s['upstream_host']}:{s.get('backend_port', 8000)}"))
    return out


async def _manifest(client: httpx.AsyncClient, app: App) -> tuple[list[Tool], dict[str, str]]:
    """The app's tools, and what it asks each role for."""
    try:
        r = await client.get(app.backend + MANIFEST_PATH)
        if r.status_code != 200:
            return [], {}
        m = r.json()
    except (httpx.HTTPError, ValueError):
        return [], {}
    uses = m.get("ai_uses")
    said = {str(role): str(words)[:200] for role, words in uses.items() if words} if isinstance(uses, dict) else {}
    out = []
    for t in m.get("tools") or []:
        try:
            out.append(
                Tool(
                    app=app.name,
                    name=str(t["name"]),
                    title=str(t.get("title") or t["name"]),
                    description=str(t.get("description") or ""),
                    kind=str(t.get("kind") or "read"),
                    input_schema=dict(t.get("input_schema") or {"type": "object", "properties": {}}),
                    backend=app.backend,
                )
            )
        except (KeyError, TypeError, ValueError):
            logger.warning("Skipping a malformed tool of %s: %r", app.name, t)
    return out, said


async def _manifests(refresh: bool) -> tuple[float, list[Tool], dict[str, dict[str, str]]]:
    global _cache
    if not refresh and time.monotonic() - _cache[0] < _CACHE_SECONDS:
        return _cache
    async with _lock:
        if not refresh and time.monotonic() - _cache[0] < _CACHE_SECONDS:
            return _cache
        try:
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                found = await apps(client)
                manifests = await asyncio.gather(*(_manifest(client, a) for a in found))
        except (httpx.HTTPError, ValueError) as e:
            logger.warning("Cannot list the apps: %s", e)
            found, manifests = [], []
        result = [t for tools_of_app, _ in manifests for t in tools_of_app]
        uses = {a.name: said for a, (_, said) in zip(found, manifests) if said}
        _cache = (time.monotonic(), result, uses)
        return _cache


async def tools(refresh: bool = False) -> list[Tool]:
    """Every tool of every installed app (best effort: an app that doesn't answer has none)."""
    return (await _manifests(refresh))[1]


async def ai_uses(refresh: bool = False) -> dict[str, dict[str, str]]:
    """What each installed app asks the hub's models for: ``{app: {role: its words}}``."""
    return (await _manifests(refresh))[2]


async def find(name: str) -> Tool | None:
    """A tool by its public name (or its own name)."""
    for t in await tools():
        if name in (t.public_name, t.name):
            return t
    return None
