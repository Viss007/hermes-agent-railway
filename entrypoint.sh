#!/bin/bash
set -euo pipefail

export HERMES_HOME="${HERMES_HOME:-/data}"
export PORT="${PORT:-8080}"
export PATH="/opt/hermes/.venv/bin:/data/.local/bin:${PATH}"
export PYTHONPATH="/opt/hermes-railway:/opt/hermes-webui:/opt/hermes:${PYTHONPATH:-}"

# Point the hermes user's $HOME at the persistent /data volume. Railway only
# allows one volume per service, so anything that lands outside /data is wiped
# on redeploy. Without this, the agent's chat workspace defaults to
# $HOME/workspace = /home/hermes/workspace, and subprocess tools (git, ssh,
# gh, npm, Playwright auth state) cache under /home/hermes — both ephemeral.
export HOME="${HERMES_HOME}/home"

ADMIN_PASSWORD_FILE="${HERMES_HOME}/admin.password"

mkdir -p \
  "${HERMES_HOME}/.hermes" \
  "${HERMES_HOME}/.hermes/webui" \
  "${HERMES_HOME}/cron" \
  "${HERMES_HOME}/home" \
  "${HERMES_HOME}/home/workspace" \
  "${HERMES_HOME}/workspace" \
  "${HERMES_HOME}/hooks" \
  "${HERMES_HOME}/logs" \
  "${HERMES_HOME}/memories" \
  "${HERMES_HOME}/plans" \
  "${HERMES_HOME}/sessions" \
  "${HERMES_HOME}/skills" \
  "${HERMES_HOME}/skins"

if [ "$(id -u)" = "0" ]; then
  # Update /etc/passwd so anything that reads home from passwd (gosu, getent,
  # Python's pwd module) agrees with $HOME. Harmless if already set.
  usermod -d "${HOME}" hermes 2>/dev/null || true
  chown -R hermes:hermes "${HERMES_HOME}" 2>/dev/null || true
fi

# Do NOT pre-seed .env or config.yaml — hermes-webui's first-run wizard creates
# them when the user picks a provider and enters an API key. Pre-seeding from
# /opt/hermes/cli-config.yaml.example sets model.default and makes the WebUI
# think provider_configured=true, which skips the wizard entirely.

# One-shot rescue for users upgrading from earlier image versions that pre-seeded
# config.yaml: that pattern caused hermes-webui to persist onboarding_completed=true
# in /data/.hermes/webui/settings.json. Without resetting it, the wizard stays
# hidden even after we drop the seeding. Skip the reset when the user has a
# legitimate setup — either an .env (API-key providers) OR an OAuth credential
# in auth.json paired with model.provider in config.yaml (Codex/Nous flows).
WEBUI_SETTINGS="${HERMES_HOME}/.hermes/webui/settings.json"
if [ -f "${WEBUI_SETTINGS}" ] && [ ! -f "${HERMES_HOME}/.env" ]; then
  python3 - <<PY || true
import json, pathlib
import yaml

p = pathlib.Path("${WEBUI_SETTINGS}")
try:
    s = json.loads(p.read_text())
except Exception:
    raise SystemExit(0)
if not s.get("onboarding_completed"):
    raise SystemExit(0)

# OAuth-configured? Skip the reset.
auth_p = pathlib.Path("${HERMES_HOME}/auth.json")
cfg_p = pathlib.Path("${HERMES_HOME}/config.yaml")
oauth_ok = False
try:
    if auth_p.exists() and cfg_p.exists():
        auth = json.loads(auth_p.read_text()) or {}
        providers = (auth.get("providers") or {}) if isinstance(auth.get("providers"), dict) else {}
        cfg = yaml.safe_load(cfg_p.read_text()) or {}
        mc = cfg.get("model") or {}
        prov = (mc.get("provider") or "").strip()
        if prov and prov in providers:
            oauth_ok = True
except Exception:
    pass

if oauth_ok:
    print("[entrypoint] keeping onboarding_completed=true (OAuth provider configured)")
    raise SystemExit(0)

s["onboarding_completed"] = False
p.write_text(json.dumps(s, indent=2))
print("[entrypoint] reset onboarding_completed (no .env, no OAuth)")
PY
fi

# Seed the Viss operator identity and user profile only on a fresh volume.
# Never overwrite existing identity or memory files on redeploy.
if [ ! -e "${HERMES_HOME}/SOUL.md" ]; then
  if [ -s "/opt/hermes-railway/profile/SOUL.md" ]; then
    cp /opt/hermes-railway/profile/SOUL.md "${HERMES_HOME}/SOUL.md"
    echo "[entrypoint] seeded primary-operator SOUL.md"
  elif [ -f "/opt/hermes/docker/SOUL.md" ]; then
    cp /opt/hermes/docker/SOUL.md "${HERMES_HOME}/SOUL.md"
  fi
fi

USER_MEMORY_FILE="${HERMES_HOME}/memories/USER.md"
if [ ! -e "${USER_MEMORY_FILE}" ] && [ -s "/opt/hermes-railway/profile/memories/USER.md" ]; then
  cp /opt/hermes-railway/profile/memories/USER.md "${USER_MEMORY_FILE}"
  echo "[entrypoint] seeded initial USER.md"
fi

# Self-heal: hermes-webui caches its model catalog at this path for 24 hours.
# If config.yaml changed externally (e.g. user ran `hermes model` after first
# boot), the cache stays stale and /api/models returns active_provider=None
# until the cache TTL expires. Wipe at boot so the catalog rebuilds fresh
# against the current config.yaml + auth.json.
rm -f "${HERMES_HOME}/.hermes/webui/models_cache.json" 2>/dev/null || true

# Self-heal: when auth.json has an OAuth credential but config.yaml lacks
# model.provider (or is missing entirely), the agent fails to determine
# credentials. The Hermes CLI's `hermes model` picker doesn't always set
# model.provider when an OAuth-already-authenticated provider is selected,
# AND `hermes auth add <provider>` doesn't write config.yaml at all.
# This normalizes the state at boot so users don't hit
# "No LLM provider configured" after running either flow.
if [ -f "${HERMES_HOME}/auth.json" ]; then
  python3 - <<PY || true
import json, sys, pathlib
import yaml
auth_p = pathlib.Path("${HERMES_HOME}/auth.json")
cfg_p = pathlib.Path("${HERMES_HOME}/config.yaml")

PROVIDER_BASE = {
    "openai-codex": "https://chatgpt.com/backend-api/codex",
    "nous": "https://inference-api.nousresearch.com/v1",
}
PROVIDER_DEFAULT_MODEL = {
    "openai-codex": "gpt-6-luna",
    "nous": "Hermes-3-Llama-3.1-70B-FP8",
}

try:
    auth = json.loads(auth_p.read_text())
except Exception:
    sys.exit(0)

providers = (auth or {}).get("providers", {})
authed = [p for p in providers.keys() if p in PROVIDER_BASE]
if not authed:
    sys.exit(0)

if cfg_p.exists():
    try:
        cfg = yaml.safe_load(cfg_p.read_text()) or {}
    except Exception:
        cfg = {}
else:
    cfg = {}

model_cfg = cfg.get("model")
if not isinstance(model_cfg, dict):
    model_cfg = {}
if model_cfg.get("provider"):
    sys.exit(0)

chosen = authed[0]
model_cfg["provider"] = chosen
if not model_cfg.get("base_url"):
    model_cfg["base_url"] = PROVIDER_BASE[chosen]
if not model_cfg.get("default") and chosen in PROVIDER_DEFAULT_MODEL:
    model_cfg["default"] = PROVIDER_DEFAULT_MODEL[chosen]
cfg["model"] = model_cfg
yaml.safe_dump(cfg, cfg_p.open("w"), sort_keys=False)
print(f"[entrypoint] wrote config.yaml: model.provider={chosen}, default={model_cfg.get('default')!r}")
PY
fi

# GPT-5.5 retired for ChatGPT-authenticated Codex on 2026-10-14.
# Migrate only this exact retired default for the openai-codex provider.
# Keep a one-time backup of the user's original config before changing it.
if [ -f "${HERMES_HOME}/config.yaml" ]; then
  python3 - <<PY || true
import os, pathlib, shutil, yaml
p = pathlib.Path("${HERMES_HOME}/config.yaml")
try:
    cfg = yaml.safe_load(p.read_text()) or {}
    model = cfg.get("model") or {}
    if model.get("provider") == "openai-codex" and model.get("default") == "gpt-5.5":
        backup = p.with_name("config.yaml.pre-gpt55-retirement.bak")
        if not backup.exists():
            shutil.copy2(p, backup)
            os.chmod(backup, 0o600)
        model["default"] = "gpt-6-luna"
        cfg["model"] = model
        tmp = p.with_name("config.yaml.retirement.tmp")
        tmp.write_text(yaml.safe_dump(cfg, sort_keys=False))
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
        print("[entrypoint] migrated openai-codex default gpt-5.5 -> gpt-6-luna (backup retained)")
except Exception as exc:
    print(f"[entrypoint] Codex model migration skipped: {type(exc).__name__}")
PY
fi




# One-time primary-agent handover. The selected OpenAI-compatible models
# passed isolated terminal-tool acceptance via the configured OpenAI API
# provider. Keep the original config backup and preserve unrelated settings.
PRIMARY_MARKER="${HERMES_HOME}/.hermes/primary-agent-config-v1.json"
if [ -f "${HERMES_HOME}/config.yaml" ] && [ ! -f "${PRIMARY_MARKER}" ]; then
  python3 - <<PY
import json, os, pathlib, shutil, yaml
home = pathlib.Path("${HERMES_HOME}")
config = home / "config.yaml"
backup = home / "config.yaml.pre-primary-agent.bak"
marker = pathlib.Path("${PRIMARY_MARKER}")
cfg = yaml.safe_load(config.read_text()) or {}
if not isinstance(cfg, dict):
    raise SystemExit("config.yaml root is not a mapping")
if not backup.exists():
    shutil.copy2(config, backup)
    os.chmod(backup, 0o600)
model = cfg.setdefault("model", {})
if not isinstance(model, dict):
    raise SystemExit("config.yaml model section is not a mapping")
base_url = os.environ.get("OPENAI_BASE_URL", "").strip()
model["provider"] = "openai-api"
model["default"] = "deepseek-v4.1-flash-uncensored"
if base_url:
    model["base_url"] = base_url
servers = cfg.get("mcp_servers")
disabled = []
if isinstance(servers, dict):
    for name in ("desktop-commander", "cuebank"):
        item = servers.get(name)
        if isinstance(item, dict):
            item["enabled"] = False
            disabled.append(name)
tmp = config.with_name("config.yaml.primary-agent.tmp")
tmp.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True))
os.chmod(tmp, 0o600)
os.replace(tmp, config)
marker.parent.mkdir(parents=True, exist_ok=True)
marker.write_text(json.dumps({
    "version": 1,
    "provider": model["provider"],
    "model": model["default"],
    "disabled_mcp_servers": disabled,
}, indent=2))
os.chmod(marker, 0o600)
os.chmod(config, 0o600)
print("[entrypoint] primary-agent config applied: provider=openai-api, model=deepseek-v4.1-flash-uncensored")
print("[entrypoint] disabled offline/unwanted MCP servers: " + ", ".join(disabled))
PY
fi

# Self-heal: backfill stale `model: <other-provider>/<name>` and missing
# model_provider on existing webui sessions, so users don't hit "No LLM
# provider configured" when reopening sessions created before they configured
# the active provider.
SESSIONS_DIR="${HERMES_HOME}/.hermes/webui/sessions"
if [ -d "${SESSIONS_DIR}" ] && [ -f "${HERMES_HOME}/config.yaml" ]; then
  python3 - <<PY || true
import json, glob, os, pathlib
import yaml
cfg = yaml.safe_load(pathlib.Path("${HERMES_HOME}/config.yaml").read_text()) or {}
model_cfg = cfg.get("model") or {}
default_model = (model_cfg.get("default") or "").strip()
default_provider = (model_cfg.get("provider") or "").strip()
if not (default_model and default_provider):
    raise SystemExit(0)

patched = 0
for p in glob.glob("${SESSIONS_DIR}/*.json"):
    try:
        s = json.loads(open(p).read())
    except Exception:
        continue
    if not isinstance(s, dict):
        continue
    m = s.get("model") or ""
    needs_fix = (
        not s.get("model_provider")
        or ("/" in m and not m.startswith(default_provider + "/"))
        or (default_provider == "openai-codex" and m == "gpt-5.5")
    )
    if needs_fix:
        s["model"] = default_model
        s["model_provider"] = default_provider
        json.dump(s, open(p, "w"))
        patched += 1

idx_p = pathlib.Path("${SESSIONS_DIR}/_index.json")
if idx_p.exists():
    try:
        idx = json.loads(idx_p.read_text())
        idx_patched = 0
        for e in idx:
            if not isinstance(e, dict):
                continue
            m = e.get("model") or ""
            if (not e.get("model_provider")
                or ("/" in m and not m.startswith(default_provider + "/"))
                or (default_provider == "openai-codex" and m == "gpt-5.5")):
                e["model"] = default_model
                e["model_provider"] = default_provider
                idx_patched += 1
        idx_p.write_text(json.dumps(idx))
        print(f"[entrypoint] backfilled {patched} sessions, {idx_patched} index entries")
    except Exception:
        pass
PY
fi

if [ -d "/opt/hermes-railway/skills" ]; then
  for skill_dir in /opt/hermes-railway/skills/*/; do
    [ -d "${skill_dir}" ] || continue
    skill_name="$(basename "${skill_dir}")"
    rm -rf "${HERMES_HOME}/skills/${skill_name}"
    cp -R "${skill_dir}" "${HERMES_HOME}/skills/${skill_name}"
    echo "Synced Railway skill: ${skill_name}"
  done
fi

if [ -z "${ADMIN_PASSWORD:-}" ]; then
  if [ -f "${ADMIN_PASSWORD_FILE}" ]; then
    ADMIN_PASSWORD="$(tr -d '\n' < "${ADMIN_PASSWORD_FILE}")"
  else
    ADMIN_PASSWORD="$(python - <<'PY'
import secrets
print(secrets.token_urlsafe(24))
PY
)"
    printf '%s\n' "${ADMIN_PASSWORD}" > "${ADMIN_PASSWORD_FILE}"
    chmod 600 "${ADMIN_PASSWORD_FILE}" 2>/dev/null || true
  fi
  echo ""
  echo "Hermes WebUI password was generated automatically."
  echo "Password: ${ADMIN_PASSWORD}"
  echo ""
fi
export ADMIN_PASSWORD

# hermes-webui listens on loopback (default 127.0.0.1:9120) so upstream
# `hermes dashboard` (default 9119) can bind when run manually from `/tui`.
# Our Starlette wrapper still serves the public listener on $PORT.
export HERMES_WEBUI_PASSWORD="${ADMIN_PASSWORD}"
export HERMES_WEBUI_HOST="${HERMES_WEBUI_HOST:-127.0.0.1}"
export HERMES_WEBUI_PORT="${HERMES_WEBUI_PORT:-9120}"
# Upstream Hermes CLI: `hermes dashboard` reads these defaults (Docker/docs also use them).
# Keep loopback in Railway builds — exposure is via Uvicorn on $PORT plus /hermes-dashboard proxy.
export HERMES_DASHBOARD_HOST="${HERMES_DASHBOARD_HOST:-127.0.0.1}"
export HERMES_DASHBOARD_PORT="${HERMES_DASHBOARD_PORT:-9119}"
export HERMES_WEBUI_STATE_DIR="${HERMES_HOME}/.hermes/webui"
# Point hermes-webui at our Hermes Agent install so agent features work
export HERMES_WEBUI_AGENT_DIR="/opt/hermes"

# Optional: auto-start the messaging gateway daemon (Telegram/Discord/Slack/email).
# Default off — users typically configure channel tokens via the WebUI Settings
# panel first, then redeploy with START_GATEWAY=true.
if [ "${START_GATEWAY:-false}" = "true" ]; then
  GATEWAY_LOG="${HERMES_HOME}/logs/gateway.log"
  GATEWAY_PID_FILE="${HERMES_HOME}/gateway.pid"
  WHATSAPP_SESSION_DIR="${HERMES_HOME}/platforms/whatsapp/session"
  PAIRING_STATE_DIR="${HERMES_HOME}/platforms/whatsapp/pairing-session"
  PAIRING_COMPLETE_FILE="${HERMES_HOME}/platforms/whatsapp/.phone-pairing-complete"
  if [ -s "${WHATSAPP_SESSION_DIR}/creds.json" ]; then
    CREDS_BYTES=$(wc -c < "${WHATSAPP_SESSION_DIR}/creds.json" | tr -d ' ')
    echo "[whatsapp-session-check] existing_credentials=present bytes=${CREDS_BYTES} pairing=disabled"
  else
    echo "[whatsapp-session-check] existing_credentials=missing pairing=disabled"
  fi
  # This device is already linked as Google Chrome (Ubuntu). Never start a new pairing flow.
  PAIRING_PHONE=""

  start_hermes_gateway() {
    printf '\n--- Starting Hermes gateway %s ---\n' "$(date)" >> "${GATEWAY_LOG}"
    if [ "$(id -u)" = "0" ]; then
      setsid gosu hermes /opt/hermes/.venv/bin/hermes gateway run --replace \
        >> "${GATEWAY_LOG}" 2>&1 < /dev/null &
    else
      setsid /opt/hermes/.venv/bin/hermes gateway run --replace \
        >> "${GATEWAY_LOG}" 2>&1 < /dev/null &
    fi
    GW_PID=$!
    echo "${GW_PID}" > "${GATEWAY_PID_FILE}"
    echo "Gateway PID: ${GW_PID} (logs at ${GATEWAY_LOG})"
  }

  if [ -n "${PAIRING_PHONE}" ] && [ ! -f "${PAIRING_COMPLETE_FILE}" ]; then
    echo "Starting Hermes gateway before WhatsApp pairing; API/UI remain available while WhatsApp is unpaired."
    # The gateway owns the local API server used by the WebUI's authenticated
    # operator channel. Do not hold that server hostage to interactive pairing.
    start_hermes_gateway

    echo "Starting fresh WhatsApp phone-number pairing in isolated session state..."
    mkdir -p "$(dirname "${WHATSAPP_SESSION_DIR}")"
    rm -rf "${PAIRING_STATE_DIR}"
    mkdir -p "${PAIRING_STATE_DIR}"
    if [ "$(id -u)" = "0" ]; then
      chown hermes:hermes "${PAIRING_STATE_DIR}"
    fi
    (
      set +e
      if [ "$(id -u)" = "0" ]; then
        gosu hermes env WHATSAPP_PAIRING_PHONE_NUMBER="${PAIRING_PHONE}" node \
          /opt/hermes/scripts/whatsapp-bridge/bridge.js --pair-only --pair-json \
          --session "${PAIRING_STATE_DIR}" --mode "${WHATSAPP_MODE:-self-chat}"
        PAIR_RC=$?
      else
        WHATSAPP_PAIRING_PHONE_NUMBER="${PAIRING_PHONE}" node \
          /opt/hermes/scripts/whatsapp-bridge/bridge.js --pair-only --pair-json \
          --session "${PAIRING_STATE_DIR}" --mode "${WHATSAPP_MODE:-self-chat}"
        PAIR_RC=$?
      fi
      if [ "${PAIR_RC}" -eq 0 ] && [ -s "${PAIRING_STATE_DIR}/creds.json" ]; then
        if [ -d "${WHATSAPP_SESSION_DIR}" ]; then
          mv "${WHATSAPP_SESSION_DIR}" "${WHATSAPP_SESSION_DIR}.pre-pair-backup-$(date +%s)"
        fi
        mv "${PAIRING_STATE_DIR}" "${WHATSAPP_SESSION_DIR}"
        touch "${PAIRING_COMPLETE_FILE}"
        if [ "$(id -u)" = "0" ]; then
          chown -R hermes:hermes "${WHATSAPP_SESSION_DIR}" "${PAIRING_COMPLETE_FILE}"
        fi
        echo "WhatsApp phone pairing completed; authenticated session installed."
        # Replace the degraded gateway so the WhatsApp adapter loads the new
        # credentials. --replace handles the existing gateway process safely.
        start_hermes_gateway
      else
        echo "WhatsApp phone pairing is not complete (exit ${PAIR_RC}); gateway stays online in degraded mode so the API and UI remain available."
      fi
    ) &
  else
    echo "Starting Hermes messaging gateway in background..."
    start_hermes_gateway
  fi

  # Verify the existing linked device and read-only chat access. No message
  # bodies, chat names, JIDs or phone numbers are emitted to Railway logs.
  if [ "${WHATSAPP_ENABLED:-false}" = "true" ]; then
    nohup python3 - <<'PY' >/proc/1/fd/1 2>&1 &
import json, time, urllib.request, urllib.parse, re
BASE = "http://127.0.0.1:3000"
def get(path, timeout=15):
    req = urllib.request.Request(BASE + path, headers={"Accept": "application/json"}, method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.load(response)
def safe_status(status):
    batch = status.get("lastHistoryBatch") or {}
    return {
        "connection": status.get("connection", "unknown"),
        "credentials": "present" if status.get("credentialsPresent") else "missing",
        "read_receipts": "disabled" if status.get("sendReadReceipts") is False else "UNVERIFIED",
        "outbound_writes": "blocked" if status.get("readOnly") else "UNVERIFIED",
        "disconnect_reason": status.get("lastDisconnectReason"),
        "history_batches": status.get("historyBatchCount", 0),
        "last_batch_chats": batch.get("chats", 0),
        "last_batch_messages": batch.get("messages", 0),
        "cached_chats": status.get("cachedChats", 0),
        "cached_messages": status.get("cachedMessages", 0),
        "bridge_events": status.get("bridgeEventCounts", {}),
    }
last = None
status = None
connect_deadline = time.monotonic() + 180
while time.monotonic() < connect_deadline:
    try:
        status = get("/status")
        snap = safe_status(status)
        if snap != last:
            print("[whatsapp-readonly-status] " + json.dumps(snap, sort_keys=True), flush=True)
            last = snap
        if status.get("connection") == "linked":
            break
    except Exception as exc:
        kind = type(exc).__name__
        if kind != last:
            print("[whatsapp-readonly-status] bridge_probe=" + kind, flush=True)
            last = kind
    time.sleep(3)
if not status or status.get("connection") != "linked":
    try:
        status = get("/status")
        print("[whatsapp-readonly-final] " + json.dumps(safe_status(status), sort_keys=True), flush=True)
    except Exception as exc:
        print("[whatsapp-readonly-final] bridge_unavailable=" + type(exc).__name__, flush=True)
    raise SystemExit(0)
# Allow the already-linked device to finish its initial history sync.
print("[whatsapp-readonly-test] linked_device_confirmed=1", flush=True)
history_deadline = time.monotonic() + 90
while time.monotonic() < history_deadline:
    try:
        status = get("/status")
        if int(status.get("cachedMessages") or 0) > 0:
            break
    except Exception:
        pass
    time.sleep(5)
print("[whatsapp-readonly-test] history_wait_finished=1", flush=True)
chats = get("/chats?limit=120").get("chats", [])
read_counts = {"private": 0, "group": 0}
for kind in ("private", "group"):
    candidate = next((c for c in chats if bool(c.get("isGroup")) == (kind == "group") and (int(c.get("messageCount") or 0) > 0 or c.get("hasHistoryAnchor"))), None)
    if candidate:
        try:
            result = get("/read?" + urllib.parse.urlencode({"chatId": candidate["id"], "limit": "3"}), timeout=45)
            read_counts[kind] = len(result.get("messages") or [])
        except Exception:
            read_counts[kind] = -1
status = get("/status")
final = safe_status(status)
final.update({
    "private_read_messages": read_counts["private"],
    "group_read_messages": read_counts["group"],
    "bridge_events": status.get("bridgeEventCounts", {}),
    "private_chat_candidates": sum(1 for c in chats if not c.get("isGroup") and (int(c.get("messageCount") or 0) > 0 or c.get("hasHistoryAnchor"))),
    "group_chat_candidates": sum(1 for c in chats if c.get("isGroup") and (int(c.get("messageCount") or 0) > 0 or c.get("hasHistoryAnchor"))),
})
print("[whatsapp-readonly-final] " + json.dumps(final, sort_keys=True), flush=True)
# Emit only fixed-format connection and sync counters from the bridge log.
bridge_log = "/data/platforms/whatsapp/bridge.log"
patterns = [
    r"WhatsApp connected!",
    r"Connection closed \(reason: [0-9]+\)\.",
    r"WhatsApp history batch: chats=[0-9]+ messages=[0-9]+ sync=[^ ]+ progress=[^ ]+ latest=(true|false) cached_chats=[0-9]+ cached_messages=[0-9]+",
    r"WhatsApp history status: sync=[^ ]+ status=[^ ]+ explicit=(true|false)",
    r"WhatsApp chat metadata: [0-9]+ chats",
]
try:
    with open(bridge_log, encoding="utf-8", errors="replace") as handle:
        for line in handle.readlines()[-120:]:
            for pattern in patterns:
                match = re.search(pattern, line)
                if match:
                    print("[whatsapp-bridge-log] " + match.group(0), flush=True)
                    break
except Exception as exc:
    print("[whatsapp-bridge-log] unavailable=" + type(exc).__name__, flush=True)
PY
  fi
fi

WEBUI_LOG="${HERMES_HOME}/logs/webui.log"
WEBUI_PID_FILE="${HERMES_HOME}/.hermes/webui/server.pid"
mkdir -p "$(dirname "${WEBUI_PID_FILE}")"

# Respawn watchdog: keeps hermes-webui alive. Lets /tui's OAuth flow kill it
# after a successful auth/model command so the user gets a fresh process (with
# refreshed catalog/session cache) without needing a full container restart.
# Don't use setsid — it detaches the child so `wait $!` can't track it,
# turning the loop into a tight respawn that breaks within a few iterations.
start_webui_watchdog() {
  local AS_USER=$1
  set +e
  while true; do
    printf '\n--- Starting Hermes WebUI on %s:%s %s ---\n' "${HERMES_WEBUI_HOST}" "${HERMES_WEBUI_PORT}" "$(date)" >> "${WEBUI_LOG}"
    if [ -n "${AS_USER}" ]; then
      gosu "${AS_USER}" python3 /opt/hermes-webui/server.py >> "${WEBUI_LOG}" 2>&1 < /dev/null &
    else
      python3 /opt/hermes-webui/server.py >> "${WEBUI_LOG}" 2>&1 < /dev/null &
    fi
    local PID=$!
    echo "${PID}" > "${WEBUI_PID_FILE}"
    echo "Hermes WebUI PID: ${PID}  (logs at ${WEBUI_LOG})"
    wait "${PID}"
    local RC=$?
    echo "[entrypoint] Hermes WebUI (pid ${PID}) exited with code ${RC}; respawning in 2s" >> "${WEBUI_LOG}"
    rm -f "${WEBUI_PID_FILE}"
    # Wipe the catalog cache before respawning so the new process picks up
    # any config.yaml / auth.json changes that triggered the restart.
    rm -f "${HERMES_HOME}/.hermes/webui/models_cache.json" 2>/dev/null || true
    sleep 2
  done
}

if [ "$(id -u)" = "0" ]; then
  chown -R hermes:hermes "${HERMES_HOME}" /opt/hermes-railway 2>/dev/null || true
  start_webui_watchdog hermes &
  exec gosu hermes python3 -m uvicorn admin.app:app --host 0.0.0.0 --port "${PORT}"
fi

start_webui_watchdog "" &
exec python3 -m uvicorn admin.app:app --host 0.0.0.0 --port "${PORT}"
