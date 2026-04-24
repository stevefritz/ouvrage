# Ouvrage Architecture

Four documents describing how this system works, what it's built from, and the decisions that shaped it.

**Start here:** [`overview.md`](overview.md) — what Ouvrage is, how it came to exist, the shape of the system at altitude, and the main tradeoffs. Read this first if you're seeing the project for the first time.

**Then pick whichever depth interests you:**

- [`context-engineering.md`](context-engineering.md) — the retrieval layer. Storage tiers (row / FTS5 / vec0), two-grain embeddings, re-rank weights, the pull-based MCP tool surface, pins as canonical.
- [`task-lifecycle.md`](task-lifecycle.md) — the finite state machine. Six core DB states, 65 declared transitions, gate pipeline as feedback loop, chain propagation, crash recovery, and the refactor that consolidated scattered direct writes into a single-owner contract.
- [`prompt-engineering.md`](prompt-engineering.md) — worker prompts and the stopping problem. Escalation as alignment problem, review gate as blind critique, feedback injection on retry, tool-surface reduction.
- [`security-and-isolation.md`](security-and-isolation.md) — isolation primitives and auth model. Worker setuid, worktree-per-task, Fernet at rest, credential resolution chain, provider ABC across GitHub / GitLab / Bitbucket, two-layer auth, localhost bypass for workers.

---

The docs are written in present tense. They describe what the system is, not what it was in earlier iterations or might become later. Where the current shape was arrived at through a refactor, the evolution is called out by name (most significantly, the task-lifecycle state-machine consolidation).

None of these documents are ADRs. Single-decision records with a strict Context / Decision / Consequences template will land in `adrs/` alongside these files in a later pass. The narrative docs call the decisions out in prose; the ADRs will make them citable.
