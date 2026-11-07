import asyncio

from ai_hub import music


def test_the_status_says_what_is_missing_in_plain_words(monkeypatch):
    async def gone(method, path, body=None, timeout=15.0):
        return 0, "no music engine helper at http://x:3144 (ConnectError)"

    monkeypatch.setattr(music, "helper", gone)
    status = asyncio.run(music.status())
    assert status["ok"] is False and status["helper"] is False and "helper" in status["why"]

    async def there(method, path, body=None, timeout=15.0):
        return 200, {"installed": False, "fits": False, "why_not": "needs 21 GB free on the disk, there are 3 GB", "tier": "small"}

    monkeypatch.setattr(music, "helper", there)
    status = asyncio.run(music.status())
    assert status["helper"] and not status["ok"] and "can't hold" in status["why"]

    async def ready(method, path, body=None, timeout=15.0):
        return 200, {"installed": True, "server": "stopped", "tier": "standard", "install": {"state": "idle"}}

    monkeypatch.setattr(music, "helper", ready)
    status = asyncio.run(music.status())
    assert status["ok"] and status["server"] == "stopped" and music.install_words(status) == ""


def test_install_progress_is_told():
    assert music.install_words({"install": {"state": "running", "step": "weights", "gb_done": 3.2, "gb_total": 10.1}}) == "installing: downloading the weights: 3.2 of 10.1 GB"
    assert music.install_words({"install": {"state": "failed", "error": "uv failed"}}).startswith("the install failed")


def test_a_token_for_the_helper_needs_the_platform_secret(monkeypatch):
    monkeypatch.delenv("DDNS_AUTH_SECRET", raising=False)
    try:
        music._token()
    except RuntimeError as e:
        assert "DDNS_AUTH_SECRET" in str(e)
    else:
        raise AssertionError("no secret, no token")
    monkeypatch.setenv("DDNS_AUTH_SECRET", "s3cret")
    import jwt

    claims = jwt.decode(music._token(), "s3cret", algorithms=["HS256"], audience="music-engine")
    assert claims["sub"] == "ai-hub"


def test_what_a_song_may_ask_for_reaches_the_helper():
    from ai_hub import music

    for key in ("reference", "repaint_start", "repaint_end", "repaint_mode", "seed", "cover_strength"):
        assert key in music.SPEC_KEYS



def test_a_song_is_followed_through_a_moment_without_an_answer(monkeypatch):
    import asyncio
    import json

    from ai_hub import music

    answers = [(0, "no music engine helper (ConnectTimeout)"), (0, "no music engine helper (ConnectTimeout)"), (200, {"state": "running", "secs": 5}), (200, {"state": "done", "secs": 9, "metas": {"seed": 7}, "models": {"dit": "x"}})]

    async def helper(method, path, body=None, timeout=15.0):
        return answers.pop(0)

    monkeypatch.setattr(music, "helper", helper)
    monkeypatch.setattr(music, "POLL", 0.0)

    async def follow():
        return [json.loads(line) async for line in music._follow("j1", "app", "ann", "cover")]

    lines = asyncio.run(follow())
    assert [line.get("status") for line in lines[:2]] == [music.STATE_WORDS["slow"]] * 2
    assert lines[-1]["done"] and lines[-1]["metas"]["seed"] == 7, "the song made all the same"

    answers[:] = [(0, "down")] * 3
    monkeypatch.setattr(music, "PATIENCE", 0.0)
    assert "lost the song" in asyncio.run(follow())[-1]["error"], "given up only after a long silence"
    answers[:] = [(404, {"error": "no such job"})]
    monkeypatch.setattr(music, "PATIENCE", 999.0)
    assert "lost the song" in asyncio.run(follow())[-1]["error"], "a job the helper doesn't know is gone"
