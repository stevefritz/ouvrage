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


    def test_no_match_returns_full_diff(self):
        result = self.fn(self.SAMPLE_DIFF, ["*.rb"])
        assert result == self.SAMPLE_DIFF


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


