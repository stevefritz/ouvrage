# Test Triage V2: Necessity-Based Assessment

**Project:** mcp-switchboard
**Date:** 2026-04-02
**Total test files:** 67 | **Total tests:** 1,921 | **Test code:** ~31K lines | **Production code:** ~18K lines
**Ratio:** 1.7x more test code than production code; 1 test per ~9 lines of production code

---

## Executive Summary

The previous triage asked "is this a good test?" and concluded 85% should be kept. This triage asks a harder question: **"does this test earn its keep?"**

**Findings:**
- **~350 tests** would adequately protect production if built from zero
- **~1,200 tests** are worth keeping in the current suite (defense in depth, regression coverage)
- **~720 tests** can be deleted or consolidated without meaningful production risk
- The biggest waste is **redundancy across files**, not individual bad tests
- The biggest brittleness risk is **mock depth in gate/lifecycle tests**, not timing or threading

**Recommended action:** Delete 7 files outright (~250 tests), consolidate 3 gate files into 1 (~70 tests saved), split test_unit.py. Net result: ~1,200 tests across ~58 files.

---

## 1. Risk-Based Assessment

### Category Definitions

| Category | Definition | Count |
|----------|-----------|-------|
| **Critical** | Protects against data loss, security holes, state corruption, or broken dispatch. Real production risk if deleted. | 25 files, ~850 tests |
| **Important** | Protects against degraded UX, wrong dashboard data, or broken workflows. Noticed and annoying. | 28 files, ~680 tests |
| **Nice-to-have** | Edge cases, cosmetic issues, or theoretical scenarios. Would take weeks to notice. | 8 files, ~210 tests |
| **Overhead** | Maintenance cost exceeds value of what it catches. | 6 files, ~180 tests |

### Per-File Categorization

#### CRITICAL (25 files, ~850 tests)

| File | Tests | What Breaks in Production If Deleted |
|------|-------|--------------------------------------|
| test_lifecycle.py | 163 | State machine accepts invalid transitions; tasks enter impossible states; dashboard shows wrong actions |
| test_lifecycle_actions.py | 38 | Dispatch/resume/retry/reopen workflows break; Bug #2 regression (resume clears gate state) |
| test_lifecycle_system_events.py | 27 | Tasks never complete; timeout/error not handled; gate results ignored; chains stall |
| test_reopen.py | 36 | Reopen feature broken; gate state lost; feedback confused; chain invalidation skipped |
| test_gate_recovery.py | 57 | Tasks stuck mid-gate after restart never resume; infinite retry loops |
| test_gate_pipeline_audit.py | 18 | Reviews fail to dispatch after tests; race conditions on concurrent tasks |
| test_gate_reentry_fix.py | 14 | Infinite loops: retry re-runs test gate for review failures instead of launching CC |
| test_crash_recovery.py | 37 | Tasks die on restart and never resume; chains deadlock; queue blocks |
| test_queue.py | 17 | No concurrency limit; FIFO violated; dependency order broken |
| test_process_isolation.py | 16 | Worker SIGTERM kills child processes; gh CLI unguarded; signal escape |
| test_sessions.py | 33 | Login broken; brute force possible; expired sessions valid |
| test_oauth.py | 45 | Claude.ai OAuth flow fails; PKCE attacks possible; token rotation bypassed |
| test_database.py | 27 | Schema validation lost; core CRUD broken |
| test_users.py | 49 | Auth broken; credentials leak in plaintext; API tokens non-functional |
| test_migration.py | 33 | Can't update task fields; bulk ops fail; config resolution broken |
| test_components.py | 53 | Component feature broken; task counts wrong; config inheritance lost |
| test_punchlist.py | 25 | Punchlist lifecycle broken; claim/resolve isolation lost |
| test_github_pat.py | 25 | Branches can't push; PR creation fails; auth errors unhandled |
| test_https_credentials.py | 37 | SSH URLs fail on push; credential helper not set up; PAT leaks to disk |
| test_worktree_reopen.py | 3 | Reopened tasks lose history; start fresh from main instead of existing branch |
| test_lean_api.py | 39 | Dashboard exposes embedding vectors; task detail views return wrong shapes |
| test_read_pagination.py | 22 | Message pagination broken; large conversations unusable |
| test_worker_endpoint.py | 18 | Workers can disable test gates and bypass controls (security regression) |
| test_onboarding_guardrails.py | 25 | Tasks created without credentials; projects with invalid PATs; orphaned resources |
| test_unit.py (critical classes) | ~70 | Chain invalidation, review verdict routing, stall detection, path traversal, branch push — see Section 1a |

#### IMPORTANT (28 files, ~680 tests)

| File | Tests | What Breaks in Production If Deleted |
|------|-------|--------------------------------------|
| test_pending_validation.py | 25 | Pending-validation gate resumption untested; escalate tool broken; worker allowlist lost |
| test_phantom_attempt.py | 5 | Failed retry leaves task in working status, blocking concurrency slot forever |
| test_dispatch_internals.py | 17 | Wrong config (max_turns/model); lost feedback on retry; worktree pipeline broken |
| test_linear_chains.py | 8 | Fan-out possible (task A → {B, C}); chain resolution could deadlock |
| test_gate_status_reset.py | 4 | Dashboard shows stale gate status; new retry starts with wrong state |
| test_gate_visibility.py | 9 | Dashboard can't show live test output; subtask logs inaccessible |
| test_review_enrich.py | 41 | Review prompt becomes skeletal; no context, guidance, or history for reviewer |
| test_review_stall.py | 28 | Reviewer session hangs indefinitely; no timeout; stalls don't escalate |
| test_auto_merge.py | 29 | Tasks merge to wrong branch; conflicts not detected; stuck worktrees |
| test_pr_sweep.py | 32 | Stale PR status; undetected merges; unguarded PR creation |
| test_sso.py | 17 | SaaS deployments can't authenticate; JWKS cache DoS |
| test_self_issued_jwt.py | 24 | JWT verification broken; revocation bypass; issuer confusion |
| test_mcp_auth.py | 26 | No audit trail; no user attribution for API operations |
| test_auth_mode.py | 20 | AUTH_MODE config broken; control plane redirects fail |
| test_ops.py | 33 | Stall detection fails; state definitions wrong; resume/retry confusion |
| test_project_pat.py | 23 | Project-specific GitHub PATs leak or get lost |
| test_audit_log.py | 16 | Audit trail lost; chain cancellation could cross-contaminate siblings |
| test_chunks.py | 16 | Semantic search degrades; messages ranked as blobs instead of sections |
| test_rag.py | 42 | Semantic search doesn't work; conversations unsearchable |
| test_smoke.py | 22 | Config resolution wrong; prompts malformed; chain queries broken |
| test_files_api.py | 64 | File upload broken; path traversal attacks possible; MIME validation lost |
| test_task_create_api.py | 9 | Dashboard task creation broken |
| test_task_update_api.py | 9 | Dashboard task editing broken |
| test_internal_api.py | 37 | Internal API exposed; concurrency limits not settable |
| test_dashboard_projects_api.py | 18 | Dashboard project CRUD broken |
| test_dashboard_components_api.py | 7 | Dashboard component management broken |
| test_dashboard_settings_tokens_api.py | 14 | Token management broken; tokens could leak in responses |
| test_settings_api.py | 36 | Settings UI broken; credentials not persisted |
| test_unit.py (important classes) | ~50 | PR creation, prompt building, approve workflow, rebase, file ops — see Section 1a |

#### NICE-TO-HAVE (8 files, ~210 tests)

| File | Tests | What You'd Lose | How Long to Notice |
|------|-------|-----------------|--------------------|
| test_log_archive.py | 28 | Previous attempt logs lost; can't debug what went wrong | Days (first time someone debugs a retry) |
| test_realtime_output.py | 24 | Dashboard can't show live review progress or attempt history | Hours (dashboard users) |
| test_rehold.py | 10 | Can't pause queued tasks before dispatch | Weeks (rare workflow) |
| test_dashboard_runtime_info.py | 7 | Dashboard runtime info unavailable | Weeks (informational only) |
| test_project_limit.py | 14 | No project quota enforcement | Months (single-user system) |
| test_convos_search.py | 24 | Conversation search broken; Graphiti integration fails | Days |
| test_integration.py | 20 | Subtask/rebase integration edge cases untested | Weeks |
| test_auth_migration.py | 8 | One-time CLI migration untested | Never (runs once per deployment) |
| test_smoke.py (subset) | ~10 | Redundant config/prompt tests | Never (covered elsewhere) |
| test_unit.py (nice-to-have) | ~22 | TailLines, FetchCache, WebPushDispatch, ComponentStrippedFields | Weeks to never |

#### OVERHEAD (6 files, ~180 tests)

These tests cost more to maintain than the bugs they'd catch:

| File | Tests | Why It's Overhead |
|------|-------|-------------------|
| **test_markdown_lightbox.py** | 69 | Source-grep tests: reads JS, asserts substring presence (`"800px" in src`). Not testing behavior. Any refactor breaks every test. Zero runtime verification. |
| **test_image_lightbox.py** | 31 | Same pattern as markdown_lightbox. Asserts CSS values exist in JS source. Refactoring the component breaks all 31 tests even if behavior is identical. |
| **test_settings_integration.py** | 19 | ~90% redundant with test_settings_api. Tests same endpoints with same mocks. Pure duplication. |
| **test_dashboard_auth.py** | 18 | ~80% redundant with test_sessions. Tests session middleware that test_sessions already covers comprehensively. |
| **test_credential_check_bypass.py** | 7 | SKIP_CREDENTIAL_CHECK dev-mode flag. 27 mocks for 7 tests. Feature may be obsolete. |
| **test_visual_check.py** | 13 | Config/fixture existence checks for visual regression harness. Tests that reference images exist on disk, not that UI renders correctly. |
| **test_unit.py (overhead subset)** | ~22 | TestTailLines, TestFetchCache, TestWebPushDispatch, TestComponentHandlerStrippedFields — pure utility functions that rarely break, or optional features |

### 1a. test_unit.py Breakdown

This 2,636-line omnibus file contains 28 test classes covering 20+ domains. Most behaviors are **only tested here** (no other file covers them), which makes wholesale deletion impossible despite the file being a maintenance burden.

| Class | Lines | Tests | Category | Only Tested Here? |
|-------|-------|-------|----------|-------------------|
| TestInvalidateChain | 64-146 | 5 | Critical | Yes — chain stale marking |
| TestProcessReviewResultInline | 152-263 | 7 | Critical | Yes — review verdict routing |
| TestCheckAndDispatchDependents | 269-398 | 8 | Critical | Yes — chain advancement |
| TestValidatePath | 1816-1839 | 5 | Critical | Yes — path traversal prevention |
| TestEnsureBranchPushed | 974-1117 | 9 | Critical | Yes — git push automation |
| TestPushFailureBlocksGatePipeline | 1123-1309 | 8 | Critical | Yes — push failure → no gates |
| TestCheckStalledTasksRouting | 617-748 | 8 | Critical | Yes — stall/orphan detection |
| TestHeldWithDependsOn | 539-611 | 5 | Critical | Yes — held persistence across deps |
| TestIsPidAlive | 48-58 | 3 | Critical | Yes — recovery PID check |
| TestMaybeCreatePr | 404-533 | 8 | Important | Yes — PR creation pipeline |
| TestCheckStalledTasksHeldChain | 754-846 | 6 | Important | Yes — held + recovery |
| TestApproveHeldChainChild | 852-897 | 3 | Important | Yes — approval dispatch |
| TestApproveTaskResponse | 903-968 | 4 | Important | Yes — approval responses |
| TestBuildTaskPrompt | 1315-1497 | 10 | Important | Yes — worker prompt construction |
| TestBuildResumePrompt | 1503-1566 | 4 | Important | Yes — resume context |
| TestRebaseAndRedispatch | 1572-1642 | 4 | Important | Yes — rebase logic |
| TestIsBinary | 1787-1810 | 4 | Important | Yes — binary detection |
| TestListTaskFiles | 1845-1957 | 7 | Important | Partial (also test_files_api) |
| TestGetTaskFile | 1963-2117 | 9 | Important | Partial (also test_files_api) |
| TestGitRunTimeout | 2123-2137 | 2 | Important | Yes — timeout enforcement |
| TestResolveGitRef | 2234-2338 | 6 | Important | Yes — git ref priority |
| TestReactiveConversationInjection | 2344-2434 | 5 | Important | Yes — conversation nudging |
| TestHeldDefaults | 2440-2514 | 4 | Important | Yes — dispatch defaults |
| TestProjectCreateValidation | 2520-2577 | 3 | Important | Yes — required field validation |
| TestTailLines | 15-42 | 4 | Nice-to-have | Yes — pure utility |
| TestWebPushDispatch | 1648-1782 | 9 | Nice-to-have | Yes — notification routing |
| TestFetchCache | 2143-2228 | 4 | Nice-to-have | Yes — cache optimization |
| TestComponentHandlerStrippedFields | 2583-2637 | 5 | Nice-to-have | Yes — field stripping |

**Key insight:** 24 of 28 classes test behavior that exists ONLY in test_unit.py. The file is unmaintainable as-is, but can't be deleted — it must be split.

---

## 2. Redundancy Analysis

### Behaviors Tested by 3+ Files

| Behavior | Files | Total Tests | Best Single File | Waste |
|----------|-------|-------------|------------------|-------|
| **Gate interrupted vs rejected routing** | test_gate_recovery, test_gate_pipeline_audit, test_gate_reentry_fix, test_gate_status_reset | 93 | test_gate_recovery (57) | ~36 redundant tests |
| **Lifecycle dispatch/retry/resume** | test_lifecycle, test_lifecycle_actions, test_lifecycle_system_events, test_pending_validation, test_unit | 5 files | test_lifecycle (163) + test_lifecycle_actions (38) | ~30 redundant across others |
| **Held flag behavior** | test_unit (3 classes), test_lifecycle_actions, test_rehold, test_reopen | 4+ files | Scattered — no single owner | ~15 redundant |
| **Credential encryption** | test_users, test_project_pat, test_settings_api, test_https_credentials | 4 files | Each tests different layer | Low redundancy |
| **Session/dashboard auth middleware** | test_sessions, test_dashboard_auth, test_auth_mode | 3 files | test_sessions (33) | ~18 redundant (dashboard_auth) |
| **Settings/credential management** | test_settings_api, test_settings_integration, test_onboarding_guardrails | 3 files | test_settings_api (36) | ~19 redundant (settings_integration) |
| **Worker security/gh CLI guard** | test_worker_endpoint, test_process_isolation, test_pr_sweep | 3 files | test_process_isolation (16) | ~5 redundant |
| **File operations (list/get)** | test_unit (ListTaskFiles, GetTaskFile), test_files_api | 2 files | test_files_api (64) | ~16 redundant in test_unit |

### Consolidation Opportunities

**Gate pipeline (high-value consolidation):**
- test_gate_pipeline_audit.py (18 tests) + test_gate_reentry_fix.py (14 tests) + test_gate_status_reset.py (4 tests) → merge into test_gate_recovery.py
- These 3 files all test the same interrupted-vs-rejected routing from different angles
- Consolidation saves ~36 tests and 3 files with no coverage loss

**Auth middleware (simple deletion):**
- test_dashboard_auth.py (18 tests) is 80% redundant with test_sessions.py
- The ~4 unique tests (localhost bypass, static file passthrough) can move to test_sessions

**Settings (simple deletion):**
- test_settings_integration.py (19 tests) tests the same endpoints as test_settings_api with the same mocks
- Delete entirely; zero coverage loss

---

## 3. Brittleness Analysis

### Why Tests Break on Non-Bug Changes

The developer reports that tests frequently break or hang. The previous triage found "only 2 mock db.update_task assertions." That's true but misleading — brittleness in this codebase comes from three different sources:

#### 3a. Mock Depth (breaks on refactors)

The biggest brittleness source. Tests that mock 8+ internal functions break whenever any internal signature changes, even if behavior is identical.

| File | Max Patches | What Breaks It |
|------|-------------|----------------|
| test_lifecycle_actions.py | 14 | Any new side effect added to dispatch/resume/retry |
| test_unit.py (stall routing) | 13 | Any change to check_stalled_tasks internal flow |
| test_unit.py (chain dispatch) | 12 | Any change to _check_and_dispatch_dependents |
| test_lifecycle_system_events.py | 12 | Any new side effect on complete/timeout/gate_pass |
| test_unit.py (push failure) | 11 | Any change to gate pipeline entry |
| test_review_enrich.py | 10 | Any change to review prompt structure |
| test_review_stall.py | varies | Async generator mocking is inherently fragile |

**Root cause:** These tests mock infrastructure (git, SDK, notifications) to test business logic. The mocks are at the right boundary, but there are many boundaries. Adding a Slack notification to an existing transition breaks every test that mocks that transition.

**Mitigation strategy:** The `_mock_launch_patches()` helper in test_lifecycle_actions.py is the right pattern — centralized patch list so adding a new side effect requires updating one place. test_unit.py does NOT use this pattern (scattered autouse fixtures), which is why it's more brittle.

#### 3b. Global State Dependencies (creates inter-test coupling)

| Global | Location | Tests That Depend On It |
|--------|----------|------------------------|
| `_running_gates` | dispatch/_state.py | test_gate_pipeline_audit, test_gate_recovery, test_gate_reentry_fix |
| `_running_tasks` | dispatch/_state.py | test_unit (TestCheckStalledTasksRouting), SDK session tests |
| `_active_clients` | dispatch/_state.py | test_unit (TestCheckStalledTasksRouting) |
| `_fetch_cache` | git/files.py | test_unit (TestListTaskFiles, TestGetTaskFile, TestFetchCache, TestResolveGitRef) |

These are properly cleared in test setup, but if a test fails mid-execution, the teardown may not run, poisoning subsequent tests. This is a potential source of flaky test runs that pass individually but fail in suite.

#### 3c. Hang Risks (blocks entire test run)

| Risk | File | Trigger | Mechanism |
|------|------|---------|-----------|
| **Real git subprocess** | test_integration.py | Unmocked git init/clone/rebase | No subprocess timeout; hangs if filesystem is slow |
| **Real subprocess** | test_gate_visibility.py | `echo` via subprocess | Low risk but no timeout guard |
| **Credential prompt** | Any test with unmocked git push/fetch | Missing mock on _run_as_worker | Git prompts for `Username:`, stdin blocks, timeout kills pytest |
| **Thread leak** | conftest.py os._exit hook | Notification threads not cleaned up | After all tests pass, hook fires, exit code != 0, gate sees failure |
| **preexec_fn** | test_process_isolation.py | setuid/setgid in subprocess | Mocked in tests; real impl could hang on permission failure |

**The most common hang scenario:** A new test or refactor introduces a code path that calls `_run_as_worker` or `setup_worktree` without mocking it. Git tries to contact github.com, prompts for credentials, stdin blocks indefinitely. The `timeout` wrapper kills pytest, but exit_code=1 with zero visible test failures. This is the hardest to debug because "all tests passed" in the output.

#### 3d. Source-Grep Tests (break on any refactor)

100 tests in test_image_lightbox.py and test_markdown_lightbox.py read JS source code and assert substring presence. Any CSS value change, variable rename, or code reorganization breaks these tests even if the UI looks identical. These are the textbook definition of tests that break on non-bug changes.

---

## 4. Minimal Viable Test Suite

**Question: If we started from zero, what would we write?**

### By Risk Domain

#### Lifecycle State Machine (~80 tests)
The core of the system. Every valid transition, precondition enforcement, side effect ordering.

| What to Test | Tests | Why |
|-------------|-------|-----|
| Valid transitions (each state pair) | 30 | State corruption prevention |
| Precondition enforcement (illegal transitions) | 15 | Data integrity |
| Side effects for critical transitions (cancel, close, complete) | 15 | Correct cleanup and chain advancement |
| Dashboard actions API (available actions per state) | 10 | UI correctness |
| Reopen workflow (end-to-end) | 10 | User-facing feature |

*Currently covered by:* test_lifecycle.py (163), test_lifecycle_actions.py (38), test_lifecycle_system_events.py (27), test_reopen.py (36) = **264 tests**. About 3x what's minimally needed.

#### Gate Pipeline (~40 tests)
Test execution, review dispatch, retry limits, recovery.

| What to Test | Tests | Why |
|-------------|-------|-----|
| Test gate: run, capture output, pass/fail routing | 8 | Core quality gate |
| Review gate: dispatch, verdict parsing, retry | 8 | Review quality |
| Interrupted vs rejected routing | 6 | Prevents infinite loops |
| Gate recovery on restart | 6 | Server restart safety |
| Push failure blocks gates | 4 | Prevents testing wrong code |
| Retry limits → needs-review escalation | 4 | Terminal state correctness |
| Review prompt enrichment (context, history) | 4 | Review quality |

*Currently covered by:* 7 files, 223 tests. About 5.5x what's minimally needed.

#### Recovery & Queue (~30 tests)
Crash recovery, concurrency, stall detection.

| What to Test | Tests | Why |
|-------------|-------|-----|
| Orphan detection and classification | 6 | Post-restart correctness |
| Recovery priority ordering | 4 | Gate subtasks first |
| Flap prevention (max recovery attempts) | 3 | Infinite loop prevention |
| FIFO queue ordering | 4 | Fairness |
| Concurrency limit enforcement | 3 | Resource protection |
| Dependency-aware queuing | 3 | Chain correctness |
| Stall detection routing | 4 | Hung worker detection |
| Chain advancement on gate pass | 3 | Dependent task dispatch |

*Currently covered by:* test_crash_recovery (37), test_queue (17), test_unit stall classes (~14) = **68 tests**. About 2x what's minimally needed.

#### Data Integrity (~60 tests)
DB operations, auth, encryption.

| What to Test | Tests | Why |
|-------------|-------|-----|
| Schema validation (tables, columns exist) | 5 | Migration safety |
| Task CRUD + status transitions | 10 | Core data operations |
| User CRUD + credential encryption | 10 | Security |
| Session lifecycle (login, logout, expiry, lockout) | 8 | Auth security |
| OAuth flow (authorize, token, refresh, revoke) | 10 | MCP auth |
| JWT signing and verification | 6 | Token security |
| Component + punchlist lifecycle | 8 | Feature data integrity |
| Audit log writes | 3 | Compliance |

*Currently covered by:* ~10 files, ~320 tests. About 5x what's minimally needed.

#### API Contracts (~50 tests)
MCP handlers, dashboard endpoints.

| What to Test | Tests | Why |
|-------------|-------|-----|
| MCP tool handler routing (happy path per tool) | 10 | MCP server works |
| Worker field restrictions | 5 | Security |
| Dashboard CRUD endpoints (project, task, component) | 15 | Dashboard works |
| Settings endpoints (credentials, tokens) | 8 | Onboarding works |
| File operations (list, get, upload) | 8 | Task artifact access |
| Pagination and filtering | 4 | Large dataset handling |

*Currently covered by:* ~15 files, ~350 tests. About 7x what's minimally needed.

#### Git Operations (~30 tests)
PAT injection, credential helper, merge, PR creation.

| What to Test | Tests | Why |
|-------------|-------|-----|
| PAT injection into HTTPS URLs | 5 | Push authentication |
| Credential helper setup and git config | 5 | Worker auth |
| Branch push (detect needed, execute, handle failure) | 5 | Code reaches remote |
| Auto-merge flow + conflict detection | 5 | Merge correctness |
| PR creation for chain tails | 3 | Workflow automation |
| PR status sweep | 3 | Status synchronization |
| Worktree reopen branch selection | 3 | Reopen correctness |
| Binary detection + path validation | 2 | Security |

*Currently covered by:* ~7 files, ~180 tests. About 6x what's minimally needed.

#### Process Safety (~10 tests)
Signal isolation, safety files, kill guards.

| What to Test | Tests | Why |
|-------------|-------|-----|
| Process group isolation (start_new_session) | 3 | Signal safety |
| Safety file existence + content | 3 | Worker guidance |
| gh CLI guard (PreToolUse hook) | 2 | Bypass prevention |
| Path traversal prevention | 2 | Security |

*Currently covered by:* test_process_isolation (16), test_unit (5) = **21 tests**. About 2x what's minimally needed.

#### Miscellaneous (~20 tests)
Prompts, search, notifications, config.

| What to Test | Tests | Why |
|-------------|-------|-----|
| Task prompt construction | 5 | Worker context |
| Resume prompt construction | 3 | Resume context |
| Message chunking | 4 | Search quality |
| Embedding + similarity search | 4 | Findability |
| Config resolution (task → project → default) | 4 | Correct behavior |

*Currently covered by:* ~5 files, ~100 tests. About 5x what's minimally needed.

### The Number

**We need approximately 350 tests to adequately protect production.**

The current suite has 1,921 tests — roughly 5.5x the minimum. That's not inherently wrong (defense in depth has value), but when the maintenance cost of those extra tests causes developers to avoid refactoring, the tests are working against their purpose.

**Recommended target: ~1,200 tests** (keeping defense in depth for critical paths, cutting pure overhead).

---

## 5. What to Cut

### DELETE (7 files, ~244 tests)

| File | Tests | Verdict | Production Risk Accepted |
|------|-------|---------|--------------------------|
| **test_markdown_lightbox.py** | 69 | DELETE | Zero — tests assert string presence in JS source, not behavior. A CSS value rename breaks every test. No runtime verification. |
| **test_image_lightbox.py** | 31 | DELETE | Zero — same source-grep pattern. Asserts `"90vw"` exists in JS. Refactoring component with identical output breaks all tests. |
| **test_settings_integration.py** | 19 | DELETE | Zero — 90% duplicate of test_settings_api. Tests same endpoints with same mocks. |
| **test_dashboard_auth.py** | 18 | DELETE | Near-zero — 80% covered by test_sessions. Move ~4 unique tests (localhost bypass, static passthrough) to test_sessions first. |
| **test_credential_check_bypass.py** | 7 | DELETE | Negligible — SKIP_CREDENTIAL_CHECK dev flag. 27 mocks for 7 tests. Feature may be obsolete. If it breaks, developers will notice immediately in dev. |
| **test_visual_check.py** | 13 | DELETE | Negligible — tests that config files and fixture images exist on disk. Not testing visual output. The visual-check.py script itself is the real test. |
| **test_auth_migration.py** | 8 | DELETE | Negligible — one-time CLI migration (migrate-auth). Runs once per deployment. If it breaks, the error is obvious (CLI fails with stack trace). |

### CONSOLIDATE (3 files → 1, saves ~36 tests and 3 files)

| Source Files | Target | What Changes |
|-------------|--------|--------------|
| test_gate_pipeline_audit.py (18) | → test_gate_recovery.py | Merge non-duplicate tests into gate_recovery. Both test interrupted-vs-rejected gate routing. |
| test_gate_reentry_fix.py (14) | → test_gate_recovery.py | Merge _resume_gate_pipeline return-value tests. Delete duplicate routing tests. |
| test_gate_status_reset.py (4) | → test_gate_recovery.py | Merge gate state lifecycle tests (retry clears, resume preserves). |

After consolidation: test_gate_recovery.py grows from 57 to ~75 tests (some duplicates dropped), but we lose 3 files and the maintenance overhead of understanding which file tests which aspect of the same gate state machine.

### SPLIT (1 file)

**test_unit.py (142 tests, 2636 lines) → split into domain files:**

| Destination | Classes to Move | Tests |
|-------------|----------------|-------|
| test_chain_dispatch.py (new) | TestInvalidateChain, TestCheckAndDispatchDependents, TestRebaseAndRedispatch | 17 |
| test_review_verdict.py (new) | TestProcessReviewResultInline | 7 |
| test_stall_detection.py (new) | TestCheckStalledTasksRouting, TestCheckStalledTasksHeldChain | 14 |
| test_approve.py (new) | TestApproveHeldChainChild, TestApproveTaskResponse, TestHeldWithDependsOn, TestHeldDefaults | 16 |
| test_git_push.py (new) | TestEnsureBranchPushed, TestPushFailureBlocksGatePipeline, TestGitRunTimeout | 19 |
| test_prompts.py (new) | TestBuildTaskPrompt, TestBuildResumePrompt | 14 |
| test_file_access.py (new) | TestListTaskFiles, TestGetTaskFile, TestResolveGitRef, TestFetchCache, TestValidatePath, TestIsBinary | 35 |
| test_notifications.py (new) | TestWebPushDispatch | 9 |
| test_project_validation.py (new) | TestProjectCreateValidation, TestComponentHandlerStrippedFields | 8 |
| test_utils.py (new) | TestTailLines, TestIsPidAlive | 7 |

This is the highest-value refactoring. test_unit.py is the #1 maintenance burden — 28 classes, 20+ domains, scattered autouse fixtures, no consolidated patch helpers.

### KEEP AS-IS (56 files)

Every other file keeps its current form. The full per-file verdict:

| File | Tests | Verdict | Notes |
|------|-------|---------|-------|
| test_lifecycle.py | 163 | Keep | Canonical lifecycle tests. High value. |
| test_lifecycle_actions.py | 38 | Keep | Critical dispatch/resume/retry side effects. |
| test_lifecycle_system_events.py | 27 | Keep | System events (complete, timeout, gate_pass). |
| test_reopen.py | 36 | Keep | Full reopen workflow. |
| test_gate_recovery.py | 57 | Keep + absorb 3 files | Becomes canonical gate pipeline test. |
| test_crash_recovery.py | 37 | Keep | Recovery is non-negotiable. |
| test_queue.py | 17 | Keep | Concurrency protection. |
| test_process_isolation.py | 16 | Keep | Process safety. |
| test_sessions.py | 33 | Keep + absorb 4 tests from dashboard_auth | Core auth. |
| test_oauth.py | 45 | Keep | OAuth flow. |
| test_sso.py | 17 | Keep | SaaS auth. |
| test_self_issued_jwt.py | 24 | Keep | JWT verification. |
| test_mcp_auth.py | 26 | Keep | API token auth. |
| test_auth_mode.py | 20 | Keep | Auth config. |
| test_database.py | 27 | Keep | Schema + CRUD foundation. |
| test_users.py | 49 | Keep | User auth + encryption. |
| test_migration.py | 33 | Keep | Task field expansion. |
| test_components.py | 53 | Keep | Component lifecycle. |
| test_punchlist.py | 25 | Keep | Punchlist lifecycle. |
| test_ops.py | 33 | Keep | Stall detection + state defs. |
| test_project_pat.py | 23 | Keep | PAT encryption. |
| test_audit_log.py | 16 | Keep | Audit trail + chain isolation. |
| test_github_pat.py | 25 | Keep | PAT injection + push. |
| test_https_credentials.py | 37 | Keep | Credential helper + bare clone. |
| test_worktree_reopen.py | 3 | Keep | Reopen branch selection. |
| test_auto_merge.py | 29 | Keep | Merge flow + conflicts. |
| test_pr_sweep.py | 32 | Keep | PR status polling. |
| test_lean_api.py | 39 | Keep | API response contracts. |
| test_read_pagination.py | 22 | Keep | Message pagination. |
| test_worker_endpoint.py | 18 | Keep | Worker security. |
| test_onboarding_guardrails.py | 25 | Keep | Credential guards. |
| test_files_api.py | 64 | Keep | File operations. |
| test_review_enrich.py | 41 | Keep | Review prompt quality. |
| test_review_stall.py | 28 | Keep | Stall detection + recovery. |
| test_log_archive.py | 28 | Keep | Attempt history. |
| test_realtime_output.py | 24 | Keep | Live output streaming. |
| test_pending_validation.py | 25 | Keep | Pending-validation feature. |
| test_phantom_attempt.py | 5 | Keep | Regression test. |
| test_dispatch_internals.py | 17 | Keep | Dispatch building blocks. |
| test_linear_chains.py | 8 | Keep | Chain constraint. |
| test_chunks.py | 16 | Keep | Search chunking. |
| test_rag.py | 42 | Keep | Embeddings + search. |
| test_smoke.py | 22 | Keep | Breadth coverage. |
| test_integration.py | 20 | Keep | Real git integration. |
| test_task_create_api.py | 9 | Keep | Dashboard task creation. |
| test_task_update_api.py | 9 | Keep | Dashboard task editing. |
| test_internal_api.py | 37 | Keep | Internal API. |
| test_dashboard_projects_api.py | 18 | Keep | Dashboard projects. |
| test_dashboard_components_api.py | 7 | Keep | Dashboard components. |
| test_dashboard_settings_tokens_api.py | 14 | Keep | Token management. |
| test_dashboard_runtime_info.py | 7 | Keep | Runtime info. |
| test_settings_api.py | 36 | Keep | Settings CRUD. |
| test_convos_search.py | 24 | Keep | Conversation search. |
| test_project_limit.py | 14 | Keep | Quota enforcement. |
| test_rehold.py | 10 | Keep | Re-hold feature. |
| test_gate_visibility.py | 9 | Keep | Dashboard gate output. |

### Summary of Changes

| Action | Files | Tests Removed | Tests Added | Net Change |
|--------|-------|---------------|-------------|------------|
| Delete 7 files | -7 | -165 | 0 | -165 |
| Consolidate 3 gate files into 1 | -3 | -36 (duplicates) | 0 | -36 |
| Move 4 tests from dashboard_auth to sessions | 0 | 0 | 0 | 0 |
| Split test_unit.py into 10 files | -1, +10 | 0 | 0 | 0 |
| **Total** | **-1 net** (67→58) | **-201** | **0** | **1,921→~1,720** |

### If You Want to Be More Aggressive

The above is the conservative recommendation. If you want to go harder, here are additional cuts with their tradeoffs:

| Additional Cut | Tests Saved | Risk Accepted |
|---------------|-------------|---------------|
| Delete test_rehold.py | 10 | Re-hold feature untested (rare workflow) |
| Delete test_dashboard_runtime_info.py | 7 | Runtime version display untested |
| Delete test_project_limit.py | 14 | Quota enforcement untested (single-user system) |
| Trim test_realtime_output.py to 8 tests | 16 | Less attempt tracking coverage |
| Trim test_log_archive.py to 10 tests | 18 | Less archive edge case coverage |
| Trim test_convos_search.py to 10 tests | 14 | Less Graphiti proxy coverage |
| Trim test_review_enrich.py to 20 tests | 21 | Less prompt detail coverage |
| Trim test_files_api.py to 30 tests | 34 | Less file operation edge cases |
| Delete test_integration.py | 20 | Real git integration untested |
| **Aggressive total** | **~155 more** | **~1,565 tests** |

---

## Appendix A: Production Code Coverage Map

What production file is tested by which test files:

| Production File | Lines | Test Files | Total Tests |
|----------------|-------|------------|-------------|
| dispatch/lifecycle.py | 1,467 | test_lifecycle, test_lifecycle_actions, test_lifecycle_system_events, test_pending_validation, test_reopen | 289 |
| dispatch/engine.py | 1,004 | test_unit (12 classes), test_audit_log, test_linear_chains, test_phantom_attempt | ~120 |
| dispatch/gates.py | 1,080 | test_gate_recovery, test_gate_pipeline_audit, test_gate_reentry_fix, test_gate_status_reset, test_review_enrich, test_review_stall, test_log_archive, test_realtime_output, test_gate_visibility | 223 |
| dispatch/recovery.py | 663 | test_crash_recovery | 37 |
| dispatch/sdk_session.py | 872 | test_unit (push failure), test_process_isolation | ~24 |
| dispatch/queue.py | 40 | test_queue | 17 |
| dispatch/pr_sweep.py | 121 | test_pr_sweep | 32 |
| dispatch/internals.py | 251 | test_dispatch_internals | 17 |
| db/tasks.py | 764 | test_database, test_migration, test_ops | ~93 |
| db/users.py | 344 | test_users | 49 |
| db/schema.py | 695 | test_database, test_migration | 60 |
| db/components.py | 283 | test_components | 53 |
| db/punchlist.py | 136 | test_punchlist | 25 |
| db/conversations.py | 134 | test_convos_search, test_read_pagination | 46 |
| db/audit.py | 47 | test_audit_log | 16 |
| auth/sessions.py | 351 | test_sessions, test_dashboard_auth | 51 |
| auth/oauth.py | 725 | test_oauth | 45 |
| auth/middleware.py | 467 | test_auth_mode, test_mcp_auth, test_dashboard_auth, test_worker_endpoint | 82 |
| auth/sso.py | 253 | test_sso | 17 |
| server/tools.py + dispatch.py | 1,349 | test_lean_api, test_onboarding_guardrails, test_files_api, test_read_pagination, test_rehold | 160 |
| dashboard/api.py | 2,043 | test_internal_api, test_dashboard_*.py, test_settings_api, test_settings_integration | 157 |
| git/operations.py | 515 | test_github_pat, test_auto_merge, test_pr_sweep, test_unit | ~94 |
| git/worktree.py | 362 | test_https_credentials, test_worktree_reopen, test_integration | 60 |
| git/files.py | 204 | test_unit (file classes), test_files_api | ~80 |

## Appendix B: Global State Inventory

| State Variable | Module | What It Tracks | Tests That Touch It |
|---------------|--------|---------------|---------------------|
| `_running_gates` | dispatch/_state.py | Set of task IDs with active gate pipeline | test_gate_pipeline_audit, test_gate_recovery, test_gate_reentry_fix |
| `_running_tasks` | dispatch/_state.py | Dict of task_id → asyncio.Task for SDK sessions | test_unit (stall classes) |
| `_active_clients` | dispatch/_state.py | Dict of task_id → MCP client handle | test_unit (stall classes) |
| `_fetch_cache` | git/files.py | Dict of repo_path → last_fetch_time (TTL cache) | test_unit (file classes) |
| `_self_issued_*` module vars | auth/middleware.py | Cached JWKS/issuer for self-issued JWT | test_self_issued_jwt |
| `_sso_jwks_cache` | auth/sso.py | Cached JWKS for SSO provider | test_sso |

All are properly cleared in test setup. Risk: if a test fails mid-execution, teardown may not run, poisoning subsequent tests in the same process.
