---
name: pytest-workflow
description: "Efficient pytest workflow for this project's 900+ test suite. TRIGGER when: running tests, debugging test failures, verifying changes. ALWAYS read this before running pytest."
---

# Pytest Workflow — Efficient Test Running

This project has 900+ tests. Running them with `-v` produces massive output that will be truncated. Follow this workflow to avoid wasting turns.

## Step 1: Quick check — what failed?

```bash
timeout 200 python3 -m pytest tests/ -q --tb=line 2>&1 | tail -40
```

`-q` gives dots + a one-line-per-failure summary. `--tb=line` shows just the failing line. This fits in one screen.

## Step 2: Get details on ONLY the failures

```bash
timeout 60 python3 -m pytest tests/ --last-failed --tb=short -v
```

`--last-failed` reruns ONLY what failed in Step 1. Now `-v` and `--tb=short` are fine because there are only a few tests.

## Step 3: Fix and verify

```bash
timeout 60 python3 -m pytest tests/ --last-failed -v
```

Run just the previously-failing tests to confirm your fix.

## Step 4: Full suite confirmation

```bash
timeout 200 python3 -m pytest tests/ -q --tb=line 2>&1 | tail -40
```

One final quiet run to make sure nothing else broke.

## NEVER DO THIS

- **NEVER** run `pytest -v` on the full suite — 900+ lines of PASSED will be truncated
- **NEVER** run the full suite and pipe through `grep FAIL` — use `--last-failed` instead
- **NEVER** re-run the full suite with different grep/tail patterns to find failures — that's a sign you need `-q` or `--last-failed`
- **NEVER** run the same test command more than twice without changing code between runs

## Quick Reference

| Goal | Command |
|------|---------|
| What failed? | `timeout 200 pytest tests/ -q --tb=line 2>&1 \| tail -40` |
| Why did it fail? | `timeout 60 pytest tests/ --last-failed --tb=short -v` |
| Did my fix work? | `timeout 60 pytest tests/ --last-failed -v` |
| All green? | `timeout 200 pytest tests/ -q --tb=line 2>&1 \| tail -40` |
| One specific test | `timeout 60 pytest tests/test_foo.py::TestClass::test_method -v --tb=long` |

## Debugging gate failures

If the gate reports exit_code != 0 but all tests show PASSED:

1. **It's NOT a test failure.** Something is killing the process before pytest writes its summary.
2. **Common causes:**
   - **Timeout** — the suite takes ~150s, `timeout 180` barely clears it. If the VPS is under load, it can exceed the limit.
   - **Interactive stdin hang** — a test makes a real git call (push/fetch/clone) without mocking. Git prompts for `Username for 'https://github.com':`, the process hangs, timeout kills it.
   - **Thread leak** — `conftest.py` has an `os._exit()` hook that fires when leaked threads are detected after all tests pass.
3. **How to diagnose:** Run the suite locally and WATCH for hangs. `grep -rn` for unmocked git calls in the test that was most recently added.
4. **Do NOT re-run the full suite trying to find a failure that doesn't exist.**
