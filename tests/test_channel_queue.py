from __future__ import annotations

import hashlib

from admin import channel


def test_reservation_is_durable_queue(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    message = "run the acceptance test"
    digest = hashlib.sha256(message.encode()).hexdigest()

    status, existing = channel._reserve("queue-test-1", digest, message)
    assert status == "new"
    assert existing is None

    claimed = channel._claim_next()
    assert claimed == ("queue-test-1", message)

    status, existing = channel._reserve("queue-test-1", digest, message)
    assert status == "existing"
    assert existing["status"] == "processing"


def test_idempotency_rejects_changed_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    first = "first"
    channel._reserve("same-id", hashlib.sha256(first.encode()).hexdigest(), first)

    second = "different"
    status, _ = channel._reserve("same-id", hashlib.sha256(second.encode()).hexdigest(), second)
    assert status == "conflict"


def test_completed_request_replays_saved_reply(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    message = "persist this result"
    digest = hashlib.sha256(message.encode()).hexdigest()
    channel._reserve("complete-1", digest, message)
    channel._finish("complete-1", "completed", reply="verified")

    status, row = channel._reserve("complete-1", digest, message)
    assert status == "existing"
    assert row["status"] == "completed"
    assert row["reply"] == "verified"


def test_outbox_is_persistent_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with channel._connect() as conn, conn:
        conn.execute(
            "INSERT INTO outbox(message_id,message,created_at) VALUES(?,?,?)",
            ("out-1", "hello ChatGPT", 1.0),
        )
        conn.execute(
            "INSERT OR IGNORE INTO outbox(message_id,message,created_at) VALUES(?,?,?)",
            ("out-1", "hello ChatGPT", 2.0),
        )
        rows = conn.execute("SELECT * FROM outbox WHERE acked_at IS NULL").fetchall()
        assert len(rows) == 1
        assert rows[0]["message"] == "hello ChatGPT"
        conn.execute("UPDATE outbox SET acked_at=3.0 WHERE message_id='out-1'")
        assert conn.execute("SELECT COUNT(*) FROM outbox WHERE acked_at IS NULL").fetchone()[0] == 0


def test_app_registers_outbox_and_worker_lifecycle():
    from admin.app import app

    paths = {}
    for route in app.routes:
        paths.setdefault(route.path, set()).update(getattr(route, "methods", []) or [])
    assert paths["/agent-channel/capabilities"] >= {"GET"}
    assert paths["/agent-channel/toolsets"] >= {"GET"}
    assert paths["/agent-channel/skills"] >= {"GET"}
    assert paths["/agent-channel/models"] >= {"GET"}
    assert paths["/agent-channel/model-options"] >= {"GET"}
    assert paths["/agent-channel/session"] >= {"GET"}
    assert paths["/agent-channel/model-test"] == {"POST"}
    assert paths["/agent-channel/diagnostics"] >= {"GET"}
    assert paths["/agent-channel/configure-primary"] == {"POST"}
    assert paths["/agent-channel/ui-audit"] >= {"GET"}
    assert paths["/agent-channel/outbox"] >= {"GET", "POST"}
    assert paths["/agent-channel/outbox/{message_id}/ack"] == {"POST"}
    assert getattr(app.router.lifespan_context, "__name__", "") == "lifespan"


def test_ui_audit_reads_recent_chat_and_redacts_secrets(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    state = tmp_path / ".hermes" / "webui"
    session_dir = state / "sessions"
    session_dir.mkdir(parents=True)
    session = {
        "session_id": "8c00d33543f6", "title": "UI rendering diagnosis",
        "model": "gpt-5.6-luna", "model_provider": "openai-api",
        "updated_at": 1794860000, "active_stream_id": "3d4b4755f146439ba9ea85696d33f6ec",
        "pending_started_at": 1794859900, "pending_user_message": "last request",
        "messages": [
            {"role": "user", "content": "The stream freezes"},
            {"role": "assistant", "content": "Bearer abcdefghijklmnopqrstuvwxyz"},
            {"role": "assistant", "content": "sk-abcdefghijklmnopqrstuv"},
            {"role": "assistant", "tool_calls": [
                {"id": "tool-1", "function": {"name": "terminal"}}
            ]},
        ],
    }
    (session_dir / "8c00d33543f6.json").write_text(json.dumps(session), encoding="utf-8")
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "webui.log").write_text("INFO start\nERROR stream closed unexpectedly\n", encoding="utf-8")
    result = channel._collect_ui_audit(8, 12, "8c00d33543f6")
    assert result["sessions"]["valid_sessions"] == 1
    row = result["selected_sessions"][0]
    assert row["session_id"] == "8c00d33543f6"
    assert row["active_stream_id"] == "3d4b4755f146439ba9ea85696d33f6ec"
    assert row["recent_messages"][1]["content"] == "Bearer [REDACTED]"
    assert "[REDACTED]" in row["recent_messages"][2]["content"]
    assert result["logs"]["webui"]["error_count_in_tail"] == 1
    assert "stream closed unexpectedly" in result["logs"]["webui"]["errors"][0]
    assert "agent" in result["logs"]
    assert "errors" in result["logs"]


def test_ui_audit_skips_corrupt_and_oversized_session_files(tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session_dir = tmp_path / ".hermes" / "webui" / "sessions"
    session_dir.mkdir(parents=True)
    (session_dir / "broken.json").write_text("{", encoding="utf-8")
    (session_dir / "too-large.json").write_bytes(b"x" * (25 * 1024 * 1024 + 1))
    (session_dir / "valid.json").write_text(
        json.dumps({"session_id": "valid", "messages": [], "updated_at": 1}), encoding="utf-8"
    )
    result = channel._collect_ui_audit(8, 12, None)
    assert result["sessions"]["json_files"] == 3
    assert result["sessions"]["valid_sessions"] == 1
    assert result["sessions"]["invalid_sessions"] == 2


