"""Embedding service — abstract interface for generating text embeddings.

Currently backed by OpenAI text-embedding-3-small. Swap the provider by
subclassing EmbeddingService and passing an instance to the functions that
accept one.

Storage format: vectors are encoded as packed float32 blobs (struct.pack)
for compact, portable storage in SQLite BLOB columns.
"""

import logging
import math
import os
import struct
from typing import Optional

logger = logging.getLogger(__name__)

# Default embedding model and dimensions
EMBEDDING_MODEL = "text-embedding-3-small"
EMBEDDING_DIMENSIONS = 1536

# Skip embedding if content is shorter than this
MIN_CONTENT_LENGTH = 50

# Message types that should never be embedded (high-noise, low-signal)
SKIP_TYPES = {"test-result"}

# Type weights for relevance scoring
TYPE_WEIGHTS: dict[str, float] = {
    "spec": 1.5,
    "review": 1.4,
    "note": 1.2,
    "result": 1.1,
    "plan": 1.1,
    "answer": 1.0,
    "question": 0.8,
    "status": 0.5,
    "test-result": 0.3,
}

# Boost for pinned messages
PINNED_BOOST = 1.3


def should_embed(content: Optional[str], msg_type: Optional[str]) -> bool:
    """Return True if this message should get an embedding."""
    if msg_type in SKIP_TYPES:
        return False
    if not content or len(content) < MIN_CONTENT_LENGTH:
        return False
    return True


def encode_vector(vector: list[float]) -> bytes:
    """Encode a float list as packed float32 bytes for SQLite BLOB storage."""
    return struct.pack(f"{len(vector)}f", *vector)


def decode_vector(blob: bytes) -> list[float]:
    """Decode a packed float32 blob back to a float list."""
    n = len(blob) // 4
    return list(struct.unpack(f"{n}f", blob))


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Compute cosine similarity between two vectors. Returns 0.0 on zero vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def compute_relevance_score(similarity: float, msg_type: Optional[str], pinned: bool) -> float:
    """Apply type weight and pinned boost to raw cosine similarity."""
    type_weight = TYPE_WEIGHTS.get(msg_type or "", 1.0)
    pinned_multiplier = PINNED_BOOST if pinned else 1.0
    return similarity * type_weight * pinned_multiplier


class EmbeddingService:
    """Abstract embedding service. Override `embed()` to swap providers."""

    async def embed(self, text: str) -> Optional[list[float]]:
        raise NotImplementedError

    async def embed_safe(self, text: str) -> Optional[list[float]]:
        """Like embed() but swallows errors — never blocks message creation."""
        try:
            return await self.embed(text)
        except Exception as e:
            logger.warning("Embedding failed (skipping): %s", e)
            return None


class OpenAIEmbeddingService(EmbeddingService):
    """OpenAI text-embedding-3-small backed embedding service."""

    def __init__(self, api_key: Optional[str] = None):
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY")
        self._client = None

    def _get_client(self):
        if self._client is None:
            if not self._api_key:
                raise ValueError(
                    "OPENAI_API_KEY environment variable not set. "
                    "Set it to enable semantic search embeddings."
                )
            from openai import AsyncOpenAI
            self._client = AsyncOpenAI(api_key=self._api_key)
        return self._client

    async def embed(self, text: str) -> list[float]:
        client = self._get_client()
        # Truncate to model token limit (approx 8191 tokens ~ 32K chars)
        truncated = text[:32000]
        response = await client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=truncated,
        )
        return response.data[0].embedding


# Module-level singleton — shared across the server process
_service: Optional[EmbeddingService] = None


def get_embedding_service() -> EmbeddingService:
    """Get the shared embedding service instance (lazy init)."""
    global _service
    if _service is None:
        _service = OpenAIEmbeddingService()
    return _service


def set_embedding_service(service: EmbeddingService) -> None:
    """Replace the shared embedding service (useful for testing)."""
    global _service
    _service = service
