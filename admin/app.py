"""Thin Starlette wrapper in front of hermes-webui.

Adds an in-browser xterm at /tui and a narrow authenticated ChatGPT relay.
All ordinary WebUI paths remain reverse-proxied to hermes-webui.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Mount, Route, WebSocketRoute

from . import proxy as hermes_proxy
from . import terminal as hermes_terminal
from . import channel as chatgpt_channel
from .dashboard_proxy import DASHBOARD_MOUNT_PREFIX, build_dashboard_starlette_app


TEMPLATE_PATH = Path(__file__).parent / "templates" / "tui.html"


async def _is_authenticated(request: Request) -> bool:
    """Probe hermes-webui's onboarding API using the user's session cookie."""
    import httpx

    cookie = request.headers.get("cookie", "")
    if not cookie:
        return False
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(
                f"{hermes_proxy.WEBUI_BASE_URL}/api/onboarding/status",
                headers={"cookie": cookie, "host": f"{hermes_proxy.WEBUI_HOST}:{hermes_proxy.WEBUI_PORT}"},
            )
        return r.status_code == 200
    except (httpx.ConnectError, httpx.ReadTimeout):
        return False


async def tui_page(request: Request):
    if not await _is_authenticated(request):
        return RedirectResponse("/login?next=/tui", status_code=303)
    return HTMLResponse(TEMPLATE_PATH.read_text(encoding="utf-8"))


routes = [
    Route("/tui", tui_page, methods=["GET"]),
    Route("/agent-channel/health", chatgpt_channel.health, methods=["GET"]),
    Route("/agent-channel/capabilities", chatgpt_channel.capabilities, methods=["GET"]),
    Route("/agent-channel/toolsets", chatgpt_channel.toolsets, methods=["GET"]),
    Route("/agent-channel/skills", chatgpt_channel.skills, methods=["GET"]),
    Route("/agent-channel/models", chatgpt_channel.models, methods=["GET"]),
    Route("/agent-channel/model-options", chatgpt_channel.model_options, methods=["GET"]),
    Route("/agent-channel/session", chatgpt_channel.current_session, methods=["GET"]),
    Route("/agent-channel/model-test", chatgpt_channel.model_test, methods=["POST"]),
    Route("/agent-channel/diagnostics", chatgpt_channel.diagnostics, methods=["GET"]),
    Route("/agent-channel/ui-audit", chatgpt_channel.ui_audit, methods=["GET"]),
    Route("/agent-channel/configure-primary", chatgpt_channel.configure_primary, methods=["POST"]),
    Route("/agent-channel/chat", chatgpt_channel.chat, methods=["POST"]),
    Route("/agent-channel/requests/{request_id}", chatgpt_channel.get_request, methods=["GET"]),
    Route("/agent-channel/outbox", chatgpt_channel.get_outbox, methods=["GET"]),
    Route("/agent-channel/outbox", chatgpt_channel.post_outbox, methods=["POST"]),
    Route("/agent-channel/outbox/{message_id}/ack", chatgpt_channel.ack_outbox, methods=["POST"]),
    WebSocketRoute("/tui/ws/auth/{provider}", hermes_terminal.login_ws),
    WebSocketRoute("/tui/ws/shell", hermes_terminal.shell_ws),
    Mount(DASHBOARD_MOUNT_PREFIX, build_dashboard_starlette_app()),
    # Catch-all proxy for every other path (HTTP + WebSocket).
    WebSocketRoute("/{path:path}", hermes_proxy.ws_proxy),
    Route("/{path:path}", hermes_proxy.http_proxy, methods=hermes_proxy.PROXY_METHODS),
    Route("/", hermes_proxy.http_proxy, methods=hermes_proxy.PROXY_METHODS),
]

@asynccontextmanager
async def lifespan(app: Starlette):
    await chatgpt_channel.start_worker()
    try:
        yield
    finally:
        await chatgpt_channel.stop_worker()


app = Starlette(debug=False, routes=routes, lifespan=lifespan)
