#!/usr/bin/env bash
set -euo pipefail

# Ouvrage setup — run once before `docker compose up -d`
# Safe to re-run: skips work already done.
# Nuclear option: ./setup.sh --reset

# ── Helper functions ──────────────────────────────────────────────────────────

prompt_owner_creds() {
  while true; do
    read -r -p "  Owner email: " owner_email
    if [[ "$owner_email" == *@* ]]; then
      break
    fi
    echo "  Must be a valid email address (must contain @)."
  done

  while true; do
    read -r -s -p "  Owner password: " owner_password
    echo
    read -r -s -p "  Confirm password: " owner_password2
    echo
    if [[ "$owner_password" == "$owner_password2" ]]; then
      break
    fi
    echo "  Passwords do not match. Try again."
  done

  if ! printf 'OUVRAGE_OWNER_EMAIL=%s\nOUVRAGE_OWNER_PASSWORD=%s\n' \
      "$owner_email" "$owner_password" > .env; then
    echo "✗ Could not write .env — check permissions in this directory."
    exit 1
  fi
  chmod 600 .env
  echo "✓ Owner credentials saved to .env"
}

prompt_openai_key() {
  echo "  OpenAI API key (optional — enables vector search; leave blank to skip)..."
  read -r -s -p "  OpenAI API key: " openai_key
  echo

  if [[ -n "$openai_key" ]]; then
    if ! printf '%s\n' "$openai_key" > ./secrets/openai_key; then
      echo "✗ Could not write secrets/openai_key — check permissions."
      exit 1
    fi
    chmod 600 ./secrets/openai_key
    echo "✓ OpenAI key saved"
  else
    touch ./secrets/openai_key
    echo "  Skipped. Conversation search will use full-text search only."
  fi
}

prompt_public_url() {
  echo "  Public URL (optional — required only for Claude.ai or any client"
  echo "  not on this machine; leave blank for local-only Claude Code)."
  echo "  Examples: https://you.ngrok.app, https://ouvrage.example.com"
  read -r -p "  OUVRAGE_PUBLIC_URL: " public_url

  if [[ -n "$public_url" ]]; then
    public_url="${public_url%/}"
    printf 'OUVRAGE_PUBLIC_URL=%s\n' "$public_url" >> .env
    chmod 600 .env
    echo "✓ Public URL saved to .env"
  else
    echo "  Skipped. Ouvrage will run localhost-only."
  fi
}

# ── --reset flag ──────────────────────────────────────────────────────────────

if [[ "${1:-}" == "--reset" ]]; then
  echo "This will delete: data/, work/, claude-auth/, secrets/, .env, gitconfig"
  read -r -p "Type 'reset' to confirm, anything else to abort: " confirm
  if [[ "$confirm" != "reset" ]]; then
    echo "Aborted. Nothing was changed."
    exit 0
  fi
  echo "→ Stopping container..."
  docker compose down 2>&1 | grep -vE "^$" || true
  echo "→ Wiping installation..."
  # State dirs contain files owned by container uids (ouvrage-svc=996,
  # ouvrage=1001) that the host user can't delete directly. Spin up a
  # throwaway Alpine container with bind mounts and let its root nuke them.
  if [[ -d data || -d work || -d claude-auth || -d gitconfig ]]; then
    docker run --rm \
      -v "$PWD:/host" \
      alpine:latest sh -c 'rm -rf /host/data /host/work /host/claude-auth /host/gitconfig' 2>/dev/null || true
  fi
  rm -rf secrets .env
  echo "✓ Wiped. Running fresh setup..."
  echo ""
fi

# ── Prerequisites ─────────────────────────────────────────────────────────────

if ! command -v docker &>/dev/null; then
  echo "✗ Docker not found. Install it from https://docs.docker.com/get-docker/ and retry."
  exit 1
fi

if ! docker compose version &>/dev/null; then
  echo "✗ Docker Compose v2 not found. Install Docker Desktop or the compose plugin and retry."
  exit 1
fi

if ! docker info &>/dev/null; then
  echo "✗ Docker daemon is not running. Start Docker and retry."
  exit 1
fi

echo "✓ Docker is ready"

# ── State directories ─────────────────────────────────────────────────────────

mkdir -p data work claude-auth secrets
for d in data work claude-auth secrets; do
  if [[ ! -f "$d/.gitignore" ]]; then
    printf '*\n!.gitignore\n' > "$d/.gitignore"
  fi
done
echo "✓ Directories ready"

# ── Git config ────────────────────────────────────────────────────────────────

echo "→ Checking git config for workers..."
write_placeholder_gitconfig() {
  cat > ./gitconfig <<'GITEOF'
[user]
	name = Your Name
	email = you@example.com
GITEOF
}

# Repair: Docker auto-creates a directory at the bind-mount target when the
# host file doesn't exist. If that happened on a previous run, the dir is
# root-owned and the host user can't delete it — nuke it via a throwaway
# container that has root inside.
if [[ -d ./gitconfig ]]; then
  echo "  Repairing ./gitconfig (Docker auto-created it as a directory)..."
  docker compose down >/dev/null 2>&1 || true
  docker run --rm -v "$PWD:/host" alpine:latest sh -c 'rm -rf /host/gitconfig' >/dev/null 2>&1 || true
fi

if [[ -f ./gitconfig ]]; then
  echo "✓ ./gitconfig already exists — skipping"
elif [[ -f "$HOME/.gitconfig" ]]; then
  read -r -p "  Copy $HOME/.gitconfig into ./gitconfig? [y/N] " yn
  if [[ "$(printf '%s' "$yn" | tr '[:upper:]' '[:lower:]')" == "y" ]]; then
    cp "$HOME/.gitconfig" ./gitconfig
    echo "✓ Copied ~/.gitconfig"
  else
    write_placeholder_gitconfig
    echo "  Wrote placeholder ./gitconfig — edit it before dispatching tasks."
  fi
else
  write_placeholder_gitconfig
  echo "  Created ./gitconfig with placeholder values — edit it before dispatching tasks."
fi

# ── Build image ───────────────────────────────────────────────────────────────

# Compose requires .env to exist (it's listed in env_file). Real contents are
# written later in prompt_owner_creds; an empty file is enough to unblock build.
touch .env
chmod 600 .env

echo "→ Building the Ouvrage image (this takes a few minutes the first time)..."
if ! docker compose build; then
  echo "✗ Build failed. Run 'docker compose build' to see the full error."
  exit 1
fi
echo "✓ Image built"

# ── Master key ────────────────────────────────────────────────────────────────

echo "→ Checking master encryption key..."
if [[ ! -s ./secrets/master_key ]]; then
  echo "  Generating master key..."
  if ! docker compose run --rm --entrypoint "" ouvrage python3 -m ouvrage generate-key > ./secrets/master_key; then
    echo "✗ Key generation failed."
    echo "  Run manually: docker compose run --rm ouvrage python3 -m ouvrage generate-key > secrets/master_key"
    exit 1
  fi
  chmod 600 ./secrets/master_key
  echo "✓ Master key generated"
else
  echo "✓ Master key already exists — skipping"
fi

# ── Owner credentials ─────────────────────────────────────────────────────────

echo "→ Checking owner account..."
if [[ -f data/ouvrage.db ]]; then
  echo "  Existing installation detected (data/ouvrage.db found)."
  echo "  Skipping owner setup. To change your password, log in and use the dashboard."
  echo "  To reset completely, run: ./setup.sh --reset"
elif [[ -f .env ]]; then
  # .env exists but DB doesn't — first boot hasn't happened yet, re-prompt
  echo "  .env found but no database yet — re-entering owner credentials."
  prompt_owner_creds
else
  prompt_owner_creds
fi

# ── Public URL (optional) ─────────────────────────────────────────────────────

echo "→ Checking public URL configuration..."
if grep -q '^OUVRAGE_PUBLIC_URL=' .env 2>/dev/null; then
  current=$(grep '^OUVRAGE_PUBLIC_URL=' .env | head -1 | cut -d= -f2-)
  read -r -p "  Existing public URL: $current — replace? [y/N] " yn
  if [[ "$(printf '%s' "$yn" | tr '[:upper:]' '[:lower:]')" == "y" ]]; then
    # Portable in-place delete: GNU and BSD sed disagree on `sed -i`.
    # Use grep -v + atomic rename instead.
    grep -v '^OUVRAGE_PUBLIC_URL=' .env > .env.tmp && mv .env.tmp .env
    chmod 600 .env
    prompt_public_url
  else
    echo "✓ Keeping existing public URL"
  fi
else
  prompt_public_url
fi

# ── OpenAI API key (optional) ─────────────────────────────────────────────────

echo "→ Checking OpenAI API key..."
if [[ -s ./secrets/openai_key ]]; then
  read -r -p "  Existing OpenAI key found. Replace it? [y/N] " yn
  if [[ "$(printf '%s' "$yn" | tr '[:upper:]' '[:lower:]')" == "y" ]]; then
    prompt_openai_key
  else
    echo "✓ Keeping existing OpenAI key"
  fi
else
  prompt_openai_key
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
echo "Setup complete. Start Ouvrage:"
echo ""
echo "  docker compose up -d"
echo "  open http://localhost:8100"
