"""switchboard.embeddings — text embedding service."""

from switchboard.embeddings.service import (
    EmbeddingService,
    OpenAIEmbeddingService,
    should_embed,
    encode_vector,
    decode_vector,
    cosine_similarity,
    compute_relevance_score,
    get_embedding_service,
    set_embedding_service,
    EMBEDDING_MODEL,
    EMBEDDING_DIMENSIONS,
    MIN_CONTENT_LENGTH,
    SKIP_TYPES,
    TYPE_WEIGHTS,
    PINNED_BOOST,
)

__all__ = [
    "EmbeddingService",
    "OpenAIEmbeddingService",
    "should_embed",
    "encode_vector",
    "decode_vector",
    "cosine_similarity",
    "compute_relevance_score",
    "get_embedding_service",
    "set_embedding_service",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIMENSIONS",
    "MIN_CONTENT_LENGTH",
    "SKIP_TYPES",
    "TYPE_WEIGHTS",
    "PINNED_BOOST",
]
