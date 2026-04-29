## Ouvrage

Ouvrage is a self-hosted development knowledge and orchestration platform for AI-assisted engineering.

You connect it to your repositories and plan work through any MCP-enabled chat client. Conversations, specs, and architectural decisions accumulate in a vectorized knowledge base that both you and the LLM can search. When you're ready to build, you use your chat client to write up the task specification and dispatch the work. Ouvrage then handles branch management, Claude Code agent execution, automated quality gates with retries, and the PR.

Agentic workflows generate enormous volumes of information and work product, most of which is ephemeral by default — scattered across chat sessions, lost between contexts, gone when the window closes. Ouvrage is an attempt to make that substrate durable. Beyond providing an orchestration pipeline and a knowledge base, the goal is that any user can connect through an MCP client and conversationally introspect the system — understanding a codebase's architecture, the reasoning behind past decisions, and the current state of work through organic conversation rather than documentation they have to go find and read.

### How it works

**The flywheel.** The accumulated knowledge powers the specifications going into new tasks. An agent can produce on-the-fly documentation from the vector store, or you can interrogate the system conversationally to learn its architecture and why past decisions were made.

**Context and state.** The knowledge layer is indexed by project and scoped by task. Pins mark canonical content, while the underlying vector database ensures the full historical record remains semantically searchable.

**Orchestration via MCP.** The LLM drives orchestration: dispatching tasks, responding to status, and handling retries. A human-in-the-loop dashboard exposes the same operations — approving held tasks, reviewing gate output, and curating the knowledge base.

The result is that both you and the LLM share a durable reference for what was decided and why. Specifications get sharper over time, planning stays coherent, and the work stays closer to the original vision.

## Quickstart

```bash
git clone https://github.com/stevefritz/switchboard.git ouvrage
cd ouvrage
./setup.sh
docker compose up -d
```

Then open http://localhost:8100.

The setup script will prompt for the few things it needs (owner email, owner password, optional OpenAI key). Everything else is handled for you.

## Resetting

Re-running `./setup.sh` is safe — it skips anything already done. If the database exists, it won't re-prompt for owner credentials (the bootstrap values are only meaningful on first boot; changing them after the database is created has no effect).

To change your owner password after first boot, log in and use the dashboard.

To wipe everything and start over:

```bash
./setup.sh --reset
```

This deletes `data/`, `work/`, `claude-auth/`, `secrets/`, `.env`, and `gitconfig`, then runs fresh setup. You will be prompted to confirm before anything is deleted.

## Configuration

After first boot the container initialises the database, creates the owner account, and generates an OAuth RSA key. The `OUVRAGE_OWNER_EMAIL` and `OUVRAGE_OWNER_PASSWORD` vars (written to `.env` by `setup.sh`) are only needed on first start. `OPENAI_API_KEY` is optional — without it, conversation search falls back to full-text search only; vector search is disabled.

## Usage

Ouvrage exposes an MCP endpoint at `/mcp`. Configuration depends on whether your client runs on the same machine as Ouvrage, or somewhere else.

### Same-machine (Claude Code on your laptop)

No extra configuration. Register the local endpoint:

```bash
claude mcp add --transport http ouvrage http://localhost:8100/mcp
```

Localhost connections are auth-bypassed by design — tools are available immediately on next session start.

### Remote / hosted (Claude.ai, or Claude Code from another machine)

> ⚠️ **Claude.ai cannot connect to `http://localhost:8100`.** It needs a publicly reachable HTTPS URL. Two ways to get one:
> - **Tunnel** for dev: [ngrok](https://ngrok.com/), [cloudflared](https://github.com/cloudflare/cloudflared), Tailscale Funnel.
> - **Hosted** for permanent: a VPS behind a reverse proxy (Caddy, nginx) on your domain.

#### Step 1 — tell Ouvrage its public URL

Set `OUVRAGE_PUBLIC_URL` in `.env`, then recreate the container:

```bash
# .env
OUVRAGE_PUBLIC_URL=https://your-tunnel.ngrok.app
```

```bash
docker compose up -d
```

This single variable propagates to the OAuth issuer, the OAuth base URL, and the MCP resource URL — the three places Ouvrage advertises "I am this server." If you skip it, OAuth flows will redirect through `localhost` and break for any external client.

(Advanced: `OAUTH_BASE_URL`, `AUTH_ISSUER_URL`, and `RESOURCE_URL` remain available as individual overrides for split-domain setups. Most users don't need them.)

`setup.sh` will prompt for `OUVRAGE_PUBLIC_URL` on first run and on subsequent re-runs.

#### Step 2 — connect your client

**Claude Code (remote)**

```bash
claude mcp add --transport http ouvrage https://your-tunnel.ngrok.app/mcp \
  --client-id <client-id> --client-secret
```

The CLI prompts for the secret. Get both values from the dashboard **Settings** page. Then in Claude Code: `/mcp` → select `ouvrage` → **Authenticate**.

**Claude.ai**

1. Settings → Connectors → Add Custom Connector
2. Name: anything (e.g. `Ouvrage`)
3. Remote URL: `https://your-tunnel.ngrok.app/mcp`
4. Advanced settings: paste OAuth client credentials from dashboard Settings
5. Save → click **Connect** → complete the OAuth handshake

If the OAuth handshake redirects you to a `localhost` URL during connect, your `OUVRAGE_PUBLIC_URL` isn't set or the container hasn't been recreated to pick it up.

### Dispatching work

```
create_project(id="my-repo", repo="https://github.com/you/my-repo.git")

dispatch_task(
  project_id="my-repo",
  goal="Add pagination to the users API endpoint",
  auto_test=true,
  auto_review=true
)
```

`dispatch_task` returns immediately with a task ID. A Claude Code worker picks it up in an isolated git worktree, commits to a branch, and reports progress back through MCP. Task status, message thread, and session log live on the dashboard at your `OUVRAGE_PUBLIC_URL/dashboard/` (or `http://localhost:8100/dashboard/` if local-only).

## Architecture

The context layer is built on SQLite with FTS5 for full-text search and sqlite-vec for vector embeddings. The orchestration layer runs Claude Code workers in isolated git worktrees, managed through a six-state lifecycle machine (`ready → working → validating → completed`, with `stopped` and `cancelled`). Both layers share the same database and MCP surface.

- [`docs/overview.md`](docs/overview.md) — System overview and component relationships
- [`docs/context-engineering.md`](docs/context-engineering.md) — How the context layer stores, pins, and retrieves
- [`docs/task-lifecycle.md`](docs/task-lifecycle.md) — Task state machine and gate pipeline
- [`docs/prompt-engineering.md`](docs/prompt-engineering.md) — How worker prompts are constructed
- [`docs/security-and-isolation.md`](docs/security-and-isolation.md) — Worker isolation, auth model, credential encryption

## Development

```bash
make docker-dev   # bind-mounted source, live reload
make test         # pytest tests/ -q --tb=short
```

Tests are in `tests/`. CI runs on Python 3.12 and 3.13.

## License

MIT. See [LICENSE](LICENSE).

## A note on how this was built

Ouvrage started as a small MCP server — a BBS where my Claudes could post in threads and read each other's messages. Claude.ai drafted specs, Claude Code read them. Around 350 lines. That was the whole thing, and it was the first time I externalized context discipline into infrastructure instead of tab-switching.

It grew from there. Once I could dispatch work from inside the system, translating ideas into implementations got noticeably easier, and the system started building itself.

The codebase is almost entirely AI-generated. Planning is collaborative — human and LLM working through design across many hours of conversation, with human judgment on what to build. Implementation is dispatched to Claude Code workers through Ouvrage. Every change goes through review (gate-enforced reviewer sessions plus human review on specs that matter), and the system has been used continuously throughout its development.

The shape of the work shifts when automation handles implementation. Time that would have gone to writing code goes instead to planning and specification — working an idea until the plan is genuinely clear, because once it is, turning it into working code is quick. Building the system tested the system.
