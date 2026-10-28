"""The hub's settings: where the AI models are, which model does what, and who may connect.

Kept in ``<data>/settings.json``; ``<data>`` is ``AI_HUB_DATA``, else ``~/.local/share/ai_hub``,
which the App Store keeps in a volume. Environment variables fill in what the file leaves out,
so a fresh install works: ``OLLAMA_URL`` (default: the Ollama on the Docker host),
``ANTHROPIC_API_KEY``, ``OPENAI_API_KEY``.

Roles are what the assistant and the apps (``reflex_ddns_auth.ai``) ask for: ``smart``, ``fast``,
``local`` and ``coder``, each set to ``<provider>/<model>``, so changing a role changes every
user of it at once. Tokens let an agent (Claude Code, Codex, ...) use the hub's MCP address; only their
SHA-256 is kept, and each acts in the apps under the name it was made for. One of them can be
the token of the Claude Code on the owner's computer (``ai_hub.session``), which then appears
as the provider ``claude-code``.

The passcode (``ADMIN_PASSCODE``, else one set on the hub's page and kept as a PBKDF2 hash)
guards the settings, and marks the owner's browser (``owner_key``): only the owner may use
the Claude Code on the owner's computer.
"""

from __future__ import annotations

import dataclasses
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any

ROLES = ("smart", "fast", "local", "coder")
ROLE_HELP = {
    "smart": "Hard work: the assistant's default.",
    "fast": "Quick, short answers.",
    "local": "Stays on this machine (Ollama): for data that must not leave it.",
    "coder": "Writing code.",
}
CLAUDE_MODEL = "claude-opus-5-5"
TOKEN_PREFIX = "aihub_"
SESSION = "claude-code"  # the provider that is the Claude Code on the owner's computer
SESSION_TOKEN_NAME = "Claude Code on my computer"
# The roles an app may hand to the Claude Code on the owner's computer: not the quick ones, which
# come many times a minute, and that Claude Code takes one message at a time.
SESSION_ROLES = ("smart", "coder")
_PASSCODE_ROUNDS = 200_000

_lock = threading.Lock()


@dataclasses.dataclass(frozen=True)
class Provider:
    name: str  # "ollama", "anthropic", "openai", "claude-code"
    # the API: "ollama" (Ollama's own), "openai" (OpenAI-style chat completions), "anthropic",
    # "session" (the Claude Code on the owner's computer, through ai_hub.session)
    kind: str
    base_url: str
    api_key: str

    @property
    def local(self) -> bool:
        return self.name == "ollama"


def data_dir() -> Path:
    path = Path(os.environ.get("AI_HUB_DATA") or Path.home() / ".local" / "share" / "ai_hub")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _file() -> Path:
    return data_dir() / "settings.json"


def load() -> dict[str, Any]:
    try:
        data = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    data.setdefault("providers", {})
    data.setdefault("roles", {})
    data.setdefault("tokens", [])
    return data


def save(data: dict[str, Any]) -> None:
    path = _file()
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _default_ollama_url() -> str:
    in_docker = os.path.exists("/.dockerenv") or os.path.exists("/run/.containerenv")
    return "http://host.docker.internal:11434" if in_docker else "http://localhost:11434"


def providers(data: dict[str, Any] | None = None) -> dict[str, Provider]:
    """The model providers the hub knows: Ollama always, Claude and OpenAI with a key, and the
    Claude Code on the owner's computer once its token is made."""
    data = data or load()
    stored = data["providers"]
    ollama = (stored.get("ollama", {}).get("url") or os.environ.get("OLLAMA_URL") or _default_ollama_url()).rstrip("/")
    out = {"ollama": Provider("ollama", "ollama", f"{ollama}/v1", "ollama")}
    anthropic_key = stored.get("anthropic", {}).get("api_key") or os.environ.get("ANTHROPIC_API_KEY", "")
    if anthropic_key:
        out["anthropic"] = Provider("anthropic", "anthropic", "", anthropic_key)
    openai_key = stored.get("openai", {}).get("api_key") or os.environ.get("OPENAI_API_KEY", "")
    if openai_key:
        out["openai"] = Provider("openai", "openai", "https://api.openai.com/v1", openai_key)
    if data.get("session", {}).get("token_id"):
        out[SESSION] = Provider(SESSION, "session", "", "")
    return out


def set_provider(name: str, **fields: str) -> None:
    """Stores a provider's settings (an empty value removes it, so the environment applies)."""
    with _lock:
        data = load()
        entry = {k: v for k, v in {**data["providers"].get(name, {}), **fields}.items() if v}
        if entry:
            data["providers"][name] = entry
        else:
            data["providers"].pop(name, None)
        save(data)


def roles(data: dict[str, Any] | None = None) -> dict[str, str]:
    """Each role's ``<provider>/<model>``, with defaults for the ones not set:
    Claude for smart and coder when there is a key, else Ollama's model (``ollama/``, picked
    when used)."""
    data = data or load()
    known = providers(data)
    smart = f"anthropic/{CLAUDE_MODEL}" if "anthropic" in known else "ollama/"
    defaults = {"smart": smart, "fast": "ollama/", "local": "ollama/", "coder": smart}
    return {role: data["roles"].get(role) or defaults[role] for role in ROLES}


def default_target(role: str, data: dict[str, Any] | None = None) -> str:
    """What a role is when nobody set it (see ``roles``): Claude with a key, else Ollama."""
    known = providers(data or load())
    smart = f"anthropic/{CLAUDE_MODEL}" if "anthropic" in known else "ollama/"
    return {"smart": smart, "fast": "ollama/", "local": "ollama/", "coder": smart}[role]


def set_role(role: str, target: str) -> None:
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    with _lock:
        data = load()
        if target:
            data["roles"][role] = target
        else:
            data["roles"].pop(role, None)
        save(data)


def resolve(role_or_target: str, for_role: str = "") -> tuple[Provider, str]:
    """``(provider, model)`` for a role (``smart``) or a target (``ollama/qwen2.5:7b``);
    the model is "" when the provider's own default should be used. A target chosen for a role
    (``for_role``) is held to it: one for ``local`` must be on this machine too."""
    data = load()
    target = roles(data).get(role_or_target, role_or_target)
    name, _, model = target.partition("/")
    known = providers(data)
    if name not in known:
        raise LookupError(f"no AI provider {name!r} is set up (add its key on the AI Hub page)")
    if "local" in (role_or_target, for_role) and not known[name].local:
        raise LookupError("the local role must stay on this machine (an Ollama model)")
    return known[name], model


# ------------------------------------------------------------ the apps' choices


def apps_asking(data: dict[str, Any] | None = None) -> dict[str, dict[str, str]]:
    """Each app that has asked the hub's models (``ai_hub.ask``): the roles it asked for, each
    with the target chosen for it on the hub's page ("" = the role's own model)."""
    data = data or load()
    out: dict[str, dict[str, str]] = {}
    for app, entry in sorted((data.get("apps") or {}).items()):
        chosen = entry.get("roles") or {}
        out[app] = {role: str(chosen.get(role) or "") for role in entry.get("asked") or []}
    return out


def note_ask(app: str, role: str) -> None:
    """Remembers that ``app`` asks for ``role``, so the page can offer a choice for it (written
    once per pair)."""
    app = app.strip().lower()
    if not app or role not in ROLES:
        return
    with _lock:
        data = load()
        entry = data.setdefault("apps", {}).setdefault(app, {"asked": [], "roles": {}})
        if role in entry.setdefault("asked", []):
            return
        entry["asked"].append(role)
        save(data)


def app_target(app: str, role: str) -> str:
    """The target chosen on the page for ``app``'s ``role`` ("" = the role's own model)."""
    return str(((load().get("apps") or {}).get(app.strip().lower(), {}).get("roles") or {}).get(role) or "")


def set_app_target(app: str, role: str, target: str) -> None:
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    if role == "local" and target and target.partition("/")[0] != "ollama":
        raise ValueError("the local role must stay on this machine (an Ollama model)")
    with _lock:
        data = load()
        entry = data.setdefault("apps", {}).setdefault(app.strip().lower(), {"asked": [], "roles": {}})
        roles = entry.setdefault("roles", {})
        if target:
            roles[role] = target
        else:
            roles.pop(role, None)
        save(data)


def forget_app(app: str) -> None:
    """Drops an app's row from the page (it comes back when the app asks again)."""
    with _lock:
        data = load()
        if data.get("apps", {}).pop(app.strip().lower(), None) is not None:
            save(data)


# ------------------------------------------------------------ tokens


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _add_token(data: dict[str, Any], name: str) -> tuple[str, str]:
    """Adds a new token to ``data``: ``(token, id)``."""
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    token_id = secrets.token_hex(4)
    data["tokens"].append({"id": token_id, "name": name, "hash": _hash(token), "created": int(time.time())})
    return token, token_id


def create_token(name: str) -> str:
    """A new token for an agent acting as ``name``; shown once, only its hash is kept."""
    with _lock:
        data = load()
        token, _ = _add_token(data, name.strip() or "agent")
        save(data)
    return token


def make_session_token() -> str:
    """A new token for the Claude Code on the owner's computer, in place of its last one."""
    with _lock:
        data = load()
        old = data.get("session", {}).get("token_id")
        data["tokens"] = [t for t in data["tokens"] if t["id"] != old]
        token, token_id = _add_token(data, SESSION_TOKEN_NAME)
        data["session"] = {"token_id": token_id}
        save(data)
    return token


def session_token_id() -> str:
    return load().get("session", {}).get("token_id", "")


def session_hooked() -> bool:
    """Whether the session's hooks have reported since its token was made (ai_hub.session): kept
    here, so the hub still knows after a restart."""
    return bool(load().get("session", {}).get("hooked"))


def mark_session_hooked() -> None:
    with _lock:
        data = load()
        if data.get("session", {}).get("token_id") and not data["session"].get("hooked"):
            data["session"]["hooked"] = True
            save(data)


def tokens() -> list[dict[str, Any]]:
    return [{k: t[k] for k in ("id", "name", "created")} for t in load()["tokens"]]


def delete_token(token_id: str) -> None:
    with _lock:
        data = load()
        data["tokens"] = [t for t in data["tokens"] if t["id"] != token_id]
        if data.get("session", {}).get("token_id") == token_id:
            data.pop("session")
        save(data)


def token_info(token: str) -> dict[str, str] | None:
    """The token's ``{"id", "name"}`` (the name it acts as); None when it isn't one of the hub's."""
    if not token.startswith(TOKEN_PREFIX):
        return None
    digest = _hash(token)
    for t in load()["tokens"]:
        if hmac.compare_digest(t["hash"], digest):
            return {"id": t["id"], "name": t["name"]}
    return None


def who(token: str) -> str | None:
    """The name a token acts as; None when it isn't one of the hub's tokens."""
    info = token_info(token)
    return info["name"] if info else None


# ------------------------------------------------------------ passcode


def _passcode_hash(passcode: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", passcode.encode(), salt, _PASSCODE_ROUNDS).hex()


def passcode_from_env() -> bool:
    return bool(os.environ.get("ADMIN_PASSCODE"))


def has_passcode() -> bool:
    return passcode_from_env() or "passcode" in load()


def check_passcode(passcode: str) -> bool:
    if passcode_from_env():
        return hmac.compare_digest(passcode.encode(), os.environ["ADMIN_PASSCODE"].encode())
    stored = load().get("passcode")
    if not stored or not passcode:
        return False
    return hmac.compare_digest(_passcode_hash(passcode, bytes.fromhex(stored["salt"])), stored["hash"])


def set_passcode(passcode: str) -> None:
    """Sets the passcode (when ``ADMIN_PASSCODE`` doesn't); browsers marked as the owner's with
    the old one have to give the new one."""
    salt = secrets.token_bytes(16)
    with _lock:
        data = load()
        data["passcode"] = {"salt": salt.hex(), "hash": _passcode_hash(passcode, salt)}
        data["owner_secret"] = secrets.token_hex(32)
        save(data)


def owner_key() -> str:
    """What marks the owner's browser once it gave the passcode (kept in its local storage)."""
    with _lock:
        data = load()
        if not data.get("owner_secret"):
            data["owner_secret"] = secrets.token_hex(32)
            save(data)
    basis = os.environ.get("ADMIN_PASSCODE") or data.get("passcode", {}).get("hash", "")
    return hmac.new(bytes.fromhex(data["owner_secret"]), basis.encode(), hashlib.sha256).hexdigest()


def is_owner(key: str) -> bool:
    """Whether ``key`` is the owner's mark (there is no owner without a passcode)."""
    return bool(key) and has_passcode() and hmac.compare_digest(key, owner_key())
