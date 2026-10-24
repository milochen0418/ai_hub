"""The Claude Code on the owner's computer: the passcode that keeps it the owner's, and how
messages, attached files, tool calls and answers pass between the assistant and its channel."""

from __future__ import annotations

import asyncio
import collections
import time

import httpx
import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.routing import Route

from ai_hub import agent, attachments, calls, config, mcp_endpoint, registry, session
from tests.test_attachments import keep, png_bytes

TOOL = registry.Tool(
    app="cad",
    name="cad.set_code",
    title="Write the room's code",
    description="Replaces the code.",
    kind="write",
    input_schema={"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
    backend="http://cad",
)


@pytest.fixture(autouse=True)
def fresh_session(monkeypatch):
    monkeypatch.setattr(session, "_queue", collections.deque())
    monkeypatch.setattr(session, "_current", None)
    monkeypatch.setattr(session, "_seen", float("-inf"))
    monkeypatch.setattr(session, "_channel", 0)
    monkeypatch.setattr(session, "_settling", 0.0)
    monkeypatch.setattr(session, "_prompt", "")
    monkeypatch.setattr(session, "_cond", None)
    monkeypatch.setattr(session, "POLL", 0.5)


def channel(token: str, files: bool = True) -> httpx.AsyncClient:
    """The hub's side of the channel, as the channel on the owner's computer reaches it (one that
    opens attached files, unless ``files`` is False: one from before them)."""
    app = Starlette(
        routes=[
            Route(session.NEXT_PATH, session.next_message),
            Route(session.REPLY_PATH, session.reply, methods=["POST"]),
            Route(session.ACTIVITY_PATH, session.activity, methods=["POST"]),
            Route(session.ATTACHMENT_PATH, session.attachment),
        ]
    )
    headers = {"Authorization": f"Bearer {token}"} | ({session.CHANNEL_HEADER: str(session.FILES_CHANNEL)} if files else {})
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://hub", headers=headers)


async def ignore(event) -> None:
    pass


def hook(name: str, **fields) -> dict:
    """A hook event as Claude Code posts it."""
    return {"hook_event_name": name, "session_id": "s1", "cwd": "/x", "effort": {"level": "max"}, **fields}


def test_the_passcode_marks_the_owners_browser() -> None:
    assert not config.has_passcode() and not config.is_owner("anything")
    config.set_passcode("open sesame")
    assert config.check_passcode("open sesame") and not config.check_passcode("open sesame!")
    key = config.owner_key()
    assert config.is_owner(key) and not config.is_owner("") and not config.is_owner(key[:-1] + "x")

    config.set_passcode("a new one")
    assert not config.is_owner(key)  # browsers have to give the new passcode
    assert config.is_owner(config.owner_key())


def test_a_passcode_from_the_environment_wins(monkeypatch) -> None:
    config.set_passcode("stored")
    key = config.owner_key()
    monkeypatch.setenv("ADMIN_PASSCODE", "from the environment")
    assert config.check_passcode("from the environment") and not config.check_passcode("stored")
    assert not config.is_owner(key)


def test_the_session_token_makes_claude_code_a_provider() -> None:
    assert config.SESSION not in config.providers()
    first = config.make_session_token()
    second = config.make_session_token()
    assert config.token_info(first) is None  # a new one replaces it
    info = config.token_info(second)
    assert info == {"id": config.session_token_id(), "name": config.SESSION_TOKEN_NAME}
    assert config.resolve(f"{config.SESSION}/")[0].kind == "session"

    config.set_role("local", f"{config.SESSION}/")
    with pytest.raises(LookupError):
        config.resolve("local")  # the local role stays on this machine

    config.delete_token(info["id"])
    assert config.session_token_id() == "" and config.SESSION not in config.providers()


async def test_a_message_goes_to_the_session_and_its_answer_comes_back() -> None:
    token = config.make_session_token()
    other = config.token_info(config.create_token("someone else"))
    events = []

    async def show(event) -> None:
        events.append(event)

    async with channel(token) as http:
        assert (await http.get(session.NEXT_PATH)).status_code == 204  # nothing yet; the channel runs
        asking = asyncio.create_task(session.ask("c1", "draw a table", "cad", "r1", show))
        r = await http.get(session.NEXT_PATH)
        assert r.json() == {"chat": "c1", "text": "draw a table", "app": "cad", "room": "r1"}

        await session.tool_called(config.session_token_id(), "cad_set_code", {"code": "x"})
        await session.tool_done(config.session_token_id(), "cad_set_code", calls.Result(True, {"lines": 1}))
        await session.tool_called(other["id"], "cad_snapshot", {})  # someone else's: not in this chat
        note = await http.post(session.REPLY_PATH, json={"chat": "c1", "text": "On it.", "done": False})
        answer = await http.post(session.REPLY_PATH, json={"chat": "c1", "text": "Done: a table."})
        assert note.status_code == answer.status_code == 200
        await asyncio.wait_for(asking, 5)

        late = await http.post(session.REPLY_PATH, json={"chat": "c1", "text": "late"})
        assert late.status_code == 404

    assert [e["type"] for e in events if e["type"] != "status"] == ["model", "tool", "result", "text", "text"]
    assert events[1]["name"] == "cad_set_code" and events[-1]["text"] == "Done: a table."


async def test_attached_files_go_to_the_session_which_opens_them() -> None:
    token = config.make_session_token()
    picture = keep("red.png", png_bytes(), "image/png")
    events = []

    async def show(event) -> None:
        events.append(event)

    async with channel(token) as http:
        await http.get(session.NEXT_PATH)
        asking = asyncio.create_task(session.ask("c1", "paint it this colour", "cad", "r1", show, [picture]))
        message = (await http.get(session.NEXT_PATH)).json()
        assert message["files"] == [{"id": picture.id, "about": picture.about()}]

        opened = await http.get(session.ATTACHMENT_PATH, params={"id": picture.id})
        assert [c["type"] for c in opened.json()["content"]] == ["text", "image"]
        await http.post(session.REPLY_PATH, json={"chat": "c1", "text": "Painted."})
        await asyncio.wait_for(asking, 5)

    rows = [e for e in events if e["type"] in ("tool", "result")]
    assert [r.get("name") for r in rows] == ["attachment: red.png", "attachment"] and rows[1]["text"] == "a picture"


async def test_a_channel_from_before_attachments_gets_the_message_without_them() -> None:
    token = config.make_session_token()
    events = []

    async def show(event) -> None:
        events.append(event)

    async with channel(token, files=False) as http:
        await http.get(session.NEXT_PATH)
        asking = asyncio.create_task(session.ask("c1", "look", "", "", show, [keep("red.png", png_bytes(), "image/png")]))
        assert "files" not in (await http.get(session.NEXT_PATH)).json()
        await http.post(session.REPLY_PATH, json={"chat": "c1", "text": "ok"})
        await asyncio.wait_for(asking, 5)

    assert {"type": "info", "text": session.OLD_CHANNEL} in events


async def test_the_session_opens_only_the_files_it_was_given() -> None:
    token = config.make_session_token()
    given, kept = keep("a.png", png_bytes(), "image/png"), keep("b.png", png_bytes(), "image/png")
    attachments.give_to_session([given])
    async with channel(token) as http:
        assert (await http.get(session.ATTACHMENT_PATH, params={"id": given.id})).status_code == 200
        assert (await http.get(session.ATTACHMENT_PATH, params={"id": kept.id})).status_code == 404
        assert (await http.get(session.ATTACHMENT_PATH, params={"id": "../settings"})).status_code == 404
    async with channel(config.create_token("someone else")) as http:
        assert (await http.get(session.ATTACHMENT_PATH, params={"id": given.id})).status_code == 401


async def test_the_next_message_waits_for_the_answer_to_the_last() -> None:
    token = config.make_session_token()
    async with channel(token) as http:
        await http.get(session.NEXT_PATH)
        first = asyncio.create_task(session.ask("a", "first", "", "", ignore))
        second = asyncio.create_task(session.ask("b", "second", "", "", ignore))
        assert (await http.get(session.NEXT_PATH)).json()["chat"] == "a"
        assert (await http.get(session.NEXT_PATH)).status_code == 204  # "a" has no answer yet
        await http.post(session.REPLY_PATH, json={"chat": "a", "text": "ok"})
        assert (await http.get(session.NEXT_PATH)).json()["chat"] == "b"
        await http.post(session.REPLY_PATH, json={"chat": "b", "text": "ok"})
        await asyncio.wait_for(asyncio.gather(first, second), 5)


@pytest.mark.parametrize("who", ["someone else", "not a token"])
async def test_only_the_session_token_may_fetch_or_answer(who) -> None:
    config.make_session_token()
    token = config.create_token(who) if who == "someone else" else "aihub_nope"
    async with channel(token) as http:
        assert (await http.get(session.NEXT_PATH)).status_code == 401
        assert (await http.post(session.REPLY_PATH, json={"chat": "c", "text": "x"})).status_code == 401
        assert (await http.post(session.ACTIVITY_PATH, json=hook("Stop"))).status_code == 401


async def test_without_the_channel_running_the_person_is_told() -> None:
    config.make_session_token()
    events = []

    async def show(event) -> None:
        events.append(event)

    await session.ask("c", "hi", "", "", show)
    assert events == [{"type": "error", "text": session.NOT_RUNNING}]


async def test_a_role_set_to_claude_code_goes_to_the_session(monkeypatch) -> None:
    config.make_session_token()
    config.set_role("smart", f"{config.SESSION}/")
    handed = []

    async def fake_ask(chat, text, app, room, on_event, files=()) -> None:
        handed.append((chat, text, app, room, list(files)))

    monkeypatch.setattr(session, "ask", fake_ask)
    chat = agent.Chat("smart", agent.Scope("cad", "r1"), user="assistant")
    picture = keep("red.png", png_bytes(), "image/png")
    await chat.send("draw a table", ignore, [picture])
    assert handed == [(chat.id, "draw a table", "cad", "r1", [picture])]


async def test_its_tool_calls_through_the_mcp_address_show_in_the_chat(monkeypatch) -> None:
    async def fake_tools(refresh: bool = False):
        return [TOOL]

    async def fake_call(tool, args, user, via):
        return calls.Result(True, {"lines": 1})

    monkeypatch.setattr(registry, "tools", fake_tools)
    monkeypatch.setattr(calls, "call", fake_call)
    token = config.make_session_token()
    turn = session._Turn("c1", "draw", "cad", "", asyncio.Queue(), delivered=time.monotonic())
    monkeypatch.setattr(session, "_current", turn)

    async with mcp_endpoint.lifespan():
        app = Starlette(routes=[Route(mcp_endpoint.PATH, mcp_endpoint.endpoint)])
        http = httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://hub", headers={"Authorization": f"Bearer {token}"}
        )
        async with Client(streamable_http_client("http://hub/api/mcp", http_client=http)) as client:
            await client.call_tool("cad_set_code", {"code": "x"})

    shown = [turn.events.get_nowait() for _ in range(turn.events.qsize())]
    shown = [e for e in shown if e["type"] != "status"]  # each also has the status line say so
    assert [e["type"] for e in shown] == ["tool", "result"] and shown[1]["ok"] is True


async def test_while_it_works_the_chat_waits_and_says_how_it_goes(monkeypatch) -> None:
    monkeypatch.setattr(session, "TICK", 0.05)
    monkeypatch.setattr(session, "IDLE_TIMEOUT", 0.3)
    token = config.make_session_token()
    events = []

    async def show(event) -> None:
        events.append(event)

    async with channel(token) as http:
        await http.get(session.NEXT_PATH)
        asking = asyncio.create_task(session.ask("c1", "build a cathedral", "cad", "r1", show))
        await http.get(session.NEXT_PATH)
        for _ in range(8):  # longer than IDLE_TIMEOUT in all, with a step every 0.1 s
            await session.tool_called(config.session_token_id(), "cad_run_preview", {})
            await asyncio.sleep(0.1)
        await http.post(session.REPLY_PATH, json={"chat": "c1", "text": "Done."})
        await asyncio.wait_for(asking, 5)

    assert "error" not in [e["type"] for e in events] and events[-1] == {"type": "text", "text": "Done."}
    statuses = [e["text"] for e in events if e["type"] == "status"]
    assert statuses and statuses[-1].startswith("Claude Code is working") and "8 steps" in statuses[-1]


async def test_after_a_long_silence_the_chat_stops_waiting(monkeypatch) -> None:
    monkeypatch.setattr(session, "TICK", 0.05)
    monkeypatch.setattr(session, "IDLE_TIMEOUT", 0.2)
    token = config.make_session_token()
    events = []

    async def show(event) -> None:
        events.append(event)

    async with channel(token) as http:
        await http.get(session.NEXT_PATH)
        asking = asyncio.create_task(session.ask("c1", "x", "", "", show))
        await http.get(session.NEXT_PATH)
        await asyncio.wait_for(asking, 5)
        assert session._current is None, "the next message may go"
    assert events[-1]["type"] == "error" and "did nothing" in events[-1]["text"]


async def test_when_claude_code_is_closed_the_chat_says_so(monkeypatch) -> None:
    monkeypatch.setattr(session, "TICK", 0.05)
    token = config.make_session_token()
    events = []

    async def show(event) -> None:
        events.append(event)

    async with channel(token) as http:
        await http.get(session.NEXT_PATH)
        asking = asyncio.create_task(session.ask("c1", "x", "", "", show))
        await http.get(session.NEXT_PATH)
        session._seen = float("-inf")  # the channel stopped fetching: Claude Code is gone
        await asyncio.wait_for(asking, 5)
    assert events[-1] == {"type": "error", "text": session.STOPPED}


async def test_its_hooks_show_what_it_does_while_it_works(monkeypatch) -> None:
    monkeypatch.setattr(session, "TICK", 0.05)
    token = config.make_session_token()
    events = []

    async def show(event) -> None:
        events.append(event)

    async with channel(token) as http:

        async def post(name: str, **fields) -> None:
            r = await http.post(session.ACTIVITY_PATH, json=hook(name, **fields))
            assert r.status_code == 204 and r.content == b"", "no body: Claude Code goes on as it would"
            await asyncio.sleep(0.02)

        await http.get(session.NEXT_PATH)
        asking = asyncio.create_task(session.ask("c1", "build its inside", "cad", "r1", show))
        await http.get(session.NEXT_PATH)
        await post("MessageDisplay", message_id="m1", delta="I'll look up the inside first.\n", index=0, final=True)
        await post("PreToolUse", tool_name="WebSearch", tool_input={"query": "Sagrada Familia interior"}, tool_use_id="t1")
        await post("PostToolUse", tool_name="WebSearch", tool_input={}, tool_response={}, tool_use_id="t1")
        await post("PreToolUse", tool_name="WebFetch", tool_input={"url": "https://example.com/inside", "prompt": "?"})
        await post("PostToolUse", tool_name="WebFetch", tool_input={}, tool_response={"code": 200})
        await session.tool_called(config.session_token_id(), "cad_set_code", {"code": "x"})
        await post("PreToolUse", tool_name="mcp__apps__cad_set_code", tool_input={"code": "x"})  # shown once
        await session.tool_done(config.session_token_id(), "cad_set_code", calls.Result(True, {"lines": 1}))
        await asyncio.sleep(0.1)
        await http.post(session.REPLY_PATH, json={"chat": "c1", "text": "Built it."})
        await asyncio.wait_for(asking, 5)

    rows = [(e["type"], e.get("text", e.get("name"))) for e in events if e["type"] not in ("status", "model")]
    assert [r for r in rows if r[0] != "result"] == [
        ("note", "I'll look up the inside first."),
        ("web", "Web search: Sagrada Familia interior"),
        ("web", "Read: example.com/inside"),
        ("tool", "cad_set_code"),
        ("text", "Built it."),
    ]
    statuses = [e["text"] for e in events if e["type"] == "status"]
    assert any(s.startswith("Claude Code is searching the web: “Sagrada Familia interior”") for s in statuses)
    assert any(s.startswith("Claude Code is reading example.com/inside") for s in statuses)
    assert any(s.startswith("Claude Code is using cad_set_code") for s in statuses)
    thinking = [s for s in statuses if s.startswith("Claude Code is thinking (max effort)")]
    assert thinking and "on this message · 3 steps" in thinking[-1]
    assert "it last wrote: “I'll look up the inside first.”" in thinking[-1]


async def test_a_turn_ended_without_an_answer_shows_what_it_last_wrote(monkeypatch) -> None:
    monkeypatch.setattr(session, "TICK", 0.05)
    token = config.make_session_token()
    events = []

    async def show(event) -> None:
        events.append(event)

    async with channel(token) as http:
        await http.get(session.NEXT_PATH)
        asking = asyncio.create_task(session.ask("c1", "build it", "cad", "r1", show))
        await http.get(session.NEXT_PATH)
        await http.post(session.ACTIVITY_PATH, json=hook("MessageDisplay", message_id="m1", delta="It is built.\n"))
        stop = hook("Stop", last_assistant_message="It is built.", background_tasks=[], stop_hook_active=False)
        await http.post(session.ACTIVITY_PATH, json=stop)
        await asyncio.wait_for(asking, 5)
        assert session._current is None, "the next message may go"

    assert [e for e in events if e["type"] not in ("status", "model")] == [
        {"type": "info", "text": session.LAST_WORDS},
        {"type": "text", "text": "It is built."},
    ]


async def test_with_its_hooks_the_chat_waits_through_long_thinking(monkeypatch) -> None:
    monkeypatch.setattr(session, "TICK", 0.05)
    monkeypatch.setattr(session, "IDLE_TIMEOUT", 0.2)
    token = config.make_session_token()
    events = []

    async def show(event) -> None:
        events.append(event)

    async with channel(token) as http:
        await http.get(session.NEXT_PATH)
        asking = asyncio.create_task(session.ask("c1", "think hard", "", "", show))
        await http.get(session.NEXT_PATH)
        await http.post(session.ACTIVITY_PATH, json=hook("MessageDisplay", message_id="m1", delta="Let me think.\n"))
        await asyncio.sleep(0.6)  # three times IDLE_TIMEOUT without a step
        assert not asking.done()
        await http.post(session.REPLY_PATH, json={"chat": "c1", "text": "Done."})
        await asyncio.wait_for(asking, 5)

    assert "error" not in [e["type"] for e in events] and events[-1] == {"type": "text", "text": "Done."}


async def test_the_next_message_waits_for_the_end_of_the_turn_it_was_answered_in() -> None:
    token = config.make_session_token()
    async with channel(token) as http:
        await http.get(session.NEXT_PATH)
        await http.post(session.ACTIVITY_PATH, json=hook("SessionStart", source="startup"))
        first = asyncio.create_task(session.ask("a", "first", "", "", ignore))
        second = asyncio.create_task(session.ask("b", "second", "", "", ignore))
        assert (await http.get(session.NEXT_PATH)).json()["chat"] == "a"
        await http.post(session.REPLY_PATH, json={"chat": "a", "text": "ok"})
        assert (await http.get(session.NEXT_PATH)).status_code == 204, "a's turn goes on: its end isn't b's"
        await http.post(session.ACTIVITY_PATH, json=hook("Stop", last_assistant_message="ok", background_tasks=[]))
        assert (await http.get(session.NEXT_PATH)).json()["chat"] == "b"
        await http.post(session.REPLY_PATH, json={"chat": "b", "text": "ok"})
        await asyncio.wait_for(asyncio.gather(first, second), 5)


@pytest.mark.parametrize(
    "name,fields,said",
    [
        ("SessionEnd", {"reason": "prompt_input_exit"}, session.STOPPED),
        ("SessionEnd", {"reason": "clear"}, session.CLEARED),
        ("StopFailure", {"error": "rate_limit", "last_assistant_message": "API Error: Rate limit reached"}, "Claude Code stopped: API Error: Rate limit reached"),
    ],
)
async def test_when_its_session_ends_or_fails_the_chat_says_so(monkeypatch, name, fields, said) -> None:
    monkeypatch.setattr(session, "TICK", 0.05)
    token = config.make_session_token()
    events = []

    async def show(event) -> None:
        events.append(event)

    async with channel(token) as http:
        await http.get(session.NEXT_PATH)
        asking = asyncio.create_task(session.ask("c1", "x", "", "", show))
        await http.get(session.NEXT_PATH)
        await http.post(session.ACTIVITY_PATH, json=hook(name, **fields))
        await asyncio.wait_for(asking, 5)
    assert events[-1] == {"type": "error", "text": said} and session._current is None


async def test_its_hooks_are_remembered_and_a_new_chat_stops_the_wait(monkeypatch) -> None:
    monkeypatch.setattr(session, "TICK", 0.05)
    monkeypatch.setattr(session, "SETTLE", 0.2)
    token = config.make_session_token()
    events = []

    async def show(event) -> None:
        events.append(event)

    async with channel(token) as http:
        await http.get(session.NEXT_PATH)
        assert not config.session_hooked()
        await http.post(session.ACTIVITY_PATH, json=hook("SessionStart", source="startup"))
        assert config.session_hooked(), "known after a restart of the hub too"
        asking = asyncio.create_task(session.ask("c1", "think for an hour", "", "", show))
        await http.get(session.NEXT_PATH)
        await session.drop("c1")  # the person pressed New chat
        await asyncio.wait_for(asking, 5)
        assert session._current is None and "error" not in [e["type"] for e in events]
        nxt = asyncio.create_task(session.ask("c2", "something else", "", "", ignore))
        assert (await http.get(session.NEXT_PATH)).json()["chat"] == "c2", "after the old turn's end, or SETTLE"
        await http.post(session.REPLY_PATH, json={"chat": "c2", "text": "ok"})
        await asyncio.wait_for(nxt, 5)


async def test_events_of_its_earlier_turn_are_not_the_new_chats(monkeypatch) -> None:
    monkeypatch.setattr(session, "TICK", 0.05)
    token = config.make_session_token()
    events = []

    async def show(event) -> None:
        events.append(event)

    async with channel(token) as http:
        await http.get(session.NEXT_PATH)
        # It is still on an earlier message (one the person stopped waiting for).
        await http.post(session.ACTIVITY_PATH, json=hook("PreToolUse", prompt_id="p0", tool_name="WebSearch", tool_input={"query": "old"}))
        asking = asyncio.create_task(session.ask("c1", "a new question", "", "", show))
        await http.get(session.NEXT_PATH)
        await http.post(session.ACTIVITY_PATH, json=hook("Stop", prompt_id="p0", last_assistant_message="old answer", background_tasks=[]))
        await asyncio.sleep(0.15)
        assert not asking.done(), "the end of the earlier turn isn't this chat's"
        await http.post(session.ACTIVITY_PATH, json=hook("MessageDisplay", prompt_id="p1", message_id="m1", delta="New answer.\n"))
        await http.post(session.ACTIVITY_PATH, json=hook("Stop", prompt_id="p1", last_assistant_message="New answer.", background_tasks=[]))
        await asyncio.wait_for(asking, 5)

    statuses = [e["text"] for e in events if e["type"] == "status"]
    assert any(s.startswith("Claude Code is finishing earlier work") for s in statuses)
    assert [e for e in events if e["type"] not in ("status", "model")] == [
        {"type": "info", "text": session.LAST_WORDS},
        {"type": "text", "text": "New answer."},
    ]
