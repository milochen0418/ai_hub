"""The MCP address, spoken to with the official MCP client (in-process; the apps are faked)."""

from __future__ import annotations

import base64
import json

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.routing import Route

from reflex_ddns_auth.tools import IMAGE_KEY

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
        app="cad-roam",
        name="roam.tour",
        title="Look from several places",
        description="Pictures from inside a model.",
        kind="read",
        input_schema={"type": "object", "properties": {}, "required": []},
        backend="http://cad-roam",
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


def picture(data: bytes, mime: str = "image/jpeg") -> dict:
    return {IMAGE_KEY: True, "mime": mime, "data": base64.b64encode(data).decode()}


# What CAD Roam's roam.tour answers: words with pictures in them.
TOUR = {"time": "7:30", "stops": [{"name": "hall", "picture": picture(b"one"), "eyes": 3.2}, {"name": "stairs", "picture": picture(b"two"), "eyes": 1.0}]}


@pytest.fixture
def seen(tmp_path, monkeypatch):
    monkeypatch.setenv("AI_HUB_DATA", str(tmp_path))
    log: list = []

    async def fake_tools(refresh: bool = False):
        return TOOLS

    async def fake_call(tool, args, user, via):
        log.append((tool.name, args, user, via))
        if tool.name == "cad.snapshot":
            return calls.Result(True, images=[("image/png", base64.b64encode(b"\x89PNG").decode())])
        if tool.name == "roam.tour":
            rest, images = calls.take_pictures(TOUR)
            return calls.Result(True, rest, images=images)
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
            assert set(by_name) == {"cad_set_code", "cad_snapshot", "roam_tour"}
            assert by_name["cad_set_code"].annotations.read_only_hint is False
            assert by_name["cad_snapshot"].annotations.read_only_hint is True
            # Claude Code reads a long answer (a big model's code) instead of saving it to a file
            assert by_name["cad_set_code"].meta == {"anthropic/maxResultSizeChars": 500_000}
            assert "App: cad" in by_name["cad_set_code"].description

            result = await client.call_tool("cad_set_code", {"code": "x = 1", "room": "r1"})
            assert result.is_error is False
            assert result.structured_content == {"room": "r1", "lines": 1}

            picture = await client.call_tool("cad_snapshot", {})
            assert [c.type for c in picture.content] == ["image"] and picture.content[0].mime_type == "image/png"

            # Words with pictures in them: the words (each picture named where it was), then the pictures.
            tour = await client.call_tool("roam_tour", {})
            assert [c.type for c in tour.content] == ["text", "image", "image"] and tour.is_error is False
            words = json.loads(tour.content[0].text)
            assert words["stops"][0] == {"name": "hall", "picture": "(picture 1)", "eyes": 3.2}
            assert words["stops"][1]["picture"] == "(picture 2)"
            assert [base64.b64decode(c.data) for c in tour.content[1:]] == [b"one", b"two"]
            assert tour.content[1].mime_type == "image/jpeg"

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


def test_pictures_are_taken_out_of_a_result_wherever_they_are() -> None:
    assert calls.take_pictures(picture(b"x", "image/png")) == (None, [("image/png", base64.b64encode(b"x").decode())])
    rest, images = calls.take_pictures({"a": [picture(b"1"), {"b": picture(b"2")}], "n": 1})
    assert rest == {"a": ["(picture 1)", {"b": "(picture 2)"}], "n": 1}
    assert [base64.b64decode(d) for _, d in images] == [b"1", b"2"]
    assert calls.take_pictures({"n": 1}) == ({"n": 1}, [])
    result = calls.Result(True, rest, images=images)
    assert result.image == images[0] and result.brief() == "2 pictures" and '"(picture 2)"' in result.text()
    assert calls.Result(True, None, images=images[:1]).text() == "(a picture, attached)"
    assert calls.Result(True, {"n": 1}).brief() == '{"n": 1}'
