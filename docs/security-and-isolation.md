# Ouvrage — Security and Isolation

Isolation primitives, credential storage, and the auth model. The through-line: security requirements shaped the architecture rather than getting bolted on. Worker isolation forced worktree-per-task, which pulled in a git provider abstraction. Credential storage forced Fernet-at-rest plus a resolution chain. Multi-tenant auth forced the two-layer cookie/JWT model with a localhost bypass for workers.

---

## Why this exists

An autonomous worker runs arbitrary code against arbitrary repositories with real credentials. It checks out a codebase, installs dependencies, runs test suites that execute project code. If the worker compromises — a prompt injection that convinces it to exfiltrate secrets, a malicious test that escapes its sandbox — the blast radius has to be bounded.

Ouvrage bounds it with two primitives: one filesystem (git worktree per task, cut from a bare clone) and one process (worker runs as a dedicated OS user via setuid). Everything else in this document — credential resolution, provider abstraction, auth layers, localhost bypass — exists to make those two primitives work without constantly interfering with operator ergonomics.

## Goals / Non-goals

**Goals:**

- A compromised worker can't touch other tasks' branches, other repos, or the service's own secrets.
- Credentials at rest are encrypted. Losing the DB file without the master key doesn't expose any stored token.
- The same credential model works across GitHub, GitLab, and Bitbucket without conditional logic at call sites.
- The dashboard (browser) and MCP endpoint (programmatic) use auth appropriate to each.
- In-container workers reach back to the service without credentials they'd have to carry in a prompt.

**Non-goals:**

- Defense against a compromised host. If the machine running Ouvrage is owned, everything is owned.
- Multi-tenant security boundaries at the application layer. The system is single-operator by design.
- FIPS / compliance certifications. Cryptography is standard library; the combinations chosen are common but uncertified.
- Zero-knowledge architecture. The service decrypts credentials to use them. An operator with root on the host can read them in memory.

## Stack

- **Linux capabilities**: `CAP_SETUID`, `CAP_SETGID`, `CAP_KILL` on the Python binary.
- **Fernet symmetric cipher** (AES-128-CBC + HMAC) via the `cryptography` library, for credentials at rest.
- **Argon2id** for password hashing.
- **RS256 JWTs** via `authlib`, self-issued by a built-in OAuth 2.1 server.
- **Session cookies** with `Secure`, `SameSite`, configurable TTL.
- **Git worktrees** + bare clone as the filesystem isolation primitive.
- **git credential provider ABC** (`GitProvider` base class) with concrete implementations for GitHub, GitLab, Bitbucket.

## 1. Worker process isolation

A worker is a subprocess. It runs as a dedicated OS user — `switchboard` by default — via `os.setuid()` before exec. The service itself runs as `switchboard-svc`, a separate account with no overlapping privileges. The worker can write the worktree (owned by `switchboard`) and its own home (credentials directory, `.claude/`); it cannot write `/app` (where the service code lives) or `/data` (where the DB lives).

The mechanism:

```python
def _resolve_worker_identity() -> tuple[int, int, str] | None:
    try:
        pw = pwd.getpwnam(WORKER_USER)
        return pw.pw_uid, pw.pw_gid, pw.pw_dir
    except KeyError:
        return None

async def _run_as_worker(*cmd, **kwargs):
    identity = _resolve_worker_identity()
    env = kwargs.pop("env", None) or os.environ.copy()

    if identity is None:
        # Fallback: run as current user. Dev, CI, OSS self-host.
        proc = await asyncio.create_subprocess_exec(*cmd, env=env, **kwargs)
    else:
        uid, gid, home = identity
        env["HOME"] = home
        def _demote():
            os.setgid(gid)
            os.setuid(uid)
        proc = await asyncio.create_subprocess_exec(
            *cmd, preexec_fn=_demote, env=env, **kwargs,
        )
    return await proc.communicate() + (proc.returncode,)
```

Two properties:

**`preexec_fn` fires in the child.** `os.setuid` happens after the fork, before the exec, inside the child process. The parent (service) never drops privilege. A fork that fails to demote would be a security bug; the code is short and reviewed specifically because of that.

**Fallback when the user doesn't exist.** On dev laptops, CI runners, and OSS self-hosters who haven't set up a dedicated account, `pwd.getpwnam` raises `KeyError`. The fallback path runs the subprocess as the current process user. This preserves the production isolation contract (container has the user, isolation happens) without forcing developers to create OS users to run tests.

The worker user's filesystem access is narrowed by Unix permissions, not just by setuid. The Docker image chowns `/app` (the service code and the DB schema module) to `root:svc-only` with mode `750/640`. The worker user (`switchboard`) is in the `switchboard` group, not the `svc-only` group — it cannot read `/app`. The directories the worker *can* read and write are its own home (`/home/switchboard/`, for credential storage and Claude session files) and its per-task worktree under `/work/{project}/{task}/`. The DB file at `/data/` is owned by `switchboard-svc` with mode 600; the worker cannot read or write it directly. Every task-state change the worker effects has to go through an MCP tool call against the service.

The combination — worker user + group-based `/app` lockdown + service-user-owned DB — means a compromised worker can't read the service's source code, can't touch other tasks' worktrees owned by other running workers, and can't read or write the database. What it can do: modify its own worktree, push that branch, and call MCP tools the service exposes to it.

Required capabilities on the Python binary:

- `CAP_SETUID` and `CAP_SETGID` to make `os.setuid`/`os.setgid` work without the process being root.
- `CAP_KILL` to send signals across UID boundaries (service user → worker user) for cancellation.

Granted at container build via `setcap 'cap_setuid,cap_setgid,cap_kill+eip' /usr/local/bin/python3`. The Docker compose file's `cap_add: [SETUID, SETGID, KILL]` exposes them to the container.

## 2. Worktree-per-task as filesystem isolation

Each task gets its own git worktree, cut from a bare clone of the project's repository.

```
/work/{project_id}/
├── .bare/                  # bare clone — the shared git object store
├── task-id-1/              # worktree, own branch, own working directory
│   ├── .git                # file, points to shared .bare/worktrees/task-id-1
│   ├── .ouvrage/           # session log, dispatch log, stderr
│   └── <project files>     # checked out on the task's branch
└── task-id-2/              # separate worktree for another task
    ├── .git
    ├── .ouvrage/
    └── <project files>
```

Why git worktrees instead of git clones per task:

- **Shared object store.** A `.bare/` directory holds the object database. Each worktree is a working copy pointing into it. Object storage is shared and deduplicated; 10 tasks against the same repo don't cost 10× the disk.
- **Branch isolation.** A worktree locks its branch — no two worktrees can check out the same branch simultaneously. The system enforces one worktree per task.
- **Cheap creation.** `git worktree add` is a constant-time operation plus the cost of checking out files. Much faster than `git clone`.
- **Clean teardown.** `git worktree remove` deletes the working copy and the lock; the object store stays.

Worktrees are cut from a **pushed** branch — specifically `origin/{branch_name}`. For chained tasks, the upstream task's work is committed and pushed before a dependent dispatches; the dependent's worktree is cut from `origin/{parent_branch}`. Base-branch resolution priority: `depends_on` (→ `origin/{parent_task.branch}`) → explicit `base_branch` param → `origin/{default_branch}`.

The "cut from origin" rule is deliberate. It means every task's output is an extractable artifact on the remote — nothing depends on uncommitted working state living on the host. An operator who wants to modify a parent task's output mid-chain can push a fix to the parent branch and re-dispatch the child; the child picks up the updated `origin/{parent_branch}` at worktree creation time.

Reuse semantics: if a worktree already exists (task resume), the system fetches from origin and merges fast-forward rather than recreating. If a stale ref blocks the worktree, it's force-deleted and recreated. Cleanup on task completion is conditional — the worktree is preserved for cancelled tasks (in case the user wants to inspect) and removed for terminal states (`completed`, `cancelled`).

## 3. Credentials at rest — Fernet

The service needs to hold credentials: git PATs, Anthropic API keys, OAuth client secrets, user password hashes. Everything except password hashes is encrypted with Fernet.

Master key resolution, in priority order:

```
1. $OUVRAGE_MASTER_KEY environment variable
2. /data/.secrets/master_key (mode 400, service-user-only, inside the data volume)
3. /run/secrets/master_key (Docker secret bind mount)
4. Raise RuntimeError — service refuses to start without a key
```

The three tiers serve different operational postures. An env var is the convenient option; it's the least protected because env vars show up in `docker inspect`, `/proc/<pid>/environ`, and anything that can see the process environment. A Docker secret bind-mount (tier 3) is the recommended production path — the master key lives as a mounted file visible only inside the container. The entrypoint copies the Docker secret to `/data/.secrets/master_key` (tier 2) with mode 400 owned by the service user on first boot, so subsequent starts resolve from there even if the secret mount is removed.

For dev the flow is different. The dev entrypoint (`dev-start.sh`) checks `$OUVRAGE_MASTER_KEY`, then `/data/.master_key` inside the data volume from a previous run, else generates a new key and persists it to `/data/.master_key`. The persisted key survives container restarts because the data volume is named and persistent. An operator who wipes the volume loses the key.

Losing the master key without a backup makes every encrypted column unreadable. The service will still start (it generates a new key), but nothing previously encrypted can be decrypted. Credentials have to be re-entered.

Fernet is symmetric — AES-128 in CBC mode with an HMAC. Same key encrypts and decrypts. Output is URL-safe base64, storable in a TEXT column.

Encrypted DB columns include:

- `instance.github_pat_encrypted` — legacy per-instance PAT (pre-provider-chain).
- `git_credentials.credential` — per-provider credentials keyed by `(provider, scope)`.
- `projects.credential_override` (encrypted) — per-project override.
- `oauth_clients.client_secret_encrypted` — OAuth client secrets.

Passwords are not Fernet-encrypted. They're hashed with argon2id (Fernet is reversible; password hashes must not be). Argon2id with library-default parameters — `m=65536, t=3, p=4` — run at registration and on every login attempt.

Key persistence differs between prod and dev:

- **Prod entrypoint.** If `/run/secrets/master_key` exists (Docker secret), use it directly. Else check `$OUVRAGE_MASTER_KEY`. Else generate an ephemeral key and log a loud warning that credentials will be unrecoverable after restart.
- **Dev start script.** Check `$OUVRAGE_MASTER_KEY`, then `/data/.master_key` from a previous run, else generate-and-persist. The persisted file lives inside the data volume; the key survives restarts. Losing the volume loses the key — acceptable for dev.

The master key is the lever. Losing it makes every encrypted column unreadable. Prod deployments should hold it in a real secrets manager; the Docker-secret path exists specifically to support that.

## 4. Credentials via MCP — workers never see decrypted secrets

Workers need to push branches, fetch updates, and create pull requests — all operations that require a credential. The design decision: workers never receive the credential. They call MCP tools; the service performs the operation on their behalf.

When a worker calls `mcp__ouvrage__git_push(task_id=...)` or `mcp__ouvrage__git_fetch(task_id=...)`, the call lands on the service (running as the `switchboard-svc` user). The service handler runs `resolve_credential(project)`, which reads the encrypted credential from the database, decrypts it with Fernet **in service-user memory**, builds the authenticated URL (`https://oauth2:{credential}@host/...`), spawns git as the worker user with that URL, and releases. The decrypted value lives only in service-process memory for the duration of one operation. It's never written to disk, never returned to the worker over MCP, never included in a worker prompt.

```
Worker (user=switchboard)
    │
    │  mcp__ouvrage__git_push(task_id="...")
    ▼
MCP endpoint — /mcp/worker (localhost bypass)
    │
    ▼
Service handler (user=switchboard-svc)
    │
    ├─ resolve_credential(project)          →  encrypted column
    ├─ fernet.decrypt(...)                   →  in-memory plaintext
    ├─ provider.build_authenticated_url(...) →  URL with embedded token
    ├─ _run_as_worker("git", "push", url...) →  spawn as worker user
    │       (authenticated URL passed as argv to the child; visible in the
    │        git process for its lifetime only)
    ├─ await completion
    └─ return status                         →  {"pushed": true}
```

A compromised worker that tried to exfiltrate the credential couldn't — it never had it. The worker sees the MCP return value, which is the operation's result (pushed, fetched, etc.), not the credential. The credential is materially inaccessible to the worker, not just obscured.

The permission model reinforces this. The worker user can't read `/app` (service source, credential-handling code), can't read `/data` (encrypted credentials, OAuth keys), and doesn't share a group with the service user. Even if a prompt injection convinced the worker to try `cat /data/.secrets/master_key`, the kernel would refuse.

The same pattern covers PR creation, PR status checks, credential validation, and anything else requiring the PAT. The service holds the key; workers hold task context. Separation is by process boundary, not by convention.

## 4a. The credential resolution chain

Git operations need an authenticated URL for push/fetch/clone. PR operations need an API token. Both come from the same resolution chain, performed inside the service handler before any subprocess runs.

```
resolve_credential(project) — returns the first match:

1. Project-level override        (projects.credential_override / github_pat_override)
   Set per-project in the dashboard or via MCP.

2. Instance-level credential     (git_credentials.credential, keyed by provider)
   Set by the operator once per provider host.

3. Legacy fallback               (instance.github_pat_encrypted — GitHub only)
   Pre-provider-chain single-PAT setup. Preserved for back-compat.

4. ValueError — nothing found.
```

First match wins. No merging. A project with an explicit override ignores the instance-level credential entirely.

Validation happens at three points:

- **Settings-test** — operator clicks "Test" on a credential. Calls `provider.validate_access()`. Informational; stores the result.
- **Project create/update** — runs automatically. Stores the result on the project. Informational; doesn't block.
- **Dispatch preflight** — hard gate. `validate_project_access()` runs before launching a Claude Code session. On failure, the task stays held with reason `credential_failed` and a message posted to the thread. A dispatch is never started with a known-bad credential.

The chain is deliberately explicit. A project that uses a forked repository owned by a different GitHub user needs its own PAT; the override slot is how. A project that inherits the instance credential needs no per-project setup; the fallback path handles that. A legacy single-PAT install keeps working through the tier-3 fallback.

## 5. Provider abstraction

A single abstract base class `GitProvider` defines the surface. Three concrete implementations follow it without divergence in shape.

Abstract methods:

```python
class GitProvider(ABC):
    name: str                             # "github", "gitlab", "bitbucket"
    default_hostname: str

    def parse_repo_url(self, url: str) -> RepoInfo
    def build_authenticated_url(self, repo_url: str, credential: str) -> str
    async def validate_access(self, credential, repo_info) -> ValidationResult
    async def create_pr(self, credential, repo_info, head, base, title, body) -> PRResult
    async def get_pr_status(self, credential, repo_info, pr_number) -> dict
    def parse_pr_url(self, pr_url) -> tuple[RepoInfo, int]
```

All three providers implement all six methods. No conditional logic in call sites. The dispatch engine calls `provider.create_pr(...)`; the provider handles the REST call specifics.

Representative difference — authenticated URL construction:

```python
# GitHub
f"https://oauth2:{credential}@github.com/{owner}/{repo}.git"

# GitLab
f"https://oauth2:{credential}@gitlab.com/{owner}/{repo}.git"

# Bitbucket (App Password)
f"https://x-token-auth:{credential}@bitbucket.org/{owner}/{repo}.git"
```

The shape is the same; the username-portion varies by platform. Call sites don't care.

`parse_repo_url` normalises `git@host:owner/repo.git`, `https://host/owner/repo`, and variants to a `RepoInfo` dataclass. Once a URL is normalised, all other provider methods operate on `RepoInfo`, not on strings.

## 6. Two-layer auth, always active

Two authentication mechanisms run simultaneously, scoped to different routes.

**Session cookies** — `ouvrage_session`. 7-day TTL with 24-hour inactivity timeout. `Secure`, `SameSite`. Issued on login (`POST /auth/login`) after argon2id password verification. Login is rate-limited: 5 failures in 15 minutes locks the account. Used for: `/dashboard/*`, `/dashboard/api/*`.

**Bearer JWT** — RS256, issued by the built-in OAuth 2.1 server at `/oauth/token`. Access tokens carry a `jti` for revocation; the `oauth_tokens` table tracks revoked jti values. Refresh tokens rotate on use. Used for: `/mcp` (programmatic MCP clients).

Route-to-layer mapping:

```
/dashboard/*, /dashboard/api/*  →  session cookie required
/mcp                            →  Bearer JWT required
/mcp/worker                     →  localhost bypass (no auth)
/proxy/anthropic                →  localhost bypass (no auth)
/health                         →  always open
/oauth/*, /auth/*               →  route-specific logic
```

Middleware inspects `scope["path"]` and `scope["client"]` (the ASGI peername) to pick the layer. There's no "auth-less" fallback for authenticated routes — a request without the right credential gets 401 or 302 depending on whether the UA is browser-shaped.

## 7. Localhost bypass for workers

In-container workers need to call MCP tools to update task state, post progress, push branches. They can't carry a token — tokens would have to be injected into the worker prompt, which means they'd be exposed to the model and could leak into session logs or message threads.

The bypass:

```python
client = scope.get("client")
if client and client[0] in ("127.0.0.1", "::1"):
    if path == "/mcp/worker" or path.startswith("/proxy/anthropic") or path == "/health":
        return await inner_app(scope, receive, send)
```

Three rules:

1. **Peername must be loopback.** `127.0.0.1` or `::1`. The check uses the ASGI `scope["client"]` tuple, which is populated from the TCP connection's peer address. Spoofing requires an attacker with kernel-level access to the host; at that point the whole machine is compromised anyway.
2. **Path must be in the bypass set.** `/mcp/worker`, `/proxy/anthropic/*`, `/health`. The user-facing `/mcp` endpoint is NOT bypassed; only the worker endpoint is.
3. **No token check layered on top.** The bypass is the whole auth check for these routes. In-container workers reach `/mcp/worker` directly.

The endpoints behind the bypass are scoped:

- `/mcp/worker` exposes a subset of tools intended for worker use (checklist, phase, messages, git, files). The user-facing admin tools aren't reachable.
- `/proxy/anthropic` forwards Anthropic API calls on behalf of workers that need it (the Agent SDK's Anthropic client). The proxy itself is off-the-shelf from Anthropic's documented pattern.
- `/health` is a liveness probe for container orchestration.

## 7a. The Anthropic API proxy

Workers call the Anthropic API to run their inner sessions. The API key is a credential — same exfiltration concern as git PATs. Same design response: the worker doesn't carry it.

The service exposes `/proxy/anthropic/{user_id}/v1/...` as a pass-through. The worker configures its Anthropic client to point at this URL instead of `api.anthropic.com`. When the worker makes a request, the service handler attaches the appropriate Anthropic key (looked up per-user, decrypted in service-user memory) to the outbound request, forwards it, streams the response back, and releases the decrypted key. The worker never sees the key.

The proxy route sits behind the localhost bypass — in-container workers call `http://127.0.0.1:8100/proxy/anthropic/...` directly. External clients can't hit it. The bypass works the same way it does for `/mcp/worker`: peer IP must be loopback, path must be in the bypass set.

The proxy is off-the-shelf from Anthropic's documented pattern for "bring your own gateway" deployments. The novelty isn't the proxy — it's that Ouvrage slots it next to the MCP worker endpoint so the same isolation rule (credential stays on the service side) covers both the git operations and the model calls.

## 8. SaaS mode gate

The service has a second mode: `AUTH_MODE=saas`. Intended for a control-plane-driven deployment where SSO happens externally and the service accepts tokens issued upstream.

In SaaS mode:

- `/auth/sso` handler activates. Accepts an RS256 JWT issued by the control plane, validates against `CONTROL_PLANE_JWKS` (cached 1 hour), upserts the user, issues a session cookie. JWT audience must equal `INSTANCE_SLUG`.
- `/internal/*` endpoints activate. Bearer-token-protected (`INTERNAL_API_TOKEN`) endpoints for the control plane to bootstrap users, push config, read usage stats.
- Dashboard redirects send unauthenticated browsers to `${CONTROL_PLANE_URL}/login?redirect=...` instead of the built-in login page.

In local mode (default):

- `/auth/sso` returns 404.
- `/internal/*` returns 404.
- Dashboard redirects send unauthenticated browsers to `/dashboard/login`.

The gate is one env var. Code paths check `if AUTH_MODE != "saas": return 404` at the top of each handler. The two modes share no mutable state; flipping between them at runtime would be unsafe, but the service doesn't support that — `AUTH_MODE` is read once at startup.

## 9. What's exposed and what isn't

Public endpoints (no auth required):

- `/health` — liveness probe.
- `/.well-known/oauth-authorization-server` — OAuth discovery metadata.
- `/oauth/authorize`, `/oauth/token`, `/oauth/jwks` — OAuth 2.1 flow.
- `/dashboard/login`, `/auth/login`, `/auth/logout` — login flow (the login page is public; login credentials are verified against the users table).

Authenticated endpoints:

- `/dashboard/*` — session cookie.
- `/dashboard/api/*` — session cookie.
- `/mcp` — Bearer JWT.

Localhost-only:

- `/mcp/worker` — loopback peer required.
- `/proxy/anthropic/*` — loopback peer required.

SaaS-only:

- `/auth/sso` — SaaS mode + valid CP JWT.
- `/internal/*` — SaaS mode + valid internal bearer.

Nothing else is served. A request to a path not in the list gets 404.

## Evolution

A brief history of how the current shape accreted:

**BBS era.** Initial implementation had no auth — a local MCP server on a laptop. Credential storage was `getenv` at startup; the worker (which was the same process) just used whatever was in env.

**VPS move.** Moving to a VPS required authentication (exposed to the internet) and credential encryption (DB file on disk someone else controlled). Added session cookies + argon2id password hashing, then Fernet for DB encryption.

**Orchestrator era.** When the service started spawning Claude Code workers, the single-process model broke. Workers needed isolation from the service; the service needed authenticated worker communication without exposing tokens in prompts. The localhost bypass landed here. Worker-user setuid followed shortly after — the first implementation ran workers as root, which was short-lived.

**Multi-platform era.** GitHub-only credential handling — a single `instance.github_pat_encrypted` column — worked for a while. Adding a GitLab-hosted repo broke it. Rather than add `instance.gitlab_pat_encrypted` and duplicate the logic at every call site, the provider ABC landed. The legacy column is preserved as the tier-3 fallback.

**Fallback era.** The worker-user setuid code assumed `pwd.getpwnam(WORKER_USER)` would always resolve. When the test suite started running on a GitHub Actions runner without a `switchboard` user, everything using `_run_as_worker` broke at import time. `_resolve_worker_identity` landed as the graceful-degradation path: production containers keep isolation; everywhere else runs as the current user.

## Alternatives considered

- **Docker-per-task for filesystem + process isolation.** Heavier than needed. Container startup adds seconds per task; resource limits and cleanup would have to be managed separately. Worktree + setuid gives equivalent isolation at a fraction of the cost.
- **Separate MCP servers per tenant.** Would avoid the localhost bypass complication. Rejected for operational cost — one service process is enough; running one per tenant multiplies operational work without security gain for a single-operator system.
- **API tokens for worker → service communication.** Workers carry a token that identifies them. Rejected because the token would have to be injected into the worker prompt — exposed to the model, loggable, leakable to message threads. Loopback auth keeps tokens out of the prompt surface entirely.
- **HashiCorp Vault / AWS Secrets Manager for credentials at rest.** External secrets manager. Rejected for deployment complexity — adds a service dependency. Fernet in SQLite is simpler; operators who need vault-grade protection can run with vault-backed env vars for the master key.
- **Hand the credential to the worker so it can run git directly.** Simpler MCP surface — worker just gets back a PAT and calls git itself. Rejected because the credential would pass through the worker's memory and logs; prompt-injection attacks would have a target. The MCP-mediated flow (service decrypts, runs git, returns status) keeps the credential in service-user memory only.
- **No localhost bypass; embed a worker token.** Ignored for the reason above — tokens in prompts are a leak surface.

## Tradeoffs

- **Setuid in Python.** `preexec_fn` runs in the forked child before exec. The code is short but the blast radius of a bug is large — a demote that didn't fire would run worker code as the service user. The code is reviewed specifically because of that risk; no abstraction layer exists between the demote and the subprocess launch.
- **Loopback trust.** The localhost bypass trusts that the peer at `127.0.0.1` is the same-host worker. An attacker with code execution on the host can reach `/mcp/worker` without credentials. At that point they also have filesystem access; the additional exposure is marginal. Operators who need stronger controls should not run untrusted code on the host.
- **Fernet master key.** Symmetric. Losing it loses every encrypted credential; stealing it exposes every encrypted credential. No key rotation is built in — changing the master key requires re-encrypting every stored credential, which doesn't happen automatically.
- **No key escrow.** If the operator loses the master key and doesn't have a backup, credentials are gone. No recovery path; the operator re-enters every credential through the dashboard.
- **SaaS mode adds complexity.** `/internal/*` endpoints, SSO handler, JWKS fetch — all code paths exist for a deployment topology that isn't the common case. The code paths are gated but non-zero. Simpler to have one mode; harder to support the control-plane deployment without them.
- **Loopback check is peername-based.** ASGI passes the peer tuple; the middleware trusts it. A reverse proxy that rewrites peername could spoof this. The Docker container does not run behind a proxy for `/mcp/worker`; the bind is directly to the service.

## Cross-cutting concerns

- **Observability.** Failed login attempts, auth rejections, and dispatch preflight failures all write to the service log. The audit log tracks every lifecycle transition and its `triggered_by`; admin actions done through the dashboard are attributable to the session user.
- **Data retention.** Session cookies expire; dead sessions are pruned from the `sessions` table. Revoked JWTs stay in `oauth_tokens.revoked` for their original TTL to prevent replay.
- **Credential rotation.** Per-project overrides are rotatable through the dashboard — set a new credential, the cache invalidates on next use. Master key rotation is manual: decrypt with old, encrypt with new, swap `$OUVRAGE_MASTER_KEY`. Not scripted.
- **Incident response.** If a credential leaks, revoke at the source (GitHub settings, GitLab settings) and replace in Ouvrage. The dashboard exposes every stored credential for inventory; `grep -r` across the DB finds references.
- **Dependency risk.** `cryptography` library for Fernet; `authlib` for OAuth and JWT; `argon2-cffi` for password hashing. All maintained; CVEs are rare but watched. Upgrading them is routine.

## Riff points

- Worker isolation is two primitives: worktree-per-task for filesystem, setuid-worker for process. Neither needs Docker.
- `_resolve_worker_identity` returns None when the OS user doesn't exist. Production keeps isolation; dev/CI/OSS self-host runs as current user.
- `CAP_SETUID`, `CAP_SETGID`, `CAP_KILL` applied to the Python binary via `setcap`. Compose exposes them to the container.
- Fernet at rest. Master key resolved from env, or `/data/.secrets/master_key`, or `/run/secrets/master_key`. No key, no start.
- Credential resolution chain: project override → instance credential → legacy PAT. First match wins, no merging.
- Provider ABC. Six abstract methods. Three implementations (GitHub, GitLab, Bitbucket), no conditional logic at call sites.
- Two auth layers, always active. Session cookies for browser; RS256 JWTs for programmatic MCP.
- Localhost bypass for `/mcp/worker` and `/proxy/anthropic`. Workers don't carry tokens in prompts.
- `AUTH_MODE` env var gates SaaS-specific code paths. `local` is the default; `saas` activates `/auth/sso` and `/internal/*`.
- Worktree-per-task is cheap because the bare clone is shared. `git worktree add` is constant-time.
- Validation runs at three points: settings-test (informational), project create/update (informational), dispatch preflight (hard gate).
