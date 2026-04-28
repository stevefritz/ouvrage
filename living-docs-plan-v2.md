# Living Docs v2 — Integration Plan

**Author:** cc-worker (`mcp-switchboard/living-docs-plan-v2`)
**Date:** 2026-04-28
**Supersedes:** `living-docs-plan.md` (v1, file id `832316bb-74a0-4aac-9fb2-6941a7db03ad`).
**Source of truth for intent:** pinned message #7864 in conversation `living-docs` ("Living Docs — design spec (canonical, v2)").

This plan integrates the v1 plan's correct findings with the v2 git-as-source-of-truth design, runs the new investigations the v2 spec calls out, and proposes a concrete implementation chain. The package is `ouvrage/`.

---

## 1. Confirmation of v1 findings carried forward

These v1 sections are still correct and are reused without re-investigation. They are referenced by section number; their content is not reproduced.

| v1 § | What carries forward | Adjustment for v2 |
|------|----------------------|--------------------|
| §1.1 | `tasks.merged_at` migration: add column, set at both merge call sites (`ouvrage/git/operations.py:459` and `ouvrage/dispatch/pr_sweep.py:108`), backfill `merged_at = pushed_at` for already-merged rows, add `list_merged_tasks_since(project_id, since_iso)` helper. | Keep verbatim. This is task #1 of the v2 chain. |
| §1.2 | `files.role TEXT NOT NULL DEFAULT 'upload'` column with partial index; `ValueError` guard in `db/files.py:delete_file` (`role='reference_doc'` → refuse direct deletion). Cascade via service-internal `delete_reference_doc_files()` that bypasses the guard. | Keep verbatim. The role is now applied to the **local cache file row** (the only `files` row that exists per slug), since v2 has no version table. |
| §1.3 | File embedding infrastructure: `files_embeddings`, `files_vec`, `file_chunks`, `file_chunks_vec` tables. `index_doc_file(file_id)` mirrors `index_message_chunks` (`ouvrage/db/search.py:443`). Backfill loop mirrors `_backfill_*` patterns (`ouvrage/server/app.py:485–499`). vec0 delete triggers mirror `chunks_vec_delete` (`ouvrage/db/schema.py:808`). Chunker contract preserved (`ouvrage/embeddings/chunks.py:8` — returns `None` when content < 500 chars / no `## ` or `### ` headers / one section). | Keep verbatim. The "current version" file_id query in §1.3 changes — there's no version table. The `scope=docs` query becomes `SELECT id FROM files WHERE role='reference_doc' AND project_id = ?` (one row per slug). |
| §1.4 | Search weight ratios — current docs **1.6**, task `.md` artifacts **1.0**, prose messages unchanged. Plug into `_handle_search` (`ouvrage/server/handlers/search.py:96`); extend `_VALID_ENTITY_TYPES` (`ouvrage/db/search_weights.py:6`). | Keep the 1.6 / 1.0 weights. **Drop** the **0.5 prior-version weight** and the **`doc_chunk` entity type** — there are no prior versions in v2 (git owns history). New entity type set: `{"task", "message", "chunk", "reference_doc"}`. |
| §3 (service shape) | `LivingDocsService` lives in `ouvrage/services/living_docs.py`. Stateless. Delegates: DB → `ouvrage/db/reference_docs.py`, files → `db.create_file` / UPLOADS_DIR, embeddings → `index_doc_file` (fire-and-forget), dispatch → `engine.dispatch_task`. | Method names mostly carry over. `add_version` becomes a **copy-from-worktree** operation. `delete_config` cascades to local cache file + embeddings (no version rows to clean up). Drop the `current_version_id` advancement logic. Drop `staleness()` — v2 has no `min_merges_to_regen` / `max_age_days` heuristic; cron uses a flat per-project interval. |
| §5 (system prompt) | The voice (terse, present-tense, technical), audience framing, citation rules (three forms: `code:<path>@<sha>`, `conversation:<id>#<message_id>`, `task:<id>`), required `## ` sections, Mermaid + ASCII conventions, anti-changelog framing, "if you cannot ground a claim, OMIT THE CLAIM" rule. | Re-drafted in §7 below. The prompt now: (a) operates on a **worktree on a branch cut from default** with existing docs at `{reference_doc_path}/{slug}.md` already present; (b) reads the existing file at HEAD as authoritative baseline (preserve human edits where still correct); (c) writes updated docs to the worktree path; (d) calls `add_reference_doc_version(task_id, slug, source_path)` with **no `unchanged` flag** — agent simply doesn't call the tool for slugs it didn't update; (e) does **not** commit/push (standard lifecycle handles that). |
| §6.1 (cron) | New file `ouvrage/dispatch/living_docs_sweep.py` modeled on `pr_sweep.py:90`. Started in lifespan via `asyncio.create_task()` at `ouvrage/server/app.py:500`-vicinity. | Keep the pattern. Cadence loop is a **flat outer interval** (e.g. 30 min); per-project trigger is gated by `living_docs_enabled=true` AND `(now - last_regen_at) >= regen_interval_hours`. Drop the v1 `min_merges_to_regen` heuristic. |
| §10 risks (subset) | Risks #1 (citation prompt drift), #2 (search weight calibration), #5 (regen cost monitoring via `tasks.total_cost_usd`). | Keep. |
| Path glossary (Appendix B) | `switchboard/...` → `ouvrage/...`. `embeddings/chunker.py` → `ouvrage/embeddings/chunks.py`. | Apply throughout. |

---

## 2. Investigations resolved by v2 spec

These v1 open questions are now answered by the v2 design. One line each.

| v1 question | v2 answer |
|-------------|-----------|
| Where do prior doc versions live? | **Git history.** No `reference_doc_versions` table. |
| How does `current_version_id` advance? | **It doesn't exist.** Config has `last_seen_sha`, `last_regen_at`, `last_regen_task_id` instead. |
| `add_reference_doc_version(unchanged=True)` row-with-no-file vs. lean alternative (v1 §10)? | **Lean wins.** Agent doesn't call the tool for unchanged slugs; the per-slug outcome is recorded in `reference_doc_runs.slugs_unchanged` JSON. |
| `system_prompt_prepend` dispatch parameter (v1 §6.3 / Risk #6)? | **Dropped.** Regen flow is a standard task; the prompt is embedded in the task spec body (pinned message at dispatch time). No new dispatch parameter. |
| Worktree isolation for regen — read main without merging it (v1 Risk #6)? | **Resolved.** The regen task IS in a worktree on its own branch, cut from default. Default-branch files are present from the start. No special checkout. |
| Per-config pause? | **Dropped.** Project-level `living_docs_enabled` kill switch only. |
| Project-level `living_docs_config` JSON blob? | **Dropped.** Three discrete columns: `living_docs_enabled BOOLEAN`, `reference_doc_path TEXT`, `living_docs_regen_interval_hours INTEGER`. |
| Project-level exemplars? | **Dropped.** System exemplars only (1–2 markdown files shipped in `ouvrage/services/living_docs/exemplars/`). Once a project has v1+ docs in git, those serve as shape reference for subsequent regens. |
| `unchanged=True` ratio monitoring (v1 Risk #4)? | Re-derived from `reference_doc_runs.slugs_unchanged` over time. The metric still matters; the source is the audit log, not version rows. |
| Auto-merge for regen PRs? | **No.** Auto-PR only. User decides via PR review. Rejected PRs are reconciled by the next regen cycle. |
| UI scope? | **In the same chain.** UI tasks are part of the implementation chain, not deferred. |
| Doc encoding risk (v1 §10)? | Reframed. Worker writes to git path inside the worktree. The validator on `add_reference_doc_version` should warn (not reject) on non-chunkable structure; the whole-file embedding still makes the doc searchable. |

---

## 3. New investigations required (v2)

Each item from the spec's "What's new in v2" section, investigated against actual code.

### 3.1 Default-branch checkout in current dispatch infra

**Question:** Does the regen worker land on a branch cut from the project's default branch with default's files present in the worktree?

**Answer: Yes.** Verified at `ouvrage/git/worktree.py:251–281`:

```python
# Priority: depends_on (chain from parent) > base_branch (explicit) > origin/{default}
base_ref = f"origin/{default_branch}"
if depends_on:
    parent_task = await db.get_task(depends_on)
    if parent_task and parent_task.get("branch"):
        base_ref = f"origin/{parent_task['branch']}"
        ...
elif base_branch:
    base_ref = base_branch if base_branch.startswith("origin/") else f"origin/{base_branch}"
...
stdout, stderr, rc = await _run_as_worker(
    "git", "-C", bare_path, "worktree", "add",
    "-b", branch, worktree_path, base_ref,
)
```

For a regen task with no `depends_on` and no explicit `base_branch`, `base_ref = origin/{project.default_branch}`. `git worktree add -b {branch} {path} origin/main` materializes all default-branch files into the worktree at `{worktree_path}`. So an existing `docs/reference/architecture.md` on `main` is already present at `{worktree_path}/docs/reference/architecture.md` when the worker starts. **No special checkout logic needed.** The system prompt simply tells the worker: "read existing docs at `{reference_doc_path}/{slug}.md` if present."

### 3.2 `auto_pr=True` interaction with task lifecycle

**Question:** How does `auto_pr=True` behave for tasks that may produce zero commits (when every doc is unchanged)?

**Answer: Safe, but a no-op PR attempt is logged.** Trace via `ouvrage/dispatch/engine.py:174–184` and `ouvrage/git/operations.py:283–352`:

- `_maybe_create_pr` runs only at chain tail with no waiting dependents (`engine.py:175–177`).
- It fetches `task.worktree_path` and `task.branch`; returns silently if either missing (`operations.py:301–305`).
- It walks the chain, builds `title = task.goal[:70]` and `body = "## Summary\n" + chain goals` (`operations.py:320–325`).
- `provider.create_pr(...)` is invoked unconditionally — there is **no diff/commit-count gate**.

**Implication for regen tasks that produce no commits:** `_ensure_branch_pushed` returns `True` early when `git log origin/{branch}..HEAD --oneline` is empty (`operations.py:167–171`), so push is skipped. The PR creation then attempts to open a PR with `head` and `base` pointing to the same commit; GitHub returns `422 "no commits between base and head"`, which is logged as `"PR creation failed"` via `db.post_task_message(...)` (`operations.py:347–351`). The task still completes successfully.

**Recommended handling for v2:** before the worker exits, the regen prompt instructs it to skip the standard `add` + `commit` pattern when no slug was updated. The standard lifecycle's "always push" instruction in `sdk_session.py:297–307` is benign (push of zero commits is a no-op). The "PR creation failed" status message for no-op runs is **acceptable noise** — but we should also detect this case in the completion hook (§8) and either suppress the failure message or surface it cleanly in `reference_doc_runs.outcome='unchanged'`.

**PR title/body convention (v2):** because `_maybe_create_pr` derives title from `task.goal[:70]` and body from chain goals, the regen-task spec should use:

- **Goal:** `Living Docs regen — {project_id}` (under 70 chars; truncates to e.g. `Living Docs regen — mcp-switchboard`)
- **Body:** the standard chain summary works. The completion hook (§8) appends a follow-up task message after the run row is inserted, listing slugs changed with the run id, so reviewers can correlate the PR to the audit row.

### 3.3 Standard task body / system prompt construction

**Question:** Is there a `system_prompt_prepend` or similar dispatch-time injection point? If not, where does the regen prompt live?

**Answer: No dynamic prepend exists. Use the spec body.** Verified at `ouvrage/dispatch/sdk_session.py:90–349`:

- The CC prompt is constructed once at dispatch time by `_build_task_prompt`.
- The pinned **task spec message** (read from the `messages` table) is rendered into the prompt verbatim (sdk_session.py:160–163).
- The task `goal` is rendered as `# Task: {task['goal']}` (sdk_session.py:155).
- Task files are listed in a "Reference Files" section (sdk_session.py:210–220).
- There is **no** `system_prompt_prepend` parameter on `dispatch_task`.

**Recommendation:** Drop v1's plan to add `system_prompt_prepend` to the dispatch API. Instead, the regen prompt is composed in `LivingDocsService.regenerate(project_id)` and posted as the task's pinned spec message before dispatch (this is the standard task pattern). System exemplars are referenced inline in the prompt (their relative paths from the repo root, e.g. `ouvrage/services/living_docs/exemplars/architecture.example.md`); the worker can `cat` them from the worktree as part of normal context-gathering.

### 3.4 Dashboard: where the reference docs UI plugs in

**Files inventoried:**

```
dashboard/
├── ouvrage-app.js              # SPA shell; mounts at #ouvrage-root
├── router.js                   # Hash router; #/project/{id}/{tasks|conversations|files|settings}
├── views/
│   ├── ProjectView.js          # Owns the project tabs + SettingsTab inline
│   ├── FilesTab.js             # Project-scoped files view (under /files tab)
│   ├── TaskView.js, TaskCreateView.js, ConversationView.js, ConversationIndex.js,
│   │   LandingView.js, LoginView.js, ProjectCreateView.js
└── components/
    ├── Files.js                # Global cross-project files page
    ├── Settings.js             # Global settings (instance-level)
    ├── FormKit.js              # FormField wrapper, toggles
    ├── ProjectHeader.js, TaskList.js, TaskRow.js, FilterBar.js, GateDots.js,
    │   ChainBadge.js, StatusDot.js, Tag.js, MarkdownLightbox.js, ImageLightbox.js,
    │   TrialBanner.js, ProjectLimitBanner.js, utils.js
```

- **Router** (`dashboard/router.js:36`): `#/project/{id}/files` → `view='project', tab='files'`. Already wired.
- **Project tab dispatch** (`dashboard/views/ProjectView.js:1009-1010`): `activeTab === 'files'` renders `<FilesTab projectId=...>`.
- **SettingsTab** lives inline in `ProjectView.js:274–763`. Existing toggle pattern at lines 674–704 (Auto Test, Auto Review, Auto PR, Auto Merge), with `toggleRowStyle` / `toggleLabelStyle` / `toggleSubStyle` from lines 388–402. The kill switch (`living_docs_enabled`), regen interval, and `reference_doc_path` go here.

**Reference docs UI plugs in:**
- New `<ReferenceDocsSection>` component (or sub-section) inside `dashboard/views/FilesTab.js` rendered above the existing `UploadZone`. Shows: configs list (slug + title + last_regen_at + last_seen_sha + edit/delete), run history (last 20 runs from `reference_doc_runs`), manual regen button.
- Per-config edit/create/delete modals (or inline forms) follow the `FormField` pattern from `dashboard/components/FormKit.js` and the `DangerZone` pattern from `ProjectView.js:185–267`.
- Settings additions go into `ProjectView.js`'s SettingsTab inside or after the existing Advanced section.

**REST API surface in `ouvrage/dashboard/api.py`:**
- Existing `_handle_update_project` (line 586) already accepts arbitrary updatable fields — `living_docs_enabled`, `reference_doc_path`, `living_docs_regen_interval_hours` flow through it after we add them to the projects table and `_decode_project()`.
- New routes needed (added to the dispatcher at `api.py:99–100`):
  - `GET /dashboard/api/projects/{id}/reference_docs` → list configs + last 20 runs
  - `GET /dashboard/api/projects/{id}/reference_docs/{slug}/content` → return the local cache file content for the modal preview
  - `POST /dashboard/api/projects/{id}/reference_docs` → upsert config (proxies to MCP `set_reference_doc_config`)
  - `DELETE /dashboard/api/projects/{id}/reference_docs/{slug}` → delete config (proxies to `delete_reference_doc_config`)
  - `POST /dashboard/api/projects/{id}/reference_docs/regenerate` → dispatch a regen task (proxies to `regenerate_reference_docs`)

**Test infra:**
- `tests/test_files_api.py` (75K) — existing CRUD coverage to mirror
- `tests/test_visual_check.py` + `fixtures/visual/*.json` — visual snapshot harness; `scripts/visual-check.py` regenerates PNGs
- New fixtures required: `fixtures/visual/project-reference-docs.json`, `fixtures/visual/project-reference-docs-empty.json`, `fixtures/visual/project-settings-livingdocs.json`
- Playwright tests in `tests/test_dashboard_*.py` (existing pattern); add coverage for: list configs, manual regen, delete config (with confirm), edit modal, view local copy, kill-switch toggle.

### 3.5 Worker-only `add_reference_doc_version` server-side copy logic

**Pattern verified — reuse `add_task_file`'s shape.**

Worker check (verbatim from `ouvrage/server/handlers/files_handler.py:90–91`):
```python
if not get_request_is_worker():
    raise ValueError("add_task_file is only available on the worker endpoint")
```

Path validation pattern (verbatim from `files_handler.py:116–122`):
```python
real_src = src.resolve()
real_worktree = Path(worktree_path).resolve()
try:
    real_src.relative_to(real_worktree)
except ValueError:
    raise ValueError("Source path must be within the worktree")
```

Worker enforcement (`ouvrage/server/app.py:128–142`): `WORKER_TOOL_ALLOWLIST` (set at `ouvrage/server/tools.py:1056`) is checked twice — once when listing tools and once at dispatch. Add `"add_reference_doc_version"` to the set.

**Server-side action for `add_reference_doc_version(task_id, slug, source_path)`:**
1. Worker check (above).
2. `task = await db.get_task(task_id)`; resolve `worktree_path`.
3. Validate `source_path.resolve().relative_to(worktree_path.resolve())`. Validate `.md` extension. Validate file size < 1MB (sanity).
4. Look up `config = await db.get_reference_doc_config(project_id=task.project_id, slug=slug)`. **Refuse if missing** — configs must be created via `set_reference_doc_config` first. ValueError with clear message.
5. Compute target: `dest = data/reference_docs/{project_id}/{slug}.md`. `mkdir -p` the parent.
6. Read the source file; write atomically to `dest` (write to `dest.tmp`, rename).
7. Look up or create the `files` row keyed by `(project_id, role='reference_doc', slug)`. UNIQUE constraint catches re-call. Upsert pattern: `INSERT … ON CONFLICT(project_id, slug) WHERE role='reference_doc' DO UPDATE SET stored_path = excluded.stored_path, size_bytes = …, updated_at = now`.
8. Fire `asyncio.create_task(index_doc_file(file_id))`.
9. Validate chunkability: run the chunker once and **warn** (post a status message on the task) if it returns `None` (likely single-section). Do NOT reject. The whole-file embedding still makes the doc searchable.
10. Return `{"file_id": file_id, "stored_path": dest, "embedded": "queued"}`.

**Idempotent on re-call** for same `(project_id, slug)` — the upsert plus `index_doc_file` deletes prior chunks (mirrors `index_message_chunks`'s "delete prior, re-insert" pattern at `ouvrage/db/search.py:443`).

### 3.6 `delete_reference_doc_config` cascade

**Steps (in order):**
1. Look up `config = await db.get_reference_doc_config(project_id, slug)`. Return 404-shape if missing.
2. Look up `file = await db.get_file_by_role(project_id, role='reference_doc', slug=slug)`.
3. If `file` exists: call `db.delete_reference_doc_files(file.id)` — internal helper that bypasses the `ValueError` guard in `delete_file`. The DELETE cascades through `files_embeddings` / `file_chunks` / `files_vec` / `file_chunks_vec` via `ON DELETE CASCADE` on `files(id)` and the vec0 delete triggers.
4. Unlink `data/reference_docs/{project_id}/{slug}.md` if it exists (silently ignore ENOENT).
5. `DELETE FROM reference_doc_configs WHERE id = ?`.
6. Does **NOT** touch git. The committed `.md` file in the repo is left for the human to clean up. Document this in the tool description.

`reference_doc_runs` rows referencing the config are kept (audit log is append-only). The `slugs_*` JSON arrays may reference a slug whose config no longer exists — UI handles this gracefully.

### 3.7 `set_living_docs_enabled` semantics

- Trivial setter on `projects.living_docs_enabled`. Called by user (dashboard or MCP).
- **In-flight regen tasks complete normally.** The flag is only read by the cron sweep at the start of each iteration. A task already dispatched does not get cancelled.
- Manual regen via `regenerate_reference_docs(project_id)` **ignores** the flag — kill switch only stops the cron.
- The dashboard SettingsTab toggle calls `PATCH /dashboard/api/projects/{id}` with `{"living_docs_enabled": bool}` → existing `_handle_update_project` plumbing.

### 3.8 System exemplars — location and shape

**Location:** `ouvrage/services/living_docs/exemplars/`

Shipped files (initial cut, two exemplars; we already have a strong example in this repo's `CLAUDE.md`):
- `ouvrage/services/living_docs/exemplars/architecture.example.md` — exemplar of an "architecture" slug for a hypothetical project. Demonstrates the required `## ` sections (Overview / Architecture / Data flow / Interfaces / Risks & gotchas / Recent changes / Open questions), Mermaid state-machine diagram, ASCII subsystem sketch, and citation forms.
- `ouvrage/services/living_docs/exemplars/data-model.example.md` — exemplar of a "data-model" slug. Demonstrates ER-style Mermaid, table-of-tables structure, and prose around invariants.

**How the regen prompt references them:** the prompt enumerates their relative paths and instructs the worker to `cat` them as the canonical "shape and voice" reference when no project-level baseline exists for a slug. Once the project has v1+ of its own docs in git, the prompt explicitly says "prefer the project's existing docs as shape reference; system exemplars are fallback only."

The exemplars are **not** loaded into the prompt verbatim (they're kilobytes; the prompt is meant to be terse). They're listed by path; the worker reads them on demand. This matches the existing "Reference Files" pattern at `sdk_session.py:210–220`.

---

## 4. Schema details (v2)

All migrations append to the existing dynamic-migration block in `ouvrage/db/schema.py`. **Idempotent** (use `CREATE TABLE IF NOT EXISTS`, `CREATE INDEX IF NOT EXISTS`, and column-existence guards with `PRAGMA table_info`).

### 4.1 `tasks.merged_at` (carried from v1 §1.1)

```sql
ALTER TABLE tasks ADD COLUMN merged_at TIMESTAMP;
UPDATE tasks SET merged_at = pushed_at
 WHERE pr_status = 'merged' AND merged_at IS NULL AND pushed_at IS NOT NULL;
```

Add `merged_at` to `TASK_MUTABLE_FIELDS` (`ouvrage/config/constants.py:53`). Set at:
- `ouvrage/git/operations.py:459` — auto-merge path
- `ouvrage/dispatch/pr_sweep.py:108` — sweep-detected merge path

Add helper `list_merged_tasks_since(project_id, since_iso)` to `ouvrage/db/tasks.py` (SQL given verbatim in v1 §1.1).

### 4.2 `files.role` (carried from v1 §1.2)

```sql
ALTER TABLE files ADD COLUMN role TEXT NOT NULL DEFAULT 'upload';
CREATE INDEX IF NOT EXISTS idx_files_role
  ON files(role) WHERE role = 'reference_doc';
```

`ValueError` guard in `ouvrage/db/files.py:delete_file` (verbatim shape in v1 §1.2). Internal `delete_reference_doc_files(file_id)` helper bypasses the guard for service-driven cascade.

### 4.3 `reference_doc_configs`

```sql
CREATE TABLE IF NOT EXISTS reference_doc_configs (
    id                   TEXT PRIMARY KEY,                   -- uuid
    project_id           TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    slug                 TEXT NOT NULL,
    title                TEXT NOT NULL,
    brief                TEXT NOT NULL,
    source_hints         TEXT,                                -- nullable; user prose
    last_seen_sha        TEXT,                                -- nullable until first regen
    last_regen_at        TIMESTAMP,
    last_regen_task_id   TEXT REFERENCES tasks(id) ON DELETE SET NULL,
    created_by           INTEGER REFERENCES users(id) ON DELETE SET NULL,
    created_at           TIMESTAMP NOT NULL DEFAULT (datetime('now')),
    updated_at           TIMESTAMP NOT NULL DEFAULT (datetime('now')),
    UNIQUE (project_id, slug)
);
CREATE INDEX IF NOT EXISTS idx_refdoc_configs_project ON reference_doc_configs(project_id);
```

Slug regex (enforced in MCP handler, not DB): `^[a-z0-9][a-z0-9-]{0,63}$`.

### 4.4 `reference_doc_runs`

```sql
CREATE TABLE IF NOT EXISTS reference_doc_runs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id        TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id           TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    commit_sha        TEXT,                                  -- head sha after task commit; NULL if no changes
    outcome           TEXT NOT NULL CHECK (outcome IN ('updated','unchanged','failed')),
    slugs_changed     TEXT NOT NULL DEFAULT '[]',            -- JSON array
    slugs_unchanged   TEXT NOT NULL DEFAULT '[]',            -- JSON array
    error_message     TEXT,                                   -- non-null when outcome='failed'
    ran_at            TIMESTAMP NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_refdoc_runs_project_ranat
  ON reference_doc_runs(project_id, ran_at DESC);
CREATE INDEX IF NOT EXISTS idx_refdoc_runs_task ON reference_doc_runs(task_id);
```

Append-only. The completion hook (§8) inserts one row per regen task.

### 4.5 Project columns

```sql
ALTER TABLE projects ADD COLUMN living_docs_enabled BOOLEAN NOT NULL DEFAULT 0;
ALTER TABLE projects ADD COLUMN reference_doc_path TEXT NOT NULL DEFAULT 'docs/reference';
ALTER TABLE projects ADD COLUMN living_docs_regen_interval_hours INTEGER NOT NULL DEFAULT 24;
```

`_decode_project()` at `ouvrage/db/projects.py:67` doesn't need changes (these are scalar columns, not JSON). Add the keys to `PROJECT_MUTABLE_FIELDS` if such a list exists; otherwise verify `_handle_update_project` (`ouvrage/dashboard/api.py:586`) accepts them.

### 4.6 File embedding tables (carried from v1 §1.3)

```sql
CREATE TABLE IF NOT EXISTS files_embeddings (
    file_id    TEXT PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    embedding  BLOB,
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE VIRTUAL TABLE IF NOT EXISTS files_vec USING vec0(embedding float[1536]);
-- rowid in files_vec = files.rowid

CREATE TABLE IF NOT EXISTS file_chunks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    file_id     TEXT    NOT NULL REFERENCES files(id) ON DELETE CASCADE,
    chunk_index INTEGER NOT NULL,
    heading     TEXT,
    content     TEXT    NOT NULL,
    embedding   BLOB,
    created_at  TEXT    DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_file_chunks_file ON file_chunks(file_id);
CREATE VIRTUAL TABLE IF NOT EXISTS file_chunks_vec USING vec0(embedding float[1536]);

-- vec0 cleanup triggers (mirror messages_vec_delete / chunks_vec_delete at schema.py:797)
CREATE TRIGGER IF NOT EXISTS files_vec_delete AFTER DELETE ON files BEGIN
    DELETE FROM files_vec WHERE rowid = old.rowid;
END;
CREATE TRIGGER IF NOT EXISTS file_chunks_vec_delete AFTER DELETE ON file_chunks BEGIN
    DELETE FROM file_chunks_vec WHERE rowid = old.id;
END;
```

The vec0 declarations sit inside the existing `try/except` block in `ouvrage/db/schema.py:638–660` and are gated by `VEC_AVAILABLE`. Update the `if len(vec_tables_for_triggers) == 3:` block to `== 5:` to register the two new triggers.

### 4.7 Migration ordering

Within the dynamic-migration block in `schema.py`, run in this order (each step idempotent):

1. `tasks.merged_at` ALTER + backfill UPDATE
2. `files.role` ALTER + partial index
3. `projects.living_docs_*` ALTERs (×3)
4. `reference_doc_configs` CREATE + index
5. `reference_doc_runs` CREATE + indexes
6. `files_embeddings` CREATE
7. `file_chunks` CREATE + index
8. `files_vec`, `file_chunks_vec` CREATE VIRTUAL (gated by `VEC_AVAILABLE`)
9. `files_vec_delete`, `file_chunks_vec_delete` CREATE TRIGGER (gated by `VEC_AVAILABLE`)

### 4.8 Indexes summary

- `idx_refdoc_configs_project` — fast list-by-project
- `idx_refdoc_runs_project_ranat` — fast last-N-runs query for UI history
- `idx_refdoc_runs_task` — completion-hook lookup
- `idx_files_role` (partial, `WHERE role='reference_doc'`) — fast `scope=docs` candidate retrieval and `delete_reference_doc_config` lookup
- `idx_file_chunks_file` — chunk-by-file lookup mirroring `idx_message_chunks_message`

---

## 5. Service class shape (v2)

New file: `ouvrage/services/living_docs.py`. Stateless. Delegates DB → `ouvrage/db/reference_docs.py` (new); files → `db.create_file` / UPLOADS_DIR convention; embeddings → `index_doc_file` (fire-and-forget); dispatch → `engine.dispatch_task`.

```python
class LivingDocsService:
    """v2 — git-as-source-of-truth orchestration.

    DB ops: ouvrage.db.reference_docs.* (configs CRUD + runs append-only).
    Local cache: data/reference_docs/{project_id}/{slug}.md (managed by add_version).
    Embeddings: ouvrage.db.search.index_doc_file via asyncio.create_task.
    Dispatch: ouvrage.dispatch.engine.dispatch_task.
    No version table, no current_version_id, no staleness heuristic.
    """

    LOCAL_ROOT = Path(os.environ.get("OUVRAGE_LIVING_DOCS_ROOT",
                                     "data/reference_docs"))

    # --- config CRUD ---
    async def set_config(self, project_id: str, slug: str, title: str,
                         brief: str, source_hints: str | None = None,
                         user_id: int | None = None) -> dict:
        """Upsert reference_doc_configs by (project_id, slug). Slug regex enforced."""

    async def get_config(self, project_id: str, slug: str) -> dict | None: ...
    async def list_configs(self, project_id: str) -> list[dict]: ...
    async def delete_config(self, project_id: str, slug: str) -> None:
        """Cascade: drop files row (via internal bypass helper) → embeddings cascade
           → unlink local cache file → DELETE config row. Does NOT touch git."""

    # --- worker-only version add (the v2 simplification) ---
    async def add_version(self, *, task_id: str, slug: str,
                          source_path: str) -> dict:
        """Server-side action when worker calls add_reference_doc_version.
           1) Resolve task → worktree_path → project_id.
           2) Validate source_path within worktree (resolve+relative_to).
           3) Validate .md extension and size < 1MB.
           4) Refuse if config (project_id, slug) doesn't exist.
           5) Atomic copy to LOCAL_ROOT/{project_id}/{slug}.md.
           6) Upsert files row keyed (project_id, slug, role='reference_doc').
           7) Fire-and-forget asyncio.create_task(index_doc_file(file_id)).
           8) Warn (status message, not exception) if chunker returns None.
           Idempotent on re-call: upsert path, embeddings replace prior chunks.
        """

    # --- dashboard / LLM read access ---
    async def get_local_copy(self, project_id: str, slug: str) -> str | None:
        """Read LOCAL_ROOT/{project_id}/{slug}.md, return text or None."""

    # --- runs (audit log) ---
    async def list_runs(self, project_id: str, limit: int = 20) -> list[dict]: ...

    # --- regen orchestration ---
    async def regenerate(self, *, project_id: str,
                         user_id: int | None = None) -> dict:
        """
        1) Load configs for project (refuse if zero).
        2) Build the regen prompt (§7) including all configs + system exemplar paths.
        3) Resolve project default branch (project.default_branch).
        4) Dispatch standard task via engine.dispatch_task with:
             goal=f"Living Docs regen — {project_id}"  (under 70 chars)
             tags=["living-docs"]
             model="opus"
             auto_pr=True, auto_merge=False, auto_test=False, auto_review=False
             max_turns=120, max_wall_clock=30
             spec=<the prompt body, posted as the pinned task message>
        5) Return the dispatched task id.
        """

    # --- completion hook (called by lifecycle on gate-pass) ---
    async def on_regen_complete(self, *, task_id: str) -> None:
        """
        Called from lifecycle._check_gates_passed when task tags contain 'living-docs'.
        1) Read task → project_id, branch, head sha (git rev-parse).
        2) Compute slugs_changed: which configs got an add_reference_doc_version call
           in this task (via files row updated_at within task window).
        3) Compute slugs_unchanged: configured slugs not in slugs_changed.
        4) Insert reference_doc_runs row with outcome ∈ {'updated','unchanged','failed'}.
        5) Update each touched config: last_seen_sha, last_regen_at, last_regen_task_id.
        """
```

The internal DB helpers in `ouvrage/db/reference_docs.py`:
```python
# config
async def upsert_config(...): ...        # ON CONFLICT DO UPDATE on (project_id, slug)
async def get_config(...): ...
async def list_configs(project_id): ...
async def delete_config_row(id): ...     # raw row delete, no cascade logic
async def update_config_meta(id, *, last_seen_sha=None,
                             last_regen_at=None, last_regen_task_id=None): ...

# runs
async def insert_run(project_id, task_id, commit_sha, outcome,
                     slugs_changed, slugs_unchanged, error_message=None): ...
async def list_runs(project_id, limit): ...
async def get_runs_by_task(task_id): ...

# files (cascade bypass)
async def delete_reference_doc_files(file_id): ...  # bypasses ValueError guard
```

---

## 6. MCP tool specs (v2)

All tools route through `ouvrage/server/dispatch.py:TOOL_HANDLERS`. Schemas in `ouvrage/server/tools.py`. Handlers in new file `ouvrage/server/handlers/living_docs_handler.py`.

### 6.1 `set_reference_doc_config` (user + worker)

Inputs:
- `project_id` (string, required)
- `slug` (string, required, regex `^[a-z0-9][a-z0-9-]{0,63}$`)
- `title` (string, required, ≤200 chars)
- `brief` (string, required, ≤8000 chars) — semantic description of what to cover
- `source_hints` (string, optional, ≤4000 chars) — curated file/area hints

Behavior: idempotent upsert keyed on `(project_id, slug)`. Returns the row.

Validation:
- Slug regex via `re.match`. ValueError with clear message on mismatch.
- Project must exist; ValueError if not.

### 6.2 `delete_reference_doc_config` (user-only)

Inputs:
- `project_id` (string, required)
- `slug` (string, required)

Behavior: Cascade per §3.6. Worker-callable would be a foot-gun; restrict to the user endpoint (default — not added to `WORKER_TOOL_ALLOWLIST`).

### 6.3 `set_living_docs_enabled` (user-only)

Inputs:
- `project_id` (string, required)
- `enabled` (boolean, required)

Behavior: trivial setter on `projects.living_docs_enabled`. Returns `{enabled: bool}`. Per §3.7, does **not** affect in-flight tasks.

### 6.4 `regenerate_reference_docs` (user-only)

Inputs:
- `project_id` (string, required)

Behavior:
- Refuses if project has zero configs (ValueError "No reference doc configs to regenerate. Use set_reference_doc_config first.").
- Calls `LivingDocsService.regenerate(project_id=...)`.
- Returns `{task_id, dispatched_at}`.
- **Ignores** `living_docs_enabled` (kill switch only stops cron, not manual triggers).
- Note: v1 had per-config and `force` parameters. **Dropped.** v2's regen task is project-scoped (one task regenerates all configs); per-slug targeting is unnecessary because the agent decides per-slug whether to update.

### 6.5 `add_reference_doc_version` (worker-only)

Inputs:
- `task_id` (string, required) — the dispatching regen task
- `slug` (string, required) — natural key (drop `config_id` per v2)
- `source_path` (string, required) — absolute path within the worktree to the new `.md`

Behavior: Per §3.5. Add to `WORKER_TOOL_ALLOWLIST` at `ouvrage/server/tools.py:1056`.

Validation:
- Worker check.
- `source_path.resolve().relative_to(task.worktree_path.resolve())`.
- `.md` suffix.
- Size < 1MB.
- Config `(project_id, slug)` must exist. ValueError if missing — instructs worker that configs must be set up first; don't auto-create.

Returns: `{file_id, stored_path, embedded: "queued", chunkable: bool}`.

### 6.6 `list_reference_doc_configs` (user + worker, read)

Inputs:
- `project_id` (string, required)

Returns: list of config rows + `last_seen_sha`, `last_regen_at`, `last_regen_task_id`. The dashboard and the regen prompt both consume this.

### 6.7 `get_reference_doc_config` (user + worker, read)

Inputs:
- `project_id` (string, required)
- `slug` (string, required)

Returns: full config row + computed `local_copy_present` boolean. Used by the per-config edit modal and by the regen prompt to verify a config exists before writing.

### 6.8 Routing

In `ouvrage/server/dispatch.py:71`, extend `TOOL_HANDLERS`:
```python
TOOL_HANDLERS = {
    ...,
    "set_reference_doc_config":   _handle_set_reference_doc_config,
    "delete_reference_doc_config":_handle_delete_reference_doc_config,
    "set_living_docs_enabled":    _handle_set_living_docs_enabled,
    "regenerate_reference_docs":  _handle_regenerate_reference_docs,
    "add_reference_doc_version":  _handle_add_reference_doc_version,
    "list_reference_doc_configs": _handle_list_reference_doc_configs,
    "get_reference_doc_config":   _handle_get_reference_doc_config,
}
```

In `ouvrage/server/tools.py`, add a new `LIVING_DOCS_TOOLS` group; concatenate it into `TOOLS`. Add `"add_reference_doc_version"` to `WORKER_TOOL_ALLOWLIST`.

---

## 7. System prompt for regen (v2)

The full prompt body. Composed in `LivingDocsService.regenerate(...)` and posted as the **pinned task spec message** before dispatch. Three template sections + per-config blocks + system exemplar paths.

```markdown
# Living Docs regen — {project_id}

You are the **Living Docs regenerator** for the Ouvrage project `{project_id}`.
Your job is to refresh the project's curated reference docs based on the current
state of the codebase on `{default_branch}` and any human edits already in git.

## How this works

You are running in a standard task worktree. Your branch was just cut from
`origin/{default_branch}`. Existing reference docs (if any) are already present at
`{reference_doc_path}/{slug}.md` for each configured slug — read them as the
authoritative baseline.

## Your output

For each slug below, you will either:

1. **Update** — write a new version of the doc to
   `{worktree_root}/{reference_doc_path}/{slug}.md`, then call:
   `add_reference_doc_version(task_id="{task_id}", slug="{slug}", source_path="<absolute path you just wrote>")`
   The server copies the file to Ouvrage's local cache and re-embeds it for search.
   You then `git add` the file. The standard task lifecycle commits, pushes,
   and opens a PR after you finish.

2. **Leave unchanged** — do not write the file, do not call the tool. The slug
   will be recorded as unchanged in the run audit log.

You MUST process every configured slug. UNCHANGED is a valid outcome — see criteria below.

## Audience and voice

- **Audience:** a future contributor who has read the project's `CLAUDE.md` but
  not this specific reference doc.
- **Voice:** terse, present-tense, technical. No marketing copy.
- **Length:** ~600–1500 words per doc unless the topic genuinely warrants more.
- **Anti-pattern:** you are NOT writing release notes. You are NOT writing a
  changelog. You are refreshing a stable reference document that is the first
  place to look for understanding this slice of the project.

## Reconciliation rule (preserve human edits)

If the existing file at `{reference_doc_path}/{slug}.md` exists, treat it as
**authoritative baseline**. Humans may have edited it directly in git. Your job:

- Only rewrite a section when its **content is wrong** (because the underlying
  code/behavior changed) or when a new fact must be added.
- Preserve the existing wording, structure, and section ordering where it is
  still correct. Do not rewrite for stylistic preference.
- If the only change is to add a "Recent changes" bullet, that's fine — that's
  what the section is for. But do not regenerate prose around it.

## Required `## ` sections (per doc)

Every reference doc has these top-level `## ` sections, in this order:

1. **Overview** — what this slice does and why it exists, in 2–4 sentences.
2. **Architecture** — components, modules, classes that matter. Mermaid diagram
   when there's a state machine, flow, or ER relationship to depict.
3. **Data flow** — how data moves through the slice (request → response, or
   producer → consumer). ASCII sketch when it helps.
4. **Interfaces** — public surface: MCP tools, REST endpoints, function
   signatures, config knobs, env vars.
5. **Risks & gotchas** — invariants that can be violated, footguns, debugging
   tips, rate limits, concurrency hazards.
6. **Recent changes** — bullet list of meaningful changes since the last regen
   (commits / PRs / merged tasks). Cite shas and tasks.
7. **Open questions** — unresolved design questions or planned work.

You may add more `## ` sections if the topic demands. You may use `### ` freely
inside any `## ` section. Do **not** use `# ` (the file's identity is the title)
or `#### ` (the chunker doesn't split on those).

## Citation rules

Every non-trivial claim must cite its source. Three forms:

- `code:<path>@<sha>` — for code references. Example: `code:ouvrage/dispatch/lifecycle.py@7b24053`
- `conversation:<id>#<message_id>` — for design decisions. Example: `conversation:living-docs#7864`
- `task:<id>` — for implementation history. Example: `task:mcp-switchboard/living-docs-plan-v2`

Citations go inline at the end of the sentence they support, comma-separated
for multiple sources. **If you cannot ground a claim, OMIT THE CLAIM.** Do not
invent shas. Do not paraphrase from memory.

## Diagrams

- **Mermaid** for graph-like structures: state machines, flowcharts, ER diagrams.
  Use the standard `\`\`\`mermaid` fence.
- **ASCII** for small subsystem sketches inline.
- **No images.** No external diagram tools.

State-machine example (see `ouvrage/dispatch/lifecycle.py`):
\`\`\`mermaid
stateDiagram-v2
    [*] --> ready
    ready --> working: dispatch
    working --> validating: complete
    validating --> completed: gate_pass
    validating --> stopped: gate_fail
    stopped --> working: resume
    completed --> stopped: reopen
    [*] --> cancelled: cancel
\`\`\`

## UNCHANGED criteria

Skip a slug (no file write, no tool call) if **all** hold:

1. The diff of merged work since `last_seen_sha` does not touch any file or
   behavior the doc describes by name.
2. No new public surface (function, MCP tool, schema column, route, CLI flag,
   env var) was introduced in this slice.
3. No invariant or constraint stated in the doc has been violated or strengthened.
4. The Recent changes section would not need a new bullet.

Cosmetic rewrites count as material **only** if existing phrasing is wrong or
ambiguous. "I could phrase this better" is not enough.

## System exemplars (shape reference for v0)

If a slug has **no** existing file at `{reference_doc_path}/{slug}.md` (this is
the project's first regen for that slug), use these exemplars as the shape and
voice reference. Read them as needed:

- `ouvrage/services/living_docs/exemplars/architecture.example.md`
- `ouvrage/services/living_docs/exemplars/data-model.example.md`

If the project already has reference docs in git, **prefer those** as the
shape reference. The system exemplars are fallback only.

## Project context

- **Project ID:** `{project_id}`
- **Default branch:** `{default_branch}`
- **Reference doc path:** `{reference_doc_path}`
- **Last regen at:** `{last_regen_at_or_never}`
- **Slugs configured:** {n_configs}

## Configs

For each slug:

### `{slug}` — {title}
- **Brief:** {brief}
- **Source hints:** {source_hints or "(none)"}
- **Last seen sha:** {last_seen_sha or "never"}
- **Existing file at HEAD:** `{reference_doc_path}/{slug}.md` ({"present" or "missing"})

(... one block per configured slug ...)

## Merged work since last regen

The following PRs have merged into `{default_branch}` since this project's last
regen run. Use these as the primary signal for what's changed:

- {merged_task_id} — `{branch}` — `{pr_url}` — {goal}
  (... up to 50 most recent ...)

If `last_regen_at` is null (first regen), this list is the last 50 merged tasks
overall. Note this is informational; you should also use `git log` and direct
file reads in the worktree to verify what changed.

## Workflow

1. Read the existing files at `{reference_doc_path}/{slug}.md` for each
   configured slug. These are your baselines.
2. For each slug, read the relevant source code (use the `source_hints` and
   the merged-work list above to scope your reading). Use `git log -p` and
   `git diff` against `{last_seen_sha}` (where present) to focus.
3. Decide UNCHANGED or UPDATED per slug.
4. For UPDATED slugs:
   a. Write the new doc to `{worktree_root}/{reference_doc_path}/{slug}.md`.
   b. Call `add_reference_doc_version(task_id="{task_id}", slug="{slug}",
      source_path="{worktree_root}/{reference_doc_path}/{slug}.md")`.
   c. `git add {reference_doc_path}/{slug}.md`.
5. After processing all slugs:
   - If you updated at least one: `git commit -m "Living Docs regen: {n_changed}
     docs updated"` with the slug list in the body.
   - If you updated zero: do not commit. The lifecycle will detect no commits
     and skip PR creation. The completion hook records the run as `unchanged`.
6. Push happens automatically. PR opens automatically.
7. Post a single result message summarizing what changed.

## Hard rules

- Do **not** modify any file outside `{reference_doc_path}/`.
- Do **not** call MCP tools other than `add_reference_doc_version`,
  `post_task_message`, `git_push` (the standard lifecycle uses git_push;
  you do not need to call it directly).
- Do **not** invent citations. Only cite shas you have verified via `git log`
  or `git show`.
- Do **not** rewrite human-edited text that is still factually correct.
```

**Notes on this prompt:**

- No `unchanged=True` flag in the tool call — agent simply doesn't call the tool for skipped slugs (per v2 spec).
- `add_reference_doc_version` signature uses `slug`, not `config_id` (per v2 spec).
- The "do not commit, do not push" instruction from v1 §5 is **inverted** — v2 uses the standard lifecycle, so the agent does `git add` + `git commit`, and the lifecycle handles push + PR.
- Reconciliation rule (preserving human edits) is codified — addresses v2 spec's "treat existing content as authoritative" requirement.
- System exemplars are referenced by path, read on demand, not loaded into the prompt.

---

## 8. Regen task wiring (v2)

### 8.1 Cron sweep

New file `ouvrage/dispatch/living_docs_sweep.py`, modeled on `ouvrage/dispatch/pr_sweep.py:90`. Outer loop sleeps 30 minutes; per iteration:

```python
SWEEP_INTERVAL = 60 * 30  # 30 minutes

async def _living_docs_sweep() -> None:
    while True:
        await asyncio.sleep(SWEEP_INTERVAL)
        try:
            projects = await db.list_projects()
        except Exception as e:
            log.warning(f"Living docs sweep: failed to fetch projects: {e}")
            continue

        for project in projects:
            try:
                if not project.get("living_docs_enabled"):
                    continue
                interval_h = project.get("living_docs_regen_interval_hours") or 24
                if not await _is_due(project, interval_h):
                    continue
                # Avoid stacking: skip if a regen task is already in flight for this project
                if await _has_inflight_regen(project["id"]):
                    continue
                from ouvrage.services.living_docs import LivingDocsService
                await LivingDocsService().regenerate(project_id=project["id"])
            except Exception as e:
                log.warning(f"Living docs sweep: project {project['id']}: {e}")


async def _is_due(project: dict, interval_h: int) -> bool:
    """Due iff (now - max(last_regen_at across configs)) >= interval_h."""
    last = await db.get_latest_regen_at(project["id"])
    if last is None:
        return True
    return (now() - last) >= timedelta(hours=interval_h)


async def _has_inflight_regen(project_id: str) -> bool:
    """True if any task with tag 'living-docs' for this project is still working/validating."""
    return await db.has_inflight_tagged_task(project_id, tag="living-docs")
```

Registered in lifespan at `ouvrage/server/app.py:500`-vicinity:
```python
asyncio.create_task(_living_docs_sweep())
```

### 8.2 Dispatch

`LivingDocsService.regenerate(project_id)` builds the prompt (§7), creates a conversation message as the spec, and calls:

```python
task_id = f"{project_id}/living-docs-regen-{short_iso_now}"
await engine.dispatch_task(
    project_id=project_id,
    task_id=task_id,
    goal=f"Living Docs regen — {project_id}"[:70],
    spec=rendered_prompt,                # posted as pinned task spec message
    tags=["living-docs"],
    model="opus",
    auto_test=False,
    auto_review=False,
    auto_pr=True,
    auto_merge=False,
    max_turns=120,
    max_wall_clock_minutes=30,
    dispatched_by=user_id,
)
```

This runs through the standard dispatch lifecycle — no special parameters, no `system_prompt_prepend`. The pinned spec message is rendered into the CC prompt by `_build_task_prompt` at `sdk_session.py:160–163`.

### 8.3 Completion hook

Insertion point: `ouvrage/dispatch/lifecycle.py:_check_gates_passed` (around line 932), **after** `gate_status='passed'` is committed:

```python
async def _check_gates_passed(task_id: str) -> None:
    ...
    await db.update_task(task_id, gate_status="passed", gate_passed_at=db.now_iso())
    ...
    # >>> Living Docs completion hook <<<
    task = await db.get_task(task_id)
    if "living-docs" in (task.get("tags") or []):
        try:
            from ouvrage.services.living_docs import LivingDocsService
            await LivingDocsService().on_regen_complete(task_id=task_id)
        except Exception as e:
            log.exception(f"Living docs completion hook failed for {task_id}: {e}")
            # Hook failure does not prevent gate-pass; the run row may be missing.
    ...
    await _check_and_dispatch_dependents(task_id)
```

**Why tag-based, not goal-prefix-based:** v1 §6.4 used `task.goal.startswith("Regenerate Living Doc:")`. v2 prefers `tags=["living-docs"]` because tags are first-class on the tasks table and the goal text is user-visible (so it shouldn't carry magic prefixes).

`on_regen_complete` (§5) inserts the `reference_doc_runs` row and updates the per-config `last_seen_sha` / `last_regen_at` / `last_regen_task_id`. The `commit_sha` for the run is read from `git rev-parse HEAD` in the worktree (same pattern as `_ensure_branch_pushed`).

### 8.4 Where the prompt is assembled

`LivingDocsService.regenerate` calls a private `_build_regen_prompt(project, configs, merged_work)`:

1. Load project (`db.get_project`); reads `default_branch`, `reference_doc_path`.
2. Load configs (`db.list_reference_doc_configs(project_id)`).
3. Compute "since" cutoff: `min(c.last_regen_at for c in configs)`; if any is None, treat as project's first regen.
4. Load merged work via `db.list_merged_tasks_since(project_id, since_iso)` (the v1 §1.1 helper, which is why task #1 of the chain is `tasks.merged_at`).
5. Render the §7 template with f-strings.
6. Return the prompt string.

The prompt is posted as the task's pinned spec message via `db.post_task_message(task_id=..., type="spec", pinned=True, ...)` immediately after `engine.dispatch_task` returns the task_id.

---

## 9. Search integration (v2)

### 9.1 File embedding hook

`LivingDocsService.add_version` calls `asyncio.create_task(index_doc_file(file_id))` after the upsert (fire-and-forget — must never block the request). The signature mirrors `index_message_chunks` (`ouvrage/db/search.py:443`).

`index_doc_file(file_id)` body — verbatim from v1 §1.3 (carried forward):

```python
async def index_doc_file(file_id: str) -> None:
    record = await db.get_file(file_id)
    if not record or record["role"] != "reference_doc":
        return
    content = Path(record["stored_path"]).read_text(encoding="utf-8", errors="replace")

    service = get_embedding_service()
    whole = await service.embed_safe(content[:32000])
    if whole:
        blob = encode_vector(whole)
        await db.set_file_embedding(file_id, blob)  # writes files_embeddings + files_vec

    chunks = chunk_message(content)  # reuse existing chunker
    async with get_db() as conn:
        await conn.execute("DELETE FROM file_chunks WHERE file_id = ?", (file_id,))
        if not chunks:
            await conn.execute(
                """INSERT INTO file_chunks (file_id, chunk_index, heading, content, embedding)
                   VALUES (?, -1, NULL, '', NULL)""", (file_id,))
            await conn.commit()
            return
        for ch in chunks:
            vec = await service.embed_safe(ch["content"])
            blob = encode_vector(vec) if vec else None
            cur = await conn.execute(
                """INSERT INTO file_chunks (file_id, chunk_index, heading, content, embedding)
                   VALUES (?, ?, ?, ?, ?)""",
                (file_id, ch["chunk_index"], ch["heading"], ch["content"], blob))
            if blob and cur.lastrowid and len(blob) == 1536 * 4:
                try:
                    await conn.execute(
                        "INSERT OR REPLACE INTO file_chunks_vec(rowid, embedding) VALUES (?, ?)",
                        (cur.lastrowid, blob))
                except Exception as e:
                    log.warning("file_chunks_vec insert failed for file %s: %s", file_id, e)
        await conn.commit()
```

Backfill loop `_backfill_file_chunks` lives in `ouvrage/server/app.py` next to the existing `_backfill_message_chunks` (line 496); registered in lifespan at line 485-499.

### 9.2 vec0 tables

`files_vec(rowid = files.rowid)` and `file_chunks_vec(rowid = file_chunks.id)`. Both use `vec0(embedding float[1536])` matching `text-embedding-3-small`.

### 9.3 `scope=docs` filter

In `ouvrage/server/handlers/search.py:_handle_search` (around line 96), add:

```python
DOC_CURRENT_WEIGHT = 1.6   # project reference docs (current local copies)
TASK_ARTIFACT_WEIGHT = 1.0 # task .md uploads (role='upload')

if scope == "docs":
    # Restrict to current reference doc files
    cand_files = await db.execute_fetchall(
        """SELECT id FROM files
            WHERE role = 'reference_doc'
              AND project_id = COALESCE(?, project_id)
        """, (project_id_filter,))
    # ... semantic search against files_vec / file_chunks_vec, then weight ...
```

The candidate retrieval uses the partial index `idx_files_role`. The `scope=docs` value is passed through from MCP `search` tool input.

### 9.4 Entity types

Update `ouvrage/db/search_weights.py:6`:

```python
_VALID_ENTITY_TYPES = {"task", "message", "chunk", "reference_doc"}
```

**Drop `doc_version` and `doc_chunk` from v1.** v2 has no prior versions (git owns history); per-chunk override on a current doc is a YAGNI we can add later if real searches need it.

Manual override key for a specific reference doc: `("reference_doc", file_id)`. Updates `_handle_set_weight` schema description at `ouvrage/server/handlers/search.py:335`.

### 9.5 What NOT to do (from v1 vs v2)

- **Drop** the `doc_chunk` entity type (per-chunk overrides) — git is history. If we later want chunk-level pinning we can reintroduce it.
- **Drop** the `DOC_PRIOR_WEIGHT = 0.5` constant — there are no prior versions in the index.
- **Drop** "near-duplicate suppression" risk #3 from v1 §10 unless real searches show it as a problem.

---

## 10. UI integration

The dashboard SPA is Preact + htm via CDN, hash-routed, no build step. UI changes split into three concerns: the project files tab (configs list, run history, manual regen, view local copy), the project settings panel (kill switch + interval + path), and the per-config edit/create/delete UX.

### 10.1 Files modified

| File | Change |
|------|--------|
| `dashboard/views/FilesTab.js` | Add `<ReferenceDocsSection projectId=...>` above the existing `UploadZone`. Section contains: configs list, run history, manual regen button. |
| `dashboard/views/ProjectView.js` | Add to SettingsTab (Advanced section, after the Auto-PR/Auto-Merge toggle group around line 704): kill switch toggle (`living_docs_enabled`), regen interval input (number + "hours" suffix), reference doc path input (text). Wire all three through existing `api.updateProject(...)` and the existing inline-form pattern. |
| `dashboard/components/FormKit.js` | (No change unless we need a new field type — the existing `FormField` and toggle patterns cover this.) |

### 10.2 New files

| File | Purpose |
|------|---------|
| `dashboard/components/ReferenceDocsSection.js` | Section composite: list, run history, regen button. |
| `dashboard/components/ReferenceDocConfigModal.js` | Create/edit modal: slug (read-only on edit), title, brief (textarea), source_hints (textarea). |
| `dashboard/components/ReferenceDocViewer.js` | Lightweight markdown viewer modal, fed by `GET /dashboard/api/projects/{id}/reference_docs/{slug}/content`. Reuses `MarkdownLightbox.js` if it can be parametrized. |

### 10.3 New REST endpoints (in `ouvrage/dashboard/api.py`)

Routed via `handle_request` (api.py:99). All require an authenticated session (existing middleware applies).

| Method | Path | Handler | Body / response |
|--------|------|---------|-----------------|
| GET | `/dashboard/api/projects/{id}/reference_docs` | `_handle_list_reference_docs(send, project_id)` | `{configs: [...], runs: [...]}`. configs from `LivingDocsService.list_configs`, last 20 runs. |
| GET | `/dashboard/api/projects/{id}/reference_docs/{slug}` | `_handle_get_reference_doc(send, project_id, slug)` | Full config row + `local_copy_present`. |
| GET | `/dashboard/api/projects/{id}/reference_docs/{slug}/content` | `_handle_get_reference_doc_content(send, project_id, slug)` | `text/markdown` body from local cache. 404 if missing. |
| POST | `/dashboard/api/projects/{id}/reference_docs` | `_handle_set_reference_doc(receive, send, project_id)` | Body: `{slug, title, brief, source_hints}`. Calls `LivingDocsService.set_config`. |
| DELETE | `/dashboard/api/projects/{id}/reference_docs/{slug}` | `_handle_delete_reference_doc(send, project_id, slug)` | 204 on success. |
| POST | `/dashboard/api/projects/{id}/reference_docs/regenerate` | `_handle_regenerate_reference_docs(send, project_id)` | 202 with `{task_id}`. |

The existing `_handle_update_project` at api.py:586 already accepts arbitrary updatable fields; once `living_docs_enabled` / `reference_doc_path` / `living_docs_regen_interval_hours` are in the projects table and `_decode_project()` returns them, the SettingsTab's existing form mechanics (lines 274–763 of `ProjectView.js`) work without new endpoints.

### 10.4 UX details

- **Configs list** — table or card grid: slug (mono), title, last_regen_at (relative), last_seen_sha (short), [Edit] [Delete] [View local] buttons. Empty state: "No reference docs configured yet. Configure them to enable Living Docs regeneration."
- **Run history** — collapsible, shows last 20 runs: ran_at (relative), outcome (badge: green/grey/red), slugs_changed count, link to the regen task. Mirrors the existing TaskList row pattern.
- **Manual regen button** — `[Regenerate now]`. POSTs to the regenerate endpoint; shows a toast with the dispatched task_id and a link. Disabled while there's an in-flight regen for the project.
- **Kill switch toggle** — uses the existing `toggleRowStyle` pattern (ProjectView.js:388–402). Label: "Living Docs cron". Sub: "When on, regenerate reference docs at the configured interval. Manual regen always works regardless of this toggle."
- **Regen interval** — number input + " hours" suffix. Default 24. Min 1. Max 168 (7 days).
- **Reference doc path** — text input. Default `docs/reference`. Shows a small preview: `Files will be written to {repo}/{path}/{slug}.md`. Validate: no leading/trailing slash; no `..`.
- **Per-config delete** — `DangerZone` pattern (ProjectView.js:185–267): user types the slug to confirm. Toast: "Reference doc config '{slug}' deleted. Local cache and embeddings removed. The git file at `{path}/{slug}.md` was NOT deleted — clean it up in the repo separately."

### 10.5 Playwright + visual coverage

Per CLAUDE.md and standing rule (every dashboard task needs Playwright screenshots):

- New fixtures in `fixtures/visual/`:
  - `project-reference-docs.json` — populated configs + run history
  - `project-reference-docs-empty.json` — zero configs (empty state)
  - `project-settings-livingdocs.json` — settings tab with toggles + path
  - `reference-doc-config-modal.json` — modal open in create state
  - `reference-doc-viewer.json` — viewer modal showing rendered markdown
- New Playwright tests in `tests/test_dashboard_living_docs.py` (mirrors `tests/test_dashboard_*.py`):
  - List configs (renders table + run history)
  - Create config via modal (slug regex validation)
  - Edit config (brief + source_hints update)
  - Delete config (DangerZone confirm; verify cascade message)
  - Toggle kill switch (verify backend `living_docs_enabled` flips)
  - Click manual regen (mock service; verify task_id surfaced)
  - View local copy (verify markdown rendered)
- `scripts/visual-check.py` regenerates PNGs for the new fixtures; reference PNGs go in `fixtures/visual/png/`.

---

## 11. Implementation chain proposal

14 tasks. Ordered. Sequential vs parallel marked.

### Chain shape

```
                                                ┌── 4 db-helpers ──┐
                                                │                  │
1 merged_at ──→ 2 schema-v2 ──→ 3 files-role ───┼── 5 embeddings ──┼── 6 service ──┬── 9 regen-prompt ──┐
                                                │                  │               │                    │
                                                └──────────────────┘               │                    │
                                                                                   ├── 7 mcp-tools ──┐  │
                                                                                                     │  │
                                                                                   ┌── 8 search-int ─┘  │
                                                                                                        │
                                                                                  10 dispatch-wiring ───┘
                                                                                            │
                                                                                  11 cron ──┘
                                                                                            │
                                                                                  12 ui-files-section ──┐
                                                                                  13 ui-settings-modal ─┤
                                                                                                        │
                                                                                  14 bootstrap-test ────┘
```

### Tasks

| # | Task ID | Model | depends_on | Scope (one line) | Acceptance criteria |
|---|---------|-------|------------|------------------|---------------------|
| 1 | `mcp-switchboard/living-docs-merged-at` | sonnet | — | Add `tasks.merged_at`, set at both merge sites, backfill, `list_merged_tasks_since` helper. | Column present; both call sites set it; backfill runs once on migration; helper returns sorted rows; tests for helper. |
| 2 | `mcp-switchboard/living-docs-schema-v2` | sonnet | (1) | Add `reference_doc_configs`, `reference_doc_runs`, `files.role`, `projects.living_docs_*`, `files_embeddings`, `file_chunks`, vec0 tables + triggers. Idempotent migration block. | All schema diffs visible in `PRAGMA table_info`; idempotent on re-run; test asserts FK + UNIQUE constraints; vec0 tables only created when `VEC_AVAILABLE`. |
| 3 | `mcp-switchboard/living-docs-files-role-guard` | sonnet | (2) | `ValueError` guard in `db/files.py:delete_file` for `role='reference_doc'`; internal `delete_reference_doc_files()` cascade-bypass helper. | Direct `delete_file` on a reference doc raises ValueError with the documented message; internal helper succeeds; tests cover both paths. |
| 4 | `mcp-switchboard/living-docs-db-helpers` | sonnet | (2) | New `ouvrage/db/reference_docs.py`: configs CRUD + runs append-only + `delete_reference_doc_files`. | All helpers have unit tests; UNIQUE upsert behavior verified; cascade behavior verified. |
| 5 | `mcp-switchboard/living-docs-embeddings` | sonnet | (2) | `index_doc_file`, `set_file_embedding`, `search_files_semantic`, `search_file_chunks_semantic`, `get_doc_files_needing_chunking`, `_backfill_file_chunks` lifespan task. | Indexing populates `files_embeddings`, `file_chunks`, `files_vec`, `file_chunks_vec`; sentinel row when chunker returns None; backfill processes batched files; tests assert vec rowid mapping; lifespan task registered at app.py:485-vicinity. |
| 6 | `mcp-switchboard/living-docs-service` | sonnet | (3, 4, 5) | `LivingDocsService` — config CRUD, `add_version` (copy + embed), `delete_config` (cascade), `get_local_copy`, `list_runs`. No regen orchestration yet. | All service methods have unit tests; `add_version` path-validates source_path; `delete_config` removes file + cache + DB row; `add_version` is idempotent on re-call. |
| 7 | `mcp-switchboard/living-docs-mcp-tools` | sonnet | (6) | MCP tools: `set_reference_doc_config`, `delete_reference_doc_config`, `set_living_docs_enabled`, `add_reference_doc_version` (worker-only), `list_reference_doc_configs`, `get_reference_doc_config`. (Defer `regenerate_reference_docs` to task 10.) | Tools registered in `TOOLS`, routed in `TOOL_HANDLERS`; `add_reference_doc_version` in `WORKER_TOOL_ALLOWLIST`; slug regex enforced; worker check enforced; integration tests via dispatch handler. |
| 8 | `mcp-switchboard/living-docs-search-integration` | sonnet | (5, 7) | `_handle_search` `scope=docs` filter; weights 1.6 / 1.0; extend `_VALID_ENTITY_TYPES` with `reference_doc`; update `_handle_set_weight` schema. | `scope=docs` returns only reference doc hits; weights honored; per-doc weight override works; tests assert ranking. |
| 9 | `mcp-switchboard/living-docs-regen-prompt` | opus | (6) | `_REGEN_PROMPT_TEMPLATE` (full text per §7); `_build_regen_prompt(project, configs, merged_work)`; ship 2 system exemplar markdown files at `ouvrage/services/living_docs/exemplars/`. | Template renders deterministically given fixture inputs; exemplars present and pass markdown lint; unit test snapshots the rendered output for a 2-config fixture project. |
| 10 | `mcp-switchboard/living-docs-dispatch-wiring` | sonnet | (7, 9) | `LivingDocsService.regenerate(project_id)` → assemble prompt + `engine.dispatch_task(auto_pr=True, tags=["living-docs"], ...)`. `regenerate_reference_docs` MCP tool. Lifecycle completion hook in `_check_gates_passed` (tag-based). `on_regen_complete` inserts `reference_doc_runs` row + updates per-config metadata. | Dispatch path tested with mock_git/mock_sdk; completion hook fires only for tagged tasks; run row + config metadata updated correctly; idempotent on hook re-trigger. |
| 11 | `mcp-switchboard/living-docs-cron` | sonnet | (10) | `_living_docs_sweep` (30 min outer interval, per-project gating on `living_docs_enabled` + interval + no-inflight); registered in lifespan. | Sweep skips disabled projects; respects per-project interval; never stacks regens for the same project; tests use freezegun for interval logic. |
| 12 | `mcp-switchboard/living-docs-ui-files-section` | sonnet | (7, 10) | `dashboard/components/ReferenceDocsSection.js` (configs list + run history + manual regen + view-local-copy modal). Wire into `dashboard/views/FilesTab.js`. New REST endpoints in `ouvrage/dashboard/api.py` for list/get/content/regenerate. Playwright + visual fixtures. | Section renders empty state and populated state; manual regen dispatches a task; viewer modal renders markdown; Playwright tests pass; visual fixtures saved. |
| 13 | `mcp-switchboard/living-docs-ui-settings-and-modal` | sonnet | (7) | Project settings panel additions in `dashboard/views/ProjectView.js` (kill-switch toggle, interval, path) wired through existing `api.updateProject`. New `ReferenceDocConfigModal.js` for create/edit. Per-config delete via DangerZone confirm. New REST endpoints for set/delete config. Playwright + visual fixtures. | Toggle persists; interval validates 1–168; path validates no `..`/leading slashes; modal validates slug regex; delete shows correct cascade-warning text; Playwright tests pass. |
| 14 | `mcp-switchboard/living-docs-bootstrap-smoke` | opus | (11, 12, 13) | E2E: register two configs, force-regenerate via MCP, verify worktree got committed `.md` files, verify local cache populated, verify embeddings indexed, verify run row inserted, verify UI shows everything. | Smoke produces a green PR (gates skipped via test infra); local cache files present at `data/reference_docs/...`; `reference_doc_runs` row with `outcome='updated'`; UI snapshot matches fixture. |

**Sequential vs parallel:**

- **1 → 2** sequential (both touch `schema.py`).
- **2 → 3** sequential (both touch `db/files.py`).
- **4 ‖ 5** parallel after **2** (different files: `db/reference_docs.py` vs `db/search.py` + `app.py` lifespan).
- **6** sequential after **3, 4, 5**.
- **7 ‖ 9** parallel after **6** (`server/handlers/living_docs_handler.py` + `server/tools.py` vs `services/living_docs.py` prompt module + exemplars).
- **8** after **5, 7** (touches `server/handlers/search.py` and depends on tool registration).
- **10** after **7, 9**.
- **11** after **10**.
- **12 ‖ 13** parallel after **7, 10** (different dashboard files + endpoints).
- **14** last.

---

## 12. Bootstrap process

Bootstrap is the human + LLM workflow for defining the first project's brief list once the feature ships. It is **multi-session work in chat**, not a one-shot task.

### Required tools (post-ship)

- `set_reference_doc_config(project_id, slug, title, brief, source_hints?)` — primary write tool
- `list_reference_doc_configs(project_id)` — verify state between sessions
- `get_reference_doc_config(project_id, slug)` — read back a brief
- `delete_reference_doc_config(project_id, slug)` — remove an experimental slug
- `regenerate_reference_docs(project_id)` — kick off the first wave manually

### Workflow

1. **Open the `living-docs` conversation** (or per-project equivalent) and pin the v2 spec for context.
2. **Brainstorm slug list collaboratively.** Human + LLM propose 5–10 slugs covering the project. Typical first cut for `mcp-switchboard`: `architecture`, `lifecycle`, `dispatch-and-gates`, `dashboard`, `auth`, `data-model`, `git-providers`, `search`, `embeddings`, `cli-and-mcp`.
3. **For each slug, draft the brief and source hints together.** The brief is a 3–8 sentence semantic description: "What does this doc cover? What does it not cover?". Source hints are 5–15 file/area pointers: paths, table names, function names. Keep both terse — the agent reads code; the brief is intent, not content.
4. **LLM calls `set_reference_doc_config` per slug** in the active session. Verify with `list_reference_doc_configs`.
5. **Trigger the first wave manually:** `regenerate_reference_docs(project_id)`. The cron is off by default (`living_docs_enabled=false`) — that's intentional. The first regen produces v0 docs that the human reviews via PR.
6. **Review the PR.** If a slug's v0 is bad, two paths:
   - Revise the brief/source_hints (`set_reference_doc_config`) and trigger again.
   - Edit the doc directly in the PR; the next regen reconciles.
7. **Once happy, merge the PR.** Then enable cron: `set_living_docs_enabled(project_id, true)`. Configure the interval if the default 24h isn't right.

### Iteration in subsequent sessions

- Add new slugs as the project surface grows.
- Tune briefs based on what shows up in regen output (or doesn't).
- Use the `slugs_unchanged` count in the run history as a signal — if a slug is always unchanged for months, the brief might be too narrow or the slice too stable to need a doc.

### Acknowledgement

Bootstrap is **not** a one-shot scripted process. It's a multi-turn collaborative loop where the human and LLM iterate on intent, observe agent output, and refine. The MCP tooling exists to make each turn cheap. The first round will likely take 1–2 sessions of ~30–60 minutes each per project.

---

## 13. Risks and unknowns

What to verify after the first regen wave; what might need revision.

### Verify after first regen

1. **Citation prompt drift** (carried from v1 §10 #1) — Models may invent citation SHAs. After the first wave, audit a sample of citations in committed docs against `git log` / `git show`. If the false-citation rate is non-trivial, tighten the prompt with explicit examples and a "verify before you cite" workflow.
2. **Search weight calibration** (carried from v1 §10 #2) — 1.6 / 1.0 split is an initial guess. After ~50 real searches, retune in 0.2 increments. Use the per-entity manual override path for one-off pinning.
3. **`unchanged` ratio per project** (carried, reframed from v1 §10 #4) — Compute over `reference_doc_runs.slugs_unchanged` arrays. If the unchanged ratio sits below 20%, the agent is too aggressive (regenerating prose for cosmetic reasons). If it sits above 90% for months, briefs may be too narrow or the slice may be too stable to warrant a doc.
4. **Regen cost** (carried from v1 §10 #5) — Opus regen tasks are expensive. Monitor `tasks.total_cost_usd` filtered to `tags=["living-docs"]` over the first month. If the per-project monthly cost exceeds a target threshold (TBD per project), consider: dropping to Sonnet for unchanged-likely projects, increasing the interval, or trimming briefs.
5. **No-op PR noise** — When every slug is unchanged, the regen task pushes zero commits and the PR creation fails with `422 "no commits between base and head"`. The completion hook records `outcome='unchanged'` and the run row is the audit source. The "PR creation failed" status message on the task may confuse reviewers; consider suppressing it in the regen path (post-ship polish) or auto-filtering it from the dashboard task view based on the tag.
6. **Worktree cleanup for unchanged runs** — `auto_release_worktree=1` (default) cleans up the worktree after gate-pass. Verify this still happens for regen tasks that produced no commits; the lifecycle should not be confused by an empty diff.
7. **Doc encoding / chunkability** (carried, reframed from v1 §10) — If the agent produces a doc with only `# Title` + `### ` subheadings (single section per the chunker contract), `chunk_message` returns `None` and the file is searchable only via whole-file embedding. The validator on `add_reference_doc_version` warns but does not reject. Monitor: if the warning fires often, add a stricter shape rule to the prompt or a server-side rejection threshold.

### Things that might need revision

1. **Tag-based completion-hook keying.** If `tasks.tags` ever becomes user-mutable mid-task, an attacker (or a confused user) could remove the `living-docs` tag and prevent `on_regen_complete` from firing. Audit: tags are settable in `dispatch_task` only; `db.update_task` does not expose tag mutation. Verify before ship; fall back to a sentinel column on tasks if tags are not safe.
2. **Atomicity of `add_reference_doc_version`.** The "copy file → upsert files row → fire-and-forget embed" sequence has a window where the file is on disk but the DB row is missing if a process restart happens between steps. Mitigation: do the upsert before the fsync-rename, or wrap copy + upsert in a single function with a try/except cleanup.
3. **Slug rename UX.** v2 has no rename tool. To rename a slug, you delete the old config (drops local cache + embeddings + leaves git file alone) and create a new one (next regen produces the new file, old git file becomes orphaned). UI should surface this as a known limitation; we may add `rename_reference_doc_config` post-ship if it's commonly needed.
4. **First-regen slug count.** A project with 10 slugs and zero existing docs makes the first regen task very long (10 v0 docs in one task). Consider: chunking the first wave by slug if the v0 cost is unmanageable. Likely fine for ≤5–6 slugs; revisit if real projects push this higher.
5. **`reference_doc_path` change after content exists.** If a project changes `reference_doc_path` from `docs/reference` to `docs/spec`, the next regen looks for files at the new path (none present) and treats every slug as v0. Old files at the old path are orphaned in git. Consider: a one-time migration command or surfacing the impact in the SettingsTab confirm dialog.
6. **Auto-PR title format.** `_maybe_create_pr` derives title from `task.goal[:70]`. The regen goal `Living Docs regen — {project_id}` truncates fine for typical project IDs but could be opaque for long ones. The body's `## Summary` lists chain goals (one for a single-task chain). Reviewers see one line of context. Post-ship polish: add a custom body when `tags` includes `living-docs`, listing slugs changed inline. This requires extending `_maybe_create_pr` (small change), not a new dispatch parameter.
7. **Embeddings backfill during migration.** When the schema migration ships, existing tasks' `.md` artifacts already exist as `files` rows with `role='upload'` (default). `_backfill_file_chunks` should pick them up over time, but the first wave will be a burst of OpenAI calls. Consider: rate-limiting the backfill, or running it lazily only on first search.
8. **System exemplar drift.** The two shipped exemplar files become outdated as the prompt and conventions evolve. Ship-time exemplars must be high quality; plan a quarterly review (or trigger on prompt changes via a CI check that the exemplars still pass the prompt's UNCHANGED criteria when treated as input).

---

## Appendix A — Files touched (v2 chain)

New files:
- `ouvrage/db/reference_docs.py`
- `ouvrage/services/living_docs.py`
- `ouvrage/services/living_docs/__init__.py`
- `ouvrage/services/living_docs/exemplars/architecture.example.md`
- `ouvrage/services/living_docs/exemplars/data-model.example.md`
- `ouvrage/server/handlers/living_docs_handler.py`
- `ouvrage/dispatch/living_docs_sweep.py`
- `dashboard/components/ReferenceDocsSection.js`
- `dashboard/components/ReferenceDocConfigModal.js`
- `dashboard/components/ReferenceDocViewer.js`
- `tests/test_living_docs_service.py`
- `tests/test_living_docs_handler.py`
- `tests/test_living_docs_sweep.py`
- `tests/test_dashboard_living_docs.py`
- `fixtures/visual/project-reference-docs.json`
- `fixtures/visual/project-reference-docs-empty.json`
- `fixtures/visual/project-settings-livingdocs.json`

Modified files:
- `ouvrage/db/schema.py` (new tables/columns/indexes/triggers)
- `ouvrage/db/files.py` (delete guard)
- `ouvrage/db/search.py` (`index_doc_file`, `set_file_embedding`, `search_files_semantic`, `search_file_chunks_semantic`, `get_doc_files_needing_chunking`)
- `ouvrage/db/search_weights.py` (`_VALID_ENTITY_TYPES` extension)
- `ouvrage/db/tasks.py` (`list_merged_tasks_since`, `has_inflight_tagged_task`)
- `ouvrage/server/handlers/search.py` (`scope=docs`, weights, set_weight schema)
- `ouvrage/server/tools.py` (new tool group, `WORKER_TOOL_ALLOWLIST` extension)
- `ouvrage/server/dispatch.py` (`TOOL_HANDLERS` extension)
- `ouvrage/server/app.py` (lifespan: `_backfill_file_chunks`, `_living_docs_sweep`)
- `ouvrage/dispatch/lifecycle.py` (completion hook in `_check_gates_passed`)
- `ouvrage/dispatch/engine.py` (no change required — `tags`/`auto_pr`/`spec` already supported)
- `ouvrage/git/operations.py` (`merged_at` on auto-merge path)
- `ouvrage/dispatch/pr_sweep.py` (`merged_at` on sweep-merged path)
- `ouvrage/config/constants.py` (`merged_at` in `TASK_MUTABLE_FIELDS`)
- `ouvrage/dashboard/api.py` (new reference-docs endpoints; `_handle_update_project` already accepts new project columns)
- `dashboard/views/ProjectView.js` (SettingsTab additions)
- `dashboard/views/FilesTab.js` (mount ReferenceDocsSection)
- `dashboard/router.js` (no change — existing `/files` tab is the host)

---

## Appendix B — Path glossary (carried from v1 Appendix B)

| Spec name | Real path |
|-----------|-----------|
| `switchboard/db/schema.py` | `ouvrage/db/schema.py` |
| `switchboard/server/handlers/files_handler.py` | `ouvrage/server/handlers/files_handler.py` |
| `switchboard/git/files.py` | `ouvrage/git/files.py` (lightweight git file ops) |
| `switchboard/embeddings/chunker.py` | `ouvrage/embeddings/chunks.py` (file is `chunks.py`, not `chunker.py`) |

Apply throughout. The package is `ouvrage/`; the spec sometimes uses the legacy `switchboard/` name.
