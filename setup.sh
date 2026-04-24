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
  read -r -p "  OpenAI API key: " openai_key

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

# ── --reset flag ──────────────────────────────────────────────────────────────

if [[ "${1:-}" == "--reset" ]]; then
  echo "This will delete: data/, work/, claude-auth/, secrets/, .env, gitconfig"
  read -r -p "Type 'reset' to confirm, anything else to abort: " confirm
  if [[ "$confirm" != "reset" ]]; then
    echo "Aborted. Nothing was changed."
    exit 0
  fi
  echo "→ Wiping installation..."
  rm -rf data/ work/ claude-auth/ secrets/ .env gitconfig
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
echo "✓ Directories ready"

# ── Git config ────────────────────────────────────────────────────────────────

echo "→ Checking git config for workers..."
if [[ -f ./gitconfig ]]; then
  echo "✓ ./gitconfig already exists — skipping"
elif [[ -f "$HOME/.gitconfig" ]]; then
  read -r -p "  Copy $HOME/.gitconfig into ./gitconfig? [y/N] " yn
  if [[ "${yn,,}" == "y" ]]; then
    cp "$HOME/.gitconfig" ./gitconfig
    echo "✓ Copied ~/.gitconfig"
  else
    echo "  Skipped. Workers will use a placeholder identity."
  fi
else
  cat > ./gitconfig <<'GITEOF'
[user]
	name = Your Name
	email = you@example.com
GITEOF
  echo "  Created ./gitconfig with placeholder values — edit it before dispatching tasks."
fi

# ── Build image ───────────────────────────────────────────────────────────────

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
  if ! docker compose run --rm ouvrage python3 -m ouvrage generate-key > ./secrets/master_key; then
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

# ── OpenAI API key (optional) ─────────────────────────────────────────────────

echo "→ Checking OpenAI API key..."
if [[ -s ./secrets/openai_key ]]; then
  read -r -p "  Existing OpenAI key found. Replace it? [y/N] " yn
  if [[ "${yn,,}" == "y" ]]; then
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
