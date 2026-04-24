# Ouvrage — production container image
# Multi-runtime image with Python, Node, PHP, Go, Ruby, and Rust pre-installed.
#
# Build:
#   docker compose build                                        # standard build
#   docker build --build-arg WITH_PLAYWRIGHT=true -t ouvrage:latest .  # with Playwright+Chromium (~400MB extra)
#
# Run:    docker compose up -d
#
# Layers ordered by change frequency: system libs → runtimes → app code
# Day-to-day deploys only rebuild from COPY onward (~30s).

ARG WITH_PLAYWRIGHT=false

FROM ubuntu:24.04 AS base

ENV DEBIAN_FRONTEND=noninteractive

# ── Layer 1: System libraries ────────────────────────────────────────────────
# Shared deps needed by multiple runtimes. Rarely changes.
RUN apt-get update && apt-get install -y --no-install-recommends \
        # Build tools
        build-essential gcc g++ make cmake pkg-config autoconf automake libtool \
        # SSL / crypto
        libssl-dev libffi-dev \
        # XML / text
        libxml2-dev libxslt-dev libonig-dev \
        # Database clients
        libpq-dev libmysqlclient-dev libsqlite3-dev \
        # Image processing
        libjpeg-dev libpng-dev libwebp-dev libfreetype-dev libgd-dev libmagickwand-dev \
        # Compression
        libzip-dev libbz2-dev zlib1g-dev \
        # Internationalization
        libicu-dev \
        # Network
        libcurl4-openssl-dev \
        # Ruby
        libyaml-dev \
        # Memcached client
        libmemcached-dev \
        # Essential tools
        git curl wget unzip jq software-properties-common \
        gnupg ca-certificates gosu libcap2-bin \
        # ripgrep and fd
        ripgrep fd-find \
    && ln -sf /usr/bin/fdfind /usr/bin/fd \
    && rm -rf /var/lib/apt/lists/*

# ── Layer 2: Python 3.11, 3.12, 3.13 ────────────────────────────────────────
RUN add-apt-repository -y ppa:deadsnakes/ppa \
    && apt-get update && apt-get install -y --no-install-recommends \
        python3.11 python3.11-venv python3.11-dev \
        python3.12 python3.12-venv python3.12-dev \
        python3.13 python3.13-venv python3.13-dev \
        python3-pip \
    && rm -rf /var/lib/apt/lists/* \
    # Don't change default python3 — system tools (add-apt-repository) need the
    # system Python with apt_pkg. Users call python3.11/3.12/3.13 explicitly.
    && python3.13 -m pip install --no-cache-dir --break-system-packages --ignore-installed poetry uv

# ── Layer 3: Node.js 18, 20, 22 via fnm ─────────────────────────────────────
ENV FNM_DIR="/usr/local/fnm"
ENV PATH="$FNM_DIR:$PATH"
RUN curl -fsSL https://fnm.vercel.app/install | bash -s -- --install-dir "$FNM_DIR" --skip-shell \
    && eval "$(fnm env)" \
    && fnm install 22 && fnm install 20 && fnm install 18 \
    && fnm default 22 \
    && fnm exec --using=22 npm install -g yarn pnpm \
    # Make fnm available to all users via profile
    && echo 'eval "$(/usr/local/fnm/fnm env --use-on-cd)"' >> /etc/bash.bashrc \
    && echo 'eval "$(/usr/local/fnm/fnm env --use-on-cd)"' >> /etc/profile.d/fnm.sh

# Ensure node/npm are on PATH for non-interactive shells
RUN eval "$(fnm env)" && ln -sf "$(fnm exec --using=22 which node)" /usr/local/bin/node \
    && ln -sf "$(fnm exec --using=22 which npm)" /usr/local/bin/npm \
    && ln -sf "$(fnm exec --using=22 which npx)" /usr/local/bin/npx \
    && ln -sf "$(fnm exec --using=22 which yarn)" /usr/local/bin/yarn \
    && ln -sf "$(fnm exec --using=22 which pnpm)" /usr/local/bin/pnpm

# ── Layer 4: PHP 8.2, 8.3, 8.4 + extensions ─────────────────────────────────
RUN add-apt-repository -y ppa:ondrej/php \
    && apt-get update \
    # Install CLI + common extensions for each version
    # Extensions bundled in php-common (no separate package needed):
    # pdo, tokenizer, ctype, fileinfo, dom, simplexml, xmlwriter, iconv,
    # calendar, gettext, exif, pcntl, posix, sockets, opcache
    && for V in 8.2 8.3 8.4; do \
        apt-get install -y --no-install-recommends \
            php${V}-cli php${V}-common \
            php${V}-mbstring php${V}-xml php${V}-curl php${V}-zip \
            php${V}-gd php${V}-intl php${V}-bcmath \
            php${V}-mysql php${V}-pgsql php${V}-sqlite3 \
            php${V}-redis php${V}-apcu php${V}-memcached \
            php${V}-imagick php${V}-soap php${V}-xdebug \
        ; done \
    && rm -rf /var/lib/apt/lists/* \
    # Default php → 8.4
    && update-alternatives --set php /usr/bin/php8.4 \
    # Composer
    && curl -fsSL https://getcomposer.org/installer | php -- --install-dir=/usr/local/bin --filename=composer

# ── Layer 5: Go 1.22 ────────────────────────────────────────────────────────
RUN curl -fsSL https://go.dev/dl/go1.22.12.linux-amd64.tar.gz | tar -C /usr/local -xz \
    && ln -sf /usr/local/go/bin/go /usr/local/bin/go \
    && ln -sf /usr/local/go/bin/gofmt /usr/local/bin/gofmt
ENV PATH="/usr/local/go/bin:$PATH"

# ── Layer 6: Ruby 3.3 via rbenv ──────────────────────────────────────────────
ENV RBENV_ROOT="/usr/local/rbenv"
ENV PATH="$RBENV_ROOT/bin:$RBENV_ROOT/shims:$PATH"
RUN git clone --depth 1 https://github.com/rbenv/rbenv.git "$RBENV_ROOT" \
    && git clone --depth 1 https://github.com/rbenv/ruby-build.git "$RBENV_ROOT/plugins/ruby-build" \
    && "$RBENV_ROOT/bin/rbenv" install 3.3.6 \
    && "$RBENV_ROOT/bin/rbenv" global 3.3.6 \
    && eval "$($RBENV_ROOT/bin/rbenv init -)" \
    && gem install bundler \
    && echo 'eval "$(rbenv init -)"' >> /etc/bash.bashrc \
    && echo 'export PATH="/usr/local/rbenv/bin:/usr/local/rbenv/shims:$PATH"' >> /etc/profile.d/rbenv.sh \
    && echo 'eval "$(rbenv init -)"' >> /etc/profile.d/rbenv.sh

# ── Layer 7: OS users ───────────────────────────────────────────────────────
# UID/GID 1001 — avoids conflict with systemd-journal (999) on Ubuntu 24.04.
# The entrypoint chowns /data and /work on every boot, so existing volumes
# with different ownership are fixed automatically.
RUN groupadd -g 1001 switchboard \
    && useradd -u 1001 -g switchboard -m -s /bin/bash switchboard \
    && groupadd -r svc-only \
    && useradd -r -g switchboard -G svc-only -s /usr/sbin/nologin switchboard-svc

# ── Layer 8: Rust (as switchboard user — rustup is user-scoped) ──────────────
USER switchboard
RUN curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y --default-toolchain stable
ENV PATH="/home/switchboard/.cargo/bin:$PATH"
USER root
# Make cargo/rustc available system-wide
RUN ln -sf /home/switchboard/.cargo/bin/cargo /usr/local/bin/cargo \
    && ln -sf /home/switchboard/.cargo/bin/rustc /usr/local/bin/rustc \
    && ln -sf /home/switchboard/.cargo/bin/rustup /usr/local/bin/rustup

# ── Layer 9: Claude Code ────────────────────────────────────────────────────
# The installer writes to ~/.local/bin/claude (NOT ~/.claude/local/bin — that
# was the old path). Symlink target must match the new location, otherwise
# /usr/local/bin/claude points at nothing.
#
# IMPORTANT: do not install the binary inside /home/switchboard/.claude/ —
# that directory is bind-mounted in production (./claude-auth volume) for
# auth state persistence, and the mount would shadow the binary on first run.
# Installing to ~/.local/bin/ keeps it outside the bind mount.
USER switchboard
RUN curl -fsSL https://claude.ai/install.sh | bash
USER root
RUN ln -sf /home/switchboard/.local/bin/claude /usr/local/bin/claude

# ── Layer 10: App code (busts cache on every deploy) ─────────────────────────
WORKDIR /app

COPY pyproject.toml ./
COPY ouvrage/ ouvrage/
COPY dashboard/ dashboard/
COPY index.html ./

RUN apt-get update && apt-get install -y --no-install-recommends tmpreaper \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m pip install --no-cache-dir --break-system-packages --ignore-installed hatchling \
    && python3 -m pip install --no-cache-dir --break-system-packages --ignore-installed ".[dev]" sqlite-vec

# Lock down /app — only root and svc-only group (switchboard-svc) can read.
# Worker user (switchboard) cannot access the codebase or DB schema.
RUN chown -R root:svc-only /app/ \
    && find /app -type d -exec chmod 750 {} + \
    && find /app -type f -exec chmod 640 {} +

# ── Layer 11: Playwright (optional) ──────────────────────────────────────────
ARG WITH_PLAYWRIGHT
RUN if [ "$WITH_PLAYWRIGHT" = "true" ]; then \
        python3 -m pip install --no-cache-dir --break-system-packages playwright \
        && npx playwright install --with-deps chromium \
    ; fi

# ── Volumes ──────────────────────────────────────────────────────────────────
VOLUME ["/data", "/work"]
RUN mkdir -p /data /work \
    && chown switchboard-svc:switchboard /data \
    && chown switchboard:switchboard /work

# ── Entrypoint ───────────────────────────────────────────────────────────────
COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

ENV OUVRAGE_DB=/data/ouvrage.db \
    OAUTH_RSA_KEY_PATH=/data/oauth_rsa_key.pem \
    WORKER_USER=switchboard \
    AUTH_MODE=local \
    TMPDIR=/work/.tmp \
    PORT=8100

EXPOSE 8100

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["python3", "-m", "ouvrage"]
