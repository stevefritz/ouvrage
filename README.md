# Ouvrage

Ouvrage is a context engineering and orchestration system for agentic coding. The context layer stores conversations, decisions, and specs with hybrid keyword and vector search, so an LLM can help you plan and then retrieve that planning later at whatever grain you need. The orchestration layer dispatches Claude Code workers to your repos, chains them through dependencies, runs test and review gates, and opens PRs. Both are exposed through an MCP server for use from any MCP-enabled client, and through a web dashboard for direct access when needed.

The context layer is indexed by project and scoped by conversation or task. Pins mark canonical content; the rest of the history stays searchable. Retrieval is available to both humans (through the dashboard or an MCP client) and workers (through the same MCP tools).

Orchestration is primarily driven by the LLM through MCP — dispatching tasks, responding to status, handling retries. The dashboard exposes the same operations for human-in-the-loop work: approving held tasks, reviewing gate output, pinning messages, reading task threads.

Working at AI-driven speed creates its own coordination problem: long conversations, cross-cutting concerns, and multiple parallel work streams that become difficult to manage inside a single conversation. Ouvrage is an attempt to address that. Separate conversations converge into a single knowledge base, and that knowledge can be retrieved in a fresh context without losing the nuance of the original reasoning. The result is that both the human and the LLM have a durable reference for what was decided and why — which makes specifications sharper, planning more coherent, and the work itself closer to the original vision.

## Quickstart

Requires Docker 24+ and Docker Compose v2.

```bash
git clone https://github.com/stevefritz/switchboard.git ouvrage
cd ouvrage
```

Generate a Fernet master key to encrypt stored credentials:

```bash
mkdir -p secrets
docker run --rm --entrypoint python3 python:3.13-slim -c \
  "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" \
  > secrets/master_key
```

If you're just trying it out, skip the key step — the container will generate an ephemeral key automatically on startup, with a warning that encrypted data won't survive a restart.

Start the stack with your owner credentials:

```bash
OUVRAGE_OWNER_EMAIL=you@example.com \
OUVRAGE_OWNER_PASSWORD=change-me \
docker compose -f docker-compose.example.yml up -d --build
```

```bash
open http://localhost:8100/dashboard/login
```

On first boot the container initialises the database, creates the owner account, and generates an OAuth RSA key. The bootstrap env vars can be removed after first start.

## Configuration

See [`.env.example`](.env.example) for the full variable reference. The required vars are `OUVRAGE_MASTER_KEY` (the Fernet key generated above), `ANTHROPIC_API_KEY` (used by dispatched Claude Code workers), `OUVRAGE_OWNER_EMAIL`, and `OUVRAGE_OWNER_PASSWORD`. `OPENAI_API_KEY` is optional — without it, conversation search falls back to full-text search only; vector search is disabled.

## Usage

Connect an MCP-enabled client to `http://localhost:8100/mcp`. Claude.ai connects via OAuth (credentials printed on first boot); Claude Code and other local clients connect without auth from localhost.

Register a project and dispatch work:

```
create_project(id="my-repo", repo="https://github.com/you/my-repo.git", working_dir="/work/my-repo")

dispatch_task(
  project_id="my-repo",
  goal="Add pagination to the users API endpoint",
  auto_test=true,
  auto_review=true
)
```

`dispatch_task` returns immediately with a task ID. A Claude Code worker picks it up, runs in an isolated git worktree, commits to a branch, and reports progress back through MCP. Task status, message thread, and session log are visible at `http://localhost:8100/dashboard/`.

## Architecture

The context layer is built on SQLite with FTS5 for full-text search and sqlite-vec for vector embeddings. The orchestration layer runs Claude Code workers in isolated git worktrees, managed through a six-state lifecycle machine (`ready → working → validating → completed`, with `stopped` and `cancelled`). Both layers share the same database and MCP surface.

- [`docs/architecture/overview.md`](docs/architecture/overview.md) — System overview and component relationships
- [`docs/architecture/context-engineering.md`](docs/architecture/context-engineering.md) — How the context layer stores, pins, and retrieves
- [`docs/architecture/task-lifecycle.md`](docs/architecture/task-lifecycle.md) — Task state machine and gate pipeline
- [`docs/architecture/prompt-engineering.md`](docs/architecture/prompt-engineering.md) — How worker prompts are constructed
- [`docs/architecture/security-and-isolation.md`](docs/architecture/security-and-isolation.md) — Worker isolation, auth model, credential encryption

## Development

```bash
make docker-dev   # bind-mounted source, live reload
make test         # pytest tests/ -q --tb=short
```

Tests are in `tests/`. CI runs on Python 3.12 and 3.13. [`CLAUDE.md`](CLAUDE.md) documents internal conventions — the state-machine contract, provider interface, and test-fixture patterns — and is the right starting point for contributors.

[`docs/internal/pre-oss-architecture-review.md`](docs/internal/pre-oss-architecture-review.md) covers the pre-cleanup state of the repo for anyone wanting historical context.

## License

MIT. See [LICENSE](LICENSE).

## A note on how this was built

Ouvrage started as a small MCP server — a BBS where my Claudes could post in threads and read each other's messages. Claude.ai drafted specs, Claude Code read them. Around 350 lines. That was the whole thing, and it was the first time I externalized context discipline into infrastructure instead of tab-switching.

It grew from there. Once I could dispatch work from inside the system, translating ideas into implementations got noticeably easier, and the system started building itself.

The codebase is almost entirely AI-generated. Planning is collaborative — human and LLM working through design across many hours of conversation, with human judgment on what to build. Implementation is dispatched to Claude Code workers through Ouvrage. Every change goes through review (gate-enforced reviewer sessions plus human review on specs that matter), and the system has been used continuously throughout its development.

The shape of the work shifts when automation handles implementation. Time that would have gone to writing code goes instead to planning and specification — working an idea until the plan is genuinely clear, because once it is, turning it into working code is quick. Building the system tested the system.
