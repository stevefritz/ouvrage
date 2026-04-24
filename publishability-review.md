# Publishability Review — mcp-switchboard (Ouvrage)

**Date:** 2026-04-24
**Scope:** Post-cleanup publishability assessment — code quality, documentation, setup effort
**Branch reviewed:** `main` at `76987b6`

---

## Executive summary

Ouvrage is an MCP server that dispatches autonomous Claude Code sessions to work on git repos in isolated worktrees, with test gates, review gates, dependency chains, and crash recovery. It's a substantial system (~12,400 LOC in the `ouvrage/` package, ~2,700 tests, a Preact SPA dashboard) built by a single author and in daily production use. The architecture is raw ASGI, async throughout, SQLite-backed, and framework-free by design.

**What's genuinely strong.** The repo has clearly been through a serious cleanup pass. The README is one of the better project READMEs in this space — it orients a reader in seconds, the quickstart is concrete and runnable, the architecture section is honest without being defensive. The `.env.example` is well-organized with clear required/optional separation. The MIT license is correct. The Dockerfile and compose files are production-grade with proper privilege separation, Docker secrets support, and auto-generated keys. The code itself is consistently structured: the lifecycle state machine, the MCP tool pipeline, and the git provider abstraction are all clean. The test suite at 2,700+ tests signals seriousness. The TaskStatus enum — flagged in the prior review as confusing — now has a proper docstring explaining the display-state vs DB-state distinction.

**What's genuinely concerning.** There are no blockers that would embarrass the author. The remaining issues are all should-fix or nice-to-have: three silent `except Exception: pass` blocks in `app.py` that swallow embedding failures, two `print()` calls in `app.py` that should be `log.info()`, the `/foreman` redirect still in the router, `LOG_DIR` defaulting to `/opt/ouvrage/logs` (won't exist outside Docker), and the `ouvrage/migrate.py` Authelia migration residue being visible to anyone browsing the source. None of these would form a negative impression on a stranger scanning the repo — they're internal details you'd only notice reading specific files.

**Verdict: publish now.** The repo is in good shape. The items below are worth addressing but none are publication blockers.

---

## 1. Code quality smell

### Real concerns

**1.1 Silent exception swallowing in `ouvrage/server/app.py:313-314, 337-338, 362-363`**

Three identical patterns in the vec0 backfill startup code:

```python
except Exception:
    pass
```

These silently drop sqlite-vec INSERT failures during startup backfill. If embedding insertion fails (e.g. corrupt data, schema mismatch), there's zero indication. The adjacent code in `db/connection.py:24-25` has the same pattern for initial sqlite-vec loading, but that one is justified — vec isn't a hard dependency. The startup backfill ones should at minimum `log.debug()`.

**Severity:** Should-fix. A stranger reading the code would notice bare `except: pass` and wonder what's being hidden.

**1.2 `print()` calls in production code — `ouvrage/server/app.py:581, 583`**

```python
print(f"OAuth enabled — self-issued JWTs (issuer: {_get_self_base_url()})")
print(f"OAuth enabled — external issuer: {auth.AUTH_ISSUER_URL}")
```

These are startup info messages that bypass the logging system. Every other startup message uses `log.info()`. These two stand out as "I added these during debugging and forgot to convert them."

**Severity:** Should-fix. Easy fix, and `print()` in production code is a classic code-review flag.

**1.3 Logger variable naming inconsistency**

Most modules use `log = logging.getLogger(...)`. Two exceptions use `logger`:
- `ouvrage/dispatch/lifecycle.py:20`
- `ouvrage/auth/middleware.py:35`

Not a functional issue, but inconsistent enough that a reader would notice the two patterns in the same codebase.

**Severity:** Nice-to-have. Cosmetic.

### Cosmetic cleanup

**1.4 Legacy `/foreman` redirect — `ouvrage/server/app.py:534-536`**

```python
elif path.startswith("/foreman"):
    # Legacy redirect: /foreman* → /dashboard equivalent
    new_path = "/dashboard" + path[len("/foreman"):]
```

The "Foreman" branding was cleaned up across the codebase, but this redirect remains. It's functional (backward compat), but a stranger seeing "foreman" in the router would wonder what it is. The comment explains it, but the redirect serves no purpose for a fresh OSS installation.

**Severity:** Nice-to-have. The comment is clear enough.

**1.5 `scripts/` still references "Foreman" heavily**

`scripts/visual-check.py:50-51`, `scripts/take-conv-screenshots.py:58`, `scripts/take-index-toc-screenshots.py:78` — all contain a `ForemanHandler` class and `foreman.html` references. These are internal dev/testing scripts, not production code, but they'd be visible to anyone browsing the repo.

**Severity:** Nice-to-have. Internal tooling, low visibility.

**1.6 `ouvrage/migrate.py` — Authelia migration residue**

This 148-line module exists solely for a one-time migration from Authelia to built-in auth. It's referenced from `__main__.py` as the `migrate-auth` CLI command. For an OSS user who never ran Authelia, this is dead weight. The module itself is clean — no personal data, no hardcoded secrets — but it's conceptual noise.

**Severity:** Nice-to-have. Could be removed or moved to a `contrib/` directory.

**1.7 `db/__init__.py` re-exports private symbols (lines 19-26)**

```python
from ouvrage.db._helpers import (
    now_iso,
    _strip_embedding,
    _read_messages,
    _list_with_aggregates,
    _make_snippet,
    _determine_attempt_outcome,
    read_messages_around,
)
```

Five `_`-prefixed symbols are re-exported as public API. The comment on line 18 explains: "private but some are re-exported for test access." This works but signals leaky encapsulation.

**Severity:** Nice-to-have. The comment is honest about why.

**1.8 Deprecated tool aliases still registered**

`ouvrage/server/dispatch.py` still maps `get_attached_file` to `_handle_get_file` (deprecated alias). `ouvrage/server/tools.py` still accepts `github_pat_override` on project tools. Both are properly marked deprecated in comments and descriptions. Not confusing, but adds dead surface area.

**Severity:** Nice-to-have. Properly deprecated, could be removed for a clean OSS launch.

---

## 2. Documentation smell

### README.md — Strong

The README is well-structured and does its job:
- **First 30 seconds:** The opening paragraph explains what Ouvrage is (MCP server for dispatching CC agents) clearly and concisely. Three bullet points for interaction modes. Good.
- **Status section:** Honest — "Functional and in daily use. Public under MIT as of 2026-04. Still rough edges..." Links to the internal architecture review. This is exactly the right tone.
- **Quickstart:** Four concrete steps from clone to running. The `docker run --rm` Fernet key generation is clever (no local Python needed). The `OUVRAGE_OWNER_PASSWORD` env var approach is explained with the pre-hashed alternative.
- **Claims accuracy:** "2,700+ pytest tests" — accurate. "Python 3.12+" — matches `pyproject.toml`. "Docker 24+" — reasonable requirement.
- **Undefined acronyms:** "MCP" is used without expansion. For the target audience (developers who know Claude Code), this is probably fine — MCP is well-known in that ecosystem. For a truly cold reader, a one-line "(Model Context Protocol)" parenthetical on first use would help.
- **OAuth credential retrieval (lines 92-101):** The inline Python one-liner for re-printing OAuth credentials is functional but ugly. It's a 7-line Python script crammed into a `docker exec` command. A `python3 -m ouvrage show-credentials` CLI command would be cleaner, but this works.

**One issue:** Line 15 links to `docs/internal/pre-oss-architecture-review.md` — this is an internal self-assessment document. Having it committed and linked from the README is actually a strength (shows self-awareness), but the `docs/internal/` path makes it feel like something that shouldn't be public. Consider whether this doc should be public-facing or moved.

### .env.example — Strong

Well-organized with clear sections: Required, Core paths, First-run bootstrap, Worker isolation, Auth mode, SaaS mode, Optional integrations, Quotas, Dev/testing. Required vs optional is obvious. The `WORKER_USER` default is `switchboard` — this matches the Dockerfile's OS user and is correct.

**One issue:** Line 38 defaults `WORKER_USER` to `switchboard`. The `.env.example` sets it, the Dockerfile sets it, and `settings.py:38` also defaults to `"switchboard"`. This is fine but slightly confusing — the actual default comes from `settings.py`, not from `.env.example`. A comment clarifying "this matches the Dockerfile's OS user" would help a reader understand why this specific value.

### LICENSE — Correct

MIT, 2026, Stephen Fritz. Present, standard, no issues.

### Missing top-level docs

- **CONTRIBUTING.md** — absent. The README has a brief "Contributing" section (lines 273-277) that covers the basics ("Issues and PRs welcome. Run `make test`. Read CLAUDE.md."). For a "link from LinkedIn" level of publication, this is sufficient. A separate CONTRIBUTING.md becomes important when you're actively seeking contributors.
- **SECURITY.md** — absent. For a self-hosted tool, this is notable. If someone finds a vulnerability, where do they report it? A one-paragraph SECURITY.md with a contact email would be appropriate.
- **CHANGELOG.md** — absent. Not needed for initial publication.
- **CODE_OF_CONDUCT.md** — absent. Not needed for initial publication.

**Verdict:** SECURITY.md is the only one that matters at this stage.

### In-code docstrings — Spot-checked, clean

- `ouvrage/models/task.py:11-23` — TaskStatus enum now has a thorough docstring explaining the 6 DB states vs display states distinction. This was flagged in the prior review and has been properly fixed.
- `ouvrage/config/settings.py` — Every env var has a clear comment. Section headers are consistent.
- `ouvrage/dispatch/lifecycle.py` — Module and TRANSITIONS dict are well-documented.
- `ouvrage/crypto.py` — Clean, clear docstring.
- `ouvrage/auth/middleware.py:18, 150, 269` — Three "legacy Authelia compat" comments remain. These are descriptive (explain why the code exists) rather than misleading. Acceptable.

### RUNTIMES.md

Present at repo root. Comprehensive reference for CC worker runtime environments. No personal data, appropriate for public consumption.

### `docs/internal/pre-oss-architecture-review.md`

This file is the prior architecture review. It references file structures and naming (`switchboard/`, `foreman-app.js`) that have since been cleaned up. The document itself is a snapshot of the pre-cleanup state. It's valuable as historical context but may confuse a reader who sees it referencing files that no longer exist. The README links to it — this is fine if the reader understands it's a historical document. The document title says "Pre-Open-Source" which sets the right expectation.

---

## 3. Setup effort

### Clone-to-running walkthrough

Following the README quickstart exactly:

```
1. git clone https://github.com/stevefritz/switchboard.git ouvrage
2. cd ouvrage
3. mkdir -p secrets
4. docker run --rm --entrypoint python3 python:3.13-slim -c \
     "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" \
     > secrets/master_key
5. docker compose -f docker-compose.example.yml build
6. OUVRAGE_OWNER_EMAIL=you@example.com OUVRAGE_OWNER_PASSWORD=change-me \
     docker compose -f docker-compose.example.yml up -d
7. open http://localhost:8100/dashboard/login
```

**Total steps:** 7 (including clone and open browser).
**Total friction points:** 3.

### Friction-point inventory

| Friction point | Avoidable? | Notes |
|---|---|---|
| Generate Fernet master key | Partially | The `docker run` one-liner works but is surprising. The entrypoint already auto-generates an ephemeral key if none is provided (lines 53-58 of `docker-entrypoint.sh`). For a "just try it" experience, you could skip this step entirely and get a warning. The README could mention this as the "fast path" with a caveat about persistence. |
| Set owner email/password | No | Unavoidable — you need credentials to log in. The env var approach is standard. |
| Anthropic API key | Not in quickstart | Not required for the dashboard to work, only for dispatching CC sessions. Correctly omitted from quickstart. |

**Assessment:** The setup is genuinely minimal for what the system does. Three real steps after clone (generate key, build, up). The Docker image handles Node.js, Claude Code, OS users, volume permissions, RSA key generation, and DB initialization automatically. This is better than most self-hosted tools in this space.

### The plaintext password env var

`OUVRAGE_OWNER_PASSWORD` is passed as a plaintext env var, hashed server-side with Argon2id at bootstrap. The README and compose file both document the pre-hashed alternative (`OUVRAGE_OWNER_PASSWORD_HASH`).

**Is this defensible?** Yes, with caveats:

- **MySQL precedent:** `MYSQL_ROOT_PASSWORD` uses the exact same pattern and has been criticized, but it's the de facto standard for container-bootstrapped databases. PostgreSQL (`POSTGRES_PASSWORD`), MongoDB (`MONGO_INITDB_ROOT_PASSWORD`), and Redis all do the same thing. Users understand and expect this.
- **The risk:** Plaintext passwords in env vars can leak via `docker inspect`, process listing (`/proc/*/environ`), shell history, and CI logs. But this is a first-boot-only variable — the README says "After first boot these can be removed."
- **The mitigation is already in place:** The compose file documents the pre-hashed alternative. The entrypoint (`docker-entrypoint.sh:91-95`) handles both paths. The code in `ouvrage/migrate.py` accepts `--password` or `--password-hash`.
- **What would improve it without adding complexity:** The README could explicitly say: "For production, use the pre-hashed form to avoid plaintext in your environment. For local testing, plaintext is fine." This is already implied but not stated directly.

**Verdict:** The current approach is defensible and follows container ecosystem conventions. The pre-hashed alternative is there for security-conscious users. No changes needed.

### Silent footguns

**3.1 `LOG_DIR` defaults to `/opt/ouvrage/logs` — `ouvrage/config/settings.py:12`**

This path won't exist outside the Docker container. If someone runs `make run` (bare-metal), the logging setup will try to create `/opt/ouvrage/logs` and likely fail silently or error. The Dockerfile doesn't set `LOG_DIR` either — the Docker default works because `/opt/ouvrage/logs` doesn't need to exist (the code creates it). But this is a surprise for bare-metal users.

**Risk:** Low. The `make run` path is a dev convenience, and the error would be obvious (permission denied on `/opt/ouvrage/logs`). Setting `LOG_DIR=./logs` would be a better bare-metal default.

**3.2 `WORKER_USER` defaults to `switchboard` — `ouvrage/config/settings.py:38`**

If running bare-metal (not in Docker), the app will try to find an OS user named `switchboard` for worker process isolation. If that user doesn't exist, the code falls back to the current user (this was fixed in the cleanup phase). The fallback is clean — no crash, just runs workers as the current user.

**Risk:** None. The fallback was explicitly added for OSS/dev use.

**3.3 Ephemeral master key warning**

If you skip the Fernet key step entirely, the entrypoint generates an ephemeral key and prints a clear warning:

```
WARNING: No /run/secrets/master_key or OUVRAGE_MASTER_KEY env var found
Generated ephemeral key — credentials will be unrecoverable if container restarts
```

This is good behavior — lets you try the system without ceremony, warns about the consequence. No footgun here.

**3.4 `docker-compose.example.yml` owner bootstrap is commented out**

The owner email/password section in `docker-compose.example.yml:49-59` is entirely commented out. The README's quickstart passes these as inline env vars on the `docker compose up` command. This means: if someone copies the compose file and does `docker compose up` without reading the README, they get a running system with no owner user and can't log in.

**Risk:** Moderate UX friction. The system works (it serves the login page), but there's no user to log in as. The error would be obvious ("invalid credentials") but the fix requires going back to the README. Consider uncommenting the defaults with placeholder values so first-time users see them.

**3.5 SaaS mode config visible in compose file**

The `docker-compose.example.yml` contains commented-out SaaS mode configuration (control plane URL, JWKS, instance slug). For an OSS user, this is noise that adds cognitive load. It's properly commented and clearly labeled, but a dedicated `docker-compose.saas.yml` example would be cleaner.

**Risk:** None. Just visual noise.

---

## Ranked action list

| # | Item | Axis | Classification | Effort | Rationale |
|---|---|---|---|---|---|
| 1 | Add `log.debug()` to silent `except Exception: pass` in `app.py:313-314, 337-338, 362-363` | Code quality | Should-fix | S | Bare `except: pass` is a code-review red flag. Three instances in the same file. |
| 2 | Replace `print()` with `log.info()` in `app.py:581, 583` | Code quality | Should-fix | S | `print()` in production code stands out as unfinished. Two-line fix. |
| 3 | Add `SECURITY.md` with vulnerability reporting contact | Docs | Should-fix | S | Standard for any public repo. One paragraph + email. |
| 4 | Expand "MCP" on first use in README | Docs | Should-fix | S | "(Model Context Protocol)" parenthetical. One word change. |
| 5 | Uncomment owner bootstrap defaults in `docker-compose.example.yml` | Setup | Should-fix | S | Prevents "can't log in" confusion for users who don't read the full quickstart. |
| 6 | Note the bare-metal `LOG_DIR` default in `.env.example` or quickstart | Setup | Should-fix | S | `/opt/ouvrage/logs` won't exist on a dev machine. Add a comment or change default. |
| 7 | Remove `/foreman` redirect from `app.py:534-536` | Code quality | Nice-to-have | S | Dead code for OSS users. The comment explains it but it's unnecessary for a fresh installation. |
| 8 | Clean up `ForemanHandler` in `scripts/visual-check.py`, `scripts/take-conv-screenshots.py`, `scripts/take-index-toc-screenshots.py` | Code quality | Nice-to-have | S | Rename class and references to match current "Ouvrage" branding. |
| 9 | Standardize logger variable naming (`log` vs `logger`) | Code quality | Nice-to-have | S | Pick one, apply globally. Most files use `log`. |
| 10 | Remove or relocate `ouvrage/migrate.py` Authelia migration | Code quality | Nice-to-have | S | Dead weight for OSS users. Could move to `contrib/` or delete. |
| 11 | Remove deprecated `get_attached_file` alias and `github_pat_override` field | Code quality | Nice-to-have | S | Clean OSS launch surface. Both are properly deprecated but add noise. |
| 12 | Stop re-exporting `_`-prefixed symbols from `db/__init__.py` | Code quality | Nice-to-have | S | Tests should import from the private module directly. |
| 13 | Add `python3 -m ouvrage show-credentials` CLI command | Docs | Nice-to-have | M | Replace the ugly inline Python in README's OAuth credential retrieval section. |
| 14 | Move SaaS config out of `docker-compose.example.yml` into a separate file | Setup | Nice-to-have | S | Reduces cognitive load for OSS users who don't need SaaS mode. |
