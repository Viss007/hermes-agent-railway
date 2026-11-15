"""Authenticated, durable ChatGPT-to-Hermes message relay.

The public wrapper accepts one narrow chat operation. The Hermes API server remains
loopback-only and its full toolset is never exposed directly to the internet.
"""
from __future__ import annotations

import asyncio
from contextlib import closing
import hashlib
import hmac
import json
import os
import re
import sqlite3
import time
from pathlib import Path

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse

_SYSTEM_MESSAGE = """You are Railway Hermes, the execution agent in a persistent private
conversation with ChatGPT, acting on behalf of Viss. Treat each authenticated relay
message as an instruction delegated by Viss. Use your connected tools and persistent
workspace to complete the requested work, then verify the actual result. Do not replace
execution with a plan or progress report. Do not claim success without evidence. Keep
the reply concise and identify only genuine blockers. Follow normal safety, security,
and approval requirements for irreversible or externally consequential actions."""

_SESSION_ID = os.getenv("HERMES_CHATGPT_SESSION_ID", "chatgpt-operator-channel")
_CHANNEL_MODEL = os.getenv("HERMES_CHATGPT_CHANNEL_MODEL", "gpt-6-luna")
_CHANNEL_PROVIDER = os.getenv("HERMES_CHATGPT_CHANNEL_PROVIDER", "openai-codex")
_API_HOST = os.getenv("API_SERVER_HOST", "127.0.0.1")
_API_PORT = os.getenv("API_SERVER_PORT", "8642")
_API_BASE = f"http://{_API_HOST}:{_API_PORT}"
_LOCK = asyncio.Lock()
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _db_path() -> Path:
    return Path(os.getenv("HERMES_HOME", "/data")) / ".hermes" / "chatgpt-channel" / "channel.sqlite3"


def _connect() -> sqlite3.Connection:
    path = _db_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    conn = sqlite3.connect(path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS requests (
            request_id TEXT PRIMARY KEY,
            input_sha256 TEXT NOT NULL,
            status TEXT NOT NULL,
            reply TEXT,
            error TEXT,
            session_id TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )"""
    )
    conn.commit()
    try:
        os.chmod(path, 0o600)
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(path) + suffix)
            if sidecar.exists():
                os.chmod(sidecar, 0o600)
    except OSError:
        pass
    return conn


def _authorized(request: Request) -> bool:
    expected = os.getenv("HERMES_CHATGPT_CHANNEL_KEY", "")
    header = request.headers.get("authorization", "")
    if not expected or not header.startswith("Bearer "):
        return False
    supplied = header[7:].strip()
    return bool(supplied) and hmac.compare_digest(supplied, expected)


def _error(message: str, status: int) -> JSONResponse:
    return JSONResponse({"ok": False, "error": message}, status_code=status,
                        headers={"Cache-Control": "no-store"})


def _reserve(request_id: str, input_sha256: str) -> tuple[str, dict | None]:
    now = time.time()
    with closing(_connect()) as conn, conn:
        row = conn.execute(
            "SELECT * FROM requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row is not None:
            saved = dict(row)
            if saved["input_sha256"] != input_sha256:
                return "conflict", saved
            return "existing", saved
        conn.execute(
            """INSERT INTO requests
               (request_id, input_sha256, status, created_at, updated_at)
               VALUES (?, ?, 'processing', ?, ?)""",
            (request_id, input_sha256, now, now),
        )
    return "new", None


def _finish(request_id: str, status: str, *, reply: str | None = None,
            error: str | None = None, session_id: str | None = None) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute(
            """UPDATE requests SET status = ?, reply = ?, error = ?,
               session_id = COALESCE(?, session_id), updated_at = ?
               WHERE request_id = ?""",
            (status, reply, error, session_id, time.time(), request_id),
        )


async def _ensure_session(client: httpx.AsyncClient, headers: dict[str, str]) -> None:
    url = f"{_API_BASE}/api/sessions/{_SESSION_ID}"
    current = await client.get(url, headers=headers)
    if current.status_code == 200:
        return
    if current.status_code != 404:
        raise RuntimeError(f"Hermes session lookup returned HTTP {current.status_code}")
    created = await client.post(
        f"{_API_BASE}/api/sessions",
        headers=headers,
        json={
            "id": _SESSION_ID,
            "source": "api_server",
            "title": "ChatGPT ↔ Railway Hermes",
            "system_prompt": _SYSTEM_MESSAGE,
        },
    )
    if created.status_code in (200, 201):
        return
    # Concurrent first messages may race to create the same fixed session.
    if created.status_code == 409:
        retry = await client.get(url, headers=headers)
        if retry.status_code == 200:
            return
    raise RuntimeError(f"Hermes session creation returned HTTP {created.status_code}")


async def health(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _error("Unauthorized", 401)
    api_key = os.getenv("API_SERVER_KEY", "")
    if not api_key:
        return _error("Hermes API server key is not configured", 503)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(8.0, connect=3.0)) as client:
            response = await client.get(
                f"{_API_BASE}/v1/capabilities",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if response.status_code != 200:
            return _error(f"Hermes API server returned HTTP {response.status_code}", 503)
    except httpx.HTTPError:
        return _error("Hermes API server is unavailable", 503)
    return JSONResponse(
        {"ok": True, "status": "ready", "session_id": _SESSION_ID,
         "transport": "authenticated-local-api", "storage": "sqlite-persistent",
         "model": _CHANNEL_MODEL, "provider": _CHANNEL_PROVIDER},
        headers={"Cache-Control": "no-store"},
    )


async def chat(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _error("Unauthorized", 401)
    length = request.headers.get("content-length")
    if length and (not length.isdigit() or int(length) > 65536):
        return _error("Request body too large", 413)
    try:
        raw = await request.body()
        if len(raw) > 65536:
            return _error("Request body too large", 413)
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return _error("Expected a JSON request body", 400)
    if not isinstance(payload, dict):
        return _error("Expected a JSON object", 400)
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip():
        return _error("message must be a non-empty string", 400)
    if len(message) > 20000:
        return _error("message exceeds the 20000 character limit", 413)

    request_id = request.headers.get("idempotency-key") or payload.get("request_id")
    if request_id is None:
        request_id = "cgpt-" + os.urandom(16).hex()
    if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
        return _error("request_id must contain 1-128 safe characters", 400)

    input_sha256 = hashlib.sha256(message.encode("utf-8")).hexdigest()
    reservation, existing = await asyncio.to_thread(_reserve, request_id, input_sha256)
    if reservation == "conflict":
        return _error("request_id was already used with a different message", 409)
    if reservation == "existing" and existing is not None:
        if existing["status"] == "completed":
            return JSONResponse(
                {"ok": True, "status": "completed", "request_id": request_id,
                 "session_id": existing["session_id"] or _SESSION_ID,
                 "reply": existing["reply"] or "", "replayed": True},
                headers={"Cache-Control": "no-store"},
            )
        if existing["status"] == "processing":
            return JSONResponse(
                {"ok": True, "status": "processing", "request_id": request_id,
                 "session_id": existing["session_id"] or _SESSION_ID},
                status_code=202, headers={"Cache-Control": "no-store"},
            )
        return _error(existing["error"] or "Previous request failed; inspect status before retrying", 502)

    api_key = os.getenv("API_SERVER_KEY", "")
    if not api_key:
        await asyncio.to_thread(_finish, request_id, "failed", error="API server key missing")
        return _error("Hermes API server key is not configured", 503)

    async with _LOCK:
        try:
            api_headers = {"Authorization": f"Bearer {api_key}"}
            timeout = httpx.Timeout(180.0, connect=5.0, read=180.0, write=15.0, pool=5.0)
            async with httpx.AsyncClient(timeout=timeout) as client:
                await _ensure_session(client, api_headers)
                model_lock = await client.post(
                    f"{_API_BASE}/api/sessions/{_SESSION_ID}/model",
                    headers=api_headers,
                    json={"model": _CHANNEL_MODEL, "provider": _CHANNEL_PROVIDER},
                )
                if model_lock.status_code != 200:
                    await asyncio.to_thread(
                        _finish, request_id, "failed",
                        error=f"Hermes model selection returned HTTP {model_lock.status_code}",
                        session_id=_SESSION_ID,
                    )
                    return _error(f"Hermes model selection returned HTTP {model_lock.status_code}", 502)
                response = await client.post(
                    f"{_API_BASE}/api/sessions/{_SESSION_ID}/chat",
                    headers={**api_headers, "Idempotency-Key": request_id},
                    json={"message": message, "system_message": _SYSTEM_MESSAGE},
                )
            if response.status_code != 200:
                await asyncio.to_thread(
                    _finish, request_id, "failed",
                    error=f"Hermes API returned HTTP {response.status_code}",
                    session_id=_SESSION_ID,
                )
                return _error(f"Hermes agent returned HTTP {response.status_code}", 502)
            result = response.json()
            state = result.get("status", "completed")
            content = result.get("message", {}).get("content", "")
            if not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            if state != "completed":
                await asyncio.to_thread(
                    _finish, request_id, "failed", reply=content,
                    error=f"Hermes turn finished with status {state}", session_id=_SESSION_ID,
                )
                return JSONResponse(
                    {"ok": False, "status": state, "request_id": request_id,
                     "session_id": _SESSION_ID, "reply": content},
                    status_code=502, headers={"Cache-Control": "no-store"},
                )
            await asyncio.to_thread(
                _finish, request_id, "completed", reply=content, session_id=_SESSION_ID,
            )
            return JSONResponse(
                {"ok": True, "status": "completed", "request_id": request_id,
                 "session_id": _SESSION_ID, "reply": content,
                 "usage": result.get("usage", {}), "replayed": False},
                headers={"Cache-Control": "no-store"},
            )
        except httpx.TimeoutException:
            # Do not replay automatically: the agent may still have completed the turn.
            await asyncio.to_thread(
                _finish, request_id, "processing",
                error="Hermes response timed out; inspect request status before retrying",
                session_id=_SESSION_ID,
            )
            return JSONResponse(
                {"ok": True, "status": "processing", "request_id": request_id,
                 "session_id": _SESSION_ID},
                status_code=202, headers={"Cache-Control": "no-store"},
            )
        except httpx.HTTPError:
            await asyncio.to_thread(
                _finish, request_id, "failed", error="Could not reach the local Hermes API server",
                session_id=_SESSION_ID,
            )
            return _error("Could not reach the local Hermes API server", 503)
        except Exception:
            await asyncio.to_thread(
                _finish, request_id, "failed", error="Hermes relay failed unexpectedly",
                session_id=_SESSION_ID,
            )
            return _error("Hermes relay failed unexpectedly", 502)


async def get_request(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _error("Unauthorized", 401)
    request_id = request.path_params.get("request_id", "")
    if not isinstance(request_id, str) or not _REQUEST_ID_RE.fullmatch(request_id):
        return _error("Invalid request_id", 400)
    with closing(_connect()) as conn, conn:
        row = conn.execute(
            "SELECT request_id, status, reply, error, session_id, created_at, updated_at "
            "FROM requests WHERE request_id = ?", (request_id,)
        ).fetchone()
    if row is None:
        return _error("Request not found", 404)
    return JSONResponse({"ok": row["status"] == "completed", **dict(row)},
                        headers={"Cache-Control": "no-store"})
