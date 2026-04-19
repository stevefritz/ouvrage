# Ouvrage Rename Cutover

## Overview

This document is for the deployment operator executing the production cutover from the old `switchboard`/`Foreman` naming to the canonical `Ouvrage` name. The Python package has been renamed from `switchboard` to `ouvrage`, all `SWITCHBOARD_*` environment variables have been renamed to `OUVRAGE_*`, the systemd unit has been renamed to `ouvrage.service`, the default deployment path moves from `/opt/switchboard` to `/opt/ouvrage`, the dashboard HTML entry point is now `index.html` (was `foreman.html`), and the session cookie name has changed from `switchboard_session` to `ouvrage_session`. The database schema, stored credential values, and encryption key material are **unchanged** — only names changed.

---

## Environment variable renames

| Old name | New name | Notes |
|---|---|---|
| `SWITCHBOARD_MASTER_KEY` | `OUVRAGE_MASTER_KEY` | **CRITICAL**: Fernet key. Keep the VALUE identical — existing encrypted credentials (GitHub PATs, Anthropic API keys) become unreadable if the value changes |
| `SWITCHBOARD_DB` | `OUVRAGE_DB` | Path to SQLite database. Point at the existing file — no need to rename the file itself |
| `SWITCHBOARD_PORT` | `OUVRAGE_PORT` | Server port (default 8100). Keep value if you customized it |
| `SWITCHBOARD_OWNER_EMAIL` | `OUVRAGE_OWNER_EMAIL` | First-boot owner bootstrap only — remove after first boot |
| `SWITCHBOARD_OWNER_NAME` | `OUVRAGE_OWNER_NAME` | First-boot owner bootstrap only — remove after first boot |
| `SWITCHBOARD_OWNER_PASSWORD_HASH` | `OUVRAGE_OWNER_PASSWORD_HASH` | First-boot owner bootstrap only — remove after first boot |
| `SWITCHBOARD_INSTANCE_SLUG` | `OUVRAGE_INSTANCE_SLUG` | Tenant slug used as JWT audience in SaaS mode |
| `SWITCHBOARD_INSTANCE_NAME` | `OUVRAGE_INSTANCE_NAME` | Display name shown in the dashboard |

**Bare-metal deployment:** Edit `/etc/ouvrage/env` (was `/etc/switchboard/env`) with the renamed variables.

**Docker deployment:** Update your `environment:` block in docker-compose to use the new names.

---

## Filesystem path renames

| Old path | New path | Action required |
|---|---|---|
| `/opt/switchboard/` | `/opt/ouvrage/` | Rename directory: `mv /opt/switchboard /opt/ouvrage` |
| `/opt/switchboard/data/` | `/opt/ouvrage/data/` | Included in the directory rename above |
| `/opt/switchboard/logs/` | `/opt/ouvrage/logs/` | Included in the directory rename above |
| `/etc/switchboard/env` | `/etc/ouvrage/env` | Create `/etc/ouvrage/` dir, move env file: `mkdir -p /etc/ouvrage && mv /etc/switchboard/env /etc/ouvrage/env` — then update variable names inside the file |
| `/etc/systemd/system/switchboard.service` | `/etc/systemd/system/ouvrage.service` | See systemd section below |

**Note:** The SQLite database file itself does not need to be renamed. Just point `OUVRAGE_DB` at wherever it currently lives (e.g. `OUVRAGE_DB=/opt/ouvrage/data/switchboard.db` is fine — only the env var name changed).

---

## Systemd unit changes

The unit file has been renamed from `switchboard.service` to `ouvrage.service`. Execute these steps exactly:

```bash
# 1. Stop and disable the old unit
sudo systemctl stop switchboard
sudo systemctl disable switchboard

# 2. Remove the old unit file
sudo rm /etc/systemd/system/switchboard.service

# 3. Install the new unit file (from the deployed code)
sudo cp /opt/ouvrage/ouvrage.service /etc/systemd/system/ouvrage.service

# 4. Reload systemd
sudo systemctl daemon-reload

# 5. Enable and start the new unit
sudo systemctl enable ouvrage
sudo systemctl start ouvrage

# 6. Verify it came up
sudo systemctl status ouvrage
sudo journalctl -u ouvrage -n 50 --no-pager
```

The new `ouvrage.service` unit references `EnvironmentFile=/etc/ouvrage/env`. Ensure that file exists and contains the renamed `OUVRAGE_*` variables before starting.

---

## Docker image changes

| Old | New |
|---|---|
| `foreman:latest` | `ouvrage:latest` |
| `foreman:eyes` (with Playwright) | `ouvrage:eyes` |

Build commands (from repo root):
```bash
docker build -t ouvrage:latest .
docker build --build-arg WITH_PLAYWRIGHT=true -t ouvrage:eyes .
```

The `docker-compose.example.yml` has been updated: service name is now `ouvrage`, volumes are `ouvrage-data` and `ouvrage-work`. If you have an existing compose deployment, update your compose file accordingly.

---

## Database & credentials

**The SQLite database file does not need to move.** Point `OUVRAGE_DB` at the existing path.

**Fernet key continuity:** The master encryption key is the same VALUE — only the environment variable NAME changed from `SWITCHBOARD_MASTER_KEY` to `OUVRAGE_MASTER_KEY`. Copy the exact key value from your old env file to the new `OUVRAGE_MASTER_KEY` variable. All stored credentials (GitHub PATs, Anthropic API keys) remain readable.

**Database schema:** Unchanged. No migrations required.

**Session cookie:** The session cookie name changed from `switchboard_session` to `ouvrage_session`. This means **all existing user browser sessions are invalidated** — users will need to log in again after the cutover. This is expected and unavoidable. Inform users before the cutover window.

**RSA OAuth key:** Lives at `OAUTH_RSA_KEY_PATH` (unchanged env var name), typically `/data/oauth_rsa_key.pem`. No changes needed.

---

## In-flight worker drain

CC worker processes running at the time of cutover hold references to the old `switchboard` Python module path. They will continue to work until they complete naturally. To safely cut over without stranding running tasks:

1. **Pause the project** via the dashboard Settings → "Pause project" (or `pause_project` MCP tool). This stops new tasks from being dispatched.
2. **Wait for active tasks to finish.** Check the board — all tasks should reach `completed` or `stopped` status. Tasks in `working` state that are near completion will finish normally; the `ouvrage` rename does not affect in-flight sessions since they're running in isolated worktrees.
3. **If you cannot wait:** Tasks in `validating` or `working` status can be stopped via the dashboard. They will need to be retried after the cutover.
4. Once all tasks are done, proceed with the cutover.
5. **After cutover:** Resume the project from the dashboard.

---

## Cutover sequence (ordered steps)

1. **Announce maintenance.** Notify users that the system will be briefly unavailable and that they'll need to log in again.

2. **Pause project.** In the Ouvrage dashboard → Settings → Pause project. Stop new task dispatches.

3. **Drain workers.** Wait for all tasks in `working` or `validating` states to complete (or stop them manually).

4. **Stop the service.**
   ```bash
   sudo systemctl stop switchboard
   ```

5. **Back up the database and worktree state.**
   ```bash
   cp /opt/switchboard/data/switchboard.db /opt/switchboard/data/switchboard.db.bak.$(date +%Y%m%d_%H%M%S)
   ```

6. **Deploy new code.** Pull/update the codebase to the branch with the Ouvrage rename (this branch: `full-rename-to-ouvrage` or its merged successor on `main`).
   ```bash
   cd /opt/switchboard  # or wherever the code lives
   git pull origin main
   ```

7. **Rename the deployment directory.**
   ```bash
   mv /opt/switchboard /opt/ouvrage
   cd /opt/ouvrage
   ```

8. **Update the env file.** Create `/etc/ouvrage/` and move/update the env file:
   ```bash
   mkdir -p /etc/ouvrage
   cp /etc/switchboard/env /etc/ouvrage/env
   # Edit /etc/ouvrage/env: rename SWITCHBOARD_* to OUVRAGE_* (keep values identical)
   # Example: SWITCHBOARD_MASTER_KEY=xxx → OUVRAGE_MASTER_KEY=xxx
   #           SWITCHBOARD_DB=/opt/ouvrage/data/switchboard.db → OUVRAGE_DB=/opt/ouvrage/data/switchboard.db
   ```

9. **Install the new systemd unit.**
   ```bash
   sudo cp /opt/ouvrage/ouvrage.service /etc/systemd/system/ouvrage.service
   sudo systemctl daemon-reload
   sudo systemctl disable switchboard 2>/dev/null || true
   sudo rm -f /etc/systemd/system/switchboard.service
   sudo systemctl enable ouvrage
   ```

10. **Install new Python package.**
    ```bash
    cd /opt/ouvrage
    pip install -e ".[dev]"
    ```

11. **Start the service.**
    ```bash
    sudo systemctl start ouvrage
    sudo systemctl status ouvrage
    ```

12. **Smoke test.** Verify the system is healthy:
    - `curl http://localhost:8100/health` — should return `Ouvrage OK`
    - Open the dashboard in a browser: `http://localhost:8100/dashboard` — login page should appear
    - Log in (users must log in fresh — session cookie changed)
    - Dispatch one test task to confirm worker dispatch works

13. **Resume project.** In the dashboard → Settings → Resume project.

14. **Clean up old files** (optional, after confirming everything works):
    ```bash
    sudo rm -f /etc/systemd/system/switchboard.service
    rm -rf /etc/switchboard/  # only after verifying /etc/ouvrage/env is correct
    ```

---

## Docker deployment cutover

If running via Docker Compose (not bare metal):

1. Stop the old container: `docker compose -f docker-compose.yml down`
2. Update your `docker-compose.yml`: rename service to `ouvrage`, update image to `ouvrage:latest`, rename volumes to `ouvrage-data`/`ouvrage-work`, rename `SWITCHBOARD_*` env vars to `OUVRAGE_*` (same values)
3. Build new image: `docker build -t ouvrage:latest .`
4. Start: `docker compose up -d`
5. Check: `docker compose logs ouvrage -f`

---

## Rollback

If the cutover fails and you need to revert (within the ~30-minute rollback window):

```bash
# 1. Stop new service
sudo systemctl stop ouvrage

# 2. Restore old directory
mv /opt/ouvrage /opt/switchboard

# 3. Reinstall old unit
sudo cp /opt/switchboard/switchboard.service /etc/systemd/system/switchboard.service
sudo systemctl daemon-reload
sudo systemctl enable switchboard

# 4. Start old service
sudo systemctl start switchboard
sudo systemctl status switchboard
```

After rollback, restore `/etc/switchboard/env` with the original `SWITCHBOARD_*` variable names. The database is unchanged so no restore is needed unless you backed up for another reason.

**Keep rollback window short.** After the new service has been running and writing data (new session cookies, new task records), rollback becomes more complex. Act within 30 minutes if something is wrong.

---

## What did NOT change

| Concern | Status |
|---|---|
| Database schema | **Unchanged.** Same tables, columns, indexes. No migration needed. |
| Fernet-encrypted credential values | **Unchanged.** Only the env var name changed, not the key material. |
| Git repository paths inside worktrees | **Unchanged.** `/work/<project>/<worktree>/` paths are unaffected. |
| External URLs / DNS | **Unchanged.** The server still listens on the same port and responds to the same domain. |
| OAuth RSA key | **Unchanged.** `OAUTH_RSA_KEY_PATH` env var name is the same. |
| Worker OS user | **Unchanged.** The `switchboard` OS user (UID 999) and `switchboard-svc` service user remain as-is. The `WORKER_USER` env var value stays `switchboard`. |
| MCP tool names for existing Claude.ai connections | **Changed.** MCP server advertises as `ouvrage` now. Claude.ai clients that auto-discover tools will get `mcp__ouvrage__*` tool names instead of `mcp__switchboard__*`. Re-authorize in Claude.ai settings if needed. |
| `/dashboard` URL routes | **Unchanged.** Dashboard still served at `/dashboard`. |
| `/foreman` URL routes | **Backward-compat redirect.** `/foreman/*` still redirects to `/dashboard/*`. Old bookmarks continue to work. |
