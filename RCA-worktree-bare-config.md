# RCA: `git status` fails with `fatal: this operation must be run in a work tree`

**Branch:** `git-worktree-bare-rca`
**Date:** 2026-05-04
**Status:** Root cause identified. Recommended fix included. No code change in this branch.

## TL;DR

A 2026-04-07 commit removed the credential-helper code that wrote two related git
config settings as a pair:

1. `extensions.worktreeConfig = true` — written **once**, into the persistent `.bare/config`.
2. `core.bare = false` — written **per worktree**, into `.bare/worktrees/<name>/config.worktree`.

Removing both writes simultaneously was safe in theory, but the **first write is
sticky**: every install that ever invoked the credential helper has
`extensions.worktreeConfig = true` permanently in its `.bare/config`. The second
write was the override that kept `core.bare` from being inherited by worktrees.
With the override gone, every new worktree inherits `core.bare = true` from the
bare repo and git refuses every work-tree operation in it.

The bug technically dates to **2026-04-07** but did not become visible because
CC workers learn to work around it by passing `GIT_DIR` and `GIT_WORK_TREE` env
vars on every git command — burning ~5–15 turns per task on `git status`/`pwd`/
`ls .git` flailing before they discover the workaround.

## Timeline

| Date | Commit | Event |
|---|---|---|
| 2026-03-27 19:05 | `e91228c` | Stephen Fritz adds `extensions.worktreeConfig=true` (bare) + `--worktree`-scoped credential helper config to fix credential-helper config leakage. |
| 2026-03-27 19:08 | `dc29dd9` | Test update for `--worktree` scope. |
| 2026-03-27 19:21 | `e04463c` | Stephen adds `core.bare=false` worktree-scoped override after discovering `worktreeConfig` makes `core.bare` per-worktree and worktrees inherit `true` from bare. |
| 2026-04-07 20:17 | `ffe5eb2` | "Replace credential helper with server-side git_push/git_fetch MCP tools." `setup_credential_helper()` deleted in full, taking both `extensions.worktreeConfig=true` AND `core.bare=false` writes with it. **Bug introduced here.** |
| 2026-04-09 05:10 | — | First observed `fatal: this operation must be run in a work tree` in `.task-history` (task `coverage-driven-test-reduction`, attempt-1). |
| 2026-04-28 13:08 | — | Task `living-docs-merged-at` shows worker burning 7+ turns on `git status` then succeeding with `GIT_DIR=/work/.bare/worktrees/<name> GIT_WORK_TREE=/work/<name> git ...`. |
| 2026-05-04 16:25 | — | Diagnostic task `git-worktree-status-repro` confirms the bug pattern. |
| 2026-05-04 | — | This RCA. |

## What is currently setting `extensions.worktreeConfig = true`?

**Nothing in the current codebase.** Verified via:

```
$ grep -rn 'worktreeConfig\|--worktree\|config.worktree' .
(no matches)
```

The setting is a **leftover** from the pre-2026-04-07 credential helper. Once
written, `git config` settings persist; nothing removes it. Every install that
ran the credential helper between 2026-03-27 and 2026-04-07 — including all
existing Docker (mcp-switchboard) and baremetal (ap-erp) installs — has it
baked into `.bare/config` forever.

Confirmed on this install (`/work/mcp-switchboard/.bare/config`):

```
[extensions]
	worktreeConfig = true
```

That section sits between the `settings-polish-v2` and `settings-polish-v3`
branch entries — the rough position of writes from late March / early April,
which matches the timeline above.

## Why is `config.worktree` missing for every worktree?

`config.worktree` is **only** created when something explicitly calls
`git config --worktree <key> <value>`. `git worktree add` does **not** create
it. The deleted `setup_credential_helper()` was the only thing that ever
called `--worktree config`, and it did so for credential helper, remote URL,
and (after `e04463c`) `core.bare=false`.

Confirmed:

```
$ find /work/mcp-switchboard/.bare/worktrees/ -name 'config.worktree'
(no results)

$ ls /work/mcp-switchboard/.bare/worktrees/git-worktree-bare-rca/
HEAD ORIG_HEAD commondir gitdir index logs
```

No `config.worktree` exists on this branch's worktree. Every other worktree
on this install is the same. None of them has the `core.bare=false` override
that the bare config's `worktreeConfig=true` extension demands.

## Causal chain (full)

1. **2026-03-27** — Credential helper code is hardened to not leak into the bare
   config. To make `--worktree`-scoped writes possible, it sets
   `extensions.worktreeConfig=true` on the bare repo.
2. **2026-03-27** — Side effect discovered: with `worktreeConfig` enabled,
   `core.bare` becomes one of the settings that's worktree-scoped. Worktrees,
   lacking a `config.worktree`, fall through to the bare config and inherit
   `core.bare=true`. Fix: write `core.bare=false` to each worktree's
   `config.worktree` as part of credential-helper setup.
3. **Between 2026-03-27 and 2026-04-07** — credential helper runs on every new
   worktree; for each, it sets `extensions.worktreeConfig=true` once on the
   bare repo (idempotent) and `core.bare=false` once on the worktree config.
   Everything works.
4. **2026-04-07** — credential helper replaced by server-side `git_push`/
   `git_fetch` MCP tools. `setup_credential_helper()` deleted entirely.
   - The `extensions.worktreeConfig=true` write is gone.
   - The `core.bare=false` per-worktree write is gone.
   - **But the existing `extensions.worktreeConfig=true` setting in `.bare/config`
     is not removed** — git config writes are persistent and nothing in the new
     code path touches that setting.
5. **2026-04-07 onwards** — every new worktree:
   - is created via `git worktree add` (no `config.worktree` written),
   - inherits `core.bare=true` from the bare config (because `worktreeConfig`
     extension is still enabled and the worktree has no override),
   - fails every work-tree operation: `git status`, `git diff`, `git commit`,
     `git checkout`, `git add` — all of them refuse to run.

Existing worktrees that were created **before** 2026-04-07 still have their
`config.worktree` files with `core.bare=false` and continue to work. This is
why nothing broke on April 7 itself — only **new** worktrees from April 7
onward are affected. As old worktrees got cleaned up, the failure rate
climbed to 100% for new tasks.

## Why the issue feels recent (but isn't)

It's been broken for ~27 days. Workers have been masking the failure mode:

- **First evidence of the error**: 2026-04-09 (`coverage-driven-test-reduction`, attempt 1, 05:10 UTC).
- **49 archived tasks** in `.task-history/` contain the `must be run in a work tree` string.
- **Workaround pattern** that emerged: workers pass
  `GIT_DIR=/work/<base>/.bare/worktrees/<name> GIT_WORK_TREE=/work/<base>/<name>`
  on every git invocation. This bypasses config inheritance because the
  explicit `GIT_WORK_TREE` overrides `core.bare` resolution.
- **Cost**: a typical task with the workaround burns 5–15 extra turns flailing
  through `git status` → `pwd` → `ls .git` → `cat .git/HEAD` → eventually
  realizing they need explicit env vars. At ~$0.05/turn this is ~$0.25–0.75
  in waste per task. Across 49+ tasks, low triple digits in cumulative cost.
- **What probably triggered the recent realization**: the diagnostic task
  `git-worktree-status-repro` (2026-05-04) made the bug obvious by checking
  `.bare/config` and worktree metadata directly instead of just running
  commands and getting around them.

So the answer to "why now": it's not new. Workers were silently coping; the
diagnostic task removed the silence.

## Recommended fix

### Code fix (minimal, no behavior change for fresh installs)

In `ouvrage/git/worktree.py::setup_worktree()`, immediately after the
`git worktree add` call succeeds, write the per-worktree `core.bare=false`
override **only when needed**:

```python
# If the bare repo has extensions.worktreeConfig enabled (legacy from the
# pre-2026-04-07 credential helper), worktrees inherit core.bare=true unless
# they have a config.worktree with core.bare=false. Write the override so
# git status/commit/diff work in the worktree.
stdout, _, rc = await _run_as_worker(
    "git", "-C", bare_path, "config", "--get", "extensions.worktreeConfig",
)
if rc == 0 and stdout.decode().strip().lower() == "true":
    await _run_as_worker(
        "git", "-C", worktree_path, "config", "--worktree", "core.bare", "false",
    )
```

**Alternative (more aggressive)**: since the credential helper is gone and
nothing in the codebase relies on `extensions.worktreeConfig` anymore, simply
**unset it** on the bare repo. Once unset, worktrees stop inheriting `core.bare`
at all and the bug is gone permanently:

```python
# extensions.worktreeConfig is a leftover from the deleted credential helper.
# It causes worktrees to inherit core.bare=true from the bare repo. Remove it.
await _run_as_worker(
    "git", "-C", bare_path, "config", "--unset", "extensions.worktreeConfig",
)
```

This is idempotent (no-op if already unset) and self-heals existing installs
on next dispatch. The only risk is if some future feature wants to re-enable
worktreeConfig — which would be a deliberate decision and could re-add it.

**Recommendation**: do the aggressive variant. It's one line, self-healing,
and removes the foot-gun entirely. The minimal variant only papers over the
symptom; the aggressive variant fixes the underlying state.

Place the call **after** the `git worktree add` block at line ~310, before
the chmod / unset user.* / fetch refspec sequence at lines 316–328. Run it
even if `bare_path` already existed, so existing installs get repaired on
their next dispatch.

### Repair for existing bare repos (one-shot)

For each affected install — and to fix this branch's worktree right now without
a deploy — either:

**Option A (recommended)**: unset the extension on the bare config. Affects
all worktrees of that bare repo, immediately and forever.

```bash
git -C /work/mcp-switchboard/.bare config --unset extensions.worktreeConfig
```

(Run as the user that owns `.bare`, e.g. `ouvrage` or `switchboard`. Does not
require restart; effect is immediate on the next git command in any worktree.)

**Option B**: write the per-worktree override into each existing worktree:

```bash
for wt_meta in /work/mcp-switchboard/.bare/worktrees/*/; do
    wt_name=$(basename "$wt_meta")
    wt_path="/work/mcp-switchboard/$wt_name"
    [ -d "$wt_path" ] && \
        git -C "$wt_path" config --worktree core.bare false
done
```

This creates the missing `config.worktree` files. Less invasive but leaves the
extension enabled, so any **future** worktree created before the code fix
ships will hit the bug again.

### Why this isn't covered by tests

```
$ grep -rn 'core\.bare\|worktreeConfig\|config\.worktree' tests/
(no matches)
```

There is no test exercising the bare-repo + worktree inheritance interaction.
A regression test should: create a bare repo with `extensions.worktreeConfig=true`,
add a worktree, run `git status` in it, and assert exit 0. This belongs in
`tests/test_integration.py` (real git, no mocks) since the symptom only
manifests against actual git config behavior.

## Files referenced

- `ouvrage/git/worktree.py` — current `setup_worktree()`; needs the fix
- `/work/mcp-switchboard/.bare/config` — has the leftover `extensions.worktreeConfig=true`
- `.task-history/coverage-driven-test-reduction/attempt-1/session.jsonl` — first observed failure
- `.task-history/living-docs-merged-at/attempt-1/session.jsonl` — example of GIT_DIR/GIT_WORK_TREE workaround
- Commit `ffe5eb2` (2026-04-07) — the bug-introducing commit
- Commit `e04463c` (2026-03-27) — the original `core.bare=false` override that was deleted alongside the credential helper
