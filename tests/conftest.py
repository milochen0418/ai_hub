import pytest


@pytest.fixture(autouse=True)
def hub_data(tmp_path, monkeypatch):
    """Each test has settings of its own, and no passcode from the environment."""
    monkeypatch.setenv("AI_HUB_DATA", str(tmp_path))
    monkeypatch.delenv("ADMIN_PASSCODE", raising=False)
