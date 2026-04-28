"""Tests for file embedding functions — index_doc_file, search_files_semantic,
search_file_chunks_semantic, get_doc_files_needing_chunking, backfill loop.

Covers:
- index_doc_file: end-to-end (whole-file + chunks stored), idempotent, sentinel for short/no-header
  content, silent skip for non-reference_doc role
- search_files_semantic: indexed file found for a relevant query (Python cosine fallback path)
- search_file_chunks_semantic: indexed chunk found; adjacent context attached
- get_doc_files_needing_chunking: returns unchunked reference_doc files only
- _backfill_file_chunks: processes a batch of pending files
"""

import os
import pytest

from ouvrage.embeddings.service import (
    EmbeddingService,
    encode_vector,
    set_embedding_service,
)
from ouvrage.db.search import (
    index_doc_file,
    set_file_embedding,
    get_doc_files_needing_chunking,
    search_files_semantic,
    search_file_chunks_semantic,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unit_vec(dim: int, idx: int) -> list[float]:
    v = [0.0] * dim
    v[idx % dim] = 1.0
    return v


class _FixedService(EmbeddingService):
    """Returns a fixed vector for all embed calls — no OpenAI needed."""

    def __init__(self, vec: list[float]):
        self._vec = vec

    async def embed(self, text: str) -> list[float]:
        return list(self._vec)


async def _create_reference_doc_file(db, tmp_path, file_id: str, content: str, project_id: str = "test-project") -> str:
    """Create a file on disk + in the DB with role='reference_doc'."""
    path = tmp_path / f"{file_id}.md"
    path.write_text(content, encoding="utf-8")
    # create_file doesn't accept role; set it via raw UPDATE after
    await db.create_file(
        id=file_id,
        filename=f"{file_id}.md",
        stored_path=str(path),
        mime_type="text/markdown",
        size_bytes=len(content.encode()),
        uploaded_by=None,
        project_id=project_id,
    )
    async with db.get_db() as conn:
        await conn.execute("UPDATE files SET role = 'reference_doc' WHERE id = ?", (file_id,))
        await conn.commit()
    return str(path)


# ---------------------------------------------------------------------------
# index_doc_file: end-to-end
# ---------------------------------------------------------------------------

class TestIndexDocFile:
    @pytest.fixture(autouse=True)
    def use_fixed_service(self):
        vec = _unit_vec(4, 0)  # non-1536-dim → uses Python cosine fallback in search
        set_embedding_service(_FixedService(vec))
        yield
        set_embedding_service(None)

    async def test_end_to_end_chunks_and_whole_file(self, db, sample_project, tmp_path):
        """index_doc_file writes chunks to file_chunks and whole-file to files_embeddings."""
        content = (
            "## Introduction\n\n"
            + "This section introduces the topic in detail.\n" * 10 + "\n"
            "## Details\n\n"
            + "This section covers the details at length.\n" * 10
        )
        await _create_reference_doc_file(db, tmp_path, "doc-e2e", content)

        await index_doc_file("doc-e2e")

        async with db.get_db() as conn:
            chunks = await conn.execute_fetchall(
                "SELECT chunk_index, heading, content FROM file_chunks WHERE file_id = ? ORDER BY chunk_index",
                ("doc-e2e",),
            )
            emb_rows = await conn.execute_fetchall(
                "SELECT file_id, embedding FROM files_embeddings WHERE file_id = ?",
                ("doc-e2e",),
            )

        # Chunks with real indices must exist
        real_chunks = [c for c in chunks if c["chunk_index"] >= 0]
        assert len(real_chunks) >= 2, "Expected at least 2 chunks (Introduction, Details)"
        headings = {c["heading"] for c in real_chunks}
        assert "Introduction" in headings
        assert "Details" in headings

        # Whole-file embedding must be stored
        assert len(emb_rows) == 1
        assert emb_rows[0]["file_id"] == "doc-e2e"
        assert emb_rows[0]["embedding"] is not None

    async def test_idempotent_second_call_replaces_chunks(self, db, sample_project, tmp_path):
        """Calling index_doc_file twice doesn't duplicate chunks."""
        content = (
            "## Part A\n\n"
            + "Content for part A fills space adequately.\n" * 8 + "\n"
            "## Part B\n\n"
            + "Content for part B fills space adequately.\n" * 8
        )
        await _create_reference_doc_file(db, tmp_path, "doc-idem", content)

        await index_doc_file("doc-idem")
        await index_doc_file("doc-idem")

        async with db.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT id FROM file_chunks WHERE file_id = ? AND chunk_index >= 0",
                ("doc-idem",),
            )
        # Should have exactly the same number as one run — no duplicates
        assert len(rows) == 2

    async def test_sentinel_row_for_short_content(self, db, sample_project, tmp_path):
        """A short file (< 500 chars) gets a sentinel row with chunk_index=-1."""
        content = "This is a short file with no headers and very little content."
        await _create_reference_doc_file(db, tmp_path, "doc-short", content)

        await index_doc_file("doc-short")

        async with db.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT chunk_index FROM file_chunks WHERE file_id = ?",
                ("doc-short",),
            )
        assert len(rows) == 1
        assert rows[0]["chunk_index"] == -1

    async def test_sentinel_row_for_single_section(self, db, sample_project, tmp_path):
        """A file with only one section produces a sentinel row."""
        content = "## Only Section\n\n" + "Content goes on here for a long time. " * 20
        await _create_reference_doc_file(db, tmp_path, "doc-single", content)

        await index_doc_file("doc-single")

        async with db.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT chunk_index FROM file_chunks WHERE file_id = ?",
                ("doc-single",),
            )
        assert len(rows) == 1
        assert rows[0]["chunk_index"] == -1

    async def test_refuses_non_reference_doc_file(self, db, sample_project, tmp_path):
        """Files with role != 'reference_doc' are silently skipped."""
        content = "## Section A\n\nSome content.\n" * 20 + "## Section B\n\nMore content.\n" * 20
        path = tmp_path / "upload.md"
        path.write_text(content)
        await db.create_file(
            id="doc-upload",
            filename="upload.md",
            stored_path=str(path),
            mime_type="text/markdown",
            size_bytes=len(content.encode()),
            uploaded_by=None,
            project_id="test-project",
        )
        # role defaults to 'upload' — don't set to reference_doc

        await index_doc_file("doc-upload")

        async with db.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT id FROM file_chunks WHERE file_id = ?", ("doc-upload",)
            )
        assert len(rows) == 0

    async def test_nonexistent_file_returns_silently(self, db):
        """index_doc_file returns silently for unknown file_id."""
        await index_doc_file("no-such-file")  # should not raise

    async def test_missing_disk_file_returns_silently(self, db, sample_project, tmp_path):
        """index_doc_file logs a warning and returns silently when stored_path is missing."""
        # Register file in DB with a path that doesn't exist on disk
        await db.create_file(
            id="doc-missing",
            filename="missing.md",
            stored_path="/tmp/does_not_exist_xyz.md",
            mime_type="text/markdown",
            size_bytes=0,
            uploaded_by=None,
            project_id="test-project",
        )
        async with db.get_db() as conn:
            await conn.execute("UPDATE files SET role = 'reference_doc' WHERE id = ?", ("doc-missing",))
            await conn.commit()

        await index_doc_file("doc-missing")  # should not raise

        async with db.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT id FROM file_chunks WHERE file_id = ?", ("doc-missing",)
            )
        assert len(rows) == 0

    async def test_vec0_gate_skipped_for_non_1536_dim(self, db, sample_project, tmp_path):
        """With a 4-dim embedding service, file_chunks_vec INSERT is skipped (no error)."""
        content = (
            "## Alpha\n\n"
            + "Alpha content for this section goes on quite a bit.\n" * 8 + "\n"
            "## Beta\n\n"
            + "Beta content for this section goes on quite a bit.\n" * 8
        )
        await _create_reference_doc_file(db, tmp_path, "doc-vec-gate", content)

        # _FixedService emits 4-dim vectors, so len(blob) != 1536*4
        await index_doc_file("doc-vec-gate")

        async with db.get_db() as conn:
            chunks = await conn.execute_fetchall(
                "SELECT id FROM file_chunks WHERE file_id = ? AND chunk_index >= 0",
                ("doc-vec-gate",),
            )
        # Chunks were inserted even though vec0 INSERT was skipped
        assert len(chunks) >= 2


# ---------------------------------------------------------------------------
# set_file_embedding
# ---------------------------------------------------------------------------

class TestSetFileEmbedding:
    async def test_stores_blob_in_files_embeddings(self, db, sample_project, tmp_path):
        path = tmp_path / "f.md"
        path.write_text("hello")
        await db.create_file(
            id="fe-test", filename="f.md", stored_path=str(path),
            mime_type="text/markdown", size_bytes=5,
            uploaded_by=None, project_id="test-project",
        )
        blob = encode_vector(_unit_vec(4, 0))
        await set_file_embedding("fe-test", blob)

        async with db.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT embedding FROM files_embeddings WHERE file_id = ?", ("fe-test",)
            )
        assert len(rows) == 1
        assert rows[0]["embedding"] == blob

    async def test_idempotent_replaces_on_second_call(self, db, sample_project, tmp_path):
        path = tmp_path / "f2.md"
        path.write_text("hello")
        await db.create_file(
            id="fe-idem", filename="f2.md", stored_path=str(path),
            mime_type="text/markdown", size_bytes=5,
            uploaded_by=None, project_id="test-project",
        )
        blob1 = encode_vector(_unit_vec(4, 0))
        blob2 = encode_vector(_unit_vec(4, 1))
        await set_file_embedding("fe-idem", blob1)
        await set_file_embedding("fe-idem", blob2)

        async with db.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT embedding FROM files_embeddings WHERE file_id = ?", ("fe-idem",)
            )
        assert len(rows) == 1
        assert rows[0]["embedding"] == blob2


# ---------------------------------------------------------------------------
# get_doc_files_needing_chunking
# ---------------------------------------------------------------------------

class TestGetDocFilesNeedingChunking:
    async def test_returns_unchunked_reference_doc_files(self, db, sample_project, tmp_path):
        content = "## A\n\n" + "Some content.\n" * 20
        await _create_reference_doc_file(db, tmp_path, "doc-needed", content)

        result = await get_doc_files_needing_chunking(batch_size=100)
        assert "doc-needed" in result

    async def test_excludes_upload_role_files(self, db, sample_project, tmp_path):
        path = tmp_path / "up.md"
        path.write_text("## A\n\n" + "Some content.\n" * 20)
        await db.create_file(
            id="doc-upload-skip", filename="up.md", stored_path=str(path),
            mime_type="text/markdown", size_bytes=100,
            uploaded_by=None, project_id="test-project",
        )
        # role defaults to 'upload'

        result = await get_doc_files_needing_chunking(batch_size=100)
        assert "doc-upload-skip" not in result

    async def test_excludes_already_chunked_files(self, db, sample_project, tmp_path):
        content = "## A\n\n" + "Some content.\n" * 20
        await _create_reference_doc_file(db, tmp_path, "doc-done", content)

        # Insert a sentinel row to simulate already-chunked
        async with db.get_db() as conn:
            await conn.execute(
                "INSERT INTO file_chunks (file_id, chunk_index, heading, content) VALUES (?, -1, NULL, '')",
                ("doc-done",),
            )
            await conn.commit()

        result = await get_doc_files_needing_chunking(batch_size=100)
        assert "doc-done" not in result

    async def test_respects_batch_size(self, db, sample_project, tmp_path):
        for i in range(5):
            content = "## A\n\n" + "Some content.\n" * 20
            await _create_reference_doc_file(db, tmp_path, f"batch-{i}", content)

        result = await get_doc_files_needing_chunking(batch_size=3)
        assert len(result) <= 3


# ---------------------------------------------------------------------------
# search_files_semantic (Python cosine fallback path — non-1536-dim)
# ---------------------------------------------------------------------------

class TestSearchFilesSemantic:
    @pytest.fixture(autouse=True)
    def use_fixed_service(self):
        # 4-dim vectors → Python cosine fallback path
        set_embedding_service(_FixedService(_unit_vec(4, 0)))
        yield
        set_embedding_service(None)

    async def test_returns_indexed_file_for_matching_query(self, db, sample_project, tmp_path):
        content = (
            "## Section One\n\n"
            + "Relevant content for the search query here.\n" * 8 + "\n"
            "## Section Two\n\n"
            + "More relevant content for testing purposes.\n" * 8
        )
        await _create_reference_doc_file(db, tmp_path, "doc-search", content)
        await index_doc_file("doc-search")

        query_vec = _unit_vec(4, 0)
        results = await search_files_semantic(query_vector=query_vec, limit=10)

        file_ids = [r["file_id"] for r in results]
        assert "doc-search" in file_ids

    async def test_returns_result_shape(self, db, sample_project, tmp_path):
        content = (
            "## A\n\n" + "Section content.\n" * 8 + "\n"
            "## B\n\n" + "More section content.\n" * 8
        )
        await _create_reference_doc_file(db, tmp_path, "doc-shape", content)
        await index_doc_file("doc-shape")

        results = await search_files_semantic(query_vector=_unit_vec(4, 0), limit=10)
        assert len(results) >= 1
        r = results[0]
        for key in ("file_id", "filename", "project_id", "similarity", "created_at"):
            assert key in r, f"Missing key: {key}"
        assert 0.0 <= r["similarity"] <= 1.0

    async def test_project_filter_scopes_results(self, db, sample_project, tmp_path):
        # Create a second project
        await db.create_project(
            id="other-proj", repo="git@github.com:x/y.git",
            working_dir="/work/y", default_branch="main",
        )
        content = (
            "## A\n\n" + "Section A content for this document.\n" * 8 + "\n"
            "## B\n\n" + "Section B content for this document.\n" * 8
        )
        await _create_reference_doc_file(db, tmp_path, "doc-mine", content, project_id="test-project")
        await _create_reference_doc_file(db, tmp_path, "doc-other", content, project_id="other-proj")
        await index_doc_file("doc-mine")
        await index_doc_file("doc-other")

        results = await search_files_semantic(
            query_vector=_unit_vec(4, 0), project_id="test-project", limit=10
        )
        file_ids = [r["file_id"] for r in results]
        assert "doc-mine" in file_ids
        assert "doc-other" not in file_ids

    async def test_excludes_upload_role_files(self, db, sample_project, tmp_path):
        """Files with role='upload' are never returned even if they have embeddings."""
        path = tmp_path / "up.md"
        path.write_text("Some content")
        await db.create_file(
            id="upload-file", filename="up.md", stored_path=str(path),
            mime_type="text/markdown", size_bytes=12,
            uploaded_by=None, project_id="test-project",
        )
        # Manually store an embedding for the upload file
        blob = encode_vector(_unit_vec(4, 0))
        async with db.get_db() as conn:
            await conn.execute(
                "INSERT INTO files_embeddings (file_id, embedding) VALUES (?, ?)",
                ("upload-file", blob),
            )
            await conn.commit()

        results = await search_files_semantic(query_vector=_unit_vec(4, 0), limit=10)
        file_ids = [r["file_id"] for r in results]
        assert "upload-file" not in file_ids

    async def test_empty_when_no_embeddings(self, db, sample_project):
        results = await search_files_semantic(query_vector=_unit_vec(4, 0), limit=10)
        assert results == []


# ---------------------------------------------------------------------------
# search_file_chunks_semantic (Python cosine fallback path)
# ---------------------------------------------------------------------------

class TestSearchFileChunksSemantic:
    @pytest.fixture(autouse=True)
    def use_fixed_service(self):
        set_embedding_service(_FixedService(_unit_vec(4, 0)))
        yield
        set_embedding_service(None)

    async def test_returns_matching_chunk(self, db, sample_project, tmp_path):
        content = (
            "## Overview\n\n"
            + "This section provides an overview of the system.\n" * 8 + "\n"
            "## Implementation\n\n"
            + "This section covers implementation details.\n" * 8
        )
        await _create_reference_doc_file(db, tmp_path, "doc-chunk-search", content)
        await index_doc_file("doc-chunk-search")

        results = await search_file_chunks_semantic(query_vector=_unit_vec(4, 0), limit=5)
        assert len(results) >= 1

        r = results[0]
        for key in ("chunk_id", "file_id", "chunk_index", "chunk_heading",
                    "chunk_content", "filename", "project_id", "similarity", "created_at"):
            assert key in r, f"Missing key: {key}"
        assert r["file_id"] == "doc-chunk-search"
        assert r["chunk_index"] >= 0

    async def test_adjacent_context_chunks_attached(self, db, sample_project, tmp_path):
        """Each result has a context_chunks key with adjacent chunk info."""
        content = (
            "## One\n\n" + "Section one content here.\n" * 8 + "\n"
            "## Two\n\n" + "Section two content here.\n" * 8 + "\n"
            "## Three\n\n" + "Section three content here.\n" * 8
        )
        await _create_reference_doc_file(db, tmp_path, "doc-ctx", content)
        await index_doc_file("doc-ctx")

        results = await search_file_chunks_semantic(query_vector=_unit_vec(4, 0), limit=5)
        assert len(results) >= 1
        assert "context_chunks" in results[0]
        assert isinstance(results[0]["context_chunks"], list)

    async def test_empty_when_no_chunks(self, db, sample_project):
        results = await search_file_chunks_semantic(query_vector=_unit_vec(4, 0), limit=5)
        assert results == []


# ---------------------------------------------------------------------------
# _backfill_file_chunks lifespan task
# ---------------------------------------------------------------------------

class TestBackfillFileChunks:
    @pytest.fixture(autouse=True)
    def use_fixed_service(self):
        set_embedding_service(_FixedService(_unit_vec(4, 0)))
        yield
        set_embedding_service(None)

    async def test_processes_batch_of_files(self, db, sample_project, tmp_path):
        """_backfill_file_chunks indexes all pending reference_doc files."""
        content = (
            "## Section\n\n" + "Section content fills space for testing.\n" * 8 + "\n"
            "## Another\n\n" + "More section content fills space for testing.\n" * 8
        )
        for i in range(3):
            await _create_reference_doc_file(db, tmp_path, f"backfill-{i}", content)

        # Verify they need chunking
        pending = await get_doc_files_needing_chunking(batch_size=100)
        assert len([p for p in pending if p.startswith("backfill-")]) == 3

        from ouvrage.server.app import _backfill_file_chunks
        await _backfill_file_chunks()

        # All should now be chunked (no longer in pending list)
        pending_after = await get_doc_files_needing_chunking(batch_size=100)
        still_pending = [p for p in pending_after if p.startswith("backfill-")]
        assert still_pending == []

    async def test_continues_on_individual_file_error(self, db, sample_project, tmp_path):
        """A failing file doesn't abort the whole backfill."""
        # File with bad stored_path (will fail to read)
        await db.create_file(
            id="bad-file", filename="bad.md",
            stored_path="/tmp/nonexistent_xyz_abc.md",
            mime_type="text/markdown", size_bytes=0,
            uploaded_by=None, project_id="test-project",
        )
        async with db.get_db() as conn:
            await conn.execute("UPDATE files SET role = 'reference_doc' WHERE id = ?", ("bad-file",))
            await conn.commit()

        # Add a good file too (content must be >= 500 chars with 2+ sections for real chunks)
        content = (
            "## Good Section\n\n"
            + "Good content fills space for testing purposes here.\n" * 10 + "\n"
            "## Also Good Section\n\n"
            + "More good content fills space for testing purposes.\n" * 10
        )
        await _create_reference_doc_file(db, tmp_path, "good-file", content)

        from ouvrage.server.app import _backfill_file_chunks
        await _backfill_file_chunks()  # should not raise

        # good-file should have been indexed despite bad-file failing
        async with db.get_db() as conn:
            rows = await conn.execute_fetchall(
                "SELECT id FROM file_chunks WHERE file_id = ? AND chunk_index >= 0",
                ("good-file",),
            )
        assert len(rows) >= 1
