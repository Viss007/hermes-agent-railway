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
_WORKER_TASK: asyncio.Task | None = None
_WAKE_EVENT = asyncio.Event()
_STOP_EVENT = asyncio.Event()
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
            message TEXT,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )"""
    )
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(requests)").fetchall()}
    if "message" not in columns:
        conn.execute("ALTER TABLE requests ADD COLUMN message TEXT")
    conn.execute(
        """CREATE TABLE IF NOT EXISTS outbox (
            message_id TEXT PRIMARY KEY,
            message TEXT NOT NULL,
            created_at REAL NOT NULL,
            acked_at REAL
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


def _reserve(request_id: str, input_sha256: str, message: str) -> tuple[str, dict | None]:
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
               (request_id, input_sha256, status, message, created_at, updated_at)
               VALUES (?, ?, 'queued', ?, ?, ?)""",
            (request_id, input_sha256, message, now, now),
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




async def capabilities(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _error("Unauthorized", 401)
    api_key = os.getenv("API_SERVER_KEY", "")
    if not api_key:
        return _error("Hermes API server key is not configured", 503)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=3.0)) as client:
            response = await client.get(
                f"{_API_BASE}/v1/capabilities",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if response.status_code != 200:
            return _error(f"Hermes capabilities returned HTTP {response.status_code}", 502)
        return JSONResponse(response.json(), headers={"Cache-Control": "no-store"})
    except httpx.HTTPError:
        return _error("Hermes capabilities endpoint is unavailable", 503)
    except ValueError:
        return _error("Hermes capabilities response was invalid JSON", 502)


async def _runtime_json(request: Request, path: str) -> JSONResponse:
    if not _authorized(request):
        return _error("Unauthorized", 401)
    api_key = os.getenv("API_SERVER_KEY", "")
    if not api_key:
        return _error("Hermes API server key is not configured", 503)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=3.0)) as client:
            response = await client.get(
                f"{_API_BASE}{path}",
                headers={"Authorization": f"Bearer {api_key}"},
            )
        if response.status_code != 200:
            return _error(f"Hermes runtime endpoint returned HTTP {response.status_code}", 502)
        return JSONResponse(response.json(), headers={"Cache-Control": "no-store"})
    except httpx.HTTPError:
        return _error("Hermes runtime endpoint is unavailable", 503)
    except ValueError:
        return _error("Hermes runtime response was invalid JSON", 502)


async def toolsets(request: Request) -> JSONResponse:
    return await _runtime_json(request, "/v1/toolsets")


async def skills(request: Request) -> JSONResponse:
    return await _runtime_json(request, "/v1/skills")


async def models(request: Request) -> JSONResponse:
    return await _runtime_json(request, "/v1/models")

async def model_options(request: Request) -> JSONResponse:
    return await _runtime_json(request, "/api/model/options")


async def current_session(request: Request) -> JSONResponse:
    return await _runtime_json(request, f"/api/sessions/{_SESSION_ID}")


async def model_test(request: Request) -> JSONResponse:
    """Isolated, allowlisted tool-use test; never changes the primary session."""
    if not _authorized(request):
        return _error("Unauthorized", 401)
    try:
        payload = await request.json()
    except Exception:
        return _error("Expected a JSON request body", 400)
    if not isinstance(payload, dict):
        return _error("Expected a JSON object", 400)
    model = payload.get("model")
    allowed_models = {"glm-5.3-flash-uncensored", "deepseek-v4.1-flash-uncensored"}
    if model not in allowed_models:
        return _error("Model is not in the isolated test allowlist", 400)
    test_kind = payload.get("test_kind", "terminal")
    allowed_kinds = {"terminal", "file", "browser", "web", "connections", "bridge"}
    if test_kind not in allowed_kinds:
        return _error("test_kind is not in the isolated test allowlist", 400)
    test_id = payload.get("test_id") or ("test-" + os.urandom(8).hex())
    if not isinstance(test_id, str) or not _REQUEST_ID_RE.fullmatch(test_id):
        return _error("test_id must contain 1-128 safe characters", 400)
    session_id = "model-test-" + test_id[:90]
    api_key = os.getenv("API_SERVER_KEY", "")
    if not api_key:
        return _error("Hermes API server key is not configured", 503)
    headers = {"Authorization": f"Bearer {api_key}"}
    test_prompts = {
        "terminal": (
            "Use your terminal tool exactly once to run Python that prints the literal "
            "MODEL_TOOL_PASS. Do not run any other command and do not inspect files or "
            "environment variables. After the tool returns, reply with only the captured output."
        ),
        "file": (
            f"Use your file tool to create /data/workspace/{test_id}.txt with exactly "
            "FILE_TOOL_PASS, then read that same file back. Do not inspect any other file. "
            "Reply with the exact readback."
        ),
        "browser": (
            "Use the browser tool to open https://example.com. Return the page title and "
            "final URL only. Do not click links or submit forms."
        ),
        "web": (
            "Use the web_search tool to search for NousResearch Hermes Agent. Return the "
            "top result title and URL only."
        ),
        "connections": (
            "Use your connections tool to list existing connected integrations and their "
            "authorization status. Do not start OAuth, connect, disconnect or change anything. "
            "Return only connector names and statuses."
        ),
        "bridge": (
            "Use the configured Personal Control Bridge MCP tools bridge_whoami and "
            "bridge_health for read-only checks. Do not use terminal or any fallback. "
            "Return the exact tool names and result statuses."
        ),
    }
    test_prompt = test_prompts[test_kind]
    test_system = (
        "This is an isolated model capability test. Use only the specifically named tool "
        "for this test. Do not inspect secrets, credentials, unrelated files or personal data. "
        "Do not use another tool as a fallback."
    )
    try:
        timeout = httpx.Timeout(300.0, connect=5.0, read=300.0, write=20.0, pool=10.0)
        async with httpx.AsyncClient(timeout=timeout) as client:
            current = await client.get(f"{_API_BASE}/api/sessions/{session_id}", headers=headers)
            if current.status_code == 404:
                created = await client.post(
                    f"{_API_BASE}/api/sessions",
                    headers=headers,
                    json={"id": session_id, "source": "api_server",
                          "title": f"Isolated model test: {model}",
                          "system_prompt": test_system},
                )
                if created.status_code not in (200, 201, 409):
                    return _error(f"Test session creation returned HTTP {created.status_code}", 502)
            elif current.status_code != 200:
                return _error(f"Test session lookup returned HTTP {current.status_code}", 502)
            locked = await client.post(
                f"{_API_BASE}/api/sessions/{session_id}/model",
                headers=headers,
                json={"model": model, "provider": "openai-api"},
            )
            if locked.status_code != 200:
                detail = " ".join((locked.text or "").split())[:800]
                return _error(f"Model lock failed HTTP {locked.status_code}: {detail}", 502)
            response = await client.post(
                f"{_API_BASE}/api/sessions/{session_id}/chat",
                headers={**headers, "Idempotency-Key": test_id},
                json={"message": test_prompt, "system_message": test_system},
            )
        if response.status_code != 200:
            detail = " ".join((response.text or "").split())[:1200]
            return _error(f"Model test returned HTTP {response.status_code}: {detail}", 502)
        result = response.json()
        content = result.get("message", {}).get("content", "")
        runtime = result.get("usage", {}).get("runtime", {})
        return JSONResponse(
            {"ok": True, "test_id": test_id, "session_id": session_id,
             "requested_model": model, "provider": "openai-api", "test_kind": test_kind,
             "status": result.get("status"), "reply": content,
             "runtime": runtime, "usage": result.get("usage", {})},
            headers={"Cache-Control": "no-store"},
        )
    except httpx.TimeoutException:
        return _error("Model test exceeded 300 seconds; inspect the isolated session before retrying", 504)
    except httpx.HTTPError:
        return _error("Hermes API server was unavailable during model test", 503)
    except (ValueError, TypeError):
        return _error("Model test response was invalid", 502)


async def diagnostics(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _error("Unauthorized", 401)
    home = Path(os.getenv("HERMES_HOME", "/data"))
    cfg_path = home / "config.yaml"
    auth_path = home / "auth.json"
    cfg = {}
    try:
        import yaml
        cfg = yaml.safe_load(cfg_path.read_text()) or {} if cfg_path.exists() else {}
    except Exception:
        cfg = {}
    model_cfg = cfg.get("model") if isinstance(cfg.get("model"), dict) else {}
    base_url = str(model_cfg.get("base_url") or os.getenv("OPENAI_BASE_URL", ""))
    from urllib.parse import urlsplit
    try:
        base_host = urlsplit(base_url).hostname if base_url else None
    except ValueError:
        base_host = "invalid"
    mcp_cfg = cfg.get("mcp_servers") or cfg.get("mcpServers") or cfg.get("mcp") or {}
    mcp_servers = []
    if isinstance(mcp_cfg, dict):
        for name, value in mcp_cfg.items():
            item = value if isinstance(value, dict) else {}
            raw_url = str(item.get("url") or item.get("server_url") or "")
            try:
                host = urlsplit(raw_url).hostname if raw_url else None
            except ValueError:
                host = "invalid"
            command = str(item.get("command") or "")
            mcp_servers.append({
                "name": str(name)[:100],
                "enabled": item.get("enabled", True),
                "transport": str(item.get("transport") or item.get("type") or ("http" if raw_url else "stdio" if command else "unknown"))[:40],
                "host": host,
                "command": Path(command).name if command else None,
            })
    auth_providers = []
    try:
        auth = json.loads(auth_path.read_text()) if auth_path.exists() else {}
        providers = auth.get("providers", {}) if isinstance(auth, dict) else {}
        if isinstance(providers, dict):
            auth_providers = sorted(str(x) for x in providers.keys())
    except Exception:
        auth_providers = ["unreadable"]
    secret_name_prefixes = ("MCP_", "PERSONAL_CONTROL_", "COMPOSIO_", "OPENAI_", "GLM_", "DEEPSEEK_", "XAI_", "FIREWORKS_", "NOVITA_", "ANTHROPIC_", "GOOGLE_", "GITHUB_", "GMAIL_")
    configured_env_names = sorted(
        name for name, value in os.environ.items()
        if name.startswith(secret_name_prefixes) and bool(value)
    )
    def dirs(path: Path) -> list[str]:
        try:
            return sorted(x.name for x in path.iterdir() if x.is_dir())[:100]
        except OSError:
            return []
    return JSONResponse({
        "ok": True,
        "home": str(home),
        "model": {"provider": model_cfg.get("provider"), "default": model_cfg.get("default"),
                  "base_url_host": base_host},
        "auth_providers": auth_providers,
        "mcp_servers": mcp_servers,
        "configured_secret_env_names": configured_env_names,
        "skills_dirs": {"skills": dirs(home / "skills"), "hermes_skills": dirs(home / ".hermes" / "skills")},
        "channel": {"model": _CHANNEL_MODEL, "provider": _CHANNEL_PROVIDER},
    }, headers={"Cache-Control": "no-store"})

def _claim_next() -> tuple[str, str] | None:
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT request_id, message FROM requests WHERE status = 'queued' "
            "ORDER BY created_at LIMIT 1"
        ).fetchone()
        if row is None:
            conn.commit()
            return None
        conn.execute(
            "UPDATE requests SET status='processing', error=NULL, updated_at=? "
            "WHERE request_id=? AND status='queued'",
            (time.time(), row["request_id"]),
        )
        conn.commit()
        return row["request_id"], row["message"] or ""


def _mark_stale_uncertain() -> None:
    cutoff = time.time() - 1200
    with closing(_connect()) as conn, conn:
        conn.execute(
            "UPDATE requests SET status='uncertain', "
            "error='Worker stopped while request was processing; inspect Hermes session before retrying', "
            "updated_at=? WHERE status='processing' AND updated_at < ?",
            (time.time(), cutoff),
        )


async def _execute_request(request_id: str, message: str) -> None:
    api_key = os.getenv("API_SERVER_KEY", "")
    if not api_key:
        await asyncio.to_thread(_finish, request_id, "failed", error="API server key missing")
        return
    async with _LOCK:
        try:
            api_headers = {"Authorization": f"Bearer {api_key}"}
            timeout = httpx.Timeout(900.0, connect=5.0, read=900.0, write=30.0, pool=10.0)
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
                    return
                response = await client.post(
                    f"{_API_BASE}/api/sessions/{_SESSION_ID}/chat",
                    headers={**api_headers, "Idempotency-Key": request_id},
                    json={"message": message, "system_message": _SYSTEM_MESSAGE},
                )
            if response.status_code != 200:
                detail = " ".join((response.text or "").split())[:1200]
                await asyncio.to_thread(
                    _finish, request_id, "failed",
                    error=f"Hermes API returned HTTP {response.status_code}: {detail}",
                    session_id=_SESSION_ID,
                )
                return
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
                return
            await asyncio.to_thread(
                _finish, request_id, "completed", reply=content, session_id=_SESSION_ID,
            )
        except httpx.TimeoutException:
            await asyncio.to_thread(
                _finish, request_id, "uncertain",
                error="Hermes API exceeded 900 seconds; inspect session history before retrying",
                session_id=_SESSION_ID,
            )
        except httpx.HTTPError:
            await asyncio.to_thread(
                _finish, request_id, "failed", error="Could not reach the local Hermes API server",
                session_id=_SESSION_ID,
            )
        except Exception:
            await asyncio.to_thread(
                _finish, request_id, "failed", error="Hermes relay worker failed unexpectedly",
                session_id=_SESSION_ID,
            )


async def _worker_loop() -> None:
    await asyncio.to_thread(_mark_stale_uncertain)
    while not _STOP_EVENT.is_set():
        item = await asyncio.to_thread(_claim_next)
        if item is not None:
            request_id, message = item
            await _execute_request(request_id, message)
            continue
        _WAKE_EVENT.clear()
        try:
            await asyncio.wait_for(_WAKE_EVENT.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass


async def start_worker() -> None:
    global _WORKER_TASK
    _STOP_EVENT.clear()
    if _WORKER_TASK is None or _WORKER_TASK.done():
        _WORKER_TASK = asyncio.create_task(_worker_loop(), name="hermes-channel-worker")


async def stop_worker() -> None:
    global _WORKER_TASK
    _STOP_EVENT.set()
    _WAKE_EVENT.set()
    if _WORKER_TASK is not None:
        try:
            await asyncio.wait_for(_WORKER_TASK, timeout=5.0)
        except asyncio.TimeoutError:
            _WORKER_TASK.cancel()
        _WORKER_TASK = None


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
    reservation, existing = await asyncio.to_thread(_reserve, request_id, input_sha256, message)
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
        if existing["status"] in ("queued", "processing"):
            return JSONResponse(
                {"ok": True, "status": existing["status"], "request_id": request_id,
                 "session_id": existing["session_id"] or _SESSION_ID},
                status_code=202, headers={"Cache-Control": "no-store"},
            )
        return _error(existing["error"] or "Previous request is not safe to replay", 409)
    if _WORKER_TASK is None or _WORKER_TASK.done():
        await start_worker()
    _WAKE_EVENT.set()
    return JSONResponse(
        {"ok": True, "status": "queued", "request_id": request_id, "session_id": _SESSION_ID},
        status_code=202, headers={"Cache-Control": "no-store"},
    )


async def post_outbox(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _error("Unauthorized", 401)
    try:
        payload = await request.json()
    except Exception:
        return _error("Expected a JSON request body", 400)
    if not isinstance(payload, dict):
        return _error("Expected a JSON object", 400)
    message = payload.get("message")
    if not isinstance(message, str) or not message.strip() or len(message) > 20000:
        return _error("message must be 1-20000 characters", 400)
    message_id = payload.get("message_id") or ("hermes-" + os.urandom(16).hex())
    if not isinstance(message_id, str) or not _REQUEST_ID_RE.fullmatch(message_id):
        return _error("message_id must contain 1-128 safe characters", 400)
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT OR IGNORE INTO outbox(message_id,message,created_at) VALUES(?,?,?)",
            (message_id, message, time.time()),
        )
        row = conn.execute("SELECT message FROM outbox WHERE message_id=?", (message_id,)).fetchone()
    if row is None or row["message"] != message:
        return _error("message_id already exists with different content", 409)
    return JSONResponse({"ok": True, "message_id": message_id, "status": "stored"},
                        status_code=201, headers={"Cache-Control": "no-store"})


async def get_outbox(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _error("Unauthorized", 401)
    try:
        limit = max(1, min(100, int(request.query_params.get("limit", "50"))))
    except ValueError:
        return _error("limit must be an integer", 400)
    with closing(_connect()) as conn:
        rows = conn.execute(
            "SELECT message_id,message,created_at FROM outbox WHERE acked_at IS NULL "
            "ORDER BY created_at LIMIT ?", (limit,)
        ).fetchall()
    return JSONResponse({"ok": True, "messages": [dict(row) for row in rows]},
                        headers={"Cache-Control": "no-store"})


async def ack_outbox(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _error("Unauthorized", 401)
    message_id = request.path_params.get("message_id", "")
    if not isinstance(message_id, str) or not _REQUEST_ID_RE.fullmatch(message_id):
        return _error("Invalid message_id", 400)
    with closing(_connect()) as conn, conn:
        cur = conn.execute(
            "UPDATE outbox SET acked_at=? WHERE message_id=? AND acked_at IS NULL",
            (time.time(), message_id),
        )
    return JSONResponse({"ok": True, "acked": cur.rowcount == 1, "message_id": message_id},
                        headers={"Cache-Control": "no-store"})


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
