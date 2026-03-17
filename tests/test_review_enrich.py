"""Tests for review prompt enrichment: component context, ignore patterns, punchlist, tags."""

import json
import pytest
from unittest.mock import AsyncMock, patch, MagicMock


# ---------------------------------------------------------------------------
# _filter_diff_by_ignore_patterns — pure function
# ---------------------------------------------------------------------------

class TestFilterDiffByIgnorePatterns:
    def setup_method(self):
        from tasks import _filter_diff_by_ignore_patterns
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
        from tasks import _TAG_REVIEW_GUIDANCE, _DEFAULT_REVIEW_GUIDANCE
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

    async def fake_run_subtask(task_id, subtask_type, prompt, model):
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
    from tasks import _dispatch_review
    import database as db_module

    with patch("tasks._get_branch_diff", AsyncMock(return_value="diff --git a/src/app.py b/src/app.py\n+new line\n")), \
         patch("tasks.db.update_task", AsyncMock()), \
         patch("tasks.db.get_task_pinned", AsyncMock(return_value={"content": "# Spec\nDo the thing"})), \
         patch("tasks.db.read_task_messages", AsyncMock(return_value={"messages": []})), \
         patch("tasks.db.get_task_tags", AsyncMock(return_value=[])), \
         patch("tasks.db.list_punchlist", AsyncMock(return_value=[])), \
         patch("tasks.db.get_component", AsyncMock(return_value=None)), \
         patch("tasks._run_subtask", fake_run_subtask), \
         patch("tasks._process_review_result_inline", AsyncMock()):
        await _dispatch_review(task["id"], project, task)

    return captured.get("prompt", "")


class TestDispatchReviewComponentContext:
    async def test_no_component_shows_placeholder(self, tmp_db):
        from tasks import _dispatch_review
        task = {
            "id": "test-project/my-task", "goal": "Do thing", "component_id": None,
            "worktree_path": "/tmp/wt", "branch": "my-task", "review_model": "opus",
        }
        project = {"id": "test-project", "default_branch": "main", "review_ignore_patterns": None}
        captured = {}

        async def fake_subtask(task_id, subtask_type, prompt, model):
            captured["prompt"] = prompt
            return {"status": "completed"}

        with patch("tasks._get_branch_diff", AsyncMock(return_value="diff --git a/f.py b/f.py\n")), \
             patch("tasks.db.update_task", AsyncMock()), \
             patch("tasks.db.get_task_pinned", AsyncMock(return_value={"content": "spec"})), \
             patch("tasks.db.read_task_messages", AsyncMock(return_value={"messages": []})), \
             patch("tasks.db.get_task_tags", AsyncMock(return_value=[])), \
             patch("tasks.db.get_component", AsyncMock(return_value=None)), \
             patch("tasks._run_subtask", fake_subtask), \
             patch("tasks._process_review_result_inline", AsyncMock()):
            await _dispatch_review(task["id"], project, task)

        assert "No component assigned" in captured["prompt"]

    async def test_component_context_included(self, tmp_db):
        from tasks import _dispatch_review
        task = {
            "id": "test-project/my-task", "goal": "Do thing", "component_id": "auth",
            "worktree_path": "/tmp/wt", "branch": "my-task", "review_model": "opus",
        }
        project = {"id": "test-project", "default_branch": "main", "review_ignore_patterns": None}
        fake_component = {
            "id": "auth", "name": "Auth Service",
            "description": "Handles authentication", "phase": "implementing",
            "review_ignore_patterns": None,
        }
        captured = {}

        async def fake_subtask(task_id, subtask_type, prompt, model):
            captured["prompt"] = prompt
            return {"status": "completed"}

        with patch("tasks._get_branch_diff", AsyncMock(return_value="diff --git a/f.py b/f.py\n")), \
             patch("tasks.db.update_task", AsyncMock()), \
             patch("tasks.db.get_task_pinned", AsyncMock(return_value={"content": "spec"})), \
             patch("tasks.db.read_task_messages", AsyncMock(return_value={"messages": []})), \
             patch("tasks.db.get_task_tags", AsyncMock(return_value=[])), \
             patch("tasks.db.list_punchlist", AsyncMock(return_value=[])), \
             patch("tasks.db.get_component", AsyncMock(return_value=fake_component)), \
             patch("tasks._run_subtask", fake_subtask), \
             patch("tasks._process_review_result_inline", AsyncMock()):
            await _dispatch_review(task["id"], project, task)

        prompt = captured["prompt"]
        assert "Auth Service" in prompt
        assert "Handles authentication" in prompt
        assert "implementing" in prompt


class TestDispatchReviewIgnorePatterns:
    async def test_default_ignores_applied(self, tmp_db):
        from tasks import _dispatch_review
        task = {
            "id": "test-project/my-task", "goal": "Do thing", "component_id": None,
            "worktree_path": "/tmp/wt", "branch": "my-task", "review_model": "opus",
        }
        project = {"id": "test-project", "default_branch": "main", "review_ignore_patterns": None}
        diff_with_lockfile = (
            "diff --git a/src/app.py b/src/app.py\n+good line\n"
            "diff --git a/package-lock.json b/package-lock.json\n+lock stuff\n"
        )
        captured = {}

        async def fake_subtask(task_id, subtask_type, prompt, model):
            captured["prompt"] = prompt
            return {"status": "completed"}

        with patch("tasks._get_branch_diff", AsyncMock(return_value=diff_with_lockfile)), \
             patch("tasks.db.update_task", AsyncMock()), \
             patch("tasks.db.get_task_pinned", AsyncMock(return_value={"content": "spec"})), \
             patch("tasks.db.read_task_messages", AsyncMock(return_value={"messages": []})), \
             patch("tasks.db.get_task_tags", AsyncMock(return_value=[])), \
             patch("tasks.db.get_component", AsyncMock(return_value=None)), \
             patch("tasks._run_subtask", fake_subtask), \
             patch("tasks._process_review_result_inline", AsyncMock()):
            await _dispatch_review(task["id"], project, task)

        prompt = captured["prompt"]
        assert "+good line" in prompt
        assert "+lock stuff" not in prompt

    async def test_component_ignore_patterns_override_defaults(self, tmp_db):
        from tasks import _dispatch_review
        task = {
            "id": "test-project/my-task", "goal": "Do thing", "component_id": "ui",
            "worktree_path": "/tmp/wt", "branch": "my-task", "review_model": "opus",
        }
        project = {"id": "test-project", "default_branch": "main", "review_ignore_patterns": None}
        # Component ignores only "custom_ignore.txt"
        fake_component = {
            "id": "ui", "name": "UI", "description": None, "phase": "dev",
            "review_ignore_patterns": json.dumps(["custom_ignore.txt"]),
        }
        diff = (
            "diff --git a/custom_ignore.txt b/custom_ignore.txt\n+ignored\n"
            "diff --git a/package-lock.json b/package-lock.json\n+lock stuff\n"
        )
        captured = {}

        async def fake_subtask(task_id, subtask_type, prompt, model):
            captured["prompt"] = prompt
            return {"status": "completed"}

        with patch("tasks._get_branch_diff", AsyncMock(return_value=diff)), \
             patch("tasks.db.update_task", AsyncMock()), \
             patch("tasks.db.get_task_pinned", AsyncMock(return_value={"content": "spec"})), \
             patch("tasks.db.read_task_messages", AsyncMock(return_value={"messages": []})), \
             patch("tasks.db.get_task_tags", AsyncMock(return_value=[])), \
             patch("tasks.db.list_punchlist", AsyncMock(return_value=[])), \
             patch("tasks.db.get_component", AsyncMock(return_value=fake_component)), \
             patch("tasks._run_subtask", fake_subtask), \
             patch("tasks._process_review_result_inline", AsyncMock()):
            await _dispatch_review(task["id"], project, task)

        prompt = captured["prompt"]
        # custom_ignore.txt stripped, but package-lock.json kept (not in component's list)
        assert "+ignored" not in prompt
        assert "+lock stuff" in prompt


class TestDispatchReviewPunchlistClaims:
    async def test_punchlist_claims_included(self, tmp_db):
        from tasks import _dispatch_review
        task = {
            "id": "test-project/my-task", "goal": "Fix bugs", "component_id": "api",
            "worktree_path": "/tmp/wt", "branch": "my-task", "review_model": "opus",
        }
        project = {"id": "test-project", "default_branch": "main", "review_ignore_patterns": None}
        fake_component = {
            "id": "api", "name": "API", "description": None, "phase": "dev",
            "review_ignore_patterns": None,
        }
        claimed_items = [
            {"id": 1, "item": "Fix null pointer in login handler", "status": "claimed"},
            {"id": 2, "item": "Handle empty username edge case", "status": "claimed"},
        ]
        captured = {}

        async def fake_subtask(task_id, subtask_type, prompt, model):
            captured["prompt"] = prompt
            return {"status": "completed"}

        with patch("tasks._get_branch_diff", AsyncMock(return_value="diff --git a/f.py b/f.py\n")), \
             patch("tasks.db.update_task", AsyncMock()), \
             patch("tasks.db.get_task_pinned", AsyncMock(return_value={"content": "spec"})), \
             patch("tasks.db.read_task_messages", AsyncMock(return_value={"messages": []})), \
             patch("tasks.db.get_task_tags", AsyncMock(return_value=[])), \
             patch("tasks.db.list_punchlist", AsyncMock(return_value=claimed_items)), \
             patch("tasks.db.get_component", AsyncMock(return_value=fake_component)), \
             patch("tasks._run_subtask", fake_subtask), \
             patch("tasks._process_review_result_inline", AsyncMock()):
            await _dispatch_review(task["id"], project, task)

        prompt = captured["prompt"]
        assert "Fix null pointer in login handler" in prompt
        assert "Handle empty username edge case" in prompt
        assert "Verify" in prompt  # "Verify they are actually addressed"
        assert "#1" in prompt
        assert "#2" in prompt

    async def test_no_punchlist_shows_none(self, tmp_db):
        from tasks import _dispatch_review
        task = {
            "id": "test-project/my-task", "goal": "Do thing", "component_id": "api",
            "worktree_path": "/tmp/wt", "branch": "my-task", "review_model": "opus",
        }
        project = {"id": "test-project", "default_branch": "main", "review_ignore_patterns": None}
        fake_component = {
            "id": "api", "name": "API", "description": None, "phase": "dev",
            "review_ignore_patterns": None,
        }
        captured = {}

        async def fake_subtask(task_id, subtask_type, prompt, model):
            captured["prompt"] = prompt
            return {"status": "completed"}

        with patch("tasks._get_branch_diff", AsyncMock(return_value="diff --git a/f.py b/f.py\n")), \
             patch("tasks.db.update_task", AsyncMock()), \
             patch("tasks.db.get_task_pinned", AsyncMock(return_value={"content": "spec"})), \
             patch("tasks.db.read_task_messages", AsyncMock(return_value={"messages": []})), \
             patch("tasks.db.get_task_tags", AsyncMock(return_value=[])), \
             patch("tasks.db.list_punchlist", AsyncMock(return_value=[])), \
             patch("tasks.db.get_component", AsyncMock(return_value=fake_component)), \
             patch("tasks._run_subtask", fake_subtask), \
             patch("tasks._process_review_result_inline", AsyncMock()):
            await _dispatch_review(task["id"], project, task)

        assert "None." in captured["prompt"]


class TestDispatchReviewTagGuidance:
    async def _run_with_tags(self, tags):
        from tasks import _dispatch_review
        task = {
            "id": "test-project/my-task", "goal": "Do thing", "component_id": None,
            "worktree_path": "/tmp/wt", "branch": "my-task", "review_model": "opus",
        }
        project = {"id": "test-project", "default_branch": "main", "review_ignore_patterns": None}
        captured = {}

        async def fake_subtask(task_id, subtask_type, prompt, model):
            captured["prompt"] = prompt
            return {"status": "completed"}

        with patch("tasks._get_branch_diff", AsyncMock(return_value="diff --git a/f.py b/f.py\n")), \
             patch("tasks.db.update_task", AsyncMock()), \
             patch("tasks.db.get_task_pinned", AsyncMock(return_value={"content": "spec"})), \
             patch("tasks.db.read_task_messages", AsyncMock(return_value={"messages": []})), \
             patch("tasks.db.get_task_tags", AsyncMock(return_value=tags)), \
             patch("tasks.db.get_component", AsyncMock(return_value=None)), \
             patch("tasks._run_subtask", fake_subtask), \
             patch("tasks._process_review_result_inline", AsyncMock()):
            await _dispatch_review(task["id"], project, task)

        return captured.get("prompt", "")

    async def test_backend_tag_adds_security_focus(self, tmp_db):
        prompt = await self._run_with_tags(["backend"])
        assert "error handling" in prompt.lower() or "security" in prompt.lower()

    async def test_frontend_tag_adds_accessibility_focus(self, tmp_db):
        prompt = await self._run_with_tags(["frontend"])
        assert "accessibility" in prompt.lower()

    async def test_testing_tag_adds_coverage_focus(self, tmp_db):
        prompt = await self._run_with_tags(["testing"])
        assert "coverage" in prompt.lower()

    async def test_no_tags_uses_default_guidance(self, tmp_db):
        prompt = await self._run_with_tags([])
        assert "balanced" in prompt.lower()

    async def test_unknown_tag_uses_default_guidance(self, tmp_db):
        prompt = await self._run_with_tags(["unicorn"])
        assert "balanced" in prompt.lower()
