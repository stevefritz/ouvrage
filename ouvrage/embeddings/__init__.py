"""ouvrage.embeddings — text embedding service and chunking."""

from ouvrage.embeddings.chunks import (
    MIN_CHUNK_LENGTH,
    chunk_message,
)

from ouvrage.embeddings.service import (
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
    "MIN_CHUNK_LENGTH",
    "chunk_message",
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
