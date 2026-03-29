# Foreman (Switchboard) — production container image
# One image, many tenant containers. Each gets its own /data and /work volumes.
#
# Build:
#   docker build -t foreman:latest .                                    # base image (~300MB)
#   docker build --build-arg WITH_PLAYWRIGHT=true -t foreman:eyes .     # with Playwright+Chromium (~700MB)
#
# Run:    docker compose up -d
#
# Requires: CAP_SETUID, CAP_SETGID, CAP_KILL (for worker process isolation)

ARG WITH_PLAYWRIGHT=false

FROM python:3.13-slim AS base

# System deps: git (worktrees), curl (health checks), Node.js 22 (Claude Code)
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        curl \
        ca-certificates \
        gnupg \
        gosu \
        libcap2-bin \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Claude Code — npm global install
RUN npm install -g @anthropic-ai/claude-code

# --- OS users ---
# Worker user: CC processes run as this user via os.setuid()
RUN groupadd -g 999 switchboard \
    && useradd -u 999 -g switchboard -m -s /bin/bash switchboard

# Service user: runs the app (needs CAP_SETUID to spawn workers)
RUN useradd -r -g switchboard -s /usr/sbin/nologin switchboard-svc

# --- App install ---
WORKDIR /app

COPY pyproject.toml ./
COPY switchboard/ switchboard/
COPY dashboard/ dashboard/
COPY foreman.html ./

RUN pip install --no-cache-dir .

# --- Playwright (optional, for visual verification) ---
# Higher-tier plans get Chromium so CC workers can screenshot pages and verify UI.
# Adds ~400MB to image size. Skipped by default.
ARG WITH_PLAYWRIGHT
RUN if [ "$WITH_PLAYWRIGHT" = "true" ]; then \
        pip install --no-cache-dir playwright \
        && npx playwright install --with-deps chromium \
    ; fi

# --- Volumes ---
# /data — SQLite DB, OAuth RSA key, encryption keys (persistent tenant data)
# /work — Git bare repos and worktrees (task execution workspace)
VOLUME ["/data", "/work"]

RUN mkdir -p /data /work \
    && chown switchboard-svc:switchboard /data \
    && chown switchboard:switchboard /work

# --- Entrypoint ---
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# --- Environment defaults ---
ENV SWITCHBOARD_DB=/data/switchboard.db \
    OAUTH_RSA_KEY_PATH=/data/oauth_rsa_key.pem \
    WORKER_USER=switchboard \
    AUTH_MODE=local \
    PORT=8100

EXPOSE 8100

# Entrypoint runs as root to fix volume ownership, then drops to switchboard-svc
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python3", "-m", "switchboard"]
