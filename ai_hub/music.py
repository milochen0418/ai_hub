"""The music engine: ACE-Step on the host, which makes songs with singing and remakes
recordings, looked after by a small helper there (re_ddns ``music_engine/music_engine.py``,
installed with ``macos_music_engine.sh``). The hub is the only one that talks to it, so the apps
need no address or token of their own: they ask through ``reflex_ddns_auth.ai.music``.

- ``GET /api/ai/music/status``: the engine as the hub sees it (``{"ok", "installed",
  "server", "tier", "why", ...}``);
- ``POST /api/ai/music``: a song from a spec (caption, lyrics, language, duration; for a cover
  the recording as ``audio_b64``, with ``reference: "source"`` to aim for its sound as well; to
  remake only a stretch of a song, ``task: "repaint"`` with ``repaint_start``/``repaint_end``
  and ``repaint_mode``), answered with lines of JSON while it is made: ``{"status": words,
  "secs"}``, then ``{"done": true, "id", "secs", "metas", "models"}`` (``metas.seed`` makes the
  same song again);
- ``GET /api/ai/music/<id>/audio``: the MP3.

Requests carry the apps' usual token (``reflex_ddns_auth.ai.token``). The hub signs its own
requests to the helper with the same platform secret (audience ``music-engine``), so nothing
long-lived is stored anywhere. The helper's address is ``MUSIC_ENGINE_URL``, else the
Docker host's loopback at the helper's port; the hub's page can change it.
"""

from __future__ import annotations

import asyncio
import collections
import dataclasses
import json
import logging
import os
import time
import uuid
from typing import Any, AsyncIterator

import httpx
import jwt
from reflex_ddns_auth import ai as protocol
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from ai_hub import config

logger = logging.getLogger(__name__)

STATUS_PATH = protocol.MUSIC_STATUS_PATH
MAKE_PATH = protocol.MUSIC_PATH
AUDIO_PATH = protocol.MUSIC_PATH + "/{job}/audio"
AUDIENCE = "music-engine"
DEFAULT_PORT = 3144
SPEC_KEYS = (
    "caption", "lyrics", "language", "duration", "task", "audio_b64", "audio_format", "cover_strength", "reference",
    "repaint_start", "repaint_end", "repaint_mode", "bpm", "key_scale", "time_signature", "seed", "steps", "thinking",
)
MAX_SPEC_BYTES = 48 * 1024 * 1024
POLL = 2.0
# A song keeps being made on the Mac when one look at it fails (the Mac is busy, Docker's way to
# the host hiccups): the hub stops following it only after this long without an answer.
PATIENCE = 120.0
STATE_WORDS = {
    "waiting": "waiting for the engine (another song is being made)",
    "starting": "starting the engine: it loads its models, which takes a minute or two",
    "running": "making the song",
    "slow": "making the song (the Mac is busy: asking again)",
}
INSTALL_WORDS = {"code": "getting the engine's code", "environment": "building its Python environment", "weights": "downloading the weights"}


@dataclasses.dataclass
class Entry:
    """One song made for an app, for the hub's page."""

    at: float
    app: str
    user: str
    task: str
    secs: float
    ok: bool
    error: str = ""


LOG: collections.deque[Entry] = collections.deque(maxlen=100)


def default_url() -> str:
    in_docker = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
    return f"http://{'host.docker.internal' if in_docker else '127.0.0.1'}:{DEFAULT_PORT}"


def helper_url() -> str:
    stored = config.load().get("music_engine", {}).get("url") if isinstance(config.load().get("music_engine"), dict) else ""
    return str(stored or os.environ.get("MUSIC_ENGINE_URL") or default_url()).rstrip("/")


def set_helper_url(url: str) -> None:
    with config._lock:
        data = config.load()
        if url.strip():
            data["music_engine"] = {"url": url.strip().rstrip("/")}
        else:
            data.pop("music_engine", None)
        config.save(data)


def _token() -> str:
    secret = os.environ.get("DDNS_AUTH_SECRET", "")
    if not secret:
        raise RuntimeError("DDNS_AUTH_SECRET is not set, so the music engine can't be asked")
    now = int(time.time())
    return jwt.encode({"iss": "ai-hub", "aud": AUDIENCE, "sub": "ai-hub", "iat": now, "exp": now + 300, "jti": uuid.uuid4().hex}, secret, algorithm="HS256")


async def helper(method: str, path: str, body: dict[str, Any] | None = None, timeout: float = 15.0) -> tuple[int, Any]:
    """One call to the helper: (status code, its JSON). A helper that can't be reached is (0, why)."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            for attempt in (1, 2):  # a helper restarting answers the second time
                try:
                    r = await client.request(method, f"{helper_url()}{path}", json=body, headers={"Authorization": f"Bearer {_token()}"})
                    break
                except httpx.ConnectError:
                    if attempt == 2:
                        raise
                    await asyncio.sleep(0.5)
    except RuntimeError as e:
        return 0, str(e)
    except httpx.HTTPError as e:
        return 0, f"no music engine helper at {helper_url()} ({type(e).__name__})"
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text[:200]


async def status(timeout: float = 15.0) -> dict[str, Any]:
    """The engine's state for the apps and the page. ``ok`` is "a song can be asked for now";
    ``helper`` whether the Mac-side helper answers at all; ``why`` says what is missing."""
    code, data = await helper("GET", "/status", timeout=timeout)
    if code == 0:
        return {"ok": False, "helper": False, "installed": False, "server": "none", "url": helper_url(), "why": str(data)}
    if code != 200 or not isinstance(data, dict):
        return {"ok": False, "helper": True, "installed": False, "server": "none", "url": helper_url(), "why": str(data)[:200]}
    out = {"ok": bool(data.get("installed")), "helper": True, "url": helper_url(), **data}
    if not data.get("installed"):
        out["why"] = "the music engine isn't installed on the Mac yet" if data.get("fits", True) else f"this Mac can't hold the engine: {data.get('why_not')}"
    return out


def _who(request: Request) -> dict[str, Any] | None:
    try:
        return protocol.verify(request.headers.get("authorization", ""))
    except PermissionError as e:
        logger.info("Refused a request to %s: %s", request.url.path, e)
        return None


def _denied() -> Response:
    return JSONResponse({"error": "asking the AI Hub needs a token signed with the platform's secret (reflex_ddns_auth.ai)"}, status_code=401)


async def status_endpoint(request: Request) -> Response:
    if _who(request) is None:
        return _denied()
    data = await status()
    public = {k: data.get(k) for k in ("ok", "helper", "installed", "server", "tier", "why", "fits", "why_not", "jobs", "engine", "memory_gb", "disk_free_gb")}
    return JSONResponse(public)


async def make_endpoint(request: Request) -> Response:
    claims = _who(request)
    if claims is None:
        return _denied()
    if int(request.headers.get("content-length") or 0) > MAX_SPEC_BYTES:
        return JSONResponse({"error": "the request is too big (a recording of 48 MB at most)"}, status_code=413)
    try:
        body = await request.json()
        assert isinstance(body, dict)
    except (ValueError, AssertionError):
        return JSONResponse({"error": "expected JSON: {caption, lyrics, language, duration, ...}"}, status_code=400)
    spec = {k: body[k] for k in SPEC_KEYS if k in body}
    app, user = str(claims.get("app") or ""), str(claims.get("sub") or "")
    code, data = await helper("POST", "/generate", spec, timeout=60.0)
    if code != 202 or not isinstance(data, dict) or not data.get("id"):
        why = data.get("error") if isinstance(data, dict) else str(data)
        LOG.appendleft(Entry(time.time(), app, user, str(spec.get("task") or "text2music"), 0, False, str(why)[:200]))
        return JSONResponse({"error": f"the music engine can't take the song: {why}"}, status_code=503 if code in (0, 503) else 502)
    job = str(data["id"])
    logger.info("%s asks for a song (%s) for %s: job %s", app, spec.get("task") or "text2music", user, job)
    return StreamingResponse(_follow(job, app, user, str(spec.get("task") or "text2music")), media_type="application/x-ndjson", headers={"Cache-Control": "no-store"})


async def _follow(job: str, app: str, user: str, task: str) -> AsyncIterator[str]:
    started = time.time()
    answered = time.monotonic()  # when the helper last said how the song goes
    while True:
        await asyncio.sleep(POLL)
        code, data = await helper("GET", f"/job/{job}")
        if code == 404 or (code == 200 and not isinstance(data, dict)):
            LOG.appendleft(Entry(started, app, user, task, time.time() - started, False, str(data)[:200]))
            yield json.dumps({"error": f"lost the song: {str(data)[:200]}"}) + "\n"
            return
        if code != 200:
            if time.monotonic() - answered < PATIENCE:
                logger.info("The music engine's helper didn't answer about job %s (%s): asking again", job, str(data)[:120])
                yield json.dumps({"status": STATE_WORDS["slow"], "state": "running", "secs": round(time.time() - started)}) + "\n"
                continue
            LOG.appendleft(Entry(started, app, user, task, time.time() - started, False, str(data)[:200]))
            yield json.dumps({"error": f"lost the song: {str(data)[:200]}"}) + "\n"
            return
        answered = time.monotonic()
        state = str(data.get("state") or "")
        if state == "done":
            LOG.appendleft(Entry(started, app, user, task, float(data.get("secs") or 0), True))
            yield json.dumps({"done": True, "id": job, "secs": data.get("secs"), "metas": data.get("metas") or {}, "models": data.get("models") or {}}) + "\n"
            return
        if state == "failed":
            LOG.appendleft(Entry(started, app, user, task, float(data.get("secs") or 0), False, str(data.get("error"))[:200]))
            yield json.dumps({"error": str(data.get("error") or "the engine failed")}) + "\n"
            return
        words = STATE_WORDS.get(state, state)
        yield json.dumps({"status": words, "state": state, "secs": data.get("secs")}) + "\n"


async def audio_endpoint(request: Request) -> Response:
    if _who(request) is None:
        return _denied()
    job = str(request.path_params.get("job") or "")
    if not job.isalnum():
        return JSONResponse({"error": "no such song"}, status_code=404)
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            r = await client.get(f"{helper_url()}/job/{job}/audio", headers={"Authorization": f"Bearer {_token()}"})
    except (httpx.HTTPError, RuntimeError) as e:
        return JSONResponse({"error": f"the music engine didn't answer: {e}"}, status_code=502)
    if r.status_code != 200:
        return JSONResponse({"error": "no such song"}, status_code=404)
    return Response(r.content, media_type="audio/mpeg", headers={"Cache-Control": "private, max-age=3600"})


# ------------------------------------------------------------ for the hub's page


async def install(tier: str = "") -> str:
    """Starts the install on the Mac; "" when it began, else why not."""
    code, data = await helper("POST", "/install", {"tier": tier} if tier else {}, timeout=30.0)
    if code == 202:
        return ""
    return str(data.get("error") if isinstance(data, dict) else data)


async def start() -> str:
    code, data = await helper("POST", "/start", {}, timeout=30.0)
    return "" if code == 200 else str(data.get("error") if isinstance(data, dict) else data)


async def stop() -> str:
    code, data = await helper("POST", "/stop", {}, timeout=60.0)
    return "" if code == 200 else str(data.get("error") if isinstance(data, dict) else data)


def install_words(status: dict[str, Any]) -> str:
    """What the install job is doing, for the page."""
    job = status.get("install") or {}
    state = job.get("state")
    if state == "running":
        step = INSTALL_WORDS.get(str(job.get("step")), str(job.get("step")))
        if job.get("gb_total"):
            step += f": {job.get('gb_done', 0)} of {job['gb_total']} GB"
        return f"installing: {step}"
    if state == "failed":
        return f"the install failed: {job.get('error')}"
    if state == "done":
        return "installed"
    return ""
