"""Backward-compatible shim — embedding_service moved to switchboard.embeddings.service."""
from switchboard.embeddings.service import *  # noqa: F401, F403
from switchboard.embeddings.service import (  # noqa: F401
    EmbeddingService,
    OpenAIEmbeddingService,
    should_embed,
    encode_vector,
    decode_vector,
    cosine_similarity,
    compute_relevance_score,
    get_embedding_service,
    set_embedding_service,
)
