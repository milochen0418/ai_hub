"""The MCP address, spoken to with the official MCP client (in-process; the apps are faked)."""

from __future__ import annotations

import base64

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.routing import Route

from ai_hub import calls, config, mcp_endpoint, registry

TOOLS = [
    registry.Tool(
        app="cad",
        name="cad.set_code",
        title="Write the room's code",
        description="Replaces the code in the room's editor.",
        kind="write",
        input_schema={
            "type": "object",
            "properties": {"code": {"type": "string"}, "room": {"type": "string", "default": "default"}},
            "required": ["code"],
            "additionalProperties": False,
        },
        backend="http://cad",
    ),
    registry.Tool(
        app="cad",
        name="cad.snapshot",
        title="Look at the model",
        description="A picture of the model.",
        kind="read",
        input_schema={"type": "object", "properties": {}, "required": []},
        backend="http://cad",
    ),
]


@pytest.fixture
def seen(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_HUB_DATA", str(tmp_path))
    log: list = []

    async def fake_tools(refresh: bool = False):
        return TOOLS

    async def fake_call(tool, args, user, via):
        log.append((tool.name, args, user, via))
        if tool.name == "cad.snapshot":
            return calls.Result(True, image=("image/png", base64.b64encode(b"\x89PNG").decode()))
        return calls.Result(True, {"room": args.get("room", "default"), "lines": 1})

    monkeypatch.setattr(registry, "tools", fake_tools)
    monkeypatch.setattr(calls, "call", fake_call)
    return log


def client_for(token: str | None) -> httpx2.AsyncClient:
    app = Starlette(routes=[Route(mcp_endpoint.PATH, mcp_endpoint.endpoint)])
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://hub", headers=headers)


async def test_an_agent_lists_and_calls_the_apps_tools(seen) -> None:
    token = config.create_token("Milo's Claude Code")
    async with mcp_endpoint.lifespan():
        http = client_for(token)
        async with Client(streamable_http_client("http://hub/api/mcp", http_client=http)) as client:
            listed = await client.list_tools()
            by_name = {t.name: t for t in listed.tools}
            assert set(by_name) == {"cad_set_code", "cad_snapshot"}
            assert by_name["cad_set_code"].annotations.read_only_hint is False
            assert by_name["cad_snapshot"].annotations.read_only_hint is True
            assert "App: cad" in by_name["cad_set_code"].description

            result = await client.call_tool("cad_set_code", {"code": "x = 1", "room": "r1"})
            assert result.is_error is False
            assert result.structured_content == {"room": "r1", "lines": 1}

            picture = await client.call_tool("cad_snapshot", {})
            assert picture.content[0].type == "image" and picture.content[0].mime_type == "image/png"

            missing = await client.call_tool("grid_nope", {})
            assert missing.is_error is True

    assert seen[0] == ("cad.set_code", {"code": "x = 1", "room": "r1"}, "Milo's Claude Code", "mcp")


@pytest.mark.parametrize("token", [None, "aihub_not-a-real-token", "nonsense"])
async def test_without_a_hub_token_nothing_is_served(seen, token) -> None:
    config.create_token("someone")
    async with mcp_endpoint.lifespan():
        async with client_for(token) as http:
            r = await http.post(
                "/api/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"Accept": "application/json, text/event-stream"},
            )
    assert r.status_code == 401
    assert seen == []


async def test_a_deleted_token_stops_working(seen) -> None:
    token = config.create_token("temp")
    config.delete_token(config.tokens()[0]["id"])
    assert config.who(token) is None
