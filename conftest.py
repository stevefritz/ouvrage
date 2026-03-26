"""Shared fixtures for switchboard tests."""

import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Ensure the project root is on sys.path for test discovery
sys.path.insert(0, os.path.dirname(__file__))


# ---------------------------------------------------------------------------
# Core DB fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    """Point database.py at a temporary SQLite file and reset the singleton."""
    db_path = str(tmp_path / "test.db")
    os.environ["SWITCHBOARD_DB"] = db_path

    # Reset the real connection singleton and DB path (now in switchboard.db.connection)
    import switchboard.config.settings as _settings
    import switchboard.db.connection as _conn
    _settings.DB_PATH = db_path
    _conn.DB_PATH = db_path  # override the module-level binding in connection.py
    _conn._connection = None
    return db_path


@pytest.fixture
async def db(tmp_db):
    """Initialized database ready for use. Yields the database module."""
    import switchboard.db as _db
    await _db.init_db()
    yield _db
    await _db.close_db()
    # Reset connection singleton for test isolation
    import switchboard.db.connection as _conn
    _conn._connection = None


# ---------------------------------------------------------------------------
# Convenience fixtures — modelled on real production data shapes
# ---------------------------------------------------------------------------

@pytest.fixture
async def sample_project(db):
    """A registered project with typical config including env_overrides."""
    return await db.create_project(
        id="test-project",
        repo="git@github.com:acme/widgets.git",
        working_dir="/work/widgets",
        default_branch="main",
        test_command="python -m pytest tests/ -v",
        env_overrides={"NODE_ENV": "test", "DEBUG": "1"},
        max_turns=150,
        max_wall_clock=45,
        model="opus",
    )


@pytest.fixture
async def sample_task(db, sample_project):
    """A task in working status with checklist items and gate pipeline fields."""
    task = await db.create_task(
        id="test-project/implement-feature",
        project_id="test-project",
        goal="Implement the widget sorting feature",
        branch="implement-feature",
        auto_test=True,
        auto_review=True,
        review_model="sonnet",
        model="opus",
    )
    # Move to working status
    task = await db.update_task(task["id"], status="working")

    # Add checklist items
    await db.create_checklist_items(task["id"], [
        "Read existing widget code",
        "Implement sort algorithm",
        "Write unit tests",
        "Update documentation",
    ])

    return task


@pytest.fixture
async def sample_conversation(db, sample_project):
    """A conversation with messages including a pinned spec."""
    conv = await db.create_conversation(
        id="widget-redesign",
        project="test-project",
        goal="Plan the widget redesign for v2",
    )

    # Post a few messages
    await db.post_message(
        conversation_id="widget-redesign",
        author="stephen",
        content="We need to redesign the widget sorting. Current impl is O(n²).",
        type="note",
    )
    await db.post_message(
        conversation_id="widget-redesign",
        author="claude-ai",
        content="# Widget Redesign Spec\n\nReplace bubble sort with timsort.",
        type="spec",
        pinned=True,
    )
    await db.post_message(
        conversation_id="widget-redesign",
        author="claude-code",
        content="Implemented. PR ready for review.",
        type="status",
    )

    return conv


@pytest.fixture
async def completed_chain(db, sample_project):
    """A chain of 3 tasks with depends_on relationships: A → B → C."""
    task_a = await db.create_task(
        id="test-project/chain-a",
        project_id="test-project",
        goal="Build data models",
    )
    task_a = await db.update_task(task_a["id"],
        status="completed",
        gate_status="passed",
        gate_passed_at=db.now_iso(),
    )

    task_b = await db.create_task(
        id="test-project/chain-b",
        project_id="test-project",
        goal="Build API layer",
        depends_on="test-project/chain-a",
    )
    task_b = await db.update_task(task_b["id"],
        status="completed",
        gate_status="passed",
        gate_passed_at=db.now_iso(),
    )

    task_c = await db.create_task(
        id="test-project/chain-c",
        project_id="test-project",
        goal="Build frontend",
        depends_on="test-project/chain-b",
    )

    return {"a": task_a, "b": task_b, "c": task_c}


# ---------------------------------------------------------------------------
# Mock fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_git():
    """Mock all git/subprocess operations in dispatch engine.

    Patches: _run_as_worker, setup_worktree, cleanup_worktree.
    Returns a dict of the mocks for assertion.
    """
    mocks = {
        "run_as_worker": AsyncMock(return_value=(b"", b"", 0)),
        "setup_worktree": AsyncMock(return_value="/tmp/fake-worktree"),
        "cleanup_worktree": AsyncMock(),
    }

    patches = [
        patch("switchboard.dispatch.engine._run_as_worker", mocks["run_as_worker"]),
        patch("switchboard.dispatch.engine.setup_worktree", mocks["setup_worktree"]),
        patch("switchboard.dispatch.engine.cleanup_worktree", mocks["cleanup_worktree"]),
    ]
    for p in patches:
        p.start()
    yield mocks
    for p in patches:
        p.stop()


@pytest.fixture
def mock_sdk():
    """Mock the Claude Agent SDK. Returns a configurable mock.

    Default: returns a successful result. Set mock_sdk.result to customize.
    """
    mock_result = MagicMock()
    mock_result.type = "result"
    mock_result.text = "Task completed successfully."
    mock_result.input_tokens = 5000
    mock_result.output_tokens = 2000

    mock_agent = MagicMock()
    mock_agent.run = AsyncMock(return_value=mock_result)

    mock_module = MagicMock()
    mock_module.Agent = MagicMock(return_value=mock_agent)

    patcher = patch.dict("sys.modules", {"claude_agent_sdk": mock_module})
    patcher.start()
    yield {"agent": mock_agent, "result": mock_result, "module": mock_module}
    patcher.stop()
