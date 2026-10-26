"""The assistant's loop (a fake model), Ollama's requests, Claude's history rule after a fallback,
and how each kind of model is given the files attached to a message."""

from __future__ import annotations

import dataclasses
import json
from types import SimpleNamespace

import httpx
import pytest

from ai_hub import agent, attachments, brains, calls, config, registry
from ai_hub.brains import ollama
from ai_hub.brains.claude import ClaudeConversation, _echo
from ai_hub.brains.openai_compat import OpenAICompatConversation
from tests.test_attachments import keep, pdf_bytes, png_bytes

TOOL = registry.Tool(
    app="cad",
    name="cad.set_code",
    title="Write the room's code",
    description="Replaces the code.",
    kind="write",
    input_schema={"type": "object", "properties": {"code": {"type": "string"}}, "required": ["code"]},
    backend="http://cad",
)
OTHER = registry.Tool(app="grid", name="grid.read", title="Read", description="", kind="read", input_schema={}, backend="http://grid")


class FakeConversation:
    label = "Fake (test-model)"
    sees_images = True
    reads_pdf = False

    def __init__(self, steps):
        self.steps = list(steps)
        self.users: list[str] = []
        self.parts: list[list[attachments.Part]] = []
        self.results: list = []
        self.tools_seen: list[list[str]] = []

    def add_user(self, text, parts=()):
        self.users.append(text)
        self.parts.append(list(parts))

    async def step(self, tools):
        self.tools_seen.append([t.public_name for t in tools])
        return self.steps.pop(0)

    def add_results(self, results):
        self.results.append(results)


@pytest.fixture
def world(monkeypatch):
    called = []

    async def fake_tools(refresh=False):
        return [TOOL, OTHER]

    async def fake_call(tool, args, user, via):
        called.append((tool.name, args, user, via))
        return calls.Result(True, {"lines": 1})

    monkeypatch.setattr(registry, "tools", fake_tools)
    monkeypatch.setattr(calls, "call", fake_call)
    return called


async def test_the_model_uses_tools_until_it_is_done(world, monkeypatch) -> None:
    fake = FakeConversation(
        [
            brains.Step(text="Writing it.", calls=[brains.ToolCall("c1", "cad_set_code", {"code": "x"})]),
            brains.Step(text="Done: the table is in the room."),
        ]
    )
    systems = []

    async def fake_conversation(role, system):
        systems.append(system)
        return fake

    monkeypatch.setattr(brains, "conversation", fake_conversation)
    events = []

    async def on_event(e):
        events.append(e)

    chat = agent.Chat("smart", agent.Scope(app="cad", room="r1"), user="Fake (test-model)")
    await chat.send("draw a table", on_event)

    assert [e["type"] for e in events] == ["model", "text", "tool", "result", "text"]
    assert world == [("cad.set_code", {"code": "x"}, "Fake (test-model)", "assistant")]
    assert fake.tools_seen[0] == ["cad_set_code"]  # only the app it was opened from
    assert "room 'r1'" in systems[0] and "room='r1'" in systems[0]
    (call, result), = fake.results[0]
    assert call.id == "c1" and result.ok


async def test_an_unknown_tool_is_an_error_the_model_sees(world, monkeypatch) -> None:
    fake = FakeConversation(
        [brains.Step(calls=[brains.ToolCall("c1", "nope", {})]), brains.Step(text="Sorry.")]
    )

    async def fake_conversation(role, system):
        return fake

    monkeypatch.setattr(brains, "conversation", fake_conversation)
    events = []

    async def on_event(e):
        events.append(e)

    await agent.Chat("smart", agent.Scope(), user="u").send("do it", on_event)

    assert world == []
    assert fake.results[0][0][1].ok is False
    assert any(e["type"] == "result" and not e["ok"] for e in events)


async def test_a_refusal_ends_the_turn(world, monkeypatch) -> None:
    fake = FakeConversation([brains.Step(refused=True)])

    async def fake_conversation(role, system):
        return fake

    monkeypatch.setattr(brains, "conversation", fake_conversation)
    events = []

    async def on_event(e):
        events.append(e)

    await agent.Chat("smart", agent.Scope(), user="u").send("x", on_event)

    assert events[-1] == {"type": "error", "text": "The model declined this request."}


async def test_a_left_out_room_is_the_one_it_was_opened_in(world, monkeypatch) -> None:
    roomy = dataclasses.replace(TOOL, input_schema={"type": "object", "properties": {"code": {}, "room": {}}})

    async def tools(refresh=False):
        return [roomy]

    monkeypatch.setattr(registry, "tools", tools)
    fake = FakeConversation(
        [
            brains.Step(
                calls=[
                    brains.ToolCall("c1", "cad_set_code", {"code": "x"}),
                    brains.ToolCall("c2", "cad_set_code", {"code": "y", "room": "other"}),
                ]
            ),
            brains.Step(text="Done."),
        ]
    )

    async def fake_conversation(role, system):
        return fake

    monkeypatch.setattr(brains, "conversation", fake_conversation)

    async def on_event(e):
        pass

    await agent.Chat("smart", agent.Scope(app="cad", room="r1"), user="u").send("draw", on_event)

    assert [args for _, args, _, _ in world] == [{"code": "x", "room": "r1"}, {"code": "y", "room": "other"}]


async def test_a_model_that_stops_without_a_word_is_shown(world, monkeypatch) -> None:
    fake = FakeConversation([brains.Step()])

    async def fake_conversation(role, system):
        return fake

    monkeypatch.setattr(brains, "conversation", fake_conversation)
    events = []

    async def on_event(e):
        events.append(e)

    await agent.Chat("smart", agent.Scope(), user="u").send("x", on_event)

    assert events[-1] == {"type": "error", "text": "The model stopped without a reply."}


async def test_files_go_with_the_message(world, monkeypatch) -> None:
    fake = FakeConversation([brains.Step(text="A red square.")])

    async def fake_conversation(role, system):
        return fake

    monkeypatch.setattr(brains, "conversation", fake_conversation)
    events = []

    async def on_event(e):
        events.append(e)

    picture = keep("red.png", png_bytes(), "image/png")
    await agent.Chat("smart", agent.Scope(), user="u").send("what is this?", on_event, [picture])

    assert fake.users == ["what is this?"]
    about, shown = fake.parts[0]
    assert about.text.startswith("red.png (a picture, 40×30, ") and shown.image[0] == "image/jpeg"
    assert not any(e["type"] == "info" for e in events)


async def test_a_model_that_cant_see_pictures_gets_the_text_and_the_person_is_told(world, monkeypatch) -> None:
    fake = FakeConversation([brains.Step(text="I can't see it.")])
    fake.sees_images = False

    async def fake_conversation(role, system):
        return fake

    monkeypatch.setattr(brains, "conversation", fake_conversation)
    events = []

    async def on_event(e):
        events.append(e)

    await agent.Chat("smart", agent.Scope(), user="u").send("", on_event, [keep("red.png", png_bytes(), "image/png")])

    assert any(e["type"] == "info" and "can't see pictures" in e["text"] for e in events)


def test_claude_gets_the_pictures_and_a_pdf_as_it_is_before_the_question() -> None:
    convo = ClaudeConversation(config.Provider("anthropic", "anthropic", "", "key"), "claude-opus-5-5", "system")
    files = [keep("red.png", png_bytes(), "image/png"), keep("plan.pdf", pdf_bytes(["Ground floor"]), "application/pdf")]

    convo.add_user("build this", attachments.parts(files, pdf=convo.reads_pdf))

    content = convo.messages[0]["content"]
    assert [b["type"] for b in content] == ["text", "image", "text", "document", "text"]
    assert content[3]["source"]["media_type"] == "application/pdf" and content[-1]["text"] == "build this"
    convo.add_user("and now?")
    assert convo.messages[-1] == {"role": "user", "content": "and now?"}


def test_claude_gets_a_tools_words_and_its_pictures() -> None:
    convo = ClaudeConversation(config.Provider("anthropic", "anthropic", "", "key"), "claude-opus-5-5", "system")
    call = brains.ToolCall("t1", "roam_view", {})
    words = calls.Result(True, {"eyes": 3.2, "picture": "(picture 1)"}, images=[("image/jpeg", "AAAA")])
    alone = calls.Result(True, None, images=[("image/png", "BBBB")])

    convo.add_results([(call, words), (brains.ToolCall("t2", "cad_snapshot", {}), alone)])

    first, second = convo.messages[-1]["content"]
    assert [b["type"] for b in first["content"]] == ["text", "image"] and '"eyes": 3.2' in first["content"][0]["text"]
    assert first["content"][1]["source"] == {"type": "base64", "media_type": "image/jpeg", "data": "AAAA"}
    assert [b["type"] for b in second["content"]] == ["image"]


def test_openai_gets_pictures_as_data_urls() -> None:
    convo = OpenAICompatConversation(config.Provider("openai", "openai", "https://api.openai.com/v1", "key"), "gpt-x", "system")

    convo.add_user("what is this?", attachments.parts([keep("red.png", png_bytes(), "image/png")]))

    content = convo.messages[-1]["content"]
    assert [c["type"] for c in content] == ["text", "image_url", "text"]
    assert content[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_ollama_sends_pictures_only_to_models_that_see() -> None:
    provider = config.Provider("ollama", "ollama", "http://ollama:11434/v1", "ollama")
    parts = attachments.parts([keep("red.png", png_bytes(), "image/png")])

    seeing = ollama.OllamaConversation(provider, "qwen2.5vl:7b", "system", vision=True)
    seeing.add_user("what is this?", parts)
    blind = ollama.OllamaConversation(provider, "qwen2.5:7b", "system")
    blind.add_user("what is this?", parts)

    assert len(seeing.messages[-1]["images"]) == 1 and seeing.messages[-1]["content"].endswith("what is this?")
    assert "images" not in blind.messages[-1] and "can't be shown to you" in blind.messages[-1]["content"]


async def test_ollama_says_which_models_see(monkeypatch) -> None:
    def answer(request: httpx.Request) -> httpx.Response:
        model = json.loads(request.content)["model"]
        return httpx.Response(200, json={"capabilities": ["completion", "vision"] if "vl" in model else ["completion", "tools"]})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(answer), **kw))
    provider = config.Provider("ollama", "ollama", "http://ollama:11434/v1", "ollama")

    assert await ollama.sees_images(provider, "qwen2.5vl:7b") is True
    assert await ollama.sees_images(provider, "qwen2.5:7b") is False


async def test_ollama_roles_use_ollamas_own_api(monkeypatch) -> None:
    provider = config.providers({"providers": {"ollama": {"url": "http://ollama:11434"}}})["ollama"]
    monkeypatch.setattr(config, "resolve", lambda role: (provider, "qwen2.5:7b"))

    async def blind(provider, model):
        return False

    monkeypatch.setattr(ollama, "sees_images", blind)

    convo = await brains.conversation("smart", "system")

    assert isinstance(convo, ollama.OllamaConversation)
    assert convo.url == "http://ollama:11434/api/chat"


async def test_ollama_gets_a_context_that_holds_the_chat(monkeypatch) -> None:
    sent = []

    def answer(request: httpx.Request) -> httpx.Response:
        sent.append((str(request.url), json.loads(request.content)))
        call = {"function": {"name": "cad_set_code", "arguments": {"code": "x"}}}
        return httpx.Response(200, json={"message": {"role": "assistant", "content": "", "tool_calls": [call]}})

    real = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(answer), **kw))
    convo = ollama.OllamaConversation(config.Provider("ollama", "ollama", "http://ollama:11434/v1", "ollama"), "qwen2.5:7b", "system")
    convo.add_user("draw a table")

    step = await convo.step([TOOL])

    url, body = sent[0]
    assert url == "http://ollama:11434/api/chat"
    assert body["options"]["num_ctx"] == ollama.NUM_CTX >= 16384 and body["stream"] is False
    assert body["tools"][0]["function"]["name"] == "cad_set_code"
    assert step.calls == [brains.ToolCall("cad_set_code-0", "cad_set_code", {"code": "x"})]

    convo.add_results([(step.calls[0], calls.Result(True, images=[("image/png", "AAAA")]))])
    assert convo.messages[-1]["role"] == "tool" and convo.messages[-1]["tool_name"] == "cad_set_code"
    assert "can't be shown" in convo.messages[-1]["content"]  # not "attached": it isn't
    # Words with pictures: the words, and a note on the pictures.
    convo.add_results([(step.calls[0], calls.Result(True, {"eyes": 3.2, "picture": "(picture 1)"}, images=[("image/jpeg", "AAAA")]))])
    content = convo.messages[-1]["content"]
    assert '"eyes": 3.2' in content and "can't be shown" in content


def block(kind: str, **kw):
    return SimpleNamespace(type=kind, **kw)


def test_claude_history_is_echoed_as_it_came() -> None:
    content = [block("thinking"), block("text"), block("tool_use")]
    assert _echo(content) == content


def test_after_a_fallback_the_declined_internal_blocks_are_left_out() -> None:
    thinking, text1, use1 = block("thinking"), block("text"), block("tool_use")
    switch = block("fallback")
    thinking2, text2, use2 = block("thinking"), block("text"), block("tool_use")

    echoed = _echo([thinking, text1, use1, switch, thinking2, text2, use2])

    assert echoed == [text1, thinking2, text2, use2]
