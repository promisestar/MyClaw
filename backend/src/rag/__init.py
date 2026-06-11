"""RAG模块
- embedding 将多种类型的输入转为向量
- qdrant_store 连接到qdrant数据库
- pipeline 将向量存储到qdrant数据库

"""
from .embedding import (
    create_embedding_model,
    create_embedding_model_with_fallback,
    get_text_embedder,
    get_dimension,
    refresh_embedder,
    get_cross_encoder,
    refresh_cross_encoder,
)
from .pipeline import (
    load_and_chunk_texts,
    build_graph_from_chunks,
    index_chunks,
    embed_query,
    search_vectors,
    rank,
    merge_snippets,
    rerank_with_cross_encoder,
    expand_neighbors_from_pool,
    compute_graph_signals_from_pool,
    merge_snippets_grouped,
    search_vectors_expanded,
    compress_ranked_items,
    tldr_summarize,
)

__all__ = [
    "load_and_chunk_texts",
    "build_graph_from_chunks",
    "index_chunks",
    "embed_query",
    "search_vectors",
    "rank",
    "merge_snippets",
    "rerank_with_cross_encoder",
    "expand_neighbors_from_pool",
    "compute_graph_signals_from_pool",
    "merge_snippets_grouped",
    "search_vectors_expanded",
    "compress_ranked_items",
    "tldr_summarize",
    "create_embedding_model",
    "create_embedding_model_with_fallback",
    "get_text_embedder",
    "get_dimension",
    "refresh_embedder",
    "get_cross_encoder",
    "refresh_cross_encoder",
]