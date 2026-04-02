"""Tests for review prompt enrichment: component context, ignore patterns, punchlist, tags."""

import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# ---------------------------------------------------------------------------
# _filter_diff_by_ignore_patterns — pure function
# ---------------------------------------------------------------------------

class TestFilterDiffByIgnorePatterns:
    def setup_method(self):
        from switchboard.git.operations import _filter_diff_by_ignore_patterns
        self.fn = _filter_diff_by_ignore_patterns

    SAMPLE_DIFF = """\
diff --git a/src/app.py b/src/app.py
index abc..def 100644
--- a/src/app.py
+++ b/src/app.py
@@ -1,3 +1,4 @@
+import os
 import sys
 print("hello")
diff --git a/package-lock.json b/package-lock.json
index 111..222 100644
--- a/package-lock.json
+++ b/package-lock.json
@@ -1,2 +1,3 @@
+  "version": "2.0.0",
   "name": "myapp"
diff --git a/.switchboard/session.jsonl b/.switchboard/session.jsonl
index 333..444 100644
--- a/.switchboard/session.jsonl
+++ b/.switchboard/session.jsonl
@@ -1 +1,2 @@
+{"ts": "now"}
 {"ts": "before"}
"""

    def test_no_patterns_returns_full_diff(self):
        result = self.fn(self.SAMPLE_DIFF, [])
        assert result == self.SAMPLE_DIFF

    def test_strips_package_lock(self):
        result = self.fn(self.SAMPLE_DIFF, ["package-lock.json"])
        assert "package-lock.json" not in result
        assert "src/app.py" in result
        assert ".switchboard/" in result

    def test_strips_switchboard(self):
        result = self.fn(self.SAMPLE_DIFF, [".switchboard/"])
        assert ".switchboard/" not in result
        assert "src/app.py" in result
        assert "package-lock.json" in result

    def test_strips_multiple_patterns(self):
        result = self.fn(self.SAMPLE_DIFF, ["package-lock.json", ".switchboard/"])
        assert "package-lock.json" not in result
        assert ".switchboard/" not in result
        assert "src/app.py" in result

    def test_no_match_returns_full_diff(self):
        result = self.fn(self.SAMPLE_DIFF, ["*.rb"])
        assert result == self.SAMPLE_DIFF

    def test_empty_diff(self):
        result = self.fn("", ["package-lock.json"])
        assert result == ""

    def test_preserves_content_of_kept_files(self):
        result = self.fn(self.SAMPLE_DIFF, ["package-lock.json", ".switchboard/"])
        assert "+import os" in result
        assert "import sys" in result


# ---------------------------------------------------------------------------
# Tag-based guidance selection
# ---------------------------------------------------------------------------

class TestTagReviewGuidance:
    def setup_method(self):
        from switchboard.config.constants import _TAG_REVIEW_GUIDANCE, _DEFAULT_REVIEW_GUIDANCE
        self.tag_guidance = _TAG_REVIEW_GUIDANCE
        self.default_guidance = _DEFAULT_REVIEW_GUIDANCE

    def test_backend_tag_guidance_exists(self):
        assert "backend" in self.tag_guidance
        assert "error handling" in self.tag_guidance["backend"].lower()

    def test_frontend_tag_guidance_exists(self):
        assert "frontend" in self.tag_guidance
        assert "accessibility" in self.tag_guidance["frontend"].lower()

    def test_testing_tag_guidance_exists(self):
        assert "testing" in self.tag_guidance
        assert "coverage" in self.tag_guidance["testing"].lower()

    def test_default_guidance_is_balanced(self):
        assert "balanced" in self.default_guidance.lower()


# ---------------------------------------------------------------------------
# _dispatch_review prompt enrichment — integration with mocked DB/git
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_run_subtask():
    """Mock _run_subtask to capture prompt and return completed status."""
    captured = {}

    async def fake_run_subtask(task_id, subtask_type, prompt, model, **kwargs):
        captured["prompt"] = prompt
        captured["task_id"] = task_id
        return {"status": "completed"}

    return captured, fake_run_subtask


@pytest.fixture
def base_task():
    return {
        "id": "test-project/my-task",
        "goal": "Implement login feature",
        "component_id": None,
        "worktree_path": "/tmp/fake-worktree",
        "branch": "my-task",
        "review_model": "opus",
    }


@pytest.fixture
def base_project():
    return {
        "id": "test-project",
        "default_branch": "main",
        "review_ignore_patterns": None,
    }


async def _run_dispatch_review(task, project, captured, fake_run_subtask):
    """Helper: patches everything and runs _dispatch_review, returns captured prompt."""
    from switchboard.dispatch.gates import _dispatch_review

    with patch("switchboard.db.update_task", AsyncMock()), \
         patch("switchboard.db.get_task", AsyncMock(return_value=task)), \
         patch("switchboard.db.get_task_pinned", AsyncMock(return_value={"content": "# Spec\nDo the thing"})), \
         patch("switchboard.db.read_task_messages", AsyncMock(return_value={"messages": []})), \
         patch("switchboard.db.list_punchlist", AsyncMock(return_value=[])), \
         patch("switchboard.db.get_component", AsyncMock(return_value=None)), \
         patch("switchboard.dispatch.gates._run_as_worker", AsyncMock(return_value=(b"", b"", 0))), \
         patch("switchboard.dispatch.gates._run_subtask", fake_run_subtask), \
         patch("switchboard.dispatch.gates._process_review_result_inline", AsyncMock()):
        await _dispatch_review(task["id"], project, task)

    return captured.get("prompt", "")


class TestDispatchReviewComponentContext:
    async def test_no_component_shows_placeholder(self, tmp_db):
        from switchboard.dispatch.gates import _dispatch_review
        task = {
            "id": "test-project/my-task", "goal": "Do thing", "component_id": None,
            "worktree_path": "/tmp/wt", "branch": "my-task", "review_model": "opus",
        }
        project = {"id": "test-project", "default_branch": "main", "test_command": "pytest"}
        captured = {}

        async def fake_subtask(task_id, subtask_type, prompt, model, **kwargs):
            captured["prompt"] = prompt
            return {"status": "completed"}

        with patch("switchboard.db.update_task", AsyncMock()), \
             patch("switchboard.db.get_task", AsyncMock(return_value=task)), \
             patch("switchboard.db.get_task_pinned", AsyncMock(return_value={"content": "spec"})), \
             patch("switchboard.db.read_task_messages", AsyncMock(return_value={"messages": []})), \
             patch("switchboard.db.get_component", AsyncMock(return_value=None)), \
             patch("switchboard.dispatch.gates._run_as_worker", AsyncMock(return_value=(b"", b"", 0))), \
             patch("switchboard.dispatch.gates._run_subtask", fake_subtask), \
             patch("switchboard.dispatch.gates._process_review_result_inline", AsyncMock()):
            await _dispatch_review(task["id"], project, task)

        assert "No component assigned" in captured["prompt"]

    async def test_component_context_included(self, tmp_db):
        from switchboard.dispatch.gates import _dispatch_review
        task = {
            "id": "test-project/my-task", "goal": "Do thing", "component_id": "auth",
            "worktree_path": "/tmp/wt", "branch": "my-task", "review_model": "opus",
        }
        project = {"id": "test-project", "default_branch": "main", "test_command": "pytest"}
        fake_component = {
            "id": "auth", "name": "Auth Service",
            "description": "Handles authentication", "phase": "implementing",
        }
        captured = {}

        async def fake_subtask(task_id, subtask_type, prompt, model, **kwargs):
            captured["prompt"] = prompt
            return {"status": "completed"}

        with patch("switchboard.db.update_task", AsyncMock()), \
             patch("switchboard.db.get_task", AsyncMock(return_value=task)), \
             patch("switchboard.db.get_task_pinned", AsyncMock(return_value={"content": "spec"})), \
             patch("switchboard.db.read_task_messages", AsyncMock(return_value={"messages": []})), \
             patch("switchboard.db.list_punchlist", AsyncMock(return_value=[])), \
             patch("switchboard.db.get_component", AsyncMock(return_value=fake_component)), \
             patch("switchboard.dispatch.gates._run_as_worker", AsyncMock(return_value=(b"", b"", 0))), \
             patch("switchboard.dispatch.gates._run_subtask", fake_subtask), \
             patch("switchboard.dispatch.gates._process_review_result_inline", AsyncMock()):
            await _dispatch_review(task["id"], project, task)

        prompt = captured["prompt"]
        assert "Auth Service" in prompt
        assert "Handles authentication" in prompt
        assert "implementing" in prompt


class TestDispatchReviewPromptStructure:
    """Tests for the new reviewer prompt structure: identity, lifecycle, self-run diff."""

    async def _run(self, task_overrides=None, project_overrides=None):
        from switchboard.dispatch.gates import _dispatch_review
        task = {
            "id": "test-project/my-task", "goal": "Do thing", "component_id": None,
            "worktree_path": "/tmp/wt", "branch": "my-task", "review_model": "opus",
            "project_id": "test-project", "base_branch": "main", "current_attempt": 1,
        }
        if task_overrides:
            task.update(task_overrides)
        project = {"id": "test-project", "test_command": "pytest -v", "default_branch": "main", **(project_overrides or {})}
        captured = {}

        async def fake_subtask(task_id, subtask_type, prompt, model, **kwargs):
            captured["prompt"] = prompt
            return {"status": "completed"}

        with patch("switchboard.db.update_task", AsyncMock()), \
             patch("switchboard.db.get_task", AsyncMock(return_value=task)), \
             patch("switchboard.db.get_task_pinned", AsyncMock(return_value={"content": "spec"})), \
             patch("switchboard.db.read_task_messages", AsyncMock(return_value={"messages": []})), \
             patch("switchboard.db.get_component", AsyncMock(return_value=None)), \
             patch("switchboard.dispatch.gates._run_as_worker", AsyncMock(return_value=(b"", b"", 0))), \
             patch("switchboard.dispatch.gates._run_subtask", fake_subtask), \
             patch("switchboard.dispatch.gates._process_review_result_inline", AsyncMock()):
            await _dispatch_review(task["id"], project, task)

        return captured.get("prompt", "")

    async def test_reviewer_identity_present(self, tmp_db):
        prompt = await self._run()
        assert "You are an Ouvrage code reviewer" in prompt

    async def test_lifecycle_section_present(self, tmp_db):
        prompt = await self._run()
        assert "Task Lifecycle" in prompt
        assert "Test gate" in prompt
        assert "final gate before code ships" in prompt

    async def test_test_command_injected(self, tmp_db):
        prompt = await self._run()
        assert "pytest -v" in prompt

    async def test_self_run_diff_instruction_present(self, tmp_db):
        prompt = await self._run()
        assert "git diff" in prompt
        assert "origin/main...HEAD" in prompt

    async def test_no_pre_built_diff_injected(self, tmp_db):
        prompt = await self._run()
        # Prompt should not contain a raw diff blob (no diff header lines)
        assert "diff --git" not in prompt

    async def test_base_branch_in_diff_instruction(self, tmp_db):
        prompt = await self._run({"base_branch": "develop"})
        assert "origin/develop...HEAD" in prompt

    async def test_base_branch_falls_back_to_project_default(self, tmp_db):
        prompt = await self._run({"base_branch": None})
        assert "origin/main...HEAD" in prompt

    async def test_worktree_path_in_prompt(self, tmp_db):
        prompt = await self._run({"worktree_path": "/work/some-task"})
        assert "/work/some-task" in prompt

    async def test_task_goal_in_prompt(self, tmp_db):
        prompt = await self._run({"goal": "Add OAuth login"})
        assert "Add OAuth login" in prompt

    async def test_exact_title_guidance_present(self, tmp_db):
        prompt = await self._run()
        assert '"APPROVED"' in prompt or "APPROVED" in prompt
        assert "CHANGES REQUESTED" in prompt

    async def test_severity_calibration_present(self, tmp_db):
        prompt = await self._run()
        assert "Request changes when" in prompt
        assert "Approve when" in prompt

    async def test_feedback_format_blockers_present(self, tmp_db):
        prompt = await self._run()
        assert "BLOCKER" in prompt
        assert "SUGGESTION" in prompt

    async def test_no_retry_leniency_on_first_attempt(self, tmp_db):
        prompt = await self._run({"current_attempt": 1})
        assert "This is a retry" not in prompt

    async def test_retry_leniency_on_second_attempt(self, tmp_db):
        prompt = await self._run({"current_attempt": 2})
        assert "This is a retry" in prompt
        assert "cosmetic issues" in prompt

    async def test_tests_passed_stated_as_fact(self, tmp_db):
        prompt = await self._run()
        assert "tests passed (exit code 0) or you would not be running" in prompt

    async def test_ignore_guidance_hardcoded(self, tmp_db):
        prompt = await self._run()
        assert "lockfiles" in prompt
        assert ".switchboard/" in prompt


class TestDispatchReviewPunchlistClaims:
    async def test_punchlist_claims_included(self, tmp_db):
        from switchboard.dispatch.gates import _dispatch_review
        task = {
            "id": "test-project/my-task", "goal": "Fix bugs", "component_id": "api",
            "worktree_path": "/tmp/wt", "branch": "my-task", "review_model": "opus",
        }
        project = {"id": "test-project", "test_command": "pytest", "default_branch": "main"}
        fake_component = {
            "id": "api", "name": "API", "description": None, "phase": "dev",
        }
        claimed_items = [
            {"id": 1, "item": "Fix null pointer in login handler", "status": "claimed"},
            {"id": 2, "item": "Handle empty username edge case", "status": "claimed"},
        ]
        captured = {}

        async def fake_subtask(task_id, subtask_type, prompt, model, **kwargs):
            captured["prompt"] = prompt
            return {"status": "completed"}

        with patch("switchboard.db.update_task", AsyncMock()), \
             patch("switchboard.db.get_task", AsyncMock(return_value={"gate_status": "test-passed"})), \
             patch("switchboard.db.get_task_pinned", AsyncMock(return_value={"content": "spec"})), \
             patch("switchboard.db.read_task_messages", AsyncMock(return_value={"messages": []})), \
             patch("switchboard.db.list_punchlist", AsyncMock(return_value=claimed_items)), \
             patch("switchboard.db.get_component", AsyncMock(return_value=fake_component)), \
             patch("switchboard.dispatch.gates._run_as_worker", AsyncMock(return_value=(b"", b"", 0))), \
             patch("switchboard.dispatch.gates._run_subtask", fake_subtask), \
             patch("switchboard.dispatch.gates._process_review_result_inline", AsyncMock()):
            await _dispatch_review(task["id"], project, task)

        prompt = captured["prompt"]
        assert "Fix null pointer in login handler" in prompt
        assert "Handle empty username edge case" in prompt
        assert "Verify" in prompt  # "Verify they are actually addressed"
        assert "#1" in prompt
        assert "#2" in prompt

    async def test_no_punchlist_shows_none(self, tmp_db):
        from switchboard.dispatch.gates import _dispatch_review
        task = {
            "id": "test-project/my-task", "goal": "Do thing", "component_id": "api",
            "worktree_path": "/tmp/wt", "branch": "my-task", "review_model": "opus",
        }
        project = {"id": "test-project", "test_command": "pytest", "default_branch": "main"}
        fake_component = {
            "id": "api", "name": "API", "description": None, "phase": "dev",
        }
        captured = {}

        async def fake_subtask(task_id, subtask_type, prompt, model, **kwargs):
            captured["prompt"] = prompt
            return {"status": "completed"}

        with patch("switchboard.db.update_task", AsyncMock()), \
             patch("switchboard.db.get_task", AsyncMock(return_value={"gate_status": "test-passed"})), \
             patch("switchboard.db.get_task_pinned", AsyncMock(return_value={"content": "spec"})), \
             patch("switchboard.db.read_task_messages", AsyncMock(return_value={"messages": []})), \
             patch("switchboard.db.list_punchlist", AsyncMock(return_value=[])), \
             patch("switchboard.db.get_component", AsyncMock(return_value=fake_component)), \
             patch("switchboard.dispatch.gates._run_as_worker", AsyncMock(return_value=(b"", b"", 0))), \
             patch("switchboard.dispatch.gates._run_subtask", fake_subtask), \
             patch("switchboard.dispatch.gates._process_review_result_inline", AsyncMock()):
            await _dispatch_review(task["id"], project, task)

        assert "None." in captured["prompt"]


class TestDispatchReviewPriorReviewHistory:
    """Tests for prior review carry-forward in the reviewer prompt."""

    async def _run_with_prior_reviews(self, prior_msgs, current_attempt=2):
        from switchboard.dispatch.gates import _dispatch_review
        task = {
            "id": "test-project/my-task", "goal": "Do thing", "component_id": None,
            "worktree_path": "/tmp/wt", "branch": "my-task", "review_model": "opus",
            "project_id": "test-project", "base_branch": "main",
            "current_attempt": current_attempt,
        }
        project = {"id": "test-project", "test_command": "pytest", "default_branch": "main"}
        captured = {}

        # Build messages: prior reviews have type="review", author="cc-worker", attempt_number < current
        all_msgs = prior_msgs

        def _read_messages_side_effect(task_id, type=None):
            if type == "review":
                return {"messages": [m for m in all_msgs if m.get("type") == "review"]}
            return {"messages": [m for m in all_msgs if m.get("type") != "review"]}

        async def fake_subtask(task_id, subtask_type, prompt, model, **kwargs):
            captured["prompt"] = prompt
            return {"status": "completed"}

        with patch("switchboard.db.update_task", AsyncMock()), \
             patch("switchboard.db.get_task", AsyncMock(return_value=task)), \
             patch("switchboard.db.get_task_pinned", AsyncMock(return_value={"content": "spec"})), \
             patch("switchboard.db.read_task_messages", AsyncMock(side_effect=_read_messages_side_effect)), \
             patch("switchboard.db.get_component", AsyncMock(return_value=None)), \
             patch("switchboard.dispatch.gates._run_as_worker", AsyncMock(return_value=(b"", b"", 0))), \
             patch("switchboard.dispatch.gates._run_subtask", fake_subtask), \
             patch("switchboard.dispatch.gates._process_review_result_inline", AsyncMock()):
            await _dispatch_review(task["id"], project, task)

        return captured.get("prompt", "")

    async def test_no_prior_reviews_no_section(self, tmp_db):
        prompt = await self._run_with_prior_reviews([], current_attempt=1)
        assert "Prior Review History" not in prompt

    async def test_prior_review_section_included(self, tmp_db):
        prior = [{"type": "review", "author": "cc-worker", "attempt_number": 1,
                  "content": "Missing error handling in auth module"}]
        prompt = await self._run_with_prior_reviews(prior, current_attempt=2)
        assert "Prior Review History" in prompt
        assert "Missing error handling in auth module" in prompt

    async def test_carry_forward_instruction_present(self, tmp_db):
        prior = [{"type": "review", "author": "cc-worker", "attempt_number": 1,
                  "content": "Some review content"}]
        prompt = await self._run_with_prior_reviews(prior, current_attempt=2)
        assert "Do NOT re-flag resolved issues" in prompt
        assert "carry-forward requirements" in prompt

    async def test_course_corrections_override_language(self, tmp_db):
        from switchboard.dispatch.gates import _dispatch_review
        task = {
            "id": "test-project/my-task", "goal": "Do thing", "component_id": None,
            "worktree_path": "/tmp/wt", "branch": "my-task", "review_model": "opus",
            "project_id": "test-project", "base_branch": "main", "current_attempt": 1,
        }
        project = {"id": "test-project", "test_command": "pytest", "default_branch": "main"}
        captured = {}

        human_msg = {"type": "note", "author": "stephen", "title": "Scope change",
                     "content": "Skip the frontend part"}

        def _read_messages_side_effect(task_id, type=None):
            if type == "review":
                return {"messages": []}
            return {"messages": [human_msg]}

        async def fake_subtask(task_id, subtask_type, prompt, model, **kwargs):
            captured["prompt"] = prompt
            return {"status": "completed"}

        with patch("switchboard.db.update_task", AsyncMock()), \
             patch("switchboard.db.get_task", AsyncMock(return_value=task)), \
             patch("switchboard.db.get_task_pinned", AsyncMock(return_value={"content": "spec"})), \
             patch("switchboard.db.read_task_messages", AsyncMock(side_effect=_read_messages_side_effect)), \
             patch("switchboard.db.get_component", AsyncMock(return_value=None)), \
             patch("switchboard.dispatch.gates._run_as_worker", AsyncMock(return_value=(b"", b"", 0))), \
             patch("switchboard.dispatch.gates._run_subtask", fake_subtask), \
             patch("switchboard.dispatch.gates._process_review_result_inline", AsyncMock()):
            await _dispatch_review(task["id"], project, task)

        prompt = captured["prompt"]
        assert "Course Corrections" in prompt
        assert "override the original spec where they conflict" in prompt
        assert "Skip the frontend part" in prompt


class TestDispatchReviewFetchBeforeDiff:
    """Verify that git fetch origin is called before the review prompt is built."""

    async def _run_capturing_fetch(self, task_overrides=None):
        from switchboard.dispatch.gates import _dispatch_review
        task = {
            "id": "test-project/my-task", "goal": "Do thing", "component_id": None,
            "worktree_path": "/tmp/fake-worktree", "branch": "my-task", "review_model": "opus",
            "project_id": "test-project", "base_branch": "main", "current_attempt": 1,
        }
        if task_overrides:
            task.update(task_overrides)
        project = {"id": "test-project", "test_command": "pytest -v", "default_branch": "main"}
        fetch_calls = []
        prompt_captured = {}

        async def fake_run_as_worker(*args, **kwargs):
            fetch_calls.append(args)
            return (b"", b"", 0)

        async def fake_subtask(task_id, subtask_type, prompt, model, **kwargs):
            prompt_captured["prompt"] = prompt
            return {"status": "completed"}

        with patch("switchboard.db.update_task", AsyncMock()), \
             patch("switchboard.db.get_task", AsyncMock(return_value=task)), \
             patch("switchboard.db.get_task_pinned", AsyncMock(return_value={"content": "spec"})), \
             patch("switchboard.db.read_task_messages", AsyncMock(return_value={"messages": []})), \
             patch("switchboard.db.get_component", AsyncMock(return_value=None)), \
             patch("switchboard.dispatch.gates._run_as_worker", fake_run_as_worker), \
             patch("switchboard.dispatch.gates._run_subtask", fake_subtask), \
             patch("switchboard.dispatch.gates._process_review_result_inline", AsyncMock()):
            await _dispatch_review(task["id"], project, task)

        return fetch_calls, prompt_captured.get("prompt", "")

    async def test_fetch_called_before_review(self, tmp_db):
        fetch_calls, _ = await self._run_capturing_fetch()
        assert len(fetch_calls) >= 1
        # At least one call must be a git fetch origin <branch>
        assert any(
            "fetch" in args and "origin" in args
            for args in fetch_calls
        )

    async def test_fetch_uses_correct_base_branch(self, tmp_db):
        fetch_calls, _ = await self._run_capturing_fetch({"base_branch": "develop"})
        assert any(
            "fetch" in args and "origin" in args and "develop" in args
            for args in fetch_calls
        )

    async def test_fetch_falls_back_to_project_default(self, tmp_db):
        fetch_calls, _ = await self._run_capturing_fetch({"base_branch": None})
        assert any(
            "fetch" in args and "origin" in args and "main" in args
            for args in fetch_calls
        )

    async def test_prompt_uses_origin_prefix_in_diff(self, tmp_db):
        _, prompt = await self._run_capturing_fetch()
        assert "origin/main...HEAD" in prompt
        # Old bare ref must not appear without origin/ prefix
        assert "git diff main...HEAD" not in prompt

    async def test_prompt_uses_origin_prefix_with_custom_branch(self, tmp_db):
        _, prompt = await self._run_capturing_fetch({"base_branch": "release"})
        assert "origin/release...HEAD" in prompt
        assert "git diff release...HEAD" not in prompt

    async def test_fetch_skipped_when_no_worktree(self, tmp_db):
        """No crash if worktree_path is None — fetch is skipped gracefully."""
        fetch_calls, prompt = await self._run_capturing_fetch({"worktree_path": None})
        # fetch should not have been called
        assert not any("fetch" in args for args in fetch_calls)
        # prompt still uses origin/ prefix regardless
        assert "origin/main...HEAD" in prompt
