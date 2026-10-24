"""The Claude Code on the owner's computer as the assistant's model, for the owner's development.

The owner starts Claude Code there with the hub's channel (``claude_channel/``, a Claude Code
channel: research preview). The channel fetches what is typed into the assistant from
``NEXT_PATH`` and pushes it into that Claude Code session, which does the work with the apps'
tools (the hub's MCP address, under the session's token) and answers through the channel's
``reply`` tool (``REPLY_PATH``). The hub never holds a Claude login: the work runs in the owner's
own, unmodified Claude Code on the owner's own plan, so only the owner may use it (the
assistant asks for the hub's passcode first; see ``AssistantState``).

One message at a time: the next goes to the session once it has answered the last, so the
tool calls made with the session's token (``tool_called``/``tool_done``) belong to that chat
and show in it. The chat waits as long as the session keeps working, and says how it goes; it
gives up after IDLE_TIMEOUT without a step, or when the channel stops. State lives in this
process, like the rest of the hub's runtime state.
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import time
from typing import Any, Awaitable, Callable

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ai_hub import calls, config

NEXT_PATH = "/api/session/next"
REPLY_PATH = "/api/session/reply"
LABEL = "Claude Code (your computer)"
POLL = 25.0  # seconds a fetch by the channel waits for a message
SEEN = POLL + 20  # the channel counts as running while it fetched within this many seconds
# A chat waits for Claude Code as long as it keeps working (a long model takes many builds):
# it gives up only after this long without a step (a tool call, a note), or when the channel
# stops (Claude Code was closed). Meanwhile it says how it goes every TICK seconds.
IDLE_TIMEOUT = 20 * 60.0
TICK = 5.0
NOT_RUNNING = (
    "Claude Code on your computer isn't running with the hub's channel: start it there "
    "(see “Claude Code on my computer” on the AI Hub page)."
)
STOPPED = "Claude Code on your computer stopped before it answered (was it closed?)."

Event = dict[str, Any]


@dataclasses.dataclass(eq=False)
class _Turn:
    chat: str
    text: str
    app: str
    room: str
    events: asyncio.Queue
    delivered: float = 0.0  # when the session got it
    active: float = 0.0  # the session's last step in this chat
    steps: int = 0  # its tool calls so far

    def worked(self) -> None:
        self.active = time.monotonic()


_queue: collections.deque[_Turn] = collections.deque()
_current: _Turn | None = None  # the turn the session is working on
_seen = float("-inf")  # when the channel last fetched
_cond: asyncio.Condition | None = None


def _condition() -> asyncio.Condition:
    global _cond
    if _cond is None:
        _cond = asyncio.Condition()
    return _cond


async def _changed() -> None:
    cond = _condition()
    async with cond:
        cond.notify_all()


def running() -> bool:
    """Whether the channel on the owner's computer is fetching messages."""
    return time.monotonic() - _seen < SEEN


async def ask(chat: str, text: str, app: str, room: str, on_event: Callable[[Event], Awaitable[None]]) -> None:
    """Hands a message to the session and reports its tool calls and answers until it is done."""
    global _current
    if not running():
        await on_event({"type": "error", "text": NOT_RUNNING})
        return
    await on_event({"type": "model", "text": LABEL})
    turn = _Turn(chat, text, app, room, asyncio.Queue())
    _queue.append(turn)
    await _changed()
    try:
        while True:
            try:
                event = await asyncio.wait_for(turn.events.get(), timeout=TICK)
            except asyncio.TimeoutError:
                if not running():
                    await on_event({"type": "error", "text": STOPPED if turn.delivered else NOT_RUNNING})
                    return
                now = time.monotonic()
                if turn.delivered and now - turn.active > IDLE_TIMEOUT:
                    minutes = f"{IDLE_TIMEOUT / 60:.0f}"
                    await on_event({"type": "error", "text": f"Claude Code did nothing for {minutes} minutes, so this chat stopped waiting."})
                    return
                await on_event({"type": "status", "text": _status(turn, now)})
                continue
            if event["type"] == "done":
                return
            await on_event(event)
    finally:
        if turn in _queue:
            _queue.remove(turn)
        if _current is turn:
            _current = None
            await _changed()


def _span(seconds: float) -> str:
    s = int(seconds)
    return f"{s} s" if s < 60 else f"{s // 60} min"


def _status(turn: _Turn, now: float) -> str:
    """How the chat goes, for the person waiting."""
    if not turn.delivered:
        return "Waiting for Claude Code (it is still on an earlier message)"
    steps = f"{turn.steps} step" + ("" if turn.steps == 1 else "s")
    return f"Claude Code is working · {_span(now - turn.delivered)} · {steps} · last step {_span(now - turn.active)} ago"


async def tool_called(token_id: str, name: str, args: dict[str, Any]) -> None:
    """A tool call through the MCP address: shown in the chat when the session made it."""
    turn = _current
    if turn is not None and token_id and token_id == config.session_token_id():
        turn.steps += 1
        turn.worked()
        await turn.events.put({"type": "tool", "name": name, "app": "", "args": args})


async def tool_done(token_id: str, name: str, result: calls.Result) -> None:
    turn = _current
    if turn is not None and token_id and token_id == config.session_token_id():
        turn.worked()
        text = "a picture" if result.image else result.text()[:300]
        await turn.events.put({"type": "result", "name": name, "ok": result.ok, "text": text})


# ------------------------------------------------------------ the channel's side


def _is_session(request: Request) -> bool:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    info = config.token_info(token.strip()) if scheme.lower() == "bearer" else None
    return info is not None and info["id"] == config.session_token_id()


def _denied() -> Response:
    return JSONResponse({"error": "Only the token of “Claude Code on my computer” may use this."}, status_code=401)


async def next_message(request: Request) -> Response:
    """For the channel: the next message for the session, or 204 when none came within POLL."""
    global _current, _seen
    if not _is_session(request):
        return _denied()
    _seen = time.monotonic()
    deadline = _seen + POLL
    cond = _condition()
    async with cond:
        while _current is not None or not _queue:
            left = deadline - time.monotonic()
            if left <= 0:
                _seen = time.monotonic()
                return Response(status_code=204)
            try:
                await asyncio.wait_for(cond.wait(), left)
            except asyncio.TimeoutError:
                pass
        if await request.is_disconnected():
            return Response(status_code=204)
        turn = _current = _queue.popleft()
    turn.delivered = turn.active = _seen = time.monotonic()
    return JSONResponse({"chat": turn.chat, "text": turn.text, "app": turn.app, "room": turn.room})


async def reply(request: Request) -> Response:
    """For the channel: the session's answer in the chat it is working on (``done``: false for a
    note on the way)."""
    global _current
    if not _is_session(request):
        return _denied()
    try:
        body = await request.json()
        chat, text, done = str(body.get("chat") or ""), str(body.get("text") or ""), body.get("done") is not False
    except (ValueError, AttributeError):
        return JSONResponse({"error": "expected JSON: {chat, text, done}"}, status_code=400)
    turn = _current
    if turn is None or turn.chat != chat:
        return JSONResponse({"error": f"no chat {chat!r} is waiting for an answer"}, status_code=404)
    turn.worked()
    if text:
        await turn.events.put({"type": "text", "text": text})
    if done:
        await turn.events.put({"type": "done"})
        _current = None
        await _changed()
    return JSONResponse({"ok": True})
