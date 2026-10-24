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
and show in it. The rest of what it does comes from its hooks (``ACTIVITY_PATH``; setup.sh puts
them in the session folder's settings): its web searches and the pages it reads, what it writes
in its terminal, when it waits for the owner there, and when its turn ends. So the chat says
what it is doing all along, "thinking" between steps, and waits until it answers, ends its turn
without an answer (the chat then shows what it last wrote), fails, or is closed. Without hooks
the hub can't tell long thinking from a forgotten message: the chat then gives up after
IDLE_TIMEOUT without a step. State lives in this process, like the rest of the hub's runtime
state.
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
ACTIVITY_PATH = "/api/session/activity"
LABEL = "Claude Code (your computer)"
POLL = 25.0  # seconds a fetch by the channel waits for a message
SEEN = POLL + 20  # the channel counts as running while it fetched within this many seconds
# Without the session's hooks, a chat gives up after this long without a step (a tool call, a
# note), or when the channel stops (Claude Code was closed). Meanwhile it says how it goes every
# TICK seconds, and at once when that changes.
IDLE_TIMEOUT = 20 * 60.0
TICK = 5.0
# After an answer, the next message waits this long at most for the end of the session's turn,
# so that end isn't taken for the next message's.
SETTLE = 30.0
NOTE_CHARS = 400  # what a note in the chat shows of a longer text
NOT_RUNNING = (
    "Claude Code on your computer isn't running with the hub's channel: start it there "
    "(see “Claude Code on my computer” on the AI Hub page)."
)
STOPPED = "Claude Code on your computer stopped before it answered (was it closed?)."
CLEARED = "Claude Code on your computer started a new conversation (/clear) before it answered."
NO_ANSWER = "Claude Code ended its turn without answering here."
LAST_WORDS = NO_ANSWER + " What it last wrote in its terminal:"
WAITING = ("permission_prompt", "elicitation_dialog", "elicitation_url_dialog", "agent_needs_input")
EARLIER = "finishing earlier work"

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
    steps: int = 0  # its tool calls so far (the apps' and its own, such as web searches)
    # From its hooks: what it does now ("" while it thinks) and since when, how hard it thinks,
    # and the text it is writing (``note``, a note in the chat once a step follows it) with the
    # last line of it (``said``).
    doing: str = ""
    since: float = 0.0
    effort: str = ""
    note: str = ""
    note_id: str = ""
    said: str = ""
    # Its turn on this message, by the prompt_id its hook events carry, and the turn it was on
    # when it got the message (one the person stopped waiting for may still run).
    prompt: str = ""
    before: str = ""

    def worked(self) -> None:
        self.active = time.monotonic()

    def now_doing(self, what: str) -> None:
        self.doing, self.since = what, time.monotonic()
        self.worked()


_queue: collections.deque[_Turn] = collections.deque()
_current: _Turn | None = None  # the turn the session is working on
_seen = float("-inf")  # when the channel last fetched
_settling = 0.0  # when it answered, while the end of its turn is still to come
_prompt = ""  # the prompt_id of its latest hook event: the turn it is on
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
                if turn.delivered and not config.session_hooked() and now - turn.active > IDLE_TIMEOUT:
                    minutes = f"{IDLE_TIMEOUT / 60:.0f}"
                    await on_event({
                        "type": "error",
                        "text": f"Claude Code did nothing the hub could see for {minutes} minutes, so this chat "
                        "stopped waiting. To see its searching and thinking here too, set it up again "
                        "(claude_channel/setup.sh) and restart it.",
                    })
                    return
                await on_event({"type": "status", "text": _status(turn, now)})
                continue
            if event["type"] == "done":
                return
            if event["type"] == "status":  # what it does changed: say so now, not at the next tick
                await on_event({"type": "status", "text": _status(turn, time.monotonic())})
                continue
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


def _clip(text: str, size: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= size else text[: size - 1] + "…"


def _status(turn: _Turn, now: float) -> str:
    """How the chat goes, for the person waiting: what Claude Code does now, and (on a second
    line) how long it has been on the message."""
    if not turn.delivered:
        return "Waiting for Claude Code (it is still on an earlier message)"
    steps = f"{turn.steps} step" + ("" if turn.steps == 1 else "s")
    if not config.session_hooked():
        return f"Claude Code is working · {_span(now - turn.delivered)} · {steps} · last step {_span(now - turn.active)} ago"
    doing = turn.doing or "thinking" + (f" ({turn.effort} effort)" if turn.effort else "")
    status = f"Claude Code is {doing} · {_span(now - turn.since)}\n{_span(now - turn.delivered)} on this message · {steps}"
    return status + (f" · it last wrote: “{turn.said}”" if turn.said else "")


async def _note(turn: _Turn) -> None:
    """The text it wrote before a step, as a note in the chat (the text it ends with goes with
    its answer instead)."""
    text, turn.note, turn.note_id = turn.note.strip(), "", ""
    if text:
        await turn.events.put({"type": "note", "text": _clip(text, NOTE_CHARS)})


async def tool_called(token_id: str, name: str, args: dict[str, Any]) -> None:
    """A tool call through the MCP address: shown in the chat when the session made it."""
    turn = _current
    if turn is not None and token_id and token_id == config.session_token_id():
        await _note(turn)
        turn.steps += 1
        turn.now_doing(f"using {name}")
        await turn.events.put({"type": "tool", "name": name, "app": "", "args": args})
        await turn.events.put({"type": "status"})


async def tool_done(token_id: str, name: str, result: calls.Result) -> None:
    turn = _current
    if turn is not None and token_id and token_id == config.session_token_id():
        turn.now_doing("")
        text = "a picture" if result.image else result.text()[:300]
        await turn.events.put({"type": "result", "name": name, "ok": result.ok, "text": text})
        await turn.events.put({"type": "status"})


def _web(tool: str, args: dict[str, Any]) -> tuple[str, str]:
    """What a tool call of its own is, (for the status line, for the chat): its web searches and
    the pages it reads, or any other tool by name."""
    if tool == "WebSearch":
        query = _clip(args.get("query") or "", 120)
        return f"searching the web: “{_clip(query, 80)}”", f"Web search: {query}"
    if tool == "WebFetch":
        url = str(args.get("url") or "")
        page = _clip(url.split("://", 1)[-1], 100)
        return f"reading {_clip(page, 70)}", f"Read: {page}"
    return f"using {tool}", ""


async def _finish(turn: _Turn, *events: Event) -> None:
    """Ends the chat's wait with these last events, and lets the next message go."""
    global _current
    for event in events:
        await turn.events.put(event)
    await turn.events.put({"type": "done"})
    if _current is turn:
        _current = None
        await _changed()


async def _hook(event: dict[str, Any]) -> None:
    """One of the session's hook events, applied to the chat it works on."""
    global _settling, _prompt
    name = event.get("hook_event_name")
    prompt = str(event.get("prompt_id") or "")
    _prompt = prompt or _prompt
    turn = _current
    if name in ("Stop", "StopFailure", "SessionEnd") and _settling:
        # The end of the turn it answered in: the next message may go.
        _settling = 0.0
        await _changed()
    if name == "SessionEnd":
        if turn is not None:
            await _finish(turn, {"type": "error", "text": CLEARED if event.get("reason") in ("clear", "resume") else STOPPED})
        return
    if turn is None:
        return  # its own work in its terminal, not a chat's
    if prompt and name != "Notification":
        if not turn.prompt and prompt != turn.before:
            turn.prompt = prompt  # it began on this message
        if prompt != turn.prompt:
            if turn.doing != EARLIER:
                turn.now_doing(EARLIER)
                await turn.events.put({"type": "status"})
            return
    effort = event.get("effort")
    if isinstance(effort, dict) and effort.get("level"):
        turn.effort = str(effort["level"])
    if name == "PreToolUse":
        tool, args = str(event.get("tool_name") or ""), event.get("tool_input")
        if tool.startswith("mcp__"):
            return  # the apps' tools show through the MCP address, the reply as the answer
        await _note(turn)
        doing, row = _web(tool, args if isinstance(args, dict) else {})
        turn.steps += 1
        turn.now_doing(doing)
        await turn.events.put({"type": "web", "text": row} if row else {"type": "tool", "name": tool, "app": "", "args": {}})
    elif name in ("PostToolUse", "PostToolUseFailure"):
        tool = str(event.get("tool_name") or "")
        if tool.startswith("mcp__"):
            return
        if name == "PostToolUseFailure" and not event.get("is_interrupt"):
            await turn.events.put({"type": "error", "text": f"{tool}: {_clip(event.get('error') or 'failed', 300)}"})
        turn.now_doing("")
    elif name == "MessageDisplay":
        message = str(event.get("message_id") or "")
        if message != turn.note_id:
            await _note(turn)
            turn.note_id = message
        turn.note += str(event.get("delta") or "")
        lines = [line.strip(" #*->`") for line in turn.note.splitlines()]
        lines = [line for line in lines if line]
        if lines:
            turn.said = _clip(lines[-1], 160)
        turn.worked()
    elif name == "Notification":
        kind = event.get("notification_type")
        if kind in WAITING:
            turn.now_doing("waiting for you in its terminal: " + _clip(event.get("message") or "", 160))
        elif kind == "idle_prompt":  # it has been idle a minute: its Stop went missing
            await _finish(turn, {"type": "info", "text": NO_ANSWER})
            return
    elif name == "PreCompact":
        turn.now_doing("compacting its memory of the conversation")
    elif name == "PostCompact":
        turn.now_doing("")
    elif name == "Stop":
        if event.get("background_tasks"):
            return  # it wakes up again for work it left running
        last = str(event.get("last_assistant_message") or "").strip()
        turn.note = ""
        await _finish(turn, *([{"type": "info", "text": LAST_WORDS}, {"type": "text", "text": last}] if last else [{"type": "info", "text": NO_ANSWER}]))
        return
    elif name == "StopFailure":
        problem = event.get("last_assistant_message") or event.get("error") or "an error"
        await _finish(turn, {"type": "error", "text": f"Claude Code stopped: {_clip(problem, 300)}"})
        return
    await turn.events.put({"type": "status"})


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
        while _current is not None or not _queue or (_settling and time.monotonic() - _settling < SETTLE):
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
    turn.delivered = turn.active = turn.since = _seen = time.monotonic()
    turn.before = _prompt
    return JSONResponse({"chat": turn.chat, "text": turn.text, "app": turn.app, "room": turn.room})


async def reply(request: Request) -> Response:
    """For the channel: the session's answer in the chat it is working on (``done``: false for a
    note on the way)."""
    global _current, _settling
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
    turn.note = turn.note_id = ""  # what it wrote just before is what it sends
    if text:
        await turn.events.put({"type": "text", "text": text})
    if done:
        await turn.events.put({"type": "done"})
        _current = None
        if config.session_hooked():
            _settling = time.monotonic()
        await _changed()
    return JSONResponse({"ok": True})


async def drop(chat: str) -> None:
    """The person stopped waiting for this chat (they began a new one): it leaves the queue, or,
    when the session is on it, the session takes the next message once its turn ends."""
    global _current, _settling
    turns = [t for t in _queue if t.chat == chat]
    if _current is not None and _current.chat == chat:
        turns.append(_current)
        _current = None
        if config.session_hooked():
            _settling = time.monotonic()
    for turn in turns:
        if turn in _queue:
            _queue.remove(turn)
        while not turn.events.empty():  # nothing more of it reaches the new chat
            turn.events.get_nowait()
        await turn.events.put({"type": "done"})
    if turns:
        await _changed()


async def activity(request: Request) -> Response:
    """For the session's hooks (claude_channel/setup.sh writes them): what Claude Code does, for
    the chat it works on. Answers at once and with no body, whatever came: Claude Code waits for
    some hooks (each line it shows waits for this), and a body could change what it does."""
    if not _is_session(request):
        return _denied()
    try:
        event = await request.json()
    except ValueError:
        event = None
    if isinstance(event, dict):
        if not config.session_hooked():
            config.mark_session_hooked()
        await _hook(event)
    return Response(status_code=204)
