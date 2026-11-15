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

    paths = {route.path: set(getattr(route, "methods", []) or []) for route in app.routes}
    assert paths["/agent-channel/capabilities"] == {"GET"}
    assert paths["/agent-channel/toolsets"] == {"GET"}
    assert paths["/agent-channel/skills"] == {"GET"}
    assert paths["/agent-channel/models"] == {"GET"}
    assert paths["/agent-channel/model-options"] == {"GET"}
    assert paths["/agent-channel/session"] == {"GET"}
    assert paths["/agent-channel/model-test"] == {"POST"}
    assert paths["/agent-channel/outbox"] == {"GET", "POST"}
    assert paths["/agent-channel/outbox/{message_id}/ack"] == {"POST"}
    assert getattr(app.router.lifespan_context, "__name__", "") == "lifespan"
