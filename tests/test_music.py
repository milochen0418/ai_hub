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
