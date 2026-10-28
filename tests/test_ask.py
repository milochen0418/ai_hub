"""The apps' door (ai_hub.ask): the signed token, the roles, streamed answers, and the library
on the apps' side (reflex_ddns_auth.ai) reading them."""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from reflex_ddns_auth import ai as protocol
from starlette.applications import Starlette
from starlette.routing import Route

from ai_hub import ask, brains, config

SECRET = "test-secret-long-enough-for-hs256-hmac-keys"


def hub_app() -> Starlette:
    return Starlette(routes=[Route(ask.STREAM_PATH, ask.stream, methods=["POST"]), Route(ask.STATUS_PATH, ask.status)])


@pytest.fixture(autouse=True)
def platform(monkeypatch):
    monkeypatch.setenv("DDNS_AUTH_SECRET", SECRET)
    monkeypatch.setenv("SERVICE_SUBDOMAIN", "cellora")
    monkeypatch.setenv("DDNS_AI_HUB_URL", "http://hub")
    monkeypatch.setattr(protocol, "_transport", httpx.ASGITransport(app=hub_app()))
    protocol._clear_cache()


@pytest.fixture
def client():
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=hub_app()), base_url="http://hub")


def fake_answer(monkeypatch, pieces=("x = ", "1"), stop="end_turn", label="Ollama (qwen2.5-coder:7b)"):
    """A model that answers with ``pieces``; what it was asked is kept in the returned dict."""
    seen = {}

    async def answer(role, system, messages, max_tokens, effort, app="", user=""):
        seen.update(role=role, system=system, messages=messages, max_tokens=max_tokens, effort=effort, app=app, user=user)

        async def text():
            for p in pieces:
                yield p
            out.stop = stop

        out = brains.Answer(label, text())
        return out

    monkeypatch.setattr(brains, "answer", answer)
    return seen


def auth(user: str = "milo") -> dict:
    return {"Authorization": f"Bearer {protocol.token(user)}"}


def lines(text: str) -> list[dict]:
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_needs_the_platforms_token(client, monkeypatch) -> None:
    fake_answer(monkeypatch)

    async def run():
        async with client:
            no_token = await client.post(ask.STREAM_PATH, json={"role": "smart", "messages": [{"role": "user", "content": "q"}]})
            bad = await client.get(ask.STATUS_PATH, headers={"Authorization": "Bearer nope"})
            return no_token.status_code, bad.status_code

    assert asyncio.run(run()) == (401, 401)


def test_streams_an_answer(client, monkeypatch) -> None:
    seen = fake_answer(monkeypatch)

    async def run():
        async with client:
            r = await client.post(
                ask.STREAM_PATH,
                json={"role": "coder", "system": "Edit one cell.", "messages": [{"role": "user", "content": "write x"}], "max_tokens": 500, "effort": "low"},
                headers=auth(),
            )
            return r.status_code, r.headers["content-type"], lines(r.text)

    status, content_type, events = asyncio.run(run())
    assert status == 200 and content_type.startswith("application/x-ndjson")
    assert events == [{"model": "Ollama (qwen2.5-coder:7b)"}, {"text": "x = "}, {"text": "1"}, {"done": True, "stop": "end_turn"}]
    assert seen == {"role": "coder", "system": "Edit one cell.", "messages": [{"role": "user", "content": "write x"}], "max_tokens": 500, "effort": "low", "app": "cellora", "user": "milo"}


def test_bad_requests(client, monkeypatch) -> None:
    fake_answer(monkeypatch)

    async def run():
        async with client:
            out = []
            for body in (
                {"role": "genius", "messages": [{"role": "user", "content": "q"}]},
                {"role": "smart", "messages": []},
                {"role": "smart", "messages": [{"role": "system", "content": "q"}]},
                {"role": "smart", "messages": [{"role": "assistant", "content": "q"}]},
                {"role": "smart", "messages": [{"role": "user", "content": "q" * (ask.MAX_CHARS + 1)}]},
            ):
                r = await client.post(ask.STREAM_PATH, json=body, headers=auth())
                out.append(r.status_code)
            r = await client.post(ask.STREAM_PATH, content=b"not json", headers=auth())
            out.append(r.status_code)
            return out

    assert asyncio.run(run()) == [400, 400, 400, 400, 413, 400]


def test_limits_are_kept(client, monkeypatch) -> None:
    seen = fake_answer(monkeypatch)

    async def run():
        async with client:
            await client.post(ask.STREAM_PATH, json={"role": "smart", "messages": [{"role": "user", "content": "q"}], "max_tokens": 10**9, "effort": "max"}, headers=auth())

    asyncio.run(run())
    assert seen["max_tokens"] == ask.MAX_TOKENS and seen["effort"] == "medium"


def test_an_error_on_the_way(client, monkeypatch) -> None:
    async def answer(role, system, messages, max_tokens, effort, app="", user=""):
        async def text():
            yield "so far"
            raise RuntimeError("HTTP 404: model not found")

        return brains.Answer("Ollama (gone)", text())

    monkeypatch.setattr(brains, "answer", answer)

    async def run():
        async with client:
            r = await client.post(ask.STREAM_PATH, json={"role": "smart", "messages": [{"role": "user", "content": "q"}]}, headers=auth())
            return lines(r.text)

    assert asyncio.run(run()) == [{"model": "Ollama (gone)"}, {"text": "so far"}, {"error": "Ollama (gone): HTTP 404: model not found"}]


def test_status_and_a_role_nobody_can_answer(client, monkeypatch) -> None:
    config.set_role("coder", "anthropic/claude-opus-5-5")  # no key: not a known provider
    real = brains.describe

    async def describe(role, app=None, user=""):
        if role == "coder":
            return await real(role, app, user)  # raises: no such provider
        return config.providers()["ollama"], "qwen2.5:7b", "Ollama (qwen2.5:7b)"

    monkeypatch.setattr(brains, "describe", describe)

    async def run():
        async with client:
            ok = await client.get(ask.STATUS_PATH, params={"role": "fast"}, headers=auth())
            none = await client.get(ask.STATUS_PATH, params={"role": "coder"}, headers=auth())
            odd = await client.get(ask.STATUS_PATH, params={"role": "genius"}, headers=auth())
            return ok.json(), none.status_code, none.json()["ok"], odd.status_code

    good, missing, missing_ok, odd = asyncio.run(run())
    assert good == {"ok": True, "model": "Ollama (qwen2.5:7b)", "provider": "ollama", "local": True}
    assert missing == 404 and missing_ok is False and odd == 400


def test_the_library_end_to_end(monkeypatch) -> None:
    """reflex_ddns_auth.ai on an app's side, against the hub's door."""
    seen = fake_answer(monkeypatch, pieces=("Hel", "lo"), stop="max_tokens", label="Claude (claude-opus-5-5)")

    async def run():
        reply = protocol.stream("smart", "Be brief.", [{"role": "user", "content": "hi"}], max_tokens=50, effort="high", user="milo@example.com")
        return [p async for p in reply], reply.model, reply.stop, await protocol.status("smart")

    monkeypatch.setattr(brains, "describe", _describe_ollama)
    pieces, model, stop, status = asyncio.run(run())
    assert pieces == ["Hel", "lo"] and model == "Claude (claude-opus-5-5)" and stop == "max_tokens"
    assert seen["effort"] == "high" and seen["max_tokens"] == 50
    assert status["ok"] and status["model"] == "Ollama (qwen2.5:7b)"


async def _describe_ollama(role, app=None, user=""):
    return config.providers()["ollama"], "qwen2.5:7b", "Ollama (qwen2.5:7b)"


def test_a_model_chosen_for_one_app(client, monkeypatch) -> None:
    """The page's choice for an app's role comes first; the app shows up once it has asked."""
    seen = {}

    async def stream_text(provider, model, system, messages, max_tokens, effort, answer):
        seen["model"] = model
        yield "ok"

    from ai_hub.brains import ollama

    monkeypatch.setattr(ollama, "stream_text", stream_text)
    config.set_role("coder", "ollama/qwen2.5-coder:7b")

    async def run():
        async with client:
            before = await client.get(ask.STATUS_PATH, params={"role": "coder"}, headers=auth())
            config.set_app_target("cellora", "coder", "ollama/qwen2.5:7b")
            after = await client.get(ask.STATUS_PATH, params={"role": "coder"}, headers=auth())
            r = await client.post(ask.STREAM_PATH, json={"role": "coder", "messages": [{"role": "user", "content": "q"}]}, headers=auth())
            return before.json()["model"], after.json()["model"], lines(r.text)

    before, after, events = asyncio.run(run())
    assert before == "Ollama (qwen2.5-coder:7b)" and after == "Ollama (qwen2.5:7b)" and seen["model"] == "qwen2.5:7b"
    assert config.apps_asking() == {"cellora": {"coder": "ollama/qwen2.5:7b"}}
    assert events[0] == {"model": "Ollama (qwen2.5:7b)"}
    assert [(e.app, e.user, e.role, e.model, e.chars) for e in ask.LOG][0] == ("cellora", "milo", "coder", "Ollama (qwen2.5:7b)", 2)
    # The assistant itself (no app) keeps the role's own model.
    assert asyncio.run(brains.describe("coder"))[2] == "Ollama (qwen2.5-coder:7b)"
    # A choice whose provider is gone falls back to the role's own.
    config.set_app_target("cellora", "coder", "anthropic/claude-opus-5-5")
    assert asyncio.run(brains.describe("coder", "cellora"))[2] == "Ollama (qwen2.5-coder:7b)"
    config.forget_app("cellora")
    assert config.apps_asking() == {}


def test_a_role_on_the_owners_claude_code_answers_the_apps(client, monkeypatch) -> None:
    """A role set to the owner's Claude Code answers an app's smart and coder asks, whole, for
    everyone; a quick role gets the role's default model."""
    config.make_session_token()
    config.set_role("coder", f"{config.SESSION}/")
    config.set_role("fast", f"{config.SESSION}/")

    async def pick_model(provider, role):
        return "qwen2.5:7b"

    from ai_hub import session
    from ai_hub.brains import openai_compat

    monkeypatch.setattr(openai_compat, "pick_model", pick_model)
    handed = {}

    async def fake_ask(chat, text, app, room, on_event, files=()):
        handed.update(text=text, app=app)
        await on_event({"type": "model", "text": session.LABEL})
        await on_event({"type": "text", "text": "```python\nx = 1\n```"})

    monkeypatch.setattr(session, "ask", fake_ask)

    async def run():
        async with client:
            body = {"role": "coder", "system": "Edit one cell.", "messages": [{"role": "user", "content": "earlier"}, {"role": "assistant", "content": "ok"}, {"role": "user", "content": "write x"}]}
            status = await client.get(ask.STATUS_PATH, params={"role": "coder"}, headers=auth("ann@example.com"))
            answer = await client.post(ask.STREAM_PATH, json=body, headers=auth("ann@example.com"))
            quick = await client.post(ask.STREAM_PATH, json={**body, "role": "fast"}, headers=auth("ann@example.com"))
            return status.json()["model"], lines(answer.text), lines(quick.text)[0]

    status, answer, quick = asyncio.run(run())
    assert status == session.LABEL
    assert answer == [{"model": session.LABEL}, {"text": "```python\nx = 1\n```"}, {"done": True, "stop": "end_turn"}]
    assert handed["app"] == "cellora"
    assert handed["text"].startswith("[Rules for this answer, from the app 'cellora'") and "[Earlier in this conversation]\nThe person: earlier\n\nYou: ok" in handed["text"]
    assert handed["text"].endswith("[The person now says]\nwrite x")
    assert quick == {"model": "Ollama (qwen2.5:7b)"}

    # When the Claude Code isn't running, the app gets an error line, not silence.
    async def not_running(chat, text, app, room, on_event, files=()):
        await on_event({"type": "error", "text": session.NOT_RUNNING})

    monkeypatch.setattr(session, "ask", not_running)

    async def again():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=hub_app()), base_url="http://hub") as fresh:
            r = await fresh.post(ask.STREAM_PATH, json={"role": "coder", "messages": [{"role": "user", "content": "q"}]}, headers=auth())
            return lines(r.text)[-1]

    assert "error" in asyncio.run(again())


def test_an_apps_local_role_stays_on_this_machine(monkeypatch) -> None:
    """A model chosen for an app's local role must be on this machine: the page refuses another,
    and one already in the settings is passed over for the role's own."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    config.set_role("local", "ollama/qwen2.5:7b")
    with pytest.raises(ValueError, match="stay on this machine"):
        config.set_app_target("cellora", "local", "anthropic/claude-haiku-5-5")
    config.set_app_target("cellora", "local", "ollama/llama3.2:3b")
    assert asyncio.run(brains.describe("local", "cellora"))[2] == "Ollama (llama3.2:3b)"
    data = config.load()
    data["apps"]["cellora"]["roles"]["local"] = "anthropic/claude-haiku-5-5"  # from before the page refused it
    config.save(data)
    assert asyncio.run(brains.describe("local", "cellora"))[2] == "Ollama (qwen2.5:7b)"
    # Other roles may still go anywhere.
    config.set_app_target("cellora", "smart", "anthropic/claude-haiku-5-5")
    assert asyncio.run(brains.describe("smart", "cellora"))[2] == "Claude (claude-haiku-5-5)"


def test_the_apps_say_what_they_ask_for(monkeypatch) -> None:
    """Each app's manifest carries what it asks each role for (reflex_ddns_auth.ai.uses)."""
    from ai_hub import registry

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "cellora":
            return httpx.Response(200, json={"v": 1, "app": "cellora", "tools": [], "ai_uses": {"smart": "The assistant panel's answers", "fast": ""}})
        return httpx.Response(200, json={"v": 1, "app": "cad", "tools": [{"name": "cad.get_code"}]})

    real = httpx.AsyncClient
    monkeypatch.setattr(registry.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(registry, "_cache", (0.0, [], {}))
    monkeypatch.setenv("AI_HUB_APPS", "cellora=http://cellora:8000,cad=http://cad:8000")
    assert asyncio.run(registry.ai_uses(refresh=True)) == {"cellora": {"smart": "The assistant panel's answers"}}
    assert [(t.app, t.name) for t in asyncio.run(registry.tools())] == [("cad", "cad.get_code")]


def test_the_page_lists_what_each_app_uses(monkeypatch) -> None:
    """An app that says what it uses is listed by its words before anyone asked; one that only
    asked, by its roles. Each row offers only the models the role would really reach."""
    from ai_hub import registry

    async def ai_uses(refresh=False):
        return {"cellora": {"smart": "The assistant panel's answers", "fast": "The grey suggestions while typing", "chat": "not a role"}}

    monkeypatch.setattr(registry, "ai_uses", ai_uses)
    for role in config.ROLES:
        config.set_role(role, "ollama/qwen2.5:7b")
    config.set_app_target("cellora", "fast", "ollama/qwen2.5-coder:7b")
    config.note_ask("cellora", "local")  # asked, though it didn't say so
    config.note_ask("grid", "smart")  # only asked
    session_choice = f"{config.SESSION}/"
    choices = ["ollama/", session_choice, "anthropic/claude-opus-5-5", "ollama/qwen2.5:7b", "ollama/qwen2.5-coder:7b"]

    cellora, grid = asyncio.run(ask.app_rows(choices, config.roles()))
    assert (cellora["app"], cellora["said"], [r["role"] for r in cellora["roles"]]) == ("cellora", True, ["smart", "fast", "local"])
    smart, fast, local = cellora["roles"]
    assert (smart["title"], smart["note"], smart["chosen"]) == ("The assistant panel's answers", "smart · now: Ollama (qwen2.5:7b)", "")
    assert smart["options"] == ["", *choices]
    assert (fast["title"], fast["note"], fast["chosen"]) == ("The grey suggestions while typing", "fast · now: Ollama (qwen2.5-coder:7b)", "ollama/qwen2.5-coder:7b")
    assert session_choice not in fast["options"] and "anthropic/claude-opus-5-5" in fast["options"]
    assert (local["title"], local["note"]) == ("local", "now: Ollama (qwen2.5:7b)")
    assert local["options"] == ["", "ollama/", "ollama/qwen2.5:7b", "ollama/qwen2.5-coder:7b"]
    assert grid == {"app": "grid", "said": False, "roles": [{"role": "smart", "title": "smart", "note": "now: Ollama (qwen2.5:7b)", "chosen": "", "options": ["", *choices]}]}
