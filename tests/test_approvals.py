"""A ``danger`` tool waits for the person's Accept in the chat: what the card shows, each answer,
and both ways a model reaches a tool (the hub's own loop, Claude Code through the MCP address)."""

from __future__ import annotations

import asyncio
import collections
import time

import httpx2
import pytest
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
from starlette.applications import Starlette
from starlette.routing import Route

from ai_hub import agent, approvals, brains, calls, config, mcp_endpoint, registry, session
from tests.test_agent import FakeConversation

DANGER = registry.Tool(
    app="grid",
    name="grid.edit",
    title="Change cells",
    description="Changes cells.",
    kind="danger",
    input_schema={"type": "object", "properties": {"room": {"type": "string"}, "summary": {"type": "string"}, "steps": {"type": "array"}}},
    backend="http://grid",
)
WRITE = registry.Tool(app="cad", name="cad.set_code", title="Write", description="", kind="write", input_schema={"type": "object", "properties": {"code": {"type": "string"}}}, backend="http://cad")
ARGS = {"room": "b1", "summary": "Add the totals", "steps": [{"command": "cell.set", "params": {"cell": "B7", "input": "=SUM(B2:B6)"}}]}


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(approvals, "_pending", {})
    monkeypatch.setattr(approvals, "_trusted", set())
    monkeypatch.setattr(session, "_queue", collections.deque())
    monkeypatch.setattr(session, "_current", None)
    monkeypatch.setattr(session, "_cond", None)


class Chat:
    """A chat's card: what was shown, and an answer as soon as it shows."""

    def __init__(self, answer: str | None = None) -> None:
        self.answer = answer
        self.events: list[dict] = []

    async def post(self, event: dict) -> None:
        self.events.append(event)
        if event["type"] == "approve" and self.answer:
            assert approvals.decide(event["id"], self.answer)

    def kinds(self) -> list[str]:
        return [e["type"] for e in self.events if e["type"] != "status"]


def test_the_card_says_what_the_call_does() -> None:
    summary, details = approvals.describe({**ARGS, "code": "Sub A()\nEnd Sub\n", "module": "Module1"})
    assert summary == "Add the totals"
    assert "room" not in details and "summary" not in details
    assert "  cell.set  cell=B7  input==SUM(B2:B6)" in details
    assert approvals.describe({"controls": [{"name": "btnAdd", "kind": "CommandButton", "left": 80}], "remove": ["old"]})[1] == "controls:\n  btnAdd  kind=CommandButton  left=80\nremove:\n  old"
    assert "code:\nSub A()\nEnd Sub\n" in details and "module: Module1" in details
    assert "more characters" in approvals.describe({"code": "x" * (approvals.DETAILS_CHARS + 50)})[1]


async def test_accept_lets_the_tool_run() -> None:
    chat = Chat(approvals.ACCEPT)
    assert await approvals.ask("c1", DANGER, ARGS, chat.post) is None
    assert chat.kinds() == ["approve", "decided"] and chat.events[0]["title"] == "Change cells (grid)"
    assert chat.events[0]["summary"] == "Add the totals"
    # The next change asks again.
    again = Chat(approvals.REJECT)
    assert (await approvals.ask("c1", DANGER, ARGS, again.post)) is not None


async def test_reject_leaves_everything_and_tells_the_model() -> None:
    chat = Chat(approvals.REJECT)
    result = await approvals.ask("c1", DANGER, ARGS, chat.post)
    assert result is not None and not result.ok and "rejected" in result.error
    assert [e["decision"] for e in chat.events if e["type"] == "decided"] == [approvals.REJECT]


async def test_no_answer_counts_as_no(monkeypatch) -> None:
    monkeypatch.setattr(approvals, "WAIT", 0.05)
    chat = Chat()
    result = await approvals.ask("c1", DANGER, ARGS, chat.post)
    assert result is not None and not result.ok and "didn't answer" in result.error
    assert [e for e in chat.events if e["type"] == "decided"][0]["decision"] == approvals.TIMEOUT
    assert approvals._pending == {}


async def test_accept_all_lasts_for_that_chat_only() -> None:
    chat = Chat(approvals.ACCEPT_ALL)
    assert await approvals.ask("c1", DANGER, ARGS, chat.post) is None
    quiet = Chat()
    assert await approvals.ask("c1", DANGER, ARGS, quiet.post) is None
    assert quiet.events == []
    other = Chat(approvals.REJECT)
    assert await approvals.ask("c2", DANGER, ARGS, other.post) is not None
    approvals.forget("c1")
    asked = Chat(approvals.REJECT)
    assert await approvals.ask("c1", DANGER, ARGS, asked.post) is not None


async def test_a_new_chat_ends_what_the_old_one_asks() -> None:
    chat = Chat()
    waiting = asyncio.create_task(approvals.ask("c1", DANGER, ARGS, chat.post))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert chat.kinds() == ["approve"]
    approvals.forget("c1")
    result = await waiting
    assert result is not None and not result.ok


async def test_a_late_or_strange_answer_changes_nothing() -> None:
    assert approvals.decide("gone", approvals.ACCEPT) is False
    chat = Chat()
    waiting = asyncio.create_task(approvals.ask("c1", DANGER, ARGS, chat.post))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    request = chat.events[0]["id"]
    assert approvals.decide(request, "yes please") is False
    assert approvals.decide(request, approvals.ACCEPT) is True
    assert await waiting is None


# ---------------------------------------------------------------- the hub's own loop


@pytest.fixture
def world(monkeypatch):
    called = []

    async def fake_tools(refresh: bool = False):
        return [DANGER, WRITE]

    async def fake_call(tool, args, user, via):
        called.append(tool.name)
        return calls.Result(True, {"done": 1})

    monkeypatch.setattr(registry, "tools", fake_tools)
    monkeypatch.setattr(calls, "call", fake_call)
    return called


def scripted(monkeypatch, *names) -> FakeConversation:
    fake = FakeConversation([brains.Step(calls=[brains.ToolCall(f"c{i}", name, dict(ARGS))]) for i, name in enumerate(names)] + [brains.Step(text="Done.")])

    async def fake_conversation(role, system):
        return fake

    monkeypatch.setattr(brains, "conversation", fake_conversation)
    return fake


@pytest.mark.parametrize(("answer", "ran"), [(approvals.ACCEPT, ["grid.edit"]), (approvals.REJECT, [])])
async def test_the_assistant_runs_a_danger_tool_only_after_accept(world, monkeypatch, answer, ran) -> None:
    fake = scripted(monkeypatch, "grid_edit")
    chat = Chat(answer)
    await agent.Chat("smart", agent.Scope(app="grid", room="b1"), user="u").send("add totals", chat.post)
    assert world == ran
    assert "approve" in chat.kinds() and "decided" in chat.kinds()
    assert fake.results[0][0][1].ok is (answer == approvals.ACCEPT)


async def test_tools_that_arent_danger_run_without_asking(world, monkeypatch) -> None:
    scripted(monkeypatch, "cad_set_code")
    chat = Chat()
    await agent.Chat("smart", agent.Scope(app="cad"), user="u").send("write it", chat.post)
    assert world == ["cad.set_code"] and "approve" not in chat.kinds()


async def test_a_talk_has_nowhere_to_ask_so_a_danger_tool_doesnt_run(world, monkeypatch) -> None:
    fake = scripted(monkeypatch, "grid_edit")
    chat = Chat()
    await agent.Chat("smart", agent.Scope(app="grid"), user="u", manner="talk").send("add totals", chat.post)
    assert world == [] and "approve" not in chat.kinds()
    assert not fake.results[0][0][1].ok


# ---------------------------------------------------------------- Claude Code, through the MCP address


async def call_through_mcp(token: str, name: str):
    async with mcp_endpoint.lifespan():
        app = Starlette(routes=[Route(mcp_endpoint.PATH, mcp_endpoint.endpoint)])
        http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://hub", headers={"Authorization": f"Bearer {token}"})
        async with Client(streamable_http_client("http://hub/api/mcp", http_client=http)) as client:
            return await client.call_tool(name, {"room": "b1", "summary": "Add the totals", "steps": []})


async def answer_the_card(turn: session._Turn, decision: str) -> list[dict]:
    """The chat's side: shows what the session's call asks, and answers it."""
    seen = []
    while True:
        event = await turn.events.get()
        seen.append(event)
        if event["type"] == "approve":
            approvals.decide(event["id"], decision)
        if event["type"] == "result":
            return seen


@pytest.fixture
def mcp_world(world, monkeypatch):
    async def fake_find(name: str):
        return DANGER if name == "grid_edit" else None

    monkeypatch.setattr(registry, "find", fake_find)
    return world


@pytest.mark.parametrize(("decision", "ran"), [(approvals.ACCEPT, ["grid.edit"]), (approvals.REJECT, [])])
async def test_claude_code_on_a_chat_waits_for_the_persons_answer(mcp_world, monkeypatch, decision, ran) -> None:
    token = config.make_session_token()
    turn = session._Turn("c1", "add totals", "grid", "b1", asyncio.Queue(), delivered=time.monotonic())
    monkeypatch.setattr(session, "_current", turn)
    answering = asyncio.create_task(answer_the_card(turn, decision))
    await call_through_mcp(token, "grid_edit")
    shown = await answering
    assert mcp_world == ran
    assert "approve" in [e["type"] for e in shown]


async def test_claude_code_working_on_no_chat_and_other_agents_arent_asked_here(mcp_world, monkeypatch) -> None:
    token = config.make_session_token()
    await call_through_mcp(token, "grid_edit")  # the owner typed in its terminal
    other = config.create_token("my agent")
    turn = session._Turn("c1", "x", "grid", "b1", asyncio.Queue(), delivered=time.monotonic())
    monkeypatch.setattr(session, "_current", turn)
    await call_through_mcp(other, "grid_edit")  # another agent's client asks its own user
    assert mcp_world == ["grid.edit", "grid.edit"]
    assert turn.events.empty()


# ---------------------------------------------------------------- an app asks for the person's OK in the middle of a call

READ = registry.Tool(app="forge", name="forge.read_file", title="Read a file", description="", kind="read",
                     input_schema={"type": "object", "properties": {"path": {"type": "string"}}}, backend="http://forge")


@pytest.fixture
def asking_app(monkeypatch):
    """forge.read_file asks first for chip/ files, and reads them only with the consent of that call."""
    seen = []

    async def fake_call(tool, args, user, via, consent=""):
        seen.append(consent)
        if args.get("path", "").startswith("chip/") and consent != "k1":
            return calls.Result(False, error="this needs the person's OK first: read chip/rtl/top.sv",
                                ask={"what": "read chip/rtl/top.sv", "detail": "The chip's source isn't open to the AI.", "key": "k1"})
        return calls.Result(True, {"text": "module top;"})

    async def fake_tools(refresh: bool = False):
        return [READ]

    monkeypatch.setattr(calls, "call", fake_call)
    monkeypatch.setattr(registry, "tools", fake_tools)
    return seen


@pytest.mark.parametrize(("answer", "ok"), [(approvals.ACCEPT, True), (approvals.REJECT, False)])
async def test_an_app_can_ask_first_and_the_call_goes_on_only_when_allowed(asking_app, answer, ok) -> None:
    chat = Chat(answer)
    result = await approvals.call_asking(READ, {"path": "chip/rtl/top.sv"}, "u", "assistant", "c1", chat.post)
    assert result.ok is ok
    card = next(e for e in chat.events if e["type"] == "approve")
    assert card["ask"] == "read" and card["summary"] == "read chip/rtl/top.sv" and "isn't open" in card["details"]
    assert asking_app == (["", "k1"] if ok else [""])  # called again with the consent only after an Allow
    if not ok:
        assert "didn't allow" in result.error


async def test_open_files_need_no_ok_and_allow_all_lasts_for_the_chat(asking_app) -> None:
    chat = Chat(approvals.ACCEPT_ALL)
    assert (await approvals.call_asking(READ, {"path": "system.yaml"}, "u", "assistant", "c1", chat.post)).ok
    assert "approve" not in chat.kinds()
    assert (await approvals.call_asking(READ, {"path": "chip/a.sv"}, "u", "assistant", "c1", chat.post)).ok
    quiet = Chat()
    assert (await approvals.call_asking(READ, {"path": "chip/b.sv"}, "u", "assistant", "c1", quiet.post)).ok
    assert "approve" not in quiet.kinds()  # this chat allowed everything already


async def test_with_no_chat_to_ask_in_the_model_is_told_so(asking_app) -> None:
    result = await approvals.call_asking(READ, {"path": "chip/rtl/top.sv"}, "u", "mcp", None, None)
    assert not result.ok and "only the assistant's chat" in result.error and "chip/rtl/top.sv" in result.error


async def test_the_assistant_asks_for_the_ok_in_its_chat(asking_app, monkeypatch) -> None:
    fake = FakeConversation([brains.Step(calls=[brains.ToolCall("c0", "forge_read_file", {"path": "chip/rtl/top.sv"})]), brains.Step(text="Done.")])

    async def fake_conversation(role, system):
        return fake

    monkeypatch.setattr(brains, "conversation", fake_conversation)
    chat = Chat(approvals.ACCEPT)
    await agent.Chat("smart", agent.Scope(app="forge", room="p1"), user="u").send("read the chip", chat.post)
    assert "approve" in chat.kinds() and fake.results[0][0][1].ok
    talk = Chat(approvals.ACCEPT)
    fake2 = FakeConversation([brains.Step(calls=[brains.ToolCall("c0", "forge_read_file", {"path": "chip/rtl/top.sv"})]), brains.Step(text="Done.")])

    async def fake_conversation2(role, system):
        return fake2

    monkeypatch.setattr(brains, "conversation", fake_conversation2)
    await agent.Chat("smart", agent.Scope(app="forge"), user="u", manner="talk").send("read the chip", talk.post)
    assert "approve" not in talk.kinds() and not fake2.results[0][0][1].ok  # a talk has nowhere to ask


async def test_claude_code_on_a_chat_asks_for_the_ok_there(asking_app, monkeypatch) -> None:
    async def fake_find(name: str):
        return READ if name == "forge_read_file" else None

    monkeypatch.setattr(registry, "find", fake_find)
    token = config.make_session_token()
    turn = session._Turn("c1", "read the chip", "forge", "p1", asyncio.Queue(), delivered=time.monotonic())
    monkeypatch.setattr(session, "_current", turn)
    answering = asyncio.create_task(answer_the_card(turn, approvals.ACCEPT))
    async with mcp_endpoint.lifespan():
        app = Starlette(routes=[Route(mcp_endpoint.PATH, mcp_endpoint.endpoint)])
        http = httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://hub", headers={"Authorization": f"Bearer {token}"})
        async with Client(streamable_http_client("http://hub/api/mcp", http_client=http)) as client:
            out = await client.call_tool("forge_read_file", {"path": "chip/rtl/top.sv"})
    shown = await answering
    assert not out.is_error and "module top" in out.content[0].text
    assert any(e["type"] == "approve" and e.get("ask") == "read" for e in shown)
