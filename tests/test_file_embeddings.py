"""Tests for file embedding functions — index_doc_file, search_files_semantic,
search_file_chunks_semantic, get_doc_files_needing_chunking, backfill loop.

Covers:
- index_doc_file: end-to-end (whole-file + chunks stored), idempotent, sentinel for short/no-header
  content, silent skip for non-reference_doc role
- search_files_semantic: indexed file found for a relevant query (Python cosine fallback path)
- _backfill_file_chunks: pending reference_doc file is indexed
"""

import pytest

from ouvrage.embeddings.service import EmbeddingService, set_embedding_service
from ouvrage.db.search import (
    index_doc_file,
    get_doc_files_needing_chunking,
    search_files_semantic,
)


def _unit_vec(dim: int, idx: int) -> list[float]:
    v = [0.0] * dim
    v[idx % dim] = 1.0
    return v


class _FixedService(EmbeddingService):
    def __init__(self, vec: list[float]):
        self._vec = vec

    async def embed(self, text: str) -> list[float]:
        return list(self._vec)


async def _create_reference_doc_file(db, tmp_path, file_id: str, content: str, project_id: str = "test-project") -> None:
    path = tmp_path / f"{file_id}.md"
    path.write_text(content, encoding="utf-8")
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


# ---------------------------------------------------------------------------
# Spec-required test classes (one test each)
# ---------------------------------------------------------------------------

class TestIndexDocFile:
    @pytest.fixture(autouse=True)
    def use_fixed_service(self):
        set_embedding_service(_FixedService(_unit_vec(4, 0)))
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
                "SELECT chunk_index, heading FROM file_chunks WHERE file_id = ? ORDER BY chunk_index",
                ("doc-e2e",),
            )
            emb_rows = await conn.execute_fetchall(
                "SELECT file_id FROM files_embeddings WHERE file_id = ?",
                ("doc-e2e",),
            )

        real_chunks = [c for c in chunks if c["chunk_index"] >= 0]
        assert len(real_chunks) >= 2
        headings = {c["heading"] for c in real_chunks}
        assert "Introduction" in headings
        assert "Details" in headings
        assert len(emb_rows) == 1


class TestIndexDocFileIdempotent:
    @pytest.fixture(autouse=True)
    def use_fixed_service(self):
        set_embedding_service(_FixedService(_unit_vec(4, 0)))
        yield
        set_embedding_service(None)

    async def test_second_call_replaces_chunks(self, db, sample_project, tmp_path):
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
        assert len(rows) == 2


class TestIndexDocFileSentinel:
    @pytest.fixture(autouse=True)
    def use_fixed_service(self):
        set_embedding_service(_FixedService(_unit_vec(4, 0)))
        yield
        set_embedding_service(None)

    async def test_short_content_gets_sentinel_row(self, db, sample_project, tmp_path):
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


class TestIndexDocFileRoleGuard:
    @pytest.fixture(autouse=True)
    def use_fixed_service(self):
        set_embedding_service(_FixedService(_unit_vec(4, 0)))
        yield
        set_embedding_service(None)

    async def test_upload_role_is_silently_skipped(self, db, sample_project, tmp_path):
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


class TestSearchFilesSemantic:
    @pytest.fixture(autouse=True)
    def use_fixed_service(self):
        set_embedding_service(_FixedService(_unit_vec(4, 0)))
        yield
        set_embedding_service(None)

    async def test_returns_indexed_file_for_matching_query(self, db, sample_project, tmp_path):
        """An indexed reference_doc file appears in semantic search results."""
        content = (
            "## Section One\n\n"
            + "Relevant content for the search query here.\n" * 8 + "\n"
            "## Section Two\n\n"
            + "More relevant content for testing purposes.\n" * 8
        )
        await _create_reference_doc_file(db, tmp_path, "doc-search", content)
        await index_doc_file("doc-search")

        results = await search_files_semantic(query_vector=_unit_vec(4, 0), limit=10)
        file_ids = [r["file_id"] for r in results]
        assert "doc-search" in file_ids
        r = results[0]
        for key in ("file_id", "filename", "project_id", "similarity", "created_at"):
            assert key in r


class TestBackfillFileChunks:
    @pytest.fixture(autouse=True)
    def use_fixed_service(self):
        set_embedding_service(_FixedService(_unit_vec(4, 0)))
        yield
        set_embedding_service(None)

    async def test_processes_batch_of_files(self, db, sample_project, tmp_path):
        """_backfill_file_chunks indexes pending reference_doc files."""
        content = (
            "## Section\n\n" + "Section content fills space for testing.\n" * 8 + "\n"
            "## Another\n\n" + "More section content fills space for testing.\n" * 8
        )
        await _create_reference_doc_file(db, tmp_path, "backfill-0", content)

        from ouvrage.server.app import _backfill_file_chunks
        await _backfill_file_chunks()

        pending_after = await get_doc_files_needing_chunking(batch_size=100)
        assert "backfill-0" not in pending_after
