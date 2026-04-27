"""Shared utilities used across multiple handler modules."""

import logging
import re

from ouvrage.embeddings import service as emb
import ouvrage.db as db

log = logging.getLogger("ouvrage.server")

PR_URL_RE = re.compile(r'https://github\.com/[^\s)]+/pull/\d+')


async def _embed_message_async(message_id: int, content: str, msg_type: str | None) -> None:
    """Fire-and-forget: embed a message and store the vector. Never raises."""
    if not emb.should_embed(content, msg_type):
        return
    try:
        service = emb.get_embedding_service()
        vector = await service.embed_safe(content)
        if vector:
            blob = emb.encode_vector(vector)
            await db.set_message_embedding(message_id, blob)
            # set_message_embedding also updates messages_vec automatically
    except Exception:
        pass  # Never block — embedding is best-effort

    # Also index message chunks for paragraph-level search
    try:
        await db.index_message_chunks(message_id, content)
    except Exception:
        pass  # Never block — chunking is best-effort
