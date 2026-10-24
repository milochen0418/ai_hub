"""AI Hub: one door between AI and the apps of this re_ddns host.

- ``/api/mcp``: the MCP address for agents (ai_hub.mcp_endpoint)
- ``/``: tokens for agents, the apps' tools, the models, the last tool calls
- ``/assistant`` and the ``ai.assist`` intent: the assistant, as a page or as a dialog
  another app opens (``Intent.start(None, "ai.assist", in_app="cad", room=...)``)
- ``/api/session/*``: for the channel of the Claude Code on the owner's computer (ai_hub.session)
"""

import reflex as rx
from reflex_ddns_auth.intent import intent
from starlette.routing import Route

from ai_hub import mcp_endpoint, session
from ai_hub.states import AssistantState, HubState
from ai_hub.views import assistant_dialog, assistant_page, hub_page

app = rx.App()
app.add_page(hub_page, route="/", title="AI Hub", on_load=HubState.load)
app.add_page(assistant_page, route="/assistant", title="Assistant · AI Hub", on_load=AssistantState.on_load)


@intent(
    app,
    action="ai.assist",
    title="AI assistant",
    on_open=AssistantState.open_intent,
    params={"in_app": str, "room": str, "role": str},
)
def assist_intent() -> rx.Component:
    return assistant_dialog()


app._api.routes.append(Route(mcp_endpoint.PATH, mcp_endpoint.endpoint, methods=["GET", "POST", "DELETE"]))
app._api.routes.append(Route(session.NEXT_PATH, session.next_message, methods=["GET"]))
app._api.routes.append(Route(session.REPLY_PATH, session.reply, methods=["POST"]))
app._api.routes.append(Route(session.ACTIVITY_PATH, session.activity, methods=["POST"]))
app._api.routes.append(Route(session.ATTACHMENT_PATH, session.attachment, methods=["GET"]))
app.register_lifespan_task(mcp_endpoint.lifespan)
