from __future__ import annotations

import pytest
from starlette.testclient import TestClient, WebSocketDisconnect

from admin.dashboard_proxy import build_dashboard_starlette_app


def test_dashboard_http_requires_webui_session():
    client = TestClient(build_dashboard_starlette_app())
    response = client.get("/")
    assert response.status_code == 401
    assert response.headers["cache-control"] == "no-store"


def test_dashboard_http_rejects_cross_origin_mutations():
    client = TestClient(build_dashboard_starlette_app())
    response = client.post(
        "/settings",
        headers={"Origin": "https://attacker.example", "Host": "hermes.example.com"},
        json={"enabled": True},
    )
    assert response.status_code == 403


def test_dashboard_websocket_requires_same_origin_and_session():
    client = TestClient(build_dashboard_starlette_app())
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect(
            "/ws",
            headers={"Origin": "https://attacker.example", "Host": "hermes.example.com"},
        ):
            pass
    assert exc.value.code == 4401
