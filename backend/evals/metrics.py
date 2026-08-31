"""检索评测指标（纯函数，不依赖 Qdrant / embedding）。"""

from __future__ import annotations

from typing import Dict, Iterable, List, Sequence, Set, Union

IdLike = Union[str, int]


def _as_str_set(ids: Iterable[IdLike]) -> Set[str]:
    return {str(x) for x in ids if x is not None and str(x) != ""}


def hit_at_k(ranked_ids: Sequence[IdLike], relevant: Iterable[IdLike], k: int) -> float:
    """Hit@K：top-K 是否命中至少一个相关项。"""
    if k <= 0:
        return 0.0
    rel = _as_str_set(relevant)
    if not rel:
        return 0.0
    top = [str(x) for x in ranked_ids[:k]]
    return 1.0 if any(i in rel for i in top) else 0.0


def recall_at_k(ranked_ids: Sequence[IdLike], relevant: Iterable[IdLike], k: int) -> float:
    """Recall@K：|命中相关| / |相关集合|。"""
    rel = _as_str_set(relevant)
    if not rel or k <= 0:
        return 0.0
    top = _as_str_set(ranked_ids[:k])
    return len(top & rel) / len(rel)


def precision_at_k(ranked_ids: Sequence[IdLike], relevant: Iterable[IdLike], k: int) -> float:
    """Precision@K：|命中相关| / K。"""
    if k <= 0:
        return 0.0
    rel = _as_str_set(relevant)
    if not rel:
        return 0.0
    top = _as_str_set(ranked_ids[:k])
    return len(top & rel) / float(k)


def mrr(ranked_ids: Sequence[IdLike], relevant: Iterable[IdLike]) -> float:
    """MRR：第一个相关结果秩次的倒数；未命中为 0。"""
    rel = _as_str_set(relevant)
    if not rel:
        return 0.0
    for i, rid in enumerate(ranked_ids, start=1):
        if str(rid) in rel:
            return 1.0 / float(i)
    return 0.0


def evaluate_query(
    ranked_ids: Sequence[IdLike],
    relevant: Iterable[IdLike],
    ks: Sequence[int],
) -> Dict[str, object]:
    """对单条 query 计算一组 K 上的指标。"""
    hit: Dict[str, float] = {}
    recall: Dict[str, float] = {}
    precision: Dict[str, float] = {}
    for k in ks:
        key = str(k)
        hit[key] = hit_at_k(ranked_ids, relevant, k)
        recall[key] = recall_at_k(ranked_ids, relevant, k)
        precision[key] = precision_at_k(ranked_ids, relevant, k)
    return {
        "hit_at": hit,
        "recall_at": recall,
        "precision_at": precision,
        "mrr": mrr(ranked_ids, relevant),
    }


def mean_dict_of_floats(items: List[Dict[str, float]], keys: Sequence[str]) -> Dict[str, float]:
    """对若干 dict[str,float] 按 key 做宏平均。"""
    if not items:
        return {str(k): 0.0 for k in keys}
    out: Dict[str, float] = {}
    n = float(len(items))
    for k in keys:
        sk = str(k)
        out[sk] = sum(float(d.get(sk, 0.0)) for d in items) / n
    return out
