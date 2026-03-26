# Migrating from Authelia to Self-Issued JWT Auth

This guide covers the cutover from Authelia as the external auth provider to
Switchboard's built-in OAuth 2.1 server with self-issued JWTs.

## Prerequisites

- Switchboard is running with the self-issued JWT code (branch `saas-self-issued-jwt` or later)
- You have your Authelia `password_hash` (argon2id format — copies directly)
- `SWITCHBOARD_MASTER_KEY` is set in your environment

---

## Step 1: Run the migration command

```bash
python -m switchboard migrate-auth \
  --email stephen@stephenfritz.dev \
  --name "Stephen Fritz" \
  --password-hash '$argon2id$v=19$m=65536,t=3,p=4$...' \
  --slug switchboard
```

Or via environment variables:

```bash
export SWITCHBOARD_OWNER_EMAIL=stephen@stephenfritz.dev
export SWITCHBOARD_OWNER_NAME="Stephen Fritz"
export SWITCHBOARD_OWNER_PASSWORD_HASH='$argon2id$v=19$...'
export SWITCHBOARD_INSTANCE_SLUG=switchboard

python -m switchboard migrate-auth
```

The command will print your OAuth credentials:

```
============================================================
  OAUTH CLIENT CREDENTIALS
============================================================
  client_id:     claude-mcp
  client_secret: <your-secret-here>
============================================================
  Save these — you will need them to connect Claude.ai
============================================================
```

**Save the `client_secret`.** You will need it in Step 4.

The migration is idempotent — running it again does nothing if the owner user
already exists.

---

## Step 2: Remove AUTH_ISSUER_URL

Remove the `AUTH_ISSUER_URL` environment variable from your deployment config.
Switchboard will now issue and validate its own JWTs.

```diff
-AUTH_ISSUER_URL=https://auth.yourdomain.com
```

No other env var changes are required. The OAuth server automatically uses
`OAUTH_BASE_URL` (set to your public URL) as the issuer.

---

## Step 3: Simplify the Caddy config

Remove the `forward_auth` Authelia block. Switchboard handles auth entirely.

**Before:**

```caddyfile
switchboard.example.dev {
    forward_auth localhost:9091 {
        uri /api/verify?rd=https://auth.example.dev
        copy_headers Remote-User Remote-Groups Remote-Name Remote-Email
    }
    reverse_proxy localhost:8100
}
```

**After:**

```caddyfile
switchboard.example.dev {
    reverse_proxy localhost:8100
}
```

Reload Caddy:

```bash
caddy reload --config /etc/caddy/Caddyfile
```

---

## Step 4: Reconnect Claude.ai MCP

Your old Claude.ai MCP connection used Authelia's credentials. You need to
re-add the MCP server with the new OAuth credentials from Step 1.

1. Open **Claude.ai → Settings → Integrations → MCP Servers**
2. Remove the existing Switchboard MCP server
3. Add a new MCP server:
   - **URL:** `https://switchboard.example.dev/mcp`
   - **Client ID:** `claude-mcp`
   - **Client Secret:** *(from Step 1)*
4. Authorize when prompted — you will be redirected to Switchboard's login page

---

## Step 5: Verify

1. **Login to dashboard:** visit `https://switchboard.example.dev/foreman`
   - Log in with the email and password you migrated
   - You should see the Switchboard dashboard

2. **Connect Claude.ai:** complete the OAuth flow from Step 4
   - Claude.ai should show the MCP server as connected

3. **Dispatch a test task** from Claude.ai or the dashboard
   - Confirm it runs and completes normally

---

## Auto-migration for new SaaS deployments

For containerised deployments where you want first-boot provisioning without
running the CLI manually, set these env vars before starting the server:

```env
SWITCHBOARD_OWNER_EMAIL=owner@example.com
SWITCHBOARD_OWNER_NAME=Owner
SWITCHBOARD_OWNER_PASSWORD_HASH=$argon2id$v=19$...
SWITCHBOARD_INSTANCE_SLUG=myinstance
SWITCHBOARD_INSTANCE_NAME=My Switchboard
```

On startup, if no owner user exists, Switchboard will automatically create the
owner user and seed the OAuth client. The client credentials will be logged
(not printed to stdout — check your container logs).

---

## Troubleshooting

**Login fails with "Invalid credentials"**
- Double-check the `--password-hash` argument. The argon2id hash must be an exact
  copy from Authelia's `users_database.yml`.
- Hash format: `$argon2id$v=19$m=65536,t=3,p=4$<salt>$<hash>`

**Claude.ai OAuth flow fails**
- Ensure `OAUTH_BASE_URL` is set to your public HTTPS URL (e.g. `https://switchboard.example.dev`)
- The OAuth redirect URIs are pre-configured for `claude.ai` — no additional config needed

**"SWITCHBOARD_MASTER_KEY not set" error**
- The master key is required for encrypting stored credentials and decrypting the
  OAuth client secret. Generate one with:
  ```bash
  python -m switchboard generate-key
  ```
  Then set `SWITCHBOARD_MASTER_KEY=<key>` in your environment.
