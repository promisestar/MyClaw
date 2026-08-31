"""RAG 通道离线评测 runner（文档级命中）。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .io_utils import load_jsonl, normalize_doc_key, write_report_pair
from .metrics import evaluate_query, mean_dict_of_floats
from .paths import RAG_DATASET, REPORTS_DIR
from .schema import (
    DEFAULT_KS,
    EVAL_RAG_COLLECTION,
    EVAL_RAG_NAMESPACE,
    ChannelReport,
    QueryMetricDetail,
    RagQueryItem,
)
from .seed_rag import rag_doc_count, seed_rag

logger = logging.getLogger(__name__)


def load_rag_queries(path: Optional[Path] = None) -> List[RagQueryItem]:
    path = path or (RAG_DATASET / "queries.jsonl")
    rows = load_jsonl(path)
    items: List[RagQueryItem] = []
    for i, row in enumerate(rows):
        items.append(
            RagQueryItem(
                query=str(row["query"]),
                relevant_docs=[str(d) for d in row.get("relevant_docs") or []],
                query_id=str(row.get("query_id") or f"r{i+1:03d}"),
            )
        )
    return items


def _hit_doc_keys(hits: List[dict]) -> List[str]:
    """从检索结果提取文档键（按出现顺序去重）。"""
    seen = set()
    ordered: List[str] = []
    for h in hits:
        meta = h.get("metadata") or {}
        raw = meta.get("source_path") or meta.get("doc_id") or ""
        if not raw:
            continue
        key = normalize_doc_key(raw)
        if key not in seen:
            seen.add(key)
            ordered.append(key)
    return ordered


def run_rag_eval(
    *,
    collection_name: str = EVAL_RAG_COLLECTION,
    rag_namespace: str = EVAL_RAG_NAMESPACE,
    ks: Sequence[int] = DEFAULT_KS,
    top_k: Optional[int] = None,
    score_threshold: Optional[float] = None,
    reseed: bool = False,
    reports_dir: Optional[Path] = None,
) -> ChannelReport:
    """运行 RAG 基线检索评测（search_vectors，无 MQE/HyDE）。"""
    from src.rag.embedding import get_dimension
    from src.rag.pipeline import search_vectors
    from src.rag.qdrant_store import QdrantConnectionManager

    ks_list = [int(k) for k in ks]
    limit = int(top_k or max(ks_list))
    out_dir = reports_dir or REPORTS_DIR

    need_seed = reseed or rag_doc_count(collection_name, rag_namespace) == 0
    if need_seed:
        logger.info("Seeding RAG eval collection (reseed=%s)", reseed)
        seed_rag(
            collection_name=collection_name,
            rag_namespace=rag_namespace,
            clear=True,
        )

    from .qdrant_env import resolve_qdrant_env

    url, api_key = resolve_qdrant_env()
    dimension = get_dimension(384)
    store = QdrantConnectionManager.get_instance(
        url=url,
        api_key=api_key,
        collection_name=collection_name,
        vector_size=dimension,
        distance="cosine",
    )

    queries = load_rag_queries()
    details: List[QueryMetricDetail] = []
    hit_dicts: List[Dict[str, float]] = []
    recall_dicts: List[Dict[str, float]] = []
    precision_dicts: List[Dict[str, float]] = []
    mrrs: List[float] = []

    for q in queries:
        relevant = [normalize_doc_key(d) for d in q.relevant_docs]
        hits = search_vectors(
            store=store,
            query=q.query,
            top_k=limit,
            rag_namespace=rag_namespace,
            score_threshold=score_threshold,
        )
        # 文档级：先按 chunk 顺序抽出文档键；指标按「文档列表」截断到 K
        ranked_docs = _hit_doc_keys(hits)
        metrics = evaluate_query(ranked_docs, relevant, ks_list)
        detail = QueryMetricDetail(
            query_id=q.query_id or "",
            query=q.query,
            relevant_count=len(relevant),
            retrieved_ids=ranked_docs,
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
        channel="rag",
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
            "rag_namespace": rag_namespace,
            "hit_unit": "document",
            "search": "search_vectors",
        },
    )
    json_path, md_path = write_report_pair(out_dir, "rag", report.to_dict())
    logger.info("RAG report written: %s / %s", json_path, md_path)
    return report
