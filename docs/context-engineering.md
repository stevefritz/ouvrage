# Ouvrage — Context Engineering

The retrieval layer. How content is stored, indexed, searched, and surfaced to agents. The discipline of deciding what the agent sees, in what grain, and on whose initiative.

---

## Why this exists

Two problems with one shape.

The first is fidelity loss through compaction. When a chat interface fills its context window, it runs an automatic summarisation pass — the model decides what's important, crushes the reasoning behind decisions into a sentence, and throws the nuance away. Two weeks later, the summary tells you *what* was decided but not *why*. The why is usually what matters.

The second is briefing autonomous workers. A Claude Code session dispatched to make a change needs an accurate, narrow description of what to build and why. Not a transcript of the design conversation; a distilled spec. Producing that spec by hand for every task doesn't scale.

The same storage machinery solves both. Conversations and task threads capture the raw stream. Collaborative curation — the human and Claude working a topic together, Claude writing a spec-shaped message, the human pinning it — produces durable canonical artifacts. The pinned artifacts brief workers; the raw stream restores nuance for humans returning to the project. Retrieval pulls from both.

This document describes the storage, the embedding strategy, the retrieval shape, and the MCP surface that lets two different consumers — workers and humans — get what they need at the grain that suits them.

## Goals / Non-goals

**Goals:**

- Durable, retrievable storage for conversations, specs, decisions, and task artifacts.
- Hybrid keyword + vector search from a single query.
- A pull-based MCP surface so agents request context rather than receive it pre-chewed.
- Human-curated canonical content (pinned messages) that outranks semantically-close noise.
- Graceful degradation: the system works without OpenAI, without sqlite-vec, without exact dimensionality.

**Non-goals:**

- A general-purpose vector database. Ouvrage stores its own conversations, not arbitrary corpora.
- Automatic context summarization or compaction. Summaries discard reasoning; the system is designed to retain it.
- Streaming retrieval or sub-second latency. Planning-loop latency (hundreds of ms) is fine.
- Multi-tenant vector isolation. One operator, one database, one embedding namespace.

## Stack

- **SQLite** in WAL mode. FTS5 for keyword. `sqlite-vec` (vec0 virtual tables) for vector KNN.
- **OpenAI `text-embedding-3-small`**, 1536 dimensions, stored as packed float32 blobs (4 bytes × 1536 = ~6 KB per embedding).
- **Plain tables** for state that doesn't need search (projects, attempts, checklist, audit log).
- **MCP tools** as the pull surface: `read`, `get_pinned`, `search`, `read_task_messages`, `conversations`, `get_context`.

## 1. Storage layered by intent

The schema has one rule: if you need to search it, it gets indexed three ways. The row itself, an FTS5 mirror, a vec0 mirror. If you only need to fetch or filter it, a plain table is enough.

```
SEARCHABLE
  messages        → messages_fts (content) + messages_vec (1536-dim embedding)
  message_chunks  → chunks_vec (1536-dim embedding per chunk)
  tasks           → tasks_fts (goal) + tasks_vec (1536-dim embedding of goal)

PLAIN
  conversations, projects, attempts, checklist, punchlist,
  subtasks, audit_log, users, instance, components, files,
  push_subscriptions, git_credentials
```

FTS5 mirrors keep in sync via `INSERT`/`UPDATE`/`DELETE` triggers that only fire on content changes (triggers are scoped to the one column, so updating `created_at` doesn't re-tokenize the full text). Vec0 mirrors have delete triggers only; insert/update go through the application layer because embedding computation requires an OpenAI call.

## 2. Embeddings at two grains

Whole-message and per-chunk. Each grain answers a different query shape.

**Whole-message.** One embedding per message. Catches thematic matches — *what conversation is this about.* Computed once when the message is written (async, non-blocking). Stored in `messages_vec` keyed by rowid.

**Per-chunk.** One embedding per markdown section. Catches local precision — *which specific paragraph answers this.* Computed when a message is written, conditional on three rules:

1. Content is at least 500 characters.
2. Content has markdown headers (`#`, `##`, or `###`).
3. Splitting on those headers produces more than one section.

If any rule fails, the message gets no chunks. A sentinel row (`chunk_index = -1`, `embedding = NULL`) marks it so backfill skips it next time. Search filters `chunk_index >= 0` to exclude sentinels from results.

The grain is whatever reasoning unit the human (or agent) already marked. No sliding windows. No token-based splits. If the author didn't use headers, the message is a single unit — whole-message embedding is the right grain.

Short messages (under 50 characters) and types that never warrant search (`test-result`) skip embedding entirely. The `should_embed()` helper gates this.

## 3. Retrieval: two-stage, re-ranked

Retrieval is never one query. It's KNN → SQL filter → re-rank.

```
query text
   │
   ▼
embed via text-embedding-3-small → 1536-dim vector
   │
   ▼
vec0 KNN query with oversample
   messages_vec:  oversample = limit × 15
   chunks_vec:    oversample = limit × 10
   tasks_vec:     oversample = limit × 10
   │
   ▼
SQL filter on rowids
   conversation_id, project_id (via conversation or task FK), type filter
   │
   ▼
re-rank in Python
   base = 1 - (distance / 2)         # cosine similarity from L2 distance
   score = base × type_weight × pinned_multiplier
   │
   ▼
top-N returned
   chunk hits pull ±1 neighbors for surrounding context
```

**Oversample factors.** Messages get 15× because type-filter pruning is aggressive (status and test-result dominate raw corpus by count but score low). Chunks and tasks get 10× — smaller corpora, less pruning.

**Type weights.** Applied to cosine similarity in `compute_relevance_score`:

```
spec        1.5
review      1.4
note        1.2
plan        1.1
result      1.1
answer      1.0
(untyped)   1.0
question    0.8
status      0.5
test-result 0.3
```

These weights encode the raw-vs-curated distinction. Specs, reviews, plans, and notes are curation outputs (or semi-curated — a review is a reasoned verdict, a plan is a deliberate artifact). Questions and status are raw stream content. Test results are raw stream content that's specifically noisy. The numbers are not learned — they're chosen. Adjustable when the balance feels wrong.

**Pinned boost.** A separate 1.3× multiplier applied on top. Pins are the human-curated canonical record for a conversation: a spec, a design decision, an authoritative answer. The boost is enough that pinned content reliably outranks semantically-close unpinned content, even when the unpinned content has a slightly tighter cosine. The human decides what's canonical; the retrieval layer honors it.

**Chunk neighbors.** When a chunk hit ranks high, the `±1` neighboring chunks are fetched and returned alongside. A hit on section 2 of a spec returns sections 1, 2, 3 together. Cheap (one indexed lookup per hit), and it keeps hits coherent when the reasoning spans paragraphs.

**FTS fallback.** Keyword search runs separately, against `messages_fts` with a sanitized query (each word wrapped in double quotes for literal matching). Ranking is BM25. The two search paths (vec and FTS) aren't automatically merged by the backend — callers decide which they need. The `search` MCP tool runs both in parallel.

## 4. Graceful degradation

The system is built to not hard-fail on optional dependencies.

- **No sqlite-vec extension loaded.** Caught at connection setup; the module's `VEC_AVAILABLE` flag is set False. Vec queries return empty; FTS and direct fetch still work. `search` returns keyword results only.
- **sqlite-vec KNN query raises.** Try/except around every vec0 query. Logged, returns empty for that stage. Caller still gets re-ranked results from FTS.
- **No OpenAI key.** Embedding service's `embed_safe()` swallows errors and returns None. Messages store with `embedding = NULL`. Subsequent searches skip them but don't fail.
- **Query vector wrong dimension.** Falls back to a Python cosine loop over stored embeddings. Slow but correct.
- **Embedding dimension mismatch with stored vectors** (model change, migration in progress). Same Python cosine fallback.

The rule: vector search is a performance optimization. FTS is the floor. Everything else is best-effort.

## 5. Context delivery — hybrid: deliberate injection plus on-demand pull

A worker's context is assembled through three injection paths and one pull surface. This is a hybrid shape, not pure RAG-style stuffing and not pure agentic tool-use. Injection is the default for load-bearing content at known moments; pull is the escape hatch for anything else.

**Injected at dispatch** (one-shot when the worker launches):

- The task goal and checklist from the task record.
- The spec — the curated `type='spec'` message drafted before dispatch.
- System-level framing: identity, branch, worktree path, escalation protocol, callback tool guidance, completion protocol.

**Injected on retry** (one-shot when a gate-triggered retry dispatches):

- Test-gate failure output (stdout tail, exit code, command) from the previous attempt, verbatim.
- Review-gate feedback (the reviewer's `CHANGES REQUESTED` message), verbatim.
- Both land at the top of the new attempt's prompt, under a "REVISION REQUESTED" heading, ahead of the original spec.

**Injected mid-run** (continuous while the worker is active):

- When a human or Claude.ai posts a message to the task thread, a poller detects it within 5 seconds and injects it into the running session via `client.query()` with an explicit "LIVE MESSAGE FROM..." framing. The worker knows the content came from outside its own reasoning.
- Worker- and dispatcher-authored messages are filtered — the worker doesn't re-inject its own posts.

**Pulled on demand** (worker calls a tool when it needs more):

- `get_pinned` — fetch the conversation-scope pin, the project-wide source of truth.
- `search` — hybrid FTS + vec across all messages, filtered by project or type.
- `read` / `read_task_messages` — paginated reads with cursor (`after=<id>`) semantics.
- Filesystem reads of the codebase via standard file tools.

A well-constructed spec means the worker rarely needs to pull. The injection paths carry the load-bearing content at the right moments; the pull surface is available for when the brief is incomplete or the task's shape shifts mid-run.

**The pull surface:**

| Tool | Purpose |
|---|---|
| `read` | Read messages from a conversation. Cursor (`after=<id>`), window, author/type/pinned filters. |
| `get_pinned` | Fetch the pinned message for a conversation or task — the canonical artifact at that scope. |
| `search` | Hybrid FTS + vec across all messages, with optional project/type filters. |
| `read_task_messages` | Messages on a specific task thread. Same cursor + window semantics as `read`. |
| `conversations` | List conversations, optionally filtered by project. |
| `get_context` | Orientation snapshot — active projects, running tasks, recent events. Called at the start of a Claude.ai session. |

Pagination is cursor-based. The `after` parameter returns messages with `id > <value>`. The return shape includes the last id so callers chain reads without re-flooding context.

**Two consumers.** The same surface serves two very different readers.

A dispatched worker has the task spec injected; if it needs more, it starts with the task thread (`read_task_messages`, `get_pinned` on the task scope), widens to the parent conversation (`conversations`, `get_pinned` on the project scope), and searches for specifics only if those pulls leave something unresolved. The ordering is narrow → broad → searchable.

A human — usually through Claude.ai as an MCP client — typically starts at the broad scope. The conversation pin restores the project-level state. The raw stream on the conversation rebuilds the nuance the pin couldn't carry. Task threads are the drill-down for "what actually happened on that piece of work." Same tools, opposite traversal direction.

51 MCP tools total across the system. Six are retrieval-shaped. The rest do dispatch, worker callbacks, project management, file operations, and admin tasks.

## 6. Pins — curated artifacts at two scopes

A pin is the output of a deliberate act: the human and Claude work a topic together, Claude writes a spec-shaped message, the human pins it. The act is curation. The artifact is the pin. Mechanically it's a boolean on a message row; conceptually it's the system's answer to auto-compaction.

Two scopes, two characters.

**Conversation pin — the wider source of truth.** Pinned on a conversation thread, scoped to the project. Long-lived. Authoritative across tasks that reference the project. The conversation pin is where the architectural decisions live: "we're building X with Y, because of Z." It gets re-curated as decisions evolve; the previous pin auto-unpins but stays in the thread as timeline history. A reader returning to the project a month later starts here.

**Task spec — narrow and temporal.** Pinned on a task thread, scoped to a single unit of work. Bounded by the task's lifecycle. The task spec is the brief the worker was dispatched to execute: specific, checklist-able, testable. It doesn't survive the task; it lives as the task's pinned record and stops being actively referenced when the task completes. A worker pulls the task spec before anything else.

Mechanically both are the same thing — `pinned = TRUE` on a message row, with a partial index for fast lookup. The tool layer enforces one pin per thread; pinning a new message auto-unpins the previous. The retrieval re-rank applies the 1.3× boost. The UI shows pinned messages at the top.

The separation between scopes is by convention and thread-identity, not by schema. A conversation pin is a pinned message in a conversation thread; a task spec is a pinned message in a task thread. `get_pinned` takes a `conversation_id` or a `task_id` and returns the pin at that scope.

### How curation happens

There's no dedicated "curation" tool. The workflow rides on tools that exist for other reasons:

```
1. Human + Claude.ai work a topic across turns in a conversation or task thread.
2. At a decision point, human says "summarise what we've decided" (or similar).
3. Claude posts a message of type `spec`, `plan`, `review`, or `note`.
4. Human calls `pin` on that message via MCP.
5. Previous pin auto-unpins. Previous pin remains in the thread, unpinned.
```

The current pin outranks older history on retrieval. The unpinned predecessors remain searchable — the timeline preserves the lineage even though the canonical artifact has advanced.

Curation is distinct from auto-compaction in two ways. The author is a deliberate collaboration between human and model, not a unilateral compression pass. The artifact is a named, pinned, first-class record, not an implicit summary that replaces its source.

## 7. Backfill and the sentinel pattern

Two classes of backfill run periodically:

- **Chunk backfill.** For messages with content ≥ 500 chars and no row in `message_chunks` (either chunks or a sentinel). Chunks the message, writes either N chunks or a single `chunk_index = -1` sentinel.
- **Embedding backfill.** For messages with embeddable content and `embedding IS NULL` in `messages_vec`. Embeds and writes.

The sentinel pattern is load-bearing. Without it, the chunk backfill re-examines every short-or-unstructured message on every run. With it, a single write per message permanently marks "nothing to chunk here." The sentinel is invisible to search (filtered out by `chunk_index >= 0`) and cheap to carry.

## Alternatives considered

- **Pinecone / Qdrant / pgvector.** External vector store. Rejected because keeping embeddings inside SQLite lets the re-rank stage join vec results to message metadata (type, author, pinned status, conversation, project) in a single query. An external store forces a two-step fetch and duplicates the authoritative message data.
- **Auto-injection of pinned specs into worker prompts.** Tempting — guarantee the worker sees the spec. Rejected because pulling teaches the agent what the retrieval surface looks like; injection hides it. The worker that learns to pull can adapt when the spec changes mid-task.
- **Sliding-window chunking.** Standard RAG practice. Rejected because sliding windows sever reasoning at arbitrary token boundaries. Markdown-section chunks preserve the unit the author already marked. For content without headers, whole-message wins.
- **Compaction of old messages into summaries.** Rejected explicitly. Summaries destroy reasoning. The dev loop for planning depends on being able to retrieve *why* a decision was made; a summary crushes that into a sentence.
- **Single-tier embeddings (whole-message only, or chunks only).** Whole-message alone misses local precision — a 3,000-character spec has one vector for five distinct decisions. Chunks alone lose the "what's this conversation about" signal. Two tiers cost twice the indexing; they pay the cost back on recall.

## Tradeoffs

- **OpenAI dependency for embeddings.** External API, network, cost per message. Acceptable for current volume. An embedding-free mode works (FTS only) but loses semantic recall.
- **Fixed 1536-dim vectors.** Model switch requires re-embedding everything, or running Python cosine over mixed dimensions during migration. No online model versioning.
- **Hand-tuned type weights.** Not learned, no feedback loop. Occasionally feel wrong for a particular kind of query. The numbers are visible in one file and easy to adjust.
- **Pin is a single-writer concept.** One pin per conversation. For conversations with multiple authoritative artifacts (e.g. spec + implementation notes), the convention is to keep the spec pinned and link from it.
- **Oversample constants are magic.** 15× for messages, 10× for chunks/tasks, chosen empirically. A better retrieval harness would adapt these based on filter-selectivity estimates. Not worth the complexity at current scale.
- **Backfill is best-effort.** An interrupted chunk backfill can leave some messages unchunked until the next run. The sentinel pattern prevents duplicate work but doesn't make backfill atomic.

## Cross-cutting concerns

- **Auth.** Retrieval tools require an authenticated session (dashboard) or a valid Bearer JWT (MCP). Worker callbacks come in via the localhost `/mcp/worker` bypass; they're unauthenticated but constrained to tools the worker user is allowed to call.
- **Tenancy.** Single-operator. `conversation_id`, `project_id`, `task_id` scope retrieval. No cross-instance leakage because there's one instance.
- **Privacy.** Embedding calls send message content to OpenAI. Short messages and `test-result` types skip. Operators who need strict privacy set no OpenAI key; FTS-only mode is a supported degradation.
- **Observability.** Every tool call returns a consistent shape. Errors are logged to the service log and surfaced to the MCP client. Slow vec queries are logged with timing.
- **Cost.** Embedding cost scales with message volume, not query volume. A long planning day generates hundreds of messages but tens of thousands of query-time retrievals. `text-embedding-3-small` is cheap enough that cost hasn't been a constraint.

## Riff points

- Two kinds of material: raw stream (continuous) and curated artifacts (pinned, produced through collaboration).
- Curation is an act — human and Claude work a topic together, Claude writes spec-shaped output, human pins it. Distinct from auto-compaction.
- Pins come in two scopes: conversation pin (broad SOT, long-lived) and task spec (narrow, temporal). Same mechanism, different character.
- Re-curation replaces the pin; the previous pin stays in the thread as timeline history. Versioning is free.
- Two consumers, two traversal directions. Workers go narrow→broad; humans restoring state go broad→narrow.
- Workers get curated context injected; tools exist to pull more. Well-constructed spec = no pulling needed.
- Three-index rule: searchable content gets row + FTS mirror + vec0 mirror; plain tables for the rest.
- Two-grain embeddings at the unit the author already marked — whole message, or markdown-section chunk when the split makes sense.
- Sentinel rows (`chunk_index = -1`, `embedding = NULL`) make backfill idempotent without re-examining every message.
- Two-stage retrieval: KNN oversample → SQL filter → Python re-rank.
- Type weights encode the raw-vs-curated distinction. Specs 1.5×, reviews 1.4×, plans 1.1×, status 0.5×, test-result 0.3×.
- Pinned content gets an additional 1.3× multiplier on top of its type weight.
- Graceful degradation is a rule: vec optional, embeddings optional, dimension-fuzzy fallback always available. FTS is the floor.
