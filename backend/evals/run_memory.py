"""Memory 通道离线评测 runner。

支持两条可比通道（同一语料 / queries / collection）：

- ``memory``：纯向量召回（强制 ``enable_rerank=False``）
- ``memory_reranked``：向量多召回 + CrossEncoder 重排后截断到 top_k
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .io_utils import load_json, load_jsonl, write_report_pair
from .metrics import evaluate_query, mean_dict_of_floats
from .paths import MEMORY_DATASET, MEMORY_ID_MAP_PATH, REPORTS_DIR
from .schema import (
    DEFAULT_KS,
    EVAL_MEMORY_COLLECTION,
    ChannelReport,
    MemoryQueryItem,
    QueryMetricDetail,
)
from .seed_memory import memory_point_count, seed_memory

logger = logging.getLogger(__name__)


def load_memory_queries(path: Optional[Path] = None) -> List[MemoryQueryItem]:
    path = path or (MEMORY_DATASET / "queries.jsonl")
    rows = load_jsonl(path)
    items: List[MemoryQueryItem] = []
    for i, row in enumerate(rows):
        items.append(
            MemoryQueryItem(
                query=str(row["query"]),
                relevant_keys=[str(k) for k in row.get("relevant_keys") or []],
                category=row.get("category"),
                query_id=str(row.get("query_id") or f"m{i+1:03d}"),
            )
        )
    return items


def _resolve_relevant_ids(keys: List[str], id_map: Dict[str, str]) -> List[str]:
    missing = [k for k in keys if k not in id_map]
    if missing:
        raise KeyError(f"relevant_keys not in id_map: {missing}")
    return [id_map[k] for k in keys]


def run_memory_eval(
    *,
    collection_name: str = EVAL_MEMORY_COLLECTION,
    ks: Sequence[int] = DEFAULT_KS,
    top_k: Optional[int] = None,
    score_threshold: float = 0.3,
    reseed: bool = False,
    enable_rerank: bool = False,
    candidate_k: Optional[int] = None,
    id_map_path: Optional[Path] = None,
    reports_dir: Optional[Path] = None,
) -> ChannelReport:
    """运行 Memory 检索评测并写报告。

    ``enable_rerank=False`` 写 channel=memory；True 写 channel=memory_reranked。
    评测显式传入开关，不受线上 MEMORY_RERANK_ENABLED 环境影响，便于 A/B 对比。
    """
    from src.memory.vector_store import MemoryVectorStore, memory_rerank_candidate_k

    ks_list = [int(k) for k in ks]
    limit = int(top_k or max(ks_list))
    map_path = id_map_path or MEMORY_ID_MAP_PATH
    out_dir = reports_dir or REPORTS_DIR
    channel = "memory_reranked" if enable_rerank else "memory"
    pool = int(candidate_k) if candidate_k is not None else memory_rerank_candidate_k()
    if enable_rerank:
        pool = max(limit, pool)

    need_seed = reseed or (not map_path.exists()) or memory_point_count(collection_name) == 0
    if need_seed:
        logger.info("Seeding memory eval collection (reseed=%s)", reseed)
        id_map = seed_memory(collection_name=collection_name, clear=True, id_map_path=map_path)
    else:
        id_map = load_json(map_path)
        if not isinstance(id_map, dict) or not id_map:
            raise RuntimeError(f"Invalid id map at {map_path}; pass --reseed")

    store = MemoryVectorStore(collection_name=collection_name)
    if not store.available:
        raise RuntimeError(f"MemoryVectorStore unavailable: {collection_name}")

    queries = load_memory_queries()
    details: List[QueryMetricDetail] = []
    hit_dicts: List[Dict[str, float]] = []
    recall_dicts: List[Dict[str, float]] = []
    precision_dicts: List[Dict[str, float]] = []
    mrrs: List[float] = []

    for q in queries:
        relevant_ids = _resolve_relevant_ids(q.relevant_keys, id_map)
        hits = store.search_memories(
            query=q.query,
            top_k=limit,
            score_threshold=score_threshold,
            category=q.category,
            enable_rerank=enable_rerank,
            candidate_k=pool if enable_rerank else None,
        )
        ranked = [str(h.get("id")) for h in hits if h.get("id") is not None]
        metrics = evaluate_query(ranked, relevant_ids, ks_list)
        detail = QueryMetricDetail(
            query_id=q.query_id or "",
            query=q.query,
            relevant_count=len(relevant_ids),
            retrieved_ids=ranked,
            hit_at=metrics["hit_at"],  # type: ignore[arg-type]
            recall_at=metrics["recall_at"],  # type: ignore[arg-type]
            precision_at=metrics["precision_at"],  # type: ignore[arg-type]
            mrr=float(metrics["mrr"]),
        )
        details.append(detail)
        hit_dicts.append(detail.hit_at)
        recall_dicts.append(detail.recall_at)
        precision_dicts.append(detail.precision_at)
        mrrs.append(detail.mrr)

    report = ChannelReport(
        channel=channel,
        collection=collection_name,
        top_k=limit,
        ks=ks_list,
        score_threshold=score_threshold,
        query_count=len(details),
        mean_hit_at=mean_dict_of_floats(hit_dicts, [str(k) for k in ks_list]),
        mean_recall_at=mean_dict_of_floats(recall_dicts, [str(k) for k in ks_list]),
        mean_precision_at=mean_dict_of_floats(precision_dicts, [str(k) for k in ks_list]),
        mean_mrr=(sum(mrrs) / len(mrrs)) if mrrs else 0.0,
        details=details,
        extra={
            "id_map_path": str(map_path),
            "id_map_size": len(id_map),
            "enable_rerank": enable_rerank,
            "candidate_k": pool if enable_rerank else limit,
        },
    )
    json_path, md_path = write_report_pair(out_dir, channel, report.to_dict())
    logger.info("Memory report written: %s / %s", json_path, md_path)
    return report
