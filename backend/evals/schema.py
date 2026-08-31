"""评测语料、查询与报告的数据结构约定。"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


EVAL_MEMORY_COLLECTION = "helloclaw_eval_memory"
EVAL_RAG_COLLECTION = "helloclaw_eval_rag"
EVAL_RAG_NAMESPACE = "eval"

DEFAULT_KS = (1, 3, 5, 10)


@dataclass
class MemoryCorpusItem:
    stable_key: str
    content: str
    category: str = "fact"


@dataclass
class MemoryQueryItem:
    query: str
    relevant_keys: List[str]
    category: Optional[str] = None
    query_id: Optional[str] = None


@dataclass
class RagQueryItem:
    query: str
    relevant_docs: List[str]
    query_id: Optional[str] = None


@dataclass
class QueryMetricDetail:
    query_id: str
    query: str
    relevant_count: int
    retrieved_ids: List[str]
    hit_at: Dict[str, float]
    recall_at: Dict[str, float]
    precision_at: Dict[str, float]
    mrr: float


@dataclass
class ChannelReport:
    channel: str
    collection: str
    top_k: int
    ks: List[int]
    score_threshold: Optional[float]
    query_count: int
    mean_hit_at: Dict[str, float]
    mean_recall_at: Dict[str, float]
    mean_precision_at: Dict[str, float]
    mean_mrr: float
    details: List[QueryMetricDetail] = field(default_factory=list)
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
