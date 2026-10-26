"""The one MCP address for agents (Claude Code, Codex, Cursor, ...): every installed app's tools.

    claude mcp add --transport http apps https://ai.reflex-ddns.com/api/mcp \\
        --header "Authorization: Bearer <token>"

Served on the hub's backend at ``/api/mcp`` (nginx already sends ``/api`` to the backend) as
stateless Streamable HTTP, by the official MCP SDK: listing tools asks the registry, so a newly
installed app's tools appear by themselves. A request needs a token made on the hub's page;
the tools then act for the token's name.
"""

from __future__ import annotations

import contextlib
import contextvars
from typing import Any, AsyncIterator

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from ai_hub import calls, config, registry, session

PATH = "/api/mcp"
INSTRUCTIONS = (
    "Tools of the apps installed on this re_ddns host, one set per app (the tool names start "
    "with the app's name). What a tool changes, the people using that app see at once."
)
# Claude Code saves a text answer over 50,000 characters to a file instead (a big model's code
# is more), and the hub's own session has no tool to read files: let answers be as long as
# Claude Code allows.
MAX_RESULT_CHARS = 500_000

_user: contextvars.ContextVar[str] = contextvars.ContextVar("ai_hub_user", default="")
_token_id: contextvars.ContextVar[str] = contextvars.ContextVar("ai_hub_token_id", default="")


def _mcp_tool(t: registry.Tool) -> types.Tool:
    return types.Tool(
        name=t.public_name,
        title=f"{t.title} ({t.app})",
        description=f"{t.description}\n\nApp: {t.app}".strip(),
        input_schema=t.input_schema,
        annotations=types.ToolAnnotations(
            title=t.title,
            read_only_hint=t.kind == "read",
            destructive_hint=t.kind == "danger",
            open_world_hint=False,
        ),
        meta={"anthropic/maxResultSizeChars": MAX_RESULT_CHARS},
    )


async def _list_tools(ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
    return types.ListToolsResult(tools=[_mcp_tool(t) for t in await registry.tools()])


async def _call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
    tool = await registry.find(params.name)
    if tool is None:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"No tool named {params.name!r} (is its app running?)")],
            is_error=True,
        )
    args = params.arguments or {}
    # The Claude Code on the owner's computer works for a chat in the assistant: it shows there.
    await session.tool_called(_token_id.get(), params.name, args)
    result = await calls.call(tool, args, _user.get() or "agent", via="mcp")
    await session.tool_done(_token_id.get(), params.name, result)
    if result.ok and result.images:
        # The words (with "(picture N)" where each was), then the pictures in that order.
        pictures = [types.ImageContent(type="image", data=data, mime_type=mime) for mime, data in result.images]
        words = [types.TextContent(type="text", text=result.text())] if result.data is not None else []
        return types.CallToolResult(content=[*words, *pictures])
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=result.text())],
        structured_content=result.data if result.ok and isinstance(result.data, dict) else None,
        is_error=not result.ok,
    )


server = Server(
    "ai-hub",
    version="0.1.0",
    title="AI Hub",
    instructions=INSTRUCTIONS,
    on_list_tools=_list_tools,
    on_call_tool=_call_tool,
)
# The SDK's session manager, while the backend runs (see lifespan); one can run only once.
_manager: StreamableHTTPSessionManager | None = None


class Endpoint:
    """The ASGI app at ``PATH``: checks the token, then hands the request to the MCP SDK."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        if _manager is None:
            await JSONResponse({"error": "starting up"}, status_code=503)(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        scheme, _, token = headers.get("authorization", "").partition(" ")
        info = config.token_info(token.strip()) if scheme.lower() == "bearer" else None
        if info is None:
            response = JSONResponse(
                {"error": "This MCP address needs a token: make one on the AI Hub page."},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            await response(scope, receive, send)
            return
        reset_user, reset_id = _user.set(info["name"]), _token_id.set(info["id"])
        try:
            await StreamableHTTPASGIApp(_manager)(scope, receive, send)
        finally:
            _user.reset(reset_user)
            _token_id.reset(reset_id)


endpoint = Endpoint()


@contextlib.asynccontextmanager
async def lifespan() -> AsyncIterator[None]:
    """Runs an MCP SDK session manager while the backend runs."""
    global _manager
    manager = StreamableHTTPSessionManager(app=server, json_response=True, stateless=True)
    async with manager.run():
        _manager = manager
        try:
            yield
        finally:
            _manager = None
