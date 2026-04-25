# Ouvrage

Working at AI-driven speed creates its own coordination problem. Long conversations, cross-cutting concerns, and multiple parallel work streams quickly become difficult to manage inside a single chat session.

Ouvrage is an attempt to address that. It is a planning and orchestration system that brings order to the chaos, and it produces documentation as exhaust.

You work through ideas in conversation with an MCP-enabled chat client, recording decisions, raw notes, and designs as you go. Ouvrage decomposes that work into bounded tasks with concrete specifications. Separate conversations converge into a single, durable knowledge base. Because this entire corpus is vectorized, context can be retrieved via semantic search for a fresh session without losing the nuance of the original reasoning.

How it works:

The flywheel. The accumulated material powers the specifications going into new tasks. An agent can produce on-the-fly documentation from this vector store, or you can interrogate the system conversationally to learn its architecture and why past decisions were made.

Context and state. The context layer is indexed by project and scoped by task. Pins mark canonical content, while the underlying vector database ensures the full historical record remains semantically searchable.

Orchestration via MCP. The LLM drives orchestration: dispatching tasks, responding to status, and handling retries. A human-in-the-loop dashboard exposes the same operations, allowing you to approve held tasks, review gate output, and curate the corpus.

The result is that both the human and the LLM have a shared, durable reference for what was decided and why. This makes specifications sharper, planning more coherent, and the work itself closer to the original vision.

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

Connect an MCP-enabled client to `http://localhost:8100/mcp`. Claude.ai connects via OAuth; Claude Code and other local clients connect without auth from localhost. OAuth client credentials are available on the dashboard **Settings** page.

### Claude Code

From any project directory, register Ouvrage as an MCP server:

```bash
claude mcp add --transport http ouvrage http://localhost:8100/mcp
```

That's it for local use — the localhost bypass means no OAuth credentials are needed. To connect to a remote Ouvrage instance, add `--client-id` and `--client-secret` flags (you'll be prompted for the secret):

```bash
claude mcp add --transport http ouvrage https://your-host/mcp \
  --client-id <client-id> --client-secret
```

Inside Claude Code, run `/mcp`, select the `ouvrage` server, and choose **Authenticate** to complete the OAuth handshake. After that, Ouvrage tools are available to the session.

### Claude.ai

In Claude.ai go to **Settings → Connectors → Add Custom Connector**. Give it any name (e.g. `Ouvrage`), set the remote URL to your Ouvrage `/mcp` endpoint, and under **Advanced settings** paste the OAuth client credentials from the dashboard Settings page. Save, then click **Connect** on the connector to authenticate against the dashboard.

Claude.ai requires a publicly reachable URL — the local default `http://localhost:8100/mcp` won't work. For local machines, expose Ouvrage through a tunnel (e.g. [ngrok](https://ngrok.com/)) and use the external URL when adding the connector.

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

Tests are in `tests/`. CI runs on Python 3.12 and 3.13. [`CLAUDE.md`](CLAUDE.md) documents internal conventions — the state-machine contract, provider interface, and test-fixture patterns — and is the right starting point for contributors.

[`docs/internal/pre-oss-architecture-review.md`](docs/internal/pre-oss-architecture-review.md) covers the pre-cleanup state of the repo for anyone wanting historical context.

## License

MIT. See [LICENSE](LICENSE).

## A note on how this was built

Ouvrage started as a small MCP server — a BBS where my Claudes could post in threads and read each other's messages. Claude.ai drafted specs, Claude Code read them. Around 350 lines. That was the whole thing, and it was the first time I externalized context discipline into infrastructure instead of tab-switching.

It grew from there. Once I could dispatch work from inside the system, translating ideas into implementations got noticeably easier, and the system started building itself.

The codebase is almost entirely AI-generated. Planning is collaborative — human and LLM working through design across many hours of conversation, with human judgment on what to build. Implementation is dispatched to Claude Code workers through Ouvrage. Every change goes through review (gate-enforced reviewer sessions plus human review on specs that matter), and the system has been used continuously throughout its development.

The shape of the work shifts when automation handles implementation. Time that would have gone to writing code goes instead to planning and specification — working an idea until the plan is genuinely clear, because once it is, turning it into working code is quick. Building the system tested the system.
