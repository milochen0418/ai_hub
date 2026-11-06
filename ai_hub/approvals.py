"""Asking the person before a ``danger`` tool runs (``reflex_ddns_auth.tools``: an app marks the
tools whose changes must wait for a person's yes), and when an app asks for their OK in the middle
of a call (``reflex_ddns_auth.tools.ask_first``: to read a file that isn't open to the AI, say):
then the chat shows what the AI wants with Allow, Allow all in this chat and Decline, and on an
Allow the hub calls again with the person's consent for exactly that call (``call_asking``).

When a model working in an assistant chat calls one, the call waits here: the chat shows what
the tool would do with Accept, Accept all in this chat and Reject (``AssistantState.decide``),
and the tool runs only after an Accept. Anything else leaves the app as it was, and the model is
told so. This holds for the hub's own loop (``ai_hub.agent``) and for the Claude Code on the
owner's computer working on a chat (``ai_hub.mcp_endpoint``). An agent of its own, connected with
a token of its own, has no chat here: its client asks its user (``destructive_hint``).

Waiting state lives in this process, like the rest of the hub's runtime state.
"""

from __future__ import annotations

import asyncio
import json
import secrets
from typing import Any, Awaitable, Callable

from ai_hub import calls
from ai_hub.registry import Tool

# Claude Code gives up on a network MCP tool after 5 minutes without an answer: the chat decides
# a little earlier, so the session hears why nothing happened.
WAIT = 240.0
DETAILS_CHARS = 20_000
ACCEPT, ACCEPT_ALL, REJECT, TIMEOUT = "accept", "all", "reject", "timeout"

REJECTED = "The person rejected this change, so nothing was changed. Ask what they want instead; don't try it again as it is."
NO_ANSWER = "The person didn't answer in time, so nothing was changed. Ask them before trying again."
NOWHERE = "This change needs the person's Accept, which only the assistant's chat can ask for, so nothing was changed. Tell them to ask the assistant."
DECLINED = "The person didn't allow this, so it wasn't done: {what}. Work without it, or ask them why."
NO_ANSWER_ASK = "The person didn't answer in time, so it wasn't done: {what}. Ask them before trying again."
NOWHERE_ASK = "This needs the person's OK, which only the assistant's chat can ask for, so it wasn't done: {what}. Tell them to ask the assistant."

Event = dict[str, Any]

_pending: dict[str, tuple[str, asyncio.Future]] = {}  # request -> (chat, its answer)
_trusted: set[str] = set()  # chats where the person accepted everything


def needs_asking(tool: Tool) -> bool:
    return tool.kind == "danger"


def _text(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _item(value: Any) -> str:
    """An item of a list on one line: what it is first (its ``command`` or ``name``), then its
    fields, ``key=value``, those of a nested object among them."""
    if not isinstance(value, dict):
        return _text(value)
    head = [str(value[k]) for k in ("command", "name") if k in value]
    fields: dict[str, Any] = {}
    for key, v in value.items():
        if key in ("command", "name"):
            continue
        fields.update(v if isinstance(v, dict) else {key: v})
    return "  ".join(head + [f"{k}={_text(v)}" for k, v in fields.items()])


def describe(args: dict[str, Any]) -> tuple[str, str]:
    """What a call does, for the person: its ``summary`` (what the model says it does, when the
    tool takes one), and the rest of its arguments as plain lines; long texts (code) whole, on
    lines of their own, the items of a list one per line."""
    summary = str(args.get("summary") or "").strip()
    lines: list[str] = []
    for key, value in args.items():
        if key in ("summary", "room"):
            continue
        if isinstance(value, list):
            lines.append(f"{key}:")
            lines += [f"  {_item(item)}" for item in value]
        elif isinstance(value, str) and ("\n" in value or len(value) > 80):
            lines += [f"{key}:", value]
        else:
            lines.append(f"{key}: {_text(value)}")
    details = "\n".join(lines)
    if len(details) > DETAILS_CHARS:
        details = details[:DETAILS_CHARS] + f"\n… ({len(details) - DETAILS_CHARS:,} more characters)"
    return summary, details


async def ask(chat: str, tool: Tool, args: dict[str, Any], post: Callable[[Event], Awaitable[None]]) -> calls.Result | None:
    """Waits for the person's answer in ``chat`` (``post`` shows things there): None when the
    tool may run, else the result that tells the model why it didn't."""
    summary, details = describe(args)
    decision = await _answer(chat, post, {"title": f"{tool.title} ({tool.app})", "summary": summary, "details": details},
                             "Waiting for you to accept or reject the change")
    if decision in (ACCEPT, ACCEPT_ALL):
        return None
    return calls.Result(False, error=NO_ANSWER if decision == TIMEOUT else REJECTED)


async def call_asking(tool: Tool, args: dict[str, Any], user: str, via: str,
                      chat: str | None, post: Callable[[Event], Awaitable[None]] | None) -> calls.Result:
    """Calls the tool; when the app asks for the person's OK first, asks in ``chat`` and, when
    they allow it, calls again with their consent. With no chat to ask in, the model is told so."""
    result = await calls.call(tool, args, user, via)
    if result.ok or not result.ask:
        return result
    what = str(result.ask.get("what") or tool.title)
    if chat is None or post is None:
        return calls.Result(False, error=NOWHERE_ASK.format(what=what))
    decision = await _answer(chat, post, {"ask": "read", "title": f"{tool.title} ({tool.app})", "summary": what,
                                          "details": str(result.ask.get("detail") or "")},
                             "Waiting for you to allow or decline")
    if decision not in (ACCEPT, ACCEPT_ALL):
        return calls.Result(False, error=(NO_ANSWER_ASK if decision == TIMEOUT else DECLINED).format(what=what))
    return await calls.call(tool, args, user, via, consent=str(result.ask.get("key") or ""))


async def _answer(chat: str, post: Callable[[Event], Awaitable[None]], card: dict[str, Any], waiting: str) -> str:
    """Shows the card in ``chat`` and waits for the person's answer (at once in a chat where they
    accepted everything)."""
    if chat in _trusted:
        return ACCEPT_ALL
    request = secrets.token_hex(6)
    answer: asyncio.Future = asyncio.get_running_loop().create_future()
    _pending[request] = (chat, answer)
    try:
        await post({"type": "approve", "id": request, **card})
        await post({"type": "status", "text": waiting})
        try:
            decision = await asyncio.wait_for(asyncio.shield(answer), WAIT)
        except asyncio.TimeoutError:
            decision = TIMEOUT
    except BaseException:
        # The call went away (the session closed it, the hub is stopping): the card must not stay open.
        if not answer.done():
            answer.cancel()
        raise
    finally:
        _pending.pop(request, None)
    if decision == ACCEPT_ALL:
        _trusted.add(chat)
    await post({"type": "decided", "id": request, "decision": decision})
    await post({"type": "status", "text": ""})
    return decision


def decide(request: str, decision: str) -> bool:
    """The person's answer to a request; False when nothing waits for it any more."""
    entry = _pending.get(request)
    if entry is None or entry[1].done() or decision not in (ACCEPT, ACCEPT_ALL, REJECT):
        return False
    entry[1].set_result(decision)
    return True


def forget(chat: str) -> None:
    """A chat that ended: what it still asks counts as rejected, and its trust goes with it."""
    _trusted.discard(chat)
    for owner, answer in list(_pending.values()):
        if owner == chat and not answer.done():
            answer.set_result(REJECT)
