"""Memory 评测语料入库（隔离 collection）。"""

from __future__ import annotations

import logging
from typing import Dict, List

from .io_utils import load_jsonl, write_json
from .paths import MEMORY_DATASET, MEMORY_ID_MAP_PATH
from .schema import EVAL_MEMORY_COLLECTION, MemoryCorpusItem

logger = logging.getLogger(__name__)


def _build_store(collection_name: str = EVAL_MEMORY_COLLECTION):
    from src.memory.vector_store import MemoryVectorStore

    return MemoryVectorStore(collection_name=collection_name)


def load_memory_corpus(path=None) -> List[MemoryCorpusItem]:
    path = path or (MEMORY_DATASET / "corpus.jsonl")
    rows = load_jsonl(path)
    items: List[MemoryCorpusItem] = []
    for row in rows:
        items.append(
            MemoryCorpusItem(
                stable_key=str(row["stable_key"]),
                content=str(row["content"]),
                category=str(row.get("category") or "fact"),
            )
        )
    return items


def seed_memory(
    *,
    collection_name: str = EVAL_MEMORY_COLLECTION,
    clear: bool = True,
    corpus_path=None,
    id_map_path=None,
) -> Dict[str, str]:
    """清空评测 collection（可选）并写入语料，返回 stable_key → memory_id。"""
    store = _build_store(collection_name)
    if not store.available:
        raise RuntimeError(
            f"MemoryVectorStore unavailable for collection={collection_name}; "
            "check Qdrant / embedding config"
        )

    if clear:
        ok = store._qdrant.clear_collection()
        if not ok:
            raise RuntimeError(f"Failed to clear collection {collection_name}")
        logger.info("Cleared eval memory collection %s", collection_name)

    corpus = load_memory_corpus(corpus_path)
    id_map: Dict[str, str] = {}
    for item in corpus:
        mid = store.add_memory(
            content=item.content,
            category=item.category,
            source="eval",
        )
        if not mid:
            raise RuntimeError(f"Failed to add memory for key={item.stable_key}")
        id_map[item.stable_key] = mid
        logger.info("Seeded %s → %s [%s]", item.stable_key, mid, item.category)

    out = id_map_path or MEMORY_ID_MAP_PATH
    write_json(out, id_map)
    logger.info("Wrote memory id map (%d keys) → %s", len(id_map), out)
    return id_map


def memory_point_count(collection_name: str = EVAL_MEMORY_COLLECTION) -> int:
    store = _build_store(collection_name)
    if not store.available:
        return 0
    stats = store.get_stats()
    return int(stats.get("total_count") or 0)
