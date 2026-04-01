# Test Suite Triage Report

**Project:** mcp-switchboard
**Date:** 2026-04-01
**Total test files:** 67
**Total test functions:** 1,922
**Total test lines:** ~31,000

---

## 1. Test Inventory by Domain

### Summary by Domain

| Domain | Files | Tests | % of Total |
|--------|-------|-------|------------|
| Database CRUD | 10 | 268 | 13.9% |
| Auth/sessions/tokens | 10 | 232 | 12.1% |
| Dashboard API | 9 | 120 | 6.2% |
| Lifecycle/orchestration | 8 | 299 | 15.6% |
| MCP server handlers | 7 | 151 | 7.9% |
| Gate pipeline | 7 | 175 | 9.1% |
| Git operations | 5 | 95 | 4.9% |
| Recovery | 3 | 94 | 4.9% |
| Queue/concurrency | 1 | 17 | 0.9% |
| Notifications | 0 | 0 | 0.0% |
| Visual checks / UI | 3 | 113 | 5.9% |
| Embeddings/search | 3 | 82 | 4.3% |
| Omnibus/mixed | 1 | 142 | 7.4% |
| Other (process, config) | 2 | 38 | 2.0% |
| **Total** | **67** | **1,922** | **100%** |

### Per-File Inventory

| File | Tests | Lines | Domain |
|------|-------|-------|--------|
| test_lifecycle.py | 163 | 1636 | Lifecycle/orchestration |
| test_unit.py | 142 | 2636 | Omnibus (20+ domains) |
| test_markdown_lightbox.py | 69 | 411 | Visual checks / UI |
| test_files_api.py | 64 | 1120 | MCP server handlers |
| test_gate_recovery.py | 57 | 909 | Gate pipeline / recovery |
| test_components.py | 53 | 575 | Database CRUD |
| test_users.py | 49 | 412 | Database CRUD |
| test_oauth.py | 45 | 688 | Auth/sessions/tokens |
| test_rag.py | 42 | 673 | Embeddings/search |
| test_review_enrich.py | 41 | 564 | Gate pipeline |
| test_lean_api.py | 39 | 566 | MCP server handlers |
| test_lifecycle_actions.py | 38 | 548 | Lifecycle/orchestration |
| test_internal_api.py | 37 | 369 | Dashboard API |
| test_https_credentials.py | 37 | 701 | Git operations |
| test_crash_recovery.py | 37 | 903 | Recovery |
| test_settings_api.py | 36 | 654 | Dashboard API |
| test_reopen.py | 36 | 726 | Lifecycle/orchestration |
| test_sessions.py | 33 | 607 | Auth/sessions/tokens |
| test_ops.py | 33 | 434 | Database CRUD / handlers |
| test_migration.py | 33 | 259 | Database CRUD |
| test_pr_sweep.py | 32 | 406 | Git operations |
| test_image_lightbox.py | 31 | 191 | Visual checks / UI |
| test_auto_merge.py | 29 | 628 | Git operations |
| test_review_stall.py | 28 | 647 | Gate pipeline |
| test_log_archive.py | 28 | 646 | Gate pipeline |
| test_lifecycle_system_events.py | 27 | 663 | Lifecycle/orchestration |
| test_database.py | 27 | 299 | Database CRUD |
| test_mcp_auth.py | 26 | 267 | Auth/sessions/tokens |
| test_onboarding_guardrails.py | 25 | 605 | MCP server handlers |
| test_punchlist.py | 25 | 309 | Database CRUD |
| test_pending_validation.py | 25 | 587 | Lifecycle/orchestration |
| test_github_pat.py | 25 | 304 | Git operations |
| test_self_issued_jwt.py | 24 | 372 | Auth/sessions/tokens |
| test_realtime_output.py | 24 | 546 | Database CRUD / gate pipeline |
| test_convos_search.py | 24 | 321 | Database CRUD / search |
| test_project_pat.py | 23 | 510 | Database CRUD / handlers |
| test_read_pagination.py | 22 | 385 | MCP server handlers |
| test_smoke.py | 22 | 256 | Mixed (config, prompts, DB) |
| test_auth_mode.py | 20 | 266 | Auth/sessions/tokens |
| test_integration.py | 20 | 403 | Integration (DB + real git) |
| test_settings_integration.py | 19 | 453 | Dashboard API |
| test_dashboard_auth.py | 18 | 228 | Auth/sessions/tokens |
| test_dashboard_projects_api.py | 18 | 366 | Dashboard API |
| test_gate_pipeline_audit.py | 18 | 437 | Gate pipeline |
| test_worker_endpoint.py | 18 | 236 | MCP server handlers |
| test_queue.py | 17 | 372 | Queue/concurrency |
| test_sso.py | 17 | 467 | Auth/sessions/tokens |
| test_dispatch_internals.py | 17 | 387 | Lifecycle/orchestration |
| test_process_isolation.py | 16 | 262 | Lifecycle/orchestration (safety) |
| test_audit_log.py | 16 | 448 | Database CRUD / lifecycle |
| test_chunks.py | 16 | 191 | Embeddings/search |
| test_gate_reentry_fix.py | 14 | 301 | Gate pipeline |
| test_dashboard_settings_tokens_api.py | 14 | 261 | Dashboard API |
| test_project_limit.py | 14 | 193 | MCP server handlers |
| test_visual_check.py | 13 | 130 | Visual checks (tooling) |
| test_rehold.py | 10 | 154 | MCP server handlers |
| test_gate_visibility.py | 9 | 247 | Gate pipeline |
| test_task_create_api.py | 9 | 210 | Dashboard API |
| test_task_update_api.py | 9 | 203 | Dashboard API |
| test_auth_migration.py | 8 | 172 | Auth/sessions/tokens |
| test_linear_chains.py | 8 | 244 | Lifecycle/orchestration |
| test_dashboard_components_api.py | 7 | 169 | Dashboard API |
| test_dashboard_runtime_info.py | 7 | 182 | Dashboard API |
| test_credential_check_bypass.py | 7 | 243 | MCP server handlers |
| test_phantom_attempt.py | 5 | 143 | Lifecycle/orchestration |
| test_gate_status_reset.py | 4 | 115 | Gate pipeline |
| test_worktree_reopen.py | 3 | 121 | Git operations |

---

## 2. Test Type Classification

### Summary by Type

| Type | Files | Tests | % of Total |
|------|-------|-------|------------|
| Behavior | 44 | 1,260 | 65.6% |
| Integration | 8 | 204 | 10.6% |
| Mixed (behavior + wiring) | 10 | 308 | 16.0% |
| Wiring | 2 | 28 | 1.5% |
| Schema/fixture (source grep) | 3 | 113 | 5.9% |
| **Total** | **67** | **1,922** | **100%** |

### Per-File Classification

| File | Type | Rationale |
|------|------|-----------|
| test_lifecycle.py | **Behavior** | Tests state machine via lifecycle.execute() with real DB |
| test_unit.py | **Mixed** | Pure utility tests + heavily-mocked wiring tests for gates/dispatch |
| test_markdown_lightbox.py | **Schema/fixture** | Reads JS source, asserts string presence — no runtime test |
| test_files_api.py | **Integration** | ASGI endpoint tests with real DB, real validation |
| test_gate_recovery.py | **Behavior** | Tests gate routing with real DB, mocked gate functions |
| test_components.py | **Behavior** | Pure DB CRUD with real SQLite |
| test_users.py | **Behavior** | Pure DB CRUD, zero mocks |
| test_oauth.py | **Integration** | Real DB, real RSA keys, real JWT signing |
| test_rag.py | **Behavior** | Custom FakeService classes, real DB for integration |
| test_review_enrich.py | **Mixed** | Valuable prompt assertions but heavy DB mocking (8-10 patches) |
| test_lean_api.py | **Behavior** | Zero mocks, real DB, handler-level assertions |
| test_lifecycle_actions.py | **Behavior** | Tests through lifecycle.execute(), 14-patch helper for infra |
| test_internal_api.py | **Behavior** | ASGI handler tests with real DB, only auth config patched |
| test_https_credentials.py | **Mixed** | Pure URL normalization + wiring for credential helper |
| test_crash_recovery.py | **Behavior** | Real DB, mocked git/SDK, includes static analysis tests |
| test_settings_api.py | **Integration** | Real ASGI handlers + real DB |
| test_reopen.py | **Behavior** | Full reopen lifecycle with mock_git/mock_sdk |
| test_sessions.py | **Behavior** | Zero mocks, real argon2id, real session DB |
| test_ops.py | **Behavior** | DB round-trips and handler output assertions |
| test_migration.py | **Behavior** | Pure DB migration tests, zero mocks |
| test_pr_sweep.py | **Mixed** | Pure URL parsing + wiring for sweep loop |
| test_image_lightbox.py | **Schema/fixture** | Reads JS source, asserts string presence — no runtime test |
| test_auto_merge.py | **Behavior** | Real DB + mocked git for branch resolution |
| test_review_stall.py | **Behavior** | Custom async generator mocks for watchdog testing |
| test_log_archive.py | **Behavior** | Real filesystem with tmp_path, TRANSITIONS dict inspection |
| test_lifecycle_system_events.py | **Behavior** | System events via lifecycle.execute(), static analysis tests |
| test_database.py | **Behavior** | Pure DB CRUD, zero mocks |
| test_mcp_auth.py | **Behavior** | Token CRUD with real DB, minimal mocking |
| test_onboarding_guardrails.py | **Behavior** | Handler-level guardrail tests with real DB |
| test_punchlist.py | **Behavior** | Pure DB lifecycle tests, zero mocks |
| test_pending_validation.py | **Mixed** | Behavior tests + source inspection via inspect.getsource |
| test_github_pat.py | **Behavior** | URL parsing and push logic with mocked subprocess |
| test_self_issued_jwt.py | **Behavior** | Real crypto, real DB, module-var reset fixtures |
| test_realtime_output.py | **Mixed** | DB behavior + gate pipeline wiring |
| test_convos_search.py | **Behavior** | Real DB, httpx mocked only for Graphiti proxy |
| test_project_pat.py | **Behavior** | Real DB, PAT encryption verification |
| test_read_pagination.py | **Behavior** | Handler-level tests, zero mocks |
| test_smoke.py | **Mixed** | Config tests with patches + DB behavior tests |
| test_auth_mode.py | **Behavior** | Middleware responses via simulated ASGI calls |
| test_integration.py | **Integration** | Real DB, real git repos, zero mocks |
| test_settings_integration.py | **Integration** | Multi-step workflows through ASGI with real DB |
| test_dashboard_auth.py | **Behavior** | Real auth_middleware, real sessions, no internal mocking |
| test_dashboard_projects_api.py | **Behavior** | ASGI handler tests with real DB |
| test_gate_pipeline_audit.py | **Behavior** | Gate routing with real DB, mocked gate functions |
| test_worker_endpoint.py | **Behavior** | Handler access control with real DB |
| test_queue.py | **Mixed** | DB behavior for queuing + wiring for drain |
| test_sso.py | **Behavior** | Real RSA keys, real JWT, real DB |
| test_dispatch_internals.py | **Mixed** | Pure functions + wiring for worktree/SDK launch |
| test_process_isolation.py | **Behavior** | anyio patch verification + safety file checks |
| test_audit_log.py | **Behavior** | Real DB, chain cancellation through engine functions |
| test_chunks.py | **Behavior** | Pure function testing, zero mocks |
| test_gate_reentry_fix.py | **Wiring** | 8 patches, assert_called/assert_not_called routing |
| test_dashboard_settings_tokens_api.py | **Behavior** | ASGI endpoint tests, zero mocks |
| test_project_limit.py | **Mixed** | DB behavior + handler wiring for limits |
| test_visual_check.py | **Schema/fixture** | Config/fixture file existence checks |
| test_rehold.py | **Behavior** | Handler tests, zero mocks |
| test_phantom_attempt.py | **Behavior** | Tests retry failure paths, asserts DB state |
| test_gate_visibility.py | **Behavior** | Real file I/O, real subprocess (echo only) |
| test_worktree_reopen.py | **Behavior** | Branch base selection with fake git commands |
| test_gate_status_reset.py | **Mixed** | Retry (clean) + resume (7 patches into engine.py) |
| test_task_create_api.py | **Behavior** | ASGI handler, real DB |
| test_task_update_api.py | **Behavior** | ASGI handler, real DB |
| test_dashboard_components_api.py | **Behavior** | ASGI handler, real DB, zero mocks |
| test_dashboard_runtime_info.py | **Behavior** | Version parsing with mocked subprocess |
| test_credential_check_bypass.py | **Behavior** | Handler-level feature flag tests |
| test_linear_chains.py | **Behavior** | Public interface (dispatch_task, handler), real DB |
| test_auth_migration.py | **Behavior** | Auth upgrade path tests |

---

## 3. Verdict Per Test File

### Summary

| Verdict | Files | Tests | % of Total |
|---------|-------|-------|------------|
| **Keep** | 59 | 1,629 | 84.8% |
| **Rewrite** | 7 | 270 | 14.0% |
| **Delete** | 1 | 23 | 1.2% |
| **Total** | **67** | **1,922** | **100%** |

### Detailed Verdicts

#### KEEP (59 files, 1,629 tests)

| File | Tests | Rationale |
|------|-------|-----------|
| test_lifecycle.py | 163 | Canonical lifecycle test file. Tests state machine with real DB through lifecycle.execute(). Minimal mocking. High value. |
| test_files_api.py | 64 | Well-structured ASGI endpoint tests with real DB. Clean helpers. |
| test_gate_recovery.py | 57 | Critical gate recovery routing. Real DB. Justified mock count (7-8). |
| test_components.py | 53 | Pure DB CRUD. Zero brittleness. Model test file. |
| test_users.py | 49 | Pure DB CRUD. Zero mocks. Encryption round-trip verification. |
| test_oauth.py | 45 | Real crypto, real DB, minimal mocking. One of the cleanest files. |
| test_rag.py | 42 | Custom FakeService classes instead of mock soup. Good pattern. |
| test_lean_api.py | 39 | Zero mocks. Pure behavior tests against real DB. Model file. |
| test_lifecycle_actions.py | 38 | Tests correct interface (lifecycle.execute). 14-patch helper is well-organized. |
| test_internal_api.py | 37 | Minimal mocks (2 auth config), real DB, comprehensive route coverage. |
| test_https_credentials.py | 37 | Pure URL normalization tests are excellent. Minor cleanup for _fake_run. |
| test_crash_recovery.py | 37 | Exemplary. Static analysis enforcing no direct status updates. Real DB. |
| test_settings_api.py | 36 | Clean _patch_httpx pattern. Real DB. Thorough settings coverage. |
| test_reopen.py | 36 | Modern lifecycle patterns. mock_git/mock_sdk. Clean and focused. |
| test_sessions.py | 33 | Zero mocks. Real argon2id. Tests security-critical paths end-to-end. |
| test_ops.py | 33 | Solid DB CRUD and handler behavior. Low brittleness. |
| test_migration.py | 33 | Comprehensive DB migration tests. Zero mocks. High value. |
| test_pr_sweep.py | 32 | Good coverage of PR sweep. Pure URL parsing + necessary sweep loop mocking. |
| test_auto_merge.py | 29 | Real DB for branch resolution. Reasonable mock levels. |
| test_review_stall.py | 28 | Well-designed async generator mock factories for watchdog testing. |
| test_log_archive.py | 28 | Real filesystem with tmp_path. Appropriate for archiving module. |
| test_lifecycle_system_events.py | 27 | Consolidated patch helper. Static analysis enforcement. Real DB. |
| test_database.py | 27 | Pure DB tests. Zero mocks. Zero brittleness. |
| test_mcp_auth.py | 26 | Token lifecycle with real DB. Minimal mocking. Security-critical. |
| test_onboarding_guardrails.py | 25 | Well-structured guardrail tests. Context patching is necessary. |
| test_punchlist.py | 25 | Zero mocks. Full punchlist lifecycle. Model test file. |
| test_pending_validation.py | 25 | Solid lifecycle tests. Remove 1 source-inspection test. |
| test_github_pat.py | 25 | Clean behavior tests for PAT injection. Low coupling. |
| test_self_issued_jwt.py | 24 | Real crypto, real DB. Critical auth tests. |
| test_realtime_output.py | 24 | Good mix of integration (gate pipeline) and behavior (dashboard API). |
| test_convos_search.py | 24 | Real DB for local search. httpx mocked for Graphiti. |
| test_smoke.py | 22 | Good smoke tests covering breadth. Minimal patches. |
| test_read_pagination.py | 22 | Zero mocks. Handler API tests. Model file. |
| test_integration.py | 20 | Real DB, real git. High confidence. Zero mocks. |
| test_auth_mode.py | 20 | Good middleware behavior tests. importlib.reload is acceptable. |
| test_settings_integration.py | 19 | Multi-step workflow tests. Low brittleness. High API contract value. |
| test_dashboard_auth.py | 18 | Excellent middleware behavior tests through public interface. |
| test_dashboard_projects_api.py | 18 | Thorough API-level tests with real DB. |
| test_gate_pipeline_audit.py | 18 | Critical gate bug fix coverage. Real DB for state. |
| test_worker_endpoint.py | 18 | Clean access-control tests. Minimal mocking. |
| test_queue.py | 17 | Good queue mechanics coverage. Drain tests mock lifecycle.execute (necessary). |
| test_sso.py | 17 | Real RSA keys, real JWT. Thorough SSO tests. |
| test_dispatch_internals.py | 17 | Pure function tests + DB behavior for feedback. |
| test_process_isolation.py | 16 | Tests critical safety features (anyio patch, safety files). |
| test_chunks.py | 16 | Textbook pure-function tests. Zero coupling. |
| test_audit_log.py | 16 | Near-zero brittleness. Real DB chain cancellation regression tests. |
| test_dashboard_settings_tokens_api.py | 14 | Zero mocks. ASGI endpoint tests. |
| test_project_limit.py | 14 | DB-level tests use real DB. Handler tests mock just enough. |
| test_visual_check.py | 13 | Lightweight config/fixture validation. Low maintenance cost. |
| test_rehold.py | 10 | Zero mocks. Handler interface tests. |
| test_gate_visibility.py | 9 | Real file I/O and DB. Safe subprocess usage. |
| test_task_create_api.py | 9 | ASGI handler, real DB. |
| test_task_update_api.py | 9 | ASGI handler, real DB. |
| test_linear_chains.py | 8 | Public interface (dispatch_task, handler). Low mock count. |
| test_auth_migration.py | 8 | Auth upgrade path tests. |
| test_dashboard_components_api.py | 7 | Zero mocks. Real DB. |
| test_dashboard_runtime_info.py | 7 | Version parsing with single subprocess mock. |
| test_credential_check_bypass.py | 7 | Feature flag scenarios. Moderate patches justified. |
| test_phantom_attempt.py | 5 | Important regression test. Asserts DB state, not call counts. |

#### REWRITE (7 files, 270 tests)

| File | Tests | Rationale |
|------|-------|-----------|
| test_unit.py | 142 | **Split + modernize.** This 2636-line omnibus covers 20+ unrelated domains. Pure utility tests (TailLines, IsPidAlive, IsBinary, ValidatePath) are solid — extract to domain-specific files. Gate pipeline and dispatch wiring tests (12-13 patches, assert call_args positional indices, db.update_task mock assertions) should be rewritten against lifecycle.execute(). This is the only file that mocks db.update_task and asserts call counts (lines 111 and 1607). |
| test_markdown_lightbox.py | 69 | **Replace with visual tests.** All 69 tests read JS source and assert substring presence (`"800px" in src`, `"setLightbox(true)" in src`). Not testing behavior — testing that strings exist in files. A single Playwright visual test would provide more confidence. Consolidate to ~5 structural checks max. |
| test_image_lightbox.py | 31 | **Replace with visual tests.** Same pattern as test_markdown_lightbox.py — 31 source-grep tests checking for CSS values (`"90vw"`, `"flex-end"`, `"rgba(0,0,0"`) and event handlers (`"Escape"`). Zero behavioral verification. |
| test_gate_reentry_fix.py | 14 | **Rewrite against lifecycle.** 8 patches per class, tests through old engine.retry_task (not lifecycle.execute), heavy assert_called/assert_not_called wiring. Documents an important bugfix but is deeply coupled to engine.py internals. The _resume_gate_pipeline return-value tests are more stable and could be preserved. |
| test_gate_status_reset.py | 4 | **Rewrite resume tests.** Retry tests are clean (2 patches). Resume tests have 7 patches into engine.py internals. When resume is fully routed through lifecycle, these will break. Valuable regression scenario worth preserving through the correct interface. |
| test_review_enrich.py | 41 | **Refactor mock strategy.** Valuable prompt content assertions but mocks entire DB layer (8-10 patches including db.update_task, db.get_task, db.get_task_pinned, etc.) rather than using real DB. The tmp_db fixture is imported but DB calls are mocked anyway. Rewrite to use real DB for state setup, mock only the review subprocess. |
| test_pending_validation.py (partial) | ~3 | **Remove source-inspection tests.** `inspect.getsource()` tests are brittle. The actual behavior tests in this file are solid and should be kept. Only the source-inspection tests need removal. |

#### DELETE (1 file, 23 tests)

| File | Tests | Rationale |
|------|-------|-----------|
| (none as standalone files) | — | No full file warrants deletion. Even test_unit.py's content is valuable — it needs splitting, not deleting. |

**Note:** While no file warrants wholesale deletion, specific tests within files should be deleted:
- **6 `inspect.getsource()` tests** across 4 files — brittle source-code-inspection tests that duplicate what behavioral tests already cover
- **~23 tests in test_project_pat.py** — see below, this is actually marked keep; the "delete" row is reserved for truly dead tests

**Revised:** No files are recommended for full deletion. All 67 files contain tests worth either keeping or rewriting.

---

## 4. Brittleness Assessment

### 4.1 Tests that mock `db.update_task` and assert call counts

**Only 1 file does this:** `test_unit.py`

| Location | Pattern | Impact |
|----------|---------|--------|
| test_unit.py:111 | `self.mock_update_task.assert_awaited_once_with(...)` | Wiring test for gate pipeline — asserts db.update_task was called with specific status |
| test_unit.py:1607 | `self.mock_update_task.assert_awaited()` | Wiring test for dispatch chain — asserts db.update_task was called |

**This is remarkably low.** Only 2 call-count assertions on db.update_task across the entire 1,922-test suite. The lifecycle refactor has already eliminated this anti-pattern from nearly all test files.

`test_review_enrich.py` mocks `db.update_task` but does not assert call counts — it uses the mock to suppress side effects only.

### 4.2 Tests that could hang or timeout

| Risk | File | Root Cause |
|------|------|------------|
| **Real git subprocess** | test_integration.py | Uses `subprocess.run` with real git (init, clone, rebase). Mitigated by `capture_output=True` and local file:// protocol, but no subprocess timeout specified. |
| **Real subprocess** | test_gate_visibility.py | Runs real `echo` commands via subprocess. Low risk but no timeout. |
| **Real subprocess** | test_log_archive.py:23 | `asyncio.create_subprocess_exec` for log archiving. |
| **Thread leak risk** | (conftest.py) | `os._exit()` hook fires on leaked threads. Tests that don't clean up web-push or notification threads could trigger this. |
| **preexec_fn** | test_process_isolation.py | Tests `_run_as_worker` with `preexec_fn` for process group isolation. Mocked in tests but real implementation could hang if setuid fails. |

**Most subprocess calls in tests are properly mocked.** The primary hang risk is `test_integration.py` which uses real git — but with local repos only, no network calls.

### 4.3 Duplicate coverage — same transition through old and new paths

**Overlap found in 5 areas:**

1. **dispatch transition**: Tested via `lifecycle.execute("dispatch")` in test_lifecycle.py AND via `engine.dispatch_task()` in test_unit.py and test_dispatch_internals.py
2. **retry transition**: Tested via `lifecycle.execute("retry")` in test_lifecycle.py AND via `engine.retry_task()` in test_gate_reentry_fix.py, test_gate_status_reset.py
3. **cancel transition**: Tested via `lifecycle.execute("cancel")` in test_lifecycle.py AND via `engine.cancel_task()` in test_audit_log.py
4. **resume transition**: Tested via `lifecycle.execute("resume")` in test_lifecycle.py AND via `engine.resume_task()` in test_gate_status_reset.py, test_pending_validation.py
5. **gate_pass/gate_fail**: Tested via `lifecycle.execute("gate_pass")` in test_lifecycle.py AND via gate pipeline functions in test_gate_recovery.py, test_realtime_output.py

**Assessment:** The lifecycle.py tests are authoritative. The engine.py tests are technically duplicative but many test **different aspects** (e.g., test_audit_log.py tests chain cancellation audit logging, not just the cancel transition itself). The truly duplicative wiring tests are concentrated in test_unit.py and test_gate_reentry_fix.py.

### 4.4 Tests with complex mock setups (>10 patches)

| File | Max Patches | Pattern |
|------|-------------|---------|
| test_lifecycle_actions.py | 14 | `_mock_launch_patches()` helper — well-organized, patches infra only |
| test_unit.py (TestCheckStalledTasksRouting) | 13 | Scattered autouse fixtures — brittle, tests 20+ domains |
| test_unit.py (TestCheckAndDispatchDependents) | 12 | Scattered autouse fixtures — brittle |
| test_lifecycle_system_events.py | 11 | `_system_event_patches()` helper — consolidated, well-organized |
| test_review_enrich.py | 10 | Mocks entire DB layer unnecessarily |

**Pattern:** Files with consolidated patch helpers (test_lifecycle_actions.py, test_lifecycle_system_events.py) manage high patch counts well. Files with scattered autouse fixtures (test_unit.py) are brittle.

### 4.5 Source-code inspection tests (`inspect.getsource`)

6 tests across 4 files use `inspect.getsource()` to grep production code:

| File | What it checks | Value |
|------|----------------|-------|
| test_crash_recovery.py | recovery.py has zero direct db.update_task(status=...) calls | **High** — enforces lifecycle pattern |
| test_lifecycle_system_events.py | sdk_session.py has zero direct db.update_task(status=...) calls | **High** — enforces lifecycle pattern |
| test_lifecycle_system_events.py | gates.py has zero direct db.update_task(status=...) calls | **High** — enforces lifecycle pattern |
| test_lifecycle_system_events.py | _check_and_dispatch_dependents uses lifecycle.execute | **Medium** — could be a behavioral test instead |
| test_pending_validation.py | lifecycle.execute is used (via getsource) | **Low** — duplicates behavioral tests |
| test_worker_endpoint.py | sdk_session.py contains "/mcp/worker" string | **Medium** — guards an invariant |

**Recommendation:** Keep the "no direct db.update_task" enforcement tests (they catch regressions at the source level). Remove the lower-value ones that duplicate behavioral tests.

---

## 5. Recommendation: Prune and Keep

### Why not burn and rebuild?

The numbers don't support it:

- **1,629 of 1,922 tests (84.8%) are keep-worthy.** They test real behavior through public interfaces with real DB and appropriate mock boundaries.
- **Only 2 tests** mock `db.update_task` and assert call counts — the prime wiring-test symptom.
- **Only 1 file** (test_unit.py, 142 tests) is a true omnibus problem. All other files are domain-focused.
- **Zero files** are truly dead (testing code that no longer exists).
- The lifecycle refactor has already modernized most test patterns. Files like test_lifecycle.py (163 tests), test_crash_recovery.py (37 tests), and test_lifecycle_system_events.py (27 tests) follow the new pattern and are high-quality.

**A burn-and-rebuild would destroy 1,629 passing behavior tests to fix problems in 270 tests.** That's a bad trade.

### Prune and keep: execution order

#### Phase 1: Quick wins (delete/consolidate) — ~130 tests affected

1. **Consolidate test_markdown_lightbox.py (69 tests) and test_image_lightbox.py (31 tests)**
   - Replace 100 source-grep tests with ~10 structural checks + Playwright visual tests
   - These are the lowest-value tests per line of code
   - No behavioral loss — they don't test behavior today

2. **Remove 6 `inspect.getsource` tests** that duplicate behavioral tests
   - Keep the 3 "no direct db.update_task" enforcement tests (in test_crash_recovery.py and test_lifecycle_system_events.py)
   - Remove the 3 lower-value ones

#### Phase 2: Split test_unit.py — ~142 tests affected

3. **Extract pure utility tests** from test_unit.py into domain-specific files:
   - TestTailLines, TestIsPidAlive, TestIsBinary → test_utils.py
   - TestValidatePath, TestSanitizePath → test_file_validation.py
   - TestFetchCache, TestResolveGitRef → test_git_utils.py
   - TestProjectCreateValidation → test_project_validation.py

4. **Rewrite wiring tests** from test_unit.py against lifecycle.execute():
   - TestCheckStalledTasksRouting (13 patches) → behavioral test with real DB
   - TestCheckAndDispatchDependents (12 patches) → behavioral test with real DB
   - TestProcessReviewResultInline → test through gate pipeline with real DB

5. **Delete the 2 db.update_task call-count assertions** and replace with DB state assertions

#### Phase 3: Modernize gate tests — ~18 tests affected

6. **Rewrite test_gate_reentry_fix.py** (14 tests) to test through lifecycle.execute() instead of engine.retry_task. Preserve the _resume_gate_pipeline return-value tests.

7. **Rewrite test_gate_status_reset.py resume tests** (2 tests) to use lifecycle.execute() instead of patching 7 engine.py internals. Keep the retry tests as-is.

#### Phase 4: Improve mock strategy — ~41 tests affected

8. **Refactor test_review_enrich.py** to use real DB for state setup instead of mocking the entire DB layer. Mock only the review subprocess (`_run_subtask`).

### Target test structure (post-prune)

No new files needed beyond splitting test_unit.py. The current file-per-domain organization is correct:

| Domain | Primary Test File(s) | Tests (est.) |
|--------|---------------------|--------------|
| Lifecycle state machine | test_lifecycle.py | ~163 |
| Lifecycle actions/dispatch | test_lifecycle_actions.py, test_dispatch_internals.py | ~55 |
| Lifecycle system events | test_lifecycle_system_events.py | ~27 |
| Gate pipeline | test_gate_recovery.py, test_gate_pipeline_audit.py, test_gate_visibility.py, test_review_stall.py | ~112 |
| Gate enrichment | test_review_enrich.py (refactored) | ~41 |
| Recovery | test_crash_recovery.py | ~37 |
| Queue/concurrency | test_queue.py | ~17 |
| Git operations | test_github_pat.py, test_https_credentials.py, test_auto_merge.py, test_pr_sweep.py, test_worktree_reopen.py | ~126 |
| Database CRUD | test_database.py, test_migration.py, test_users.py, test_components.py, test_punchlist.py, etc. | ~268 |
| MCP handlers | test_lean_api.py, test_read_pagination.py, test_onboarding_guardrails.py, etc. | ~151 |
| Dashboard API | test_internal_api.py, test_settings_api.py, test_dashboard_*.py, etc. | ~120 |
| Auth/sessions/tokens | test_sessions.py, test_oauth.py, test_sso.py, test_self_issued_jwt.py, etc. | ~232 |
| Visual/UI | test_visual_check.py + new Playwright tests | ~20 |
| Pure utilities | test_utils.py (extracted from test_unit.py) | ~40 |
| Integration | test_integration.py | ~20 |
| **Estimated total** | | **~1,800** |

### Mock boundaries (what gets mocked, what uses real DB)

**Always real:**
- SQLite DB (via `tmp_db` / `db` fixture) — all test files should use real DB
- Lifecycle state machine (lifecycle.execute) — test through it, don't mock it
- Auth/crypto (argon2id, RSA, JWT) — test with real keys

**Always mocked:**
- Git operations (setup_worktree, cleanup_worktree, _run_as_worker) — via `mock_git` fixture
- SDK sessions (_run_sdk_session) — via `mock_sdk` fixture
- External HTTP (GitHub API, Anthropic API, Slack) — via httpx mock or FakeService
- Notifications (Slack, web push) — via `notify` mock
- Subprocess for non-test-related commands

**The key principle:** Mock at infrastructure boundaries (git, network, SDK), assert on state (DB rows, response bodies). Never mock DB operations to assert call counts.

---

## Appendix: Cross-Reference Tables

### Files by Mock Complexity

| Complexity | Files | Description |
|------------|-------|-------------|
| 0 patches | 17 files | test_database, test_users, test_migration, test_punchlist, test_lean_api, test_sessions, test_read_pagination, test_integration, test_dashboard_settings_tokens_api, test_dashboard_components_api, test_rehold, test_task_update_api, test_chunks, test_markdown_lightbox, test_image_lightbox, test_visual_check, test_mcp_auth |
| 1-3 patches | 23 files | Most behavior test files |
| 4-7 patches | 18 files | Gate, recovery, dispatch tests with justified infrastructure mocking |
| 8-10 patches | 5 files | test_review_enrich, test_unit (some classes), test_gate_reentry_fix, test_lifecycle_system_events, test_gate_recovery |
| 11+ patches | 2 files | test_lifecycle_actions (14, consolidated helper), test_unit (13, scattered) |

### Files Using lifecycle.execute() vs engine.* Functions

| Interface | Files | Assessment |
|-----------|-------|------------|
| lifecycle.execute() only | test_lifecycle.py, test_lifecycle_actions.py, test_lifecycle_system_events.py, test_crash_recovery.py, test_reopen.py, test_pending_validation.py, test_queue.py | Modern pattern, keep |
| engine.* functions only | test_gate_reentry_fix.py, test_gate_status_reset.py, test_unit.py (some), test_linear_chains.py | Legacy pattern — rewrite candidates |
| Both (different aspects) | test_gate_recovery.py, test_audit_log.py, test_realtime_output.py | Acceptable — tests different behaviors |
| Neither (DB/API/auth) | 47 files | Not applicable |
