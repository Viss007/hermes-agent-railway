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


def test_worker_startup_quarantines_all_orphaned_processing_requests(tmp_path, monkeypatch):
    import hashlib
    import time

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    digest = hashlib.sha256(b"possibly completed upstream").hexdigest()
    channel._reserve("orphaned-now", digest, "possibly completed upstream")
    claimed = channel._claim_next()
    assert claimed == ("orphaned-now", "possibly completed upstream")

    # Simulate a worker dying seconds after the chat request may have reached Hermes.
    channel._mark_chat_submitted("orphaned-now")
    channel._mark_stale_uncertain()
    status, row = channel._reserve("orphaned-now", digest, "possibly completed upstream")
    assert status == "existing"
    assert row["status"] == "uncertain"
    assert "inspect Hermes session" in row["error"]
    assert row["updated_at"] >= time.time() - 5



def test_same_origin_check_blocks_cross_site_websocket_and_accepts_proxy_origin():
    from admin.security import same_origin

    assert same_origin({"origin": "https://hermes.example.com", "host": "hermes.example.com"})
    assert same_origin({"origin": "https://hermes.example.com", "host": "hermes.example.com:443"})
    assert same_origin({"origin": "http://localhost:8080", "host": "localhost:8080"})
    assert not same_origin({"origin": "https://evil.example", "host": "hermes.example.com"})
    assert not same_origin({"origin": "https://hermes.example.com:444", "host": "hermes.example.com"})
    assert not same_origin({"origin": "null", "host": "hermes.example.com"})
    assert not same_origin({"host": "hermes.example.com"})


def test_model_test_identity_uses_full_id_and_separates_test_kinds():
    a = channel._model_test_identity("same-id", "glm-5.3-flash-uncensored", "terminal")
    assert a == channel._model_test_identity("same-id", "glm-5.3-flash-uncensored", "terminal")
    assert a != channel._model_test_identity("same-id", "deepseek-v4.1-flash-uncensored", "terminal")
    assert a != channel._model_test_identity("same-id", "glm-5.3-flash-uncensored", "browser")
    prefix_a = "x" * 90 + "a"
    prefix_b = "x" * 90 + "b"
    assert channel._model_test_identity(prefix_a, "glm-5.3-flash-uncensored", "terminal") != (
        channel._model_test_identity(prefix_b, "glm-5.3-flash-uncensored", "terminal")
    )


def test_pre_submission_failures_retry_with_backoff_and_stop_after_four_attempts(tmp_path, monkeypatch):
    import time

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    message = "run after local gateway recovers"
    digest = hashlib.sha256(message.encode()).hexdigest()
    channel._reserve("retry-queue-1", digest, message)

    assert channel._claim_next() == ("retry-queue-1", message)
    assert channel._retry_before_submission("retry-queue-1", "connection refused") is True
    status, row = channel._reserve("retry-queue-1", digest, message)
    assert status == "existing"
    assert row["status"] == "queued"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] > time.time()
    assert channel._claim_next() is None

    with channel._connect() as conn, conn:
        conn.execute(
            "UPDATE requests SET next_attempt_at=? WHERE request_id=?",
            (time.time() - 1, "retry-queue-1"),
        )
    assert channel._claim_next() == ("retry-queue-1", message)
    assert channel._retry_before_submission("retry-queue-1", "still unavailable") is True

    with channel._connect() as conn, conn:
        conn.execute(
            "UPDATE requests SET next_attempt_at=? WHERE request_id=?",
            (time.time() - 1, "retry-queue-1"),
        )
    assert channel._claim_next() == ("retry-queue-1", message)
    assert channel._retry_before_submission("retry-queue-1", "still unavailable") is True

    with channel._connect() as conn, conn:
        conn.execute(
            "UPDATE requests SET next_attempt_at=? WHERE request_id=?",
            (time.time() - 1, "retry-queue-1"),
        )
    assert channel._claim_next() == ("retry-queue-1", message)
    assert channel._retry_before_submission("retry-queue-1", "still unavailable") is False
    status, row = channel._reserve("retry-queue-1", digest, message)
    assert status == "existing"
    assert row["status"] == "failed"
    assert "after 4 attempts" in row["error"]


def test_old_queue_database_migrates_retry_columns(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = tmp_path / ".hermes" / "chatgpt-channel" / "channel.sqlite3"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE requests (
                request_id TEXT PRIMARY KEY,
                input_sha256 TEXT NOT NULL,
                status TEXT NOT NULL,
                reply TEXT,
                error TEXT,
                session_id TEXT,
                message TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            )"""
        )
        conn.execute(
            "INSERT INTO requests(request_id,input_sha256,status,message,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            ("legacy-in-flight", "digest", "processing", "possibly sent", 10.0, 123.0),
        )
    with channel._connect() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(requests)")}
        row = conn.execute(
            "SELECT chat_submitted_at FROM requests WHERE request_id='legacy-in-flight'"
        ).fetchone()
    assert {"attempts", "next_attempt_at", "chat_submitted_at"} <= columns
    assert row["chat_submitted_at"] == 123.0


def test_relay_only_marks_explicit_completed_response_as_completed(tmp_path, monkeypatch):
    import asyncio
    import time

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("API_SERVER_KEY", "test-api-key")
    message = "finish and verify"
    digest = hashlib.sha256(message.encode()).hexdigest()
    channel._reserve("response-state-1", digest, message)
    channel._claim_next()

    async def no_session(client, headers):
        return None

    class Response:
        status_code = 200
        text = ""

        def json(self):
            return {"message": {"content": "reply without terminal status"}}

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, url, **kwargs):
            if url.endswith("/model"):
                return ResponseModel()
            return Response()

    class ResponseModel:
        status_code = 200

    monkeypatch.setattr(channel, "_ensure_session", no_session)
    monkeypatch.setattr(channel.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    asyncio.run(channel._execute_request("response-state-1", message))

    with channel._connect() as conn:
        row = conn.execute(
            "SELECT status, error, updated_at FROM requests WHERE request_id=?",
            ("response-state-1",),
        ).fetchone()
    assert row["status"] == "uncertain"
    assert "did not confirm completion" in row["error"]
    assert row["updated_at"] <= time.time()


def test_relay_retries_connect_failure_before_chat_submission(tmp_path, monkeypatch):
    import asyncio
    import time
    import httpx

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("API_SERVER_KEY", "test-api-key")
    message = "wait for the gateway"
    digest = hashlib.sha256(message.encode()).hexdigest()
    channel._reserve("connect-retry-1", digest, message)
    channel._claim_next()

    async def no_session(client, headers):
        return None

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, url, **kwargs):
            raise httpx.ConnectError("gateway starting")

    monkeypatch.setattr(channel, "_ensure_session", no_session)
    monkeypatch.setattr(channel.httpx, "AsyncClient", lambda **kwargs: FakeClient())
    asyncio.run(channel._execute_request("connect-retry-1", message))

    with channel._connect() as conn:
        row = conn.execute(
            "SELECT status, attempts, next_attempt_at FROM requests WHERE request_id=?",
            ("connect-retry-1",),
        ).fetchone()
    assert row["status"] == "queued"
    assert row["attempts"] == 1
    assert row["next_attempt_at"] > time.time()


def test_worker_restart_requeues_only_requests_not_yet_submitted_to_hermes(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    message = "safe to resume before POST"
    digest = hashlib.sha256(message.encode()).hexdigest()
    channel._reserve("pre-submit-orphan", digest, message)
    assert channel._claim_next() == ("pre-submit-orphan", message)

    channel._mark_stale_uncertain()
    status, row = channel._reserve("pre-submit-orphan", digest, message)
    assert status == "existing"
    assert row["status"] == "queued"
    assert row["attempts"] == 0
    assert "safely requeued" in row["error"]
    assert channel._claim_next() == ("pre-submit-orphan", message)


def test_retryable_admission_refusals_are_only_known_pre_execution_responses():
    from types import SimpleNamespace

    assert channel._retryable_admission_refusal(
        SimpleNamespace(status_code=429, json=lambda: {})
    )
    assert channel._retryable_admission_refusal(
        SimpleNamespace(status_code=503, json=lambda: {"error": {"code": "gateway_draining"}})
    )
    assert not channel._retryable_admission_refusal(
        SimpleNamespace(status_code=503, json=lambda: {"error": {"code": "storage_error"}})
    )
    assert not channel._retryable_admission_refusal(
        SimpleNamespace(status_code=500, json=lambda: {})
    )


def test_authenticated_chat_request_reaches_completed_queue_receipt(tmp_path, monkeypatch):
    import time
    from starlette.testclient import TestClient
    from admin.app import app

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_CHATGPT_CHANNEL_KEY", "relay-test-key")
    monkeypatch.setenv("API_SERVER_KEY", "hermes-api-test-key")
    monkeypatch.setattr(channel, "_API_BASE", "http://hermes-api.test")

    class FakeResponse:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = ""

        def json(self):
            return self._payload

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, url, **kwargs):
            return FakeResponse(404)

        async def post(self, url, **kwargs):
            if url.endswith("/api/sessions"):
                return FakeResponse(201)
            if url.endswith("/model"):
                return FakeResponse(200)
            if url.endswith("/chat"):
                return FakeResponse(200, {
                    "status": "completed",
                    "message": {"role": "assistant", "content": "verified result"},
                })
            raise AssertionError(f"Unexpected Hermes API request: {url}")

    monkeypatch.setattr(channel.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient())

    with TestClient(app) as client:
        accepted = client.post(
            "/agent-channel/chat",
            headers={"Authorization": "Bearer relay-test-key", "Idempotency-Key": "e2e-relay-1"},
            json={"message": "Run the verified end-to-end task"},
        )
        assert accepted.status_code == 202
        deadline = time.time() + 2
        receipt = None
        while time.time() < deadline:
            response = client.get(
                "/agent-channel/requests/e2e-relay-1",
                headers={"Authorization": "Bearer relay-test-key"},
            )
            receipt = response.json()
            if receipt.get("status") == "completed":
                break
            time.sleep(0.01)

        assert receipt is not None
        assert receipt["status"] == "completed"
        assert receipt["reply"] == "verified result"


def test_worker_survives_temporary_queue_read_failure(monkeypatch):
    import asyncio

    calls = {"count": 0}

    def flaky_claim():
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("temporary database lock")
        return None

    async def run():
        stop_event = asyncio.Event()
        wake_event = asyncio.Event()
        monkeypatch.setattr(channel, "_STOP_EVENT", stop_event)
        monkeypatch.setattr(channel, "_WAKE_EVENT", wake_event)
        monkeypatch.setattr(channel, "_mark_stale_uncertain", lambda: None)
        monkeypatch.setattr(channel, "_claim_next", flaky_claim)

        async def stop_soon():
            await asyncio.sleep(0.05)
            wake_event.set()
            for _ in range(100):
                if calls["count"] >= 2:
                    break
                await asyncio.sleep(0.01)
            stop_event.set()
            wake_event.set()

        stopper = asyncio.create_task(stop_soon())
        try:
            await channel._worker_loop()
        finally:
            await stopper

    asyncio.run(run())
    assert calls["count"] >= 2


def test_worker_shutdown_cancels_and_joins_a_stuck_worker(monkeypatch):
    import asyncio

    async def run():
        stop_event = asyncio.Event()
        wake_event = asyncio.Event()
        monkeypatch.setattr(channel, "_STOP_EVENT", stop_event)
        monkeypatch.setattr(channel, "_WAKE_EVENT", wake_event)
        monkeypatch.setattr(channel, "_WORKER_STOP_TIMEOUT", 0.01)

        async def stuck_worker():
            await asyncio.Event().wait()

        task = asyncio.create_task(stuck_worker())
        monkeypatch.setattr(channel, "_WORKER_TASK", task)
        await channel.stop_worker()
        assert task.done()
        assert task.cancelled()
        assert channel._WORKER_TASK is None

    asyncio.run(run())


def test_model_test_reservation_prevents_replaying_a_started_test(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    session_id, test_key = channel._model_test_identity(
        "model-run-1", "glm-5.3-flash-uncensored", "terminal"
    )
    reserved, previous = channel._reserve_model_test(test_key, session_id)
    assert reserved is True
    assert previous is None

    channel._finish_model_test(test_key, "completed")
    reserved, previous = channel._reserve_model_test(test_key, session_id)
    assert reserved is False
    assert previous["status"] == "completed"

    # Even a started-but-not-settled attempt is fail-closed after a process crash.
    another_session, another_key = channel._model_test_identity(
        "model-run-2", "glm-5.3-flash-uncensored", "terminal"
    )
    assert channel._reserve_model_test(another_key, another_session)[0] is True
    reserved, previous = channel._reserve_model_test(another_key, another_session)
    assert reserved is False
    assert previous["status"] == "running"


def test_model_test_endpoint_never_submits_same_test_id_twice(tmp_path, monkeypatch):
    from starlette.testclient import TestClient
    from admin.app import app

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_CHATGPT_CHANNEL_KEY", "model-test-key")
    monkeypatch.setenv("API_SERVER_KEY", "hermes-api-test-key")
    monkeypatch.setattr(channel, "_API_BASE", "http://hermes-api.test")
    calls = {"chat": 0}

    class FakeResponse:
        def __init__(self, status_code, payload=None):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = ""

        def json(self):
            return self._payload

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def get(self, url, **kwargs):
            return FakeResponse(404)

        async def post(self, url, **kwargs):
            if url.endswith("/api/sessions"):
                return FakeResponse(201)
            if url.endswith("/model"):
                return FakeResponse(200)
            if url.endswith("/chat"):
                calls["chat"] += 1
                return FakeResponse(200, {
                    "status": "completed",
                    "message": {"role": "assistant", "content": "MODEL_TOOL_PASS"},
                    "usage": {},
                })
            raise AssertionError(f"Unexpected Hermes API request: {url}")

    monkeypatch.setattr(channel.httpx, "AsyncClient", lambda **kwargs: FakeAsyncClient())
    payload = {
        "model": "glm-5.3-flash-uncensored",
        "test_kind": "terminal",
        "test_id": "single-model-test-1",
    }

    with TestClient(app) as client:
        headers = {"Authorization": "Bearer model-test-key"}
        first = client.post("/agent-channel/model-test", headers=headers, json=payload)
        second = client.post("/agent-channel/model-test", headers=headers, json=payload)

    assert first.status_code == 200
    assert second.status_code == 409
    assert calls["chat"] == 1


def test_interrupted_submission_marker_migration_is_atomic_and_fail_closed(tmp_path, monkeypatch):
    import sqlite3

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = tmp_path / ".hermes" / "chatgpt-channel" / "channel.sqlite3"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute(
            """CREATE TABLE requests (
                request_id TEXT PRIMARY KEY,
                input_sha256 TEXT NOT NULL,
                status TEXT NOT NULL,
                reply TEXT,
                error TEXT,
                session_id TEXT,
                message TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at REAL,
                chat_submitted_at REAL
            )"""
        )
        conn.execute(
            "INSERT INTO requests(request_id,input_sha256,status,message,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?)",
            ("partially-migrated", "digest", "processing", "maybe sent", 10.0, 321.0),
        )
        conn.execute("PRAGMA user_version=0")

    with channel._connect() as conn:
        row = conn.execute(
            "SELECT chat_submitted_at FROM requests WHERE request_id='partially-migrated'"
        ).fetchone()
        version = conn.execute("PRAGMA user_version").fetchone()[0]

    assert row["chat_submitted_at"] == 321.0
    assert version == 1


def test_external_shutdown_cancellation_is_not_swallowed(monkeypatch):
    import asyncio
    import pytest

    async def run():
        stop_event = asyncio.Event()
        wake_event = asyncio.Event()
        monkeypatch.setattr(channel, "_STOP_EVENT", stop_event)
        monkeypatch.setattr(channel, "_WAKE_EVENT", wake_event)
        monkeypatch.setattr(channel, "_WORKER_STOP_TIMEOUT", 5.0)

        async def stuck_worker():
            await asyncio.Event().wait()

        worker = asyncio.create_task(stuck_worker())
        monkeypatch.setattr(channel, "_WORKER_TASK", worker)
        stopper = asyncio.create_task(channel.stop_worker())
        await asyncio.sleep(0.01)
        stopper.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stopper
        assert worker.done()
        assert worker.cancelled()
        assert channel._WORKER_TASK is None

    asyncio.run(run())
