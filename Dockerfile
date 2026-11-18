FROM ghcr.io/astral-sh/uv:0.11.6-python3.13-trixie-slim

ARG HERMES_REF=v0.23.1
ARG HERMES_COMMIT=3b421578588243f920886e6fe79a6faa87ea026e
ARG HERMES_WEBUI_REF=v0.51.310
ARG NODE_VERSION=26.12.1
ARG NODE_SHA256=6170b2d474e1c626f43cff3adc118df9fe3b573258a4fc3d850c8dbc6b079bbd
ARG PNPM_VERSION=12.15.0
ARG PNPM_SHA256=f5173f6127cc505cdb36ec17719fe54a8511c6fd8560c7ceb4bf7c73e3b4458b

ENV PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/opt/hermes/.playwright \
    HERMES_HOME=/data \
    PATH="/opt/node-v${NODE_VERSION}-linux-x64/bin:/opt/hermes/.venv/bin:/data/.local/bin:${PATH}" \
    PYTHONPATH="/opt/hermes-railway:/opt/hermes:/opt/hermes-webui"

# The second group is everyday tools the agent's shell and user skills may call. They came
# with the full python base image this used to build on, so keep them for existing setups.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      ffmpeg \
      git \
      gosu \
      openssh-client \
      procps \
      ripgrep \
      tini \
      file \
      gnupg \
      imagemagick \
      unzip \
      wget \
      xz-utils && \
    rm -rf /var/lib/apt/lists/*

# Hermes v0.23.1 pins Node and pnpm in its pm/lock.json. Keep those toolchains
# aligned with the exact upstream lock rather than Debian's older Node package.
RUN set -eux; \
    curl -fsSL --retry 3 -o /tmp/node.tar.xz "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-x64.tar.xz"; \
    printf '%s  %s\n' "${NODE_SHA256}" /tmp/node.tar.xz | sha256sum -c -; \
    tar -xJf /tmp/node.tar.xz -C /opt; \
    rm /tmp/node.tar.xz; \
    node --version; npm --version

RUN set -eux; \
    curl -fsSL --retry 3 -o /tmp/pnpm.tgz "https://registry.npmjs.org/pnpm/-/pnpm-${PNPM_VERSION}.tgz"; \
    printf '%s  %s\n' "${PNPM_SHA256}" /tmp/pnpm.tgz | sha256sum -c -; \
    npm install --global --no-audit --no-fund /tmp/pnpm.tgz; \
    test "$(pnpm --version)" = "${PNPM_VERSION}"; \
    rm /tmp/pnpm.tgz

# Railway's Trial and Free plans reject images over 4 GB. Keep this image well under it:
# the app directories are owned by `hermes` before anything is written into them, and
# everything below is installed as that user. A `chown -R` afterwards would copy every
# file into a new layer (that alone took the image from 4.5 GB to 6.8 GB).
# Owned by `hermes` so `git fetch`/`hermes update` from the Web TUI do not trip
# "detected dubious ownership", and so Hermes can build its web UI and TUI on first use.
RUN useradd --system --uid 10000 --create-home --home-dir /home/hermes --shell /bin/bash hermes && \
    mkdir -p /opt/hermes /opt/hermes-webui /opt/hermes-railway /data && \
    chown hermes:hermes /opt/hermes /opt/hermes-webui /opt/hermes-railway /data

USER hermes
WORKDIR /opt/hermes

RUN git init . && \
    git remote add origin https://github.com/NousResearch/hermes-agent.git && \
    (git fetch --depth 1 origin "${HERMES_REF}" || git fetch --depth 1 origin "refs/tags/${HERMES_REF}:refs/tags/${HERMES_REF}") && \
    git checkout --detach FETCH_HEAD && \
    test "$(git rev-parse HEAD)" = "${HERMES_COMMIT}"

ENV npm_config_install_links=false

# Root dependencies (agent-browser) plus the web dashboard and TUI workspaces, which
# Hermes builds with npm on first use. The apps/* workspaces are skipped: they are the
# Electron desktop app and its installers (about 600 MB), used only by `hermes desktop`.
RUN pnpm install --frozen-lockfile --trust-lockfile --prod=false \
      --filter '{./ui-tui}...' \
      --filter '{./web}...' && \
    pnpm store prune && \
    rm -rf /home/hermes/.cache

# Chromium's system libraries need root; the browser itself goes to
# PLAYWRIGHT_BROWSERS_PATH and is handed to `hermes` in the same layer.
USER root
RUN npx playwright install --with-deps chromium --only-shell && \
    chown -R hermes:hermes /opt/hermes/.playwright && \
    rm -rf /var/lib/apt/lists/* /root/.npm /root/.cache
USER hermes

RUN uv venv && \
    uv pip install --no-cache-dir -e ".[all,messaging]"

# Install WhatsApp bridge dependencies before the one-shot pairing process runs.
RUN cd /opt/hermes/scripts/whatsapp-bridge && \
    npm install --silent && \
    printf "%s" "85756e3e1c0418df" > node_modules/.hermes-pkg-hash

# Hermes WebUI: pure Python (stdlib + pyyaml) + vanilla JS, served from /opt/hermes-webui
WORKDIR /opt/hermes-webui

RUN git init . && \
    git remote add origin https://github.com/nesquena/hermes-webui.git && \
    (git fetch --depth 1 origin "refs/tags/${HERMES_WEBUI_REF}:refs/tags/${HERMES_WEBUI_REF}" || git fetch --depth 1 origin "${HERMES_WEBUI_REF}") && \
    git checkout --detach FETCH_HEAD && \
    uv pip install --python /opt/hermes/.venv/bin/python --no-cache-dir -r requirements.txt

# Wrapper extras: small Starlette app that exposes /tui (in-browser xterm with
# OAuth shortcut buttons for `hermes auth add` device-code flows plus a free-
# form `/bin/bash` pane) and reverse-proxies everything else to hermes-webui on
# loopback (HERMES_WEBUI_HOST / HERMES_WEBUI_PORT; default 127.0.0.1:9120).
# starlette/uvicorn/httpx may already be transitive Hermes
# deps, but install explicitly to pin.
RUN uv pip install --python /opt/hermes/.venv/bin/python --no-cache-dir \
    ptyprocess httpx websockets starlette uvicorn

# The entrypoint starts as root (it fixes volume ownership, then drops to `hermes`).
USER root
WORKDIR /opt/hermes-railway

COPY --chown=hermes:hermes admin ./admin
COPY --chown=hermes:hermes skills ./skills
COPY --chown=hermes:hermes profile ./profile
COPY --chown=hermes:hermes patches ./patches
COPY --chown=hermes:hermes entrypoint.sh ./entrypoint.sh
COPY --chown=hermes:hermes patches/whatsapp-bridge.js /opt/hermes/scripts/whatsapp-bridge/bridge.js

RUN sed -i 's/\r$//' /opt/hermes-railway/entrypoint.sh && \
    chmod +x /opt/hermes-railway/entrypoint.sh && \
    git config --system --add safe.directory /opt/hermes && \
    git config --system --add safe.directory /opt/hermes-webui && \
    gosu hermes python3 /opt/hermes-railway/patches/patch_webui_sse.py /opt/hermes-webui/static/messages.js && \
    gosu hermes node --check /opt/hermes/scripts/whatsapp-bridge/bridge.js && \
    gosu hermes node --check /opt/hermes-webui/static/messages.js

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -f "http://localhost:${PORT:-8080}/health" || exit 1

ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/hermes-railway/entrypoint.sh"]
