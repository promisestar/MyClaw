"""RAG 评测语料入库（隔离 collection + namespace）。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List, Optional

from .paths import RAG_CORPUS_DIR
from .schema import EVAL_RAG_COLLECTION, EVAL_RAG_NAMESPACE

logger = logging.getLogger(__name__)


def _build_store(collection_name: str = EVAL_RAG_COLLECTION):
    from src.rag.embedding import get_dimension
    from src.rag.qdrant_store import QdrantConnectionManager

    from .qdrant_env import resolve_qdrant_env

    url, api_key = resolve_qdrant_env()
    dimension = get_dimension(384)
    return QdrantConnectionManager.get_instance(
        url=url,
        api_key=api_key,
        collection_name=collection_name,
        vector_size=dimension,
        distance="cosine",
    )


def list_corpus_files(corpus_dir: Optional[Path] = None) -> List[Path]:
    root = corpus_dir or RAG_CORPUS_DIR
    files = sorted(root.glob("*.md"))
    if not files:
        raise FileNotFoundError(f"No markdown corpus under {root}")
    return files


def seed_rag(
    *,
    collection_name: str = EVAL_RAG_COLLECTION,
    rag_namespace: str = EVAL_RAG_NAMESPACE,
    clear: bool = True,
    corpus_dir: Optional[Path] = None,
    chunk_size: int = 800,
    chunk_overlap: int = 100,
) -> int:
    """清空 eval namespace（可选）并索引语料，返回 chunk 数。"""
    from src.rag.pipeline import index_chunks, load_and_chunk_texts

    store = _build_store(collection_name)
    if clear:
        ok = store.clear_namespace(rag_namespace)
        if not ok:
            # 空 namespace时部分实现仍返回 True；失败则尝试整库清空（仅评测库）
            logger.warning("clear_namespace(%s) returned False; trying clear_collection", rag_namespace)
            if not store.clear_collection():
                raise RuntimeError(f"Failed to clear RAG eval collection {collection_name}")

    paths = [str(p) for p in list_corpus_files(corpus_dir)]
    chunks = load_and_chunk_texts(
        paths=paths,
        chunk_size=chunk_size,
        chunk_overlap=chunk_overlap,
        namespace=rag_namespace,
        source_label="eval",
    )
    index_chunks(store=store, chunks=chunks, rag_namespace=rag_namespace)
    logger.info(
        "Seeded RAG eval: collection=%s namespace=%s docs=%d chunks=%d",
        collection_name,
        rag_namespace,
        len(paths),
        len(chunks),
    )
    return len(chunks)


def rag_doc_count(
    collection_name: str = EVAL_RAG_COLLECTION,
    rag_namespace: str = EVAL_RAG_NAMESPACE,
) -> int:
    store = _build_store(collection_name)
    try:
        docs = store.get_document_list(namespace=rag_namespace)
        return len(docs or [])
    except Exception:
        return 0
