"""The talking AI's words and voice (ai_hub.talk), without the models."""

import io
import wave

import pytest

from ai_hub import agent, talk


def test_mood_tag_is_read_and_removed():
    assert talk.mood_of("[happy] Hello there!") == ("happy", "Hello there!")
    assert talk.mood_of("[Laughing]: that's funny") == ("laughing", "that's funny")
    assert talk.mood_of("【驚訝】哇，這裡好高！") == ("surprised", "哇，這裡好高！")
    assert talk.mood_of("（思考）讓我看看。") == ("thinking", "讓我看看。")
    assert talk.mood_of("No tag here.") == ("neutral", "No tag here.")
    assert talk.mood_of("[room 12] is not a mood") == ("neutral", "[room 12] is not a mood")
    assert talk.mood_of("") == ("neutral", "")


def test_spoken_words_have_no_markdown():
    text = "## Title\n\n- **bold** and `code` and [a site](https://x.y/z)\n```py\nprint(1)\n```\nSee https://example.com now."
    said = talk.spoken(text)
    assert "**" not in said and "`" not in said and "https://" not in said and "#" not in said
    assert "bold and code and a site" in said and "(code)" in said and "a link" in said


def test_sentences_split_chinese_and_english():
    assert talk.sentences("你好！我在聖家堂。今天天氣如何？") == ["你好！", "我在聖家堂。", "今天天氣如何？"]
    assert talk.sentences("Hi there. It is 3.5 m high. OK!") == ["Hi there.", "It is 3.5 m high.", "OK!"]
    long = "，".join(["一段很長的話"] * 60)
    parts = talk.sentences(long, limit=100)
    assert len(parts) > 1 and all(len(p) <= 101 for p in parts) and "".join(parts).replace("，", "") == long.replace("，", "")
    assert talk.sentences("") == []


def test_language_is_chinese_with_any_cjk_character():
    assert talk.language_of("Hello CAD Roam") == "en"
    assert talk.language_of("我在 CAD Roam 裡") == "zh"


def test_context_text_takes_the_text_of_a_page():
    assert talk.context_text('{"text": "at the door", "at": [1, 2, 3]}') == "at the door"
    assert talk.context_text('{"at": [1, 2]}') == '{"at": [1, 2]}'
    assert talk.context_text("plain  words\n here") == "plain words here"
    assert talk.context_text("") == "" and talk.context_text(None) == "" and talk.context_text("null") == ""
    assert len(talk.context_text("x" * 5000)) == 1500


def test_with_context_goes_in_front_of_the_message():
    assert agent.with_context("hi", "") == "hi"
    assert agent.with_context("hi", " inside\nthe chapel ") == "[What the person's page shows now: inside the chapel]\n\nhi"


def test_chat_manner_picks_the_talk_system():
    chat = agent.Chat("fast", agent.Scope("cad-roam", "x"), user="companion", manner="talk")
    assert chat.manner == "talk" and "mood tag" in agent.MANNERS["talk"]
    assert agent.Chat("fast", agent.Scope(), user="a", manner="nonsense").manner == "assist"


def test_wav_bytes_is_16_bit_mono():
    np = pytest.importorskip("numpy")
    data = talk.wav_bytes(np.zeros(2400, dtype=np.float32), 24000)
    with wave.open(io.BytesIO(data)) as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, 24000, 2400)


def test_status_and_routes():
    s = talk.status()
    assert s["tts"]["phase"] == "off" and s["stt"]["phase"] == "off" and s["stt"]["model"] == talk.WHISPER_MODEL
    assert s["tts"]["voices"]["zh"]["female"] == "zf_001"

    class Api:
        routes: list = []

    class App:
        _api = Api()

    talk.register(App)
    talk.register(App)
    assert sorted(r.path for r in App._api.routes) == sorted([talk.STATUS_PATH, talk.TTS_PATH, talk.STT_PATH])


def test_traditional_for_a_zh_tw_page():
    pytest.importorskip("opencc")
    assert talk.traditional("你现在在查看这个模型。", "zh-TW") == "你現在在檢視這個模型。"
    assert talk.traditional("你现在在查看这个模型。", "en") == "你现在在查看这个模型。"
    assert talk.traditional("Plain English.", "zh-TW") == "Plain English."


def test_scope_note_says_the_language():
    assert "Traditional" in agent.Scope("cad-roam", "x", "zh-TW").note()
    assert "'ja'" in agent.Scope("", "", "ja").note()
    assert agent.Scope().note() == ""
