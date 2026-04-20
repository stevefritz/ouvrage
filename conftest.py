"""Shared fixtures for ouvrage tests."""

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
    os.environ["OUVRAGE_DB"] = db_path

    # Ensure OUVRAGE_MASTER_KEY is set for tests so encryption works.
    # Generate a fresh random key if none is configured in the environment.
    if not os.environ.get("OUVRAGE_MASTER_KEY"):
        from cryptography.fernet import Fernet
        os.environ["OUVRAGE_MASTER_KEY"] = Fernet.generate_key().decode()

    # Redirect filesystem-backed defaults (UPLOADS_DIR, LOG_DIR) into tmp_path
    # so tests don't require /work or /opt/ouvrage to exist on the host.
    uploads_dir = str(tmp_path / "uploads")
    log_dir = str(tmp_path / "logs")
    os.environ["UPLOADS_DIR"] = uploads_dir
    os.environ["LOG_DIR"] = log_dir

    # Reset the real connection singleton and override module-level bindings.
    import ouvrage.config.settings as _settings
    import ouvrage.db.connection as _conn
    _settings.DB_PATH = db_path
    _settings.UPLOADS_DIR = uploads_dir
    _settings.LOG_DIR = log_dir
    _conn.DB_PATH = db_path
    _conn._connection = None
    return db_path


@pytest.fixture
async def db(tmp_db):
    """Initialized database ready for use. Yields the database module."""
    import ouvrage.db as _db
    await _db.init_db()
    # Update VEC_AVAILABLE flag so tests reflect actual vec0 table availability.
    from ouvrage.db.search import _check_vec_tables
    await _check_vec_tables()
    yield _db
    await _db.close_db()
    # Reset connection singleton for test isolation
    import ouvrage.db.connection as _conn
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

@pytest.fixture(autouse=True)
def _mock_credential_validation():
    """Auto-mock credential checks for all tests so dispatch can proceed without
    a real PAT configured. Two pre-flight gates need mocking:

    1. validate_project_access — full PAT scope check (clone/push/PR)
    2. resolve_credential — looks up if any credential exists for the provider

    Tests that need the real functions should patch them back with the
    `_real_*` references captured at module level.
    """
    from ouvrage.git.providers import GitHubProvider
    fake_provider_credential = (GitHubProvider(), "ghp_test_fake_credential")

    with patch("ouvrage.git.validation.validate_project_access", AsyncMock(return_value={
        "status": "validated",
        "message": "Credential validated",
        "checked_at": "2024-01-01T00:00:00Z",
        "detail": {"clone": True, "push": True, "pr": True},
    })), patch("ouvrage.git.providers.resolve_credential", AsyncMock(return_value=fake_provider_credential)):
        yield


# Capture the real functions at module level, before any autouse fixtures run.
from ouvrage.git.validation import validate_project_access as _real_validate_project_access
from ouvrage.git.providers import resolve_credential as _real_resolve_credential


@pytest.fixture
def real_resolve_credential():
    """Opt-out fixture for tests that need the real resolve_credential function.

    The autouse _mock_credential_validation fixture mocks resolve_credential
    so dispatch tests don't fail with 'no credential configured'. Tests that
    actually exercise credential resolution should request this fixture to
    restore the real function.
    """
    with patch("ouvrage.git.providers.resolve_credential", _real_resolve_credential):
        yield _real_resolve_credential


@pytest.fixture
def mock_git():
    """Mock all git/subprocess operations in dispatch engine and lifecycle.

    Patches: _run_as_worker, setup_worktree, cleanup_worktree, _ensure_branch_pushed,
    checkout_existing_worktree, and launch_sdk_session (prevents background asyncio
    tasks that would outlive the test and cause teardown timeouts).
    Returns a dict of the mocks for assertion.
    """
    mocks = {
        "run_as_worker": AsyncMock(return_value=(b"", b"", 0)),
        "setup_worktree": AsyncMock(return_value="/tmp/fake-worktree"),
        "cleanup_worktree": AsyncMock(),
        "ensure_branch_pushed": AsyncMock(return_value=True),
        "setup_hook_config": AsyncMock(),
        "checkout_existing_worktree": AsyncMock(return_value="/tmp/fake-worktree"),
        "launch_sdk_session": AsyncMock(),
        "validate_project_access": AsyncMock(return_value={
            "status": "validated",
            "message": "Credential validated",
            "checked_at": "2024-01-01T00:00:00Z",
            "detail": {"clone": True, "push": True, "pr": True},
        }),
    }

    patches = [
        patch("ouvrage.dispatch.engine._run_as_worker", mocks["run_as_worker"]),
        patch("ouvrage.dispatch.engine.setup_worktree", mocks["setup_worktree"]),
        patch("ouvrage.dispatch.engine.cleanup_worktree", mocks["cleanup_worktree"]),
        patch("ouvrage.git.operations._ensure_branch_pushed", mocks["ensure_branch_pushed"]),
        patch("ouvrage.dispatch.internals.setup_hook_config", mocks["setup_hook_config"]),
        patch("ouvrage.dispatch.internals.checkout_existing_worktree", mocks["checkout_existing_worktree"]),
        patch("ouvrage.dispatch.internals.launch_sdk_session", mocks["launch_sdk_session"]),
        patch("ouvrage.git.validation.validate_project_access", mocks["validate_project_access"]),
    ]
    for p in patches:
        p.start()
    yield mocks
    for p in patches:
        p.stop()


@pytest.fixture
def real_fs_worker():
    """Replace _run_as_worker with a direct subprocess exec (no setuid).

    Tests that exercise setup_hook_config and similar functions need real
    filesystem operations to occur, but can't use the production setuid path
    because tests don't run as root and don't have CAP_SETUID.

    This fixture patches _run_as_worker (in every module that imports it) to
    just exec the command in the current process — files actually get created,
    directories actually get made, but no privilege drop happens.

    NOTE: production _run_as_worker raises a generic PermissionError when
    setuid fails, which is hard to debug. Consider improving the error message
    upstream so it's clear that CAP_SETUID is required and tests should mock.
    """
    import asyncio as _asyncio

    async def _direct_exec(*cmd, **kwargs):
        env = kwargs.pop("env", None)
        proc = await _asyncio.create_subprocess_exec(
            *cmd,
            stdout=_asyncio.subprocess.PIPE,
            stderr=_asyncio.subprocess.PIPE,
            env=env,
            **kwargs,
        )
        stdout, stderr = await proc.communicate()
        return stdout, stderr, proc.returncode

    # Patch every module that imports _run_as_worker by name
    patches = [
        patch("ouvrage.git.worktree._run_as_worker", side_effect=_direct_exec),
        patch("ouvrage.dispatch.internals._run_as_worker", side_effect=_direct_exec, create=True),
        patch("ouvrage.dispatch.engine._run_as_worker", side_effect=_direct_exec),
    ]
    started = []
    for p in patches:
        try:
            started.append(p.start())
        except (AttributeError, ModuleNotFoundError):
            pass  # not all modules import it directly
    yield
    for p in patches:
        try:
            p.stop()
        except RuntimeError:
            pass


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
