"""Apps ask the hub's models: the door for ``reflex_ddns_auth.ai``.

An app on the platform (Cellora's notebook assistant, say) sends a role, its rules and a
conversation; the hub answers with the model that role is set to, streamed as lines of JSON.
The app holds no API key and doesn't know which model answers: changing a role on the hub's
page changes every app at once.

- ``POST /api/ai/stream``: ``{role, system, messages, max_tokens, effort}`` -> NDJSON:
  ``{"model": label}``, then ``{"text": piece}`` as the answer comes, then ``{"done": true,
  "stop": "end_turn" | "max_tokens" | "refusal"}``; ``{"error": message}`` once the answer
  has begun, or a 4xx/5xx JSON answer before.
- ``GET /api/ai/status?role=``: ``{"ok", "model", "provider", "local"}``, 404 when nothing
  answers the role.

A request carries a short-lived token signed with the platform's ``DDNS_AUTH_SECRET``
(``reflex_ddns_auth.ai.token``) naming the app and the person asking; the hub verifies it the
way the apps verify the hub's tool calls. A role set to the Claude Code on the owner's computer
answers the apps too (``smart`` and ``coder``; a quick role gets the role's default model, since
that Claude Code takes one message at a time): the hub's owner decides so for everyone using
the app, on their own Claude login, so that is for their own development.
"""

from __future__ import annotations

import collections
import dataclasses
import json
import logging
import time
from typing import Any, AsyncIterator

from reflex_ddns_auth import ai as protocol
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from ai_hub import brains, config

logger = logging.getLogger(__name__)

STREAM_PATH = protocol.STREAM_PATH
STATUS_PATH = protocol.STATUS_PATH
MAX_TOKENS = 16000  # the most an app may ask an answer to be
MAX_CHARS = 1_000_000  # of a request's rules and conversation together
EFFORTS = ("low", "medium", "high")


@dataclasses.dataclass
class Entry:
    """One answer given to an app, for the hub's page."""

    at: float
    app: str
    user: str
    role: str
    model: str
    chars: int  # of the answer
    ms: int
    error: str = ""


LOG: collections.deque[Entry] = collections.deque(maxlen=200)


def _who(request: Request) -> dict[str, Any] | None:
    """The app and person a request is from (its token's claims), or None."""
    try:
        return protocol.verify(request.headers.get("authorization", ""))
    except PermissionError as e:
        logger.info("Refused a request to %s: %s", request.url.path, e)
        return None


def _denied() -> Response:
    return JSONResponse({"error": "asking the AI Hub needs a token signed with the platform's secret (reflex_ddns_auth.ai)"}, status_code=401)


async def status(request: Request) -> Response:
    """What answers a role for the apps (``?role=``, ``smart`` by default)."""
    claims = _who(request)
    if claims is None:
        return _denied()
    role = request.query_params.get("role") or "smart"
    if role not in config.ROLES:
        return JSONResponse({"ok": False, "error": f"unknown role {role!r}: one of {', '.join(config.ROLES)}"}, status_code=400)
    app, user = str(claims.get("app") or ""), str(claims.get("sub") or "")
    config.note_ask(app, role)
    try:
        provider, _, label = await brains.describe(role, app, user)
    except LookupError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=404)
    return JSONResponse({"ok": True, "model": label, "provider": provider.name, "local": provider.local})


async def stream(request: Request) -> Response:
    """An answer for an app, streamed (see the module)."""
    claims = _who(request)
    if claims is None:
        return _denied()
    try:
        body = await request.json()
        assert isinstance(body, dict)
    except (ValueError, AssertionError):
        return JSONResponse({"error": "expected JSON: {role, system, messages, max_tokens, effort}"}, status_code=400)
    role = str(body.get("role") or "smart")
    if role not in config.ROLES:
        return JSONResponse({"error": f"unknown role {role!r}: one of {', '.join(config.ROLES)}"}, status_code=400)
    system = str(body.get("system") or "")
    messages = _messages(body.get("messages"))
    if messages is None:
        return JSONResponse({"error": "messages must be a list of {role: user | assistant, content: text}, ending with the person's"}, status_code=400)
    if len(system) + sum(len(m["content"]) for m in messages) > MAX_CHARS:
        return JSONResponse({"error": f"the request is too long (more than {MAX_CHARS:,} characters)"}, status_code=413)
    try:
        max_tokens = max(1, min(int(body.get("max_tokens") or 4096), MAX_TOKENS))
    except (TypeError, ValueError):
        max_tokens = 4096
    effort = str(body.get("effort") or "medium")
    if effort not in EFFORTS:
        effort = "medium"
    app, user = str(claims.get("app") or ""), str(claims.get("sub") or "")
    config.note_ask(app, role)
    try:
        answer = await brains.answer(role, system, messages, max_tokens, effort, app, user)
    except LookupError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except Exception as e:  # the provider couldn't be reached, say
        logger.warning("Cannot answer %s for %s: %s", role, claims.get("app"), e)
        return JSONResponse({"error": f"{type(e).__name__}: {e}"}, status_code=502)
    logger.info("%s asks %s (%s) for %s", app, role, answer.label, user)
    return StreamingResponse(_lines(answer, Entry(time.time(), app, user, role, answer.label, 0, 0)), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})


def _messages(raw: Any) -> list[dict[str, str]] | None:
    if not isinstance(raw, list) or not raw:
        return None
    out = []
    for m in raw:
        if not isinstance(m, dict) or m.get("role") not in ("user", "assistant"):
            return None
        out.append({"role": str(m["role"]), "content": str(m.get("content") or "")})
    return out if out[-1]["role"] == "user" else None


async def _lines(answer: brains.Answer, entry: Entry) -> AsyncIterator[str]:
    started = time.monotonic()
    yield json.dumps({"model": answer.label}) + "\n"
    try:
        async for piece in answer.text:
            entry.chars += len(piece)
            yield json.dumps({"text": piece}, ensure_ascii=False) + "\n"
    except Exception as e:
        logger.warning("%s failed while answering: %s", answer.label, e)
        entry.error = str(e)[:200]
        yield json.dumps({"error": f"{answer.label}: {e}"}, ensure_ascii=False) + "\n"
        return
    finally:
        entry.ms = int((time.monotonic() - started) * 1000)
        LOG.appendleft(entry)
    yield json.dumps({"done": True, "stop": answer.stop}) + "\n"
