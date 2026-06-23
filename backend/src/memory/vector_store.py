"""MemoryVectorStore - 记忆专用 Qdrant 向量存储封装

统一的长期记忆存储层，基于 Qdrant 向量数据库 + embedding 语义检索。
支持写入、检索、删除、过期清理。
"""

import logging
import os
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Any

from ..rag.qdrant_store import QdrantConnectionManager, QdrantVectorStore
from ..rag.embedding import get_text_embedder, get_dimension

logger = logging.getLogger(__name__)

# 默认 collection 名称
DEFAULT_MEMORY_COLLECTION = os.getenv("QDRANT_COLLECTION", "helloclaw_memory")
# 默认遗忘天数（仅用于向后兼容，实际由衰减机制决定）
DEFAULT_FORGET_DAYS = 7

# ── 衰减式遗忘机制配置 ──────────────────────────────────
# 衰减周期：每 DECAY_INTERVAL_DAYS 天衰减一次
DECAY_INTERVAL_DAYS = 7
# 默认每周期衰减量
DEFAULT_DECAY_RATE = 0.25

# 分类差异化衰减速率（每 DECAY_INTERVAL_DAYS 天的衰减量）
# 值越小 → 衰减越慢 → 记忆保留越久
# entity/rule: ~70 天 | preference/relationship: ~47 天 | decision: ~35 天 | plan/fact: ~28 天 | reference: ~23 天
CATEGORY_DECAY_RATES: Dict[str, float] = {
    "entity": 0.10,        # 个人信息、账号 → 慢衰减
    "rule": 0.10,          # 规则、约束 → 慢衰减
    "preference": 0.15,    # 用户偏好 → 中慢衰减
    "relationship": 0.15,  # 人际关系 → 中慢衰减
    "decision": 0.20,      # 决策 → 中等衰减
    "plan": 0.25,          # 计划 → 标准衰减
    "fact": 0.25,          # 事实 → 标准衰减
    "reference": 0.30,     # URL、路径 → 快衰减
}


class MemoryVectorStore:
    """记忆向量存储

    封装 Qdrant 操作，提供记忆专用的 CRUD + 遗忘机制。

    使用方式：
        store = MemoryVectorStore()
        store.add_memory("用户喜欢简洁的回复风格", category="preference")
        results = store.search_memories("用户偏好")
        store.cleanup_expired(days=7)
    """

    def __init__(
        self,
        qdrant_url: Optional[str] = None,
        qdrant_api_key: Optional[str] = None,
        collection_name: str = DEFAULT_MEMORY_COLLECTION,
        forget_days: int = DEFAULT_FORGET_DAYS,
    ):
        """初始化记忆向量存储

        Qdrant 连接失败时不抛异常，而是降级为不可用状态（memory_store.available == False）。
        调用方（MyClawAgent、MemoryTool）应检查 available 属性并回退到 workspace 文件模式。

        Args:
            qdrant_url: Qdrant 服务地址（None=本地 localhost:6333）
            qdrant_api_key: Qdrant API Key
            collection_name: Qdrant collection 名称
            forget_days: 遗忘天数，超过此天数的记忆自动清除
        """
        self.collection_name = collection_name
        self.forget_days = forget_days
        self._qdrant: Optional[QdrantVectorStore] = None
        self._available = False

        # 从环境变量读取 Qdrant 配置（与 RAGTool 保持一致）
        resolved_url = qdrant_url or os.getenv("QDRANT_URL")
        resolved_api_key = qdrant_api_key or os.getenv("QDRANT_API_KEY")
        # collection 优先用参数，否则从 env QDRANT_COLLECTION 读取
        if collection_name == DEFAULT_MEMORY_COLLECTION:
            collection_name = os.getenv("QDRANT_COLLECTION", collection_name)

        # 获取共享的 embedding 实例（即使 Qdrant 不可用也需要）
        self._embedder = get_text_embedder()
        self._dimension = get_dimension()

        # 尝试连接 Qdrant，失败则降级
        try:
            self._qdrant = QdrantConnectionManager.get_instance(
                url=resolved_url,
                api_key=resolved_api_key,
                collection_name=collection_name,
                vector_size=self._dimension,
                distance="cosine",
            )
            self._available = True
            logger.info(
                "MemoryVectorStore 初始化完成: collection=%s dim=%d forget_days=%d",
                collection_name, self._dimension, forget_days,
            )
        except Exception as e:
            logger.warning(
                "⚠️ Qdrant 连接失败，记忆向量存储不可用，将回退到文件模式: %s", e
            )
            self._qdrant = None
            self._available = False

    @property
    def available(self) -> bool:
        """Qdrant 是否可用"""
        return self._available and self._qdrant is not None

    # ── 写入 ────────────────────────────────────────────

    def add_memory(
        self,
        content: str,
        category: str = "fact",
        session_id: Optional[str] = None,
        source: str = "capture",
    ) -> Optional[str]:
        """向量化并写入一条长期记忆

        Args:
            content: 记忆文本内容
            category: 分类标签（preference/decision/entity/fact/plan/relationship/reference）
            session_id: 关联的会话 ID
            source: 来源（capture/agent/flush）

        Returns:
            memory_id 字符串，失败返回 None
        """
        if not self.available:
            logger.warning("Qdrant 不可用，跳过记忆写入")
            return None

        try:
            # 向量化
            vector = self._embedder.encode(content)
            if hasattr(vector, "tolist"):
                vector = vector.tolist()

            # 生成唯一 memory_id
            memory_id = str(uuid.uuid4())

            # 构建 payload
            now = datetime.now()
            now_ts = int(now.timestamp())
            payload = {
                "content": content,
                "category": category,
                "memory_type": "longterm",
                "memory_id": memory_id,
                "timestamp": now_ts,
                "added_at": now_ts,
                "source": source,
                "decay_score": 1.0,           # 衰减分数：初始满分
                "last_decay_ts": now_ts,       # 上次衰减计算时间戳
            }
            if session_id:
                payload["session_id"] = session_id

            # 写入 Qdrant（复用 add_vectors，自动追加 timestamp/added_at）
            success = self._qdrant.add_vectors(
                vectors=[vector],
                metadata=[payload],
                ids=[memory_id],
            )

            if success:
                logger.debug("记忆写入成功: id=%s category=%s len=%d", memory_id, category, len(content))
                return memory_id
            else:
                logger.warning("记忆写入失败: category=%s len=%d", category, len(content))
                return None

        except Exception as e:
            logger.error("记忆写入异常: %s", e, exc_info=True)
            return None

    # ── 检索 ────────────────────────────────────────────

    def search_memories(
        self,
        query: str,
        top_k: int = 5,
        score_threshold: float = 0.3,
        category: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """语义检索相关记忆

        Args:
            query: 查询文本
            top_k: 返回结果数量
            score_threshold: 相似度阈值（余弦距离，0-1）

        Returns:
            记忆列表，每项包含 id, score, content, category, timestamp 等
        """
        if not self.available:
            return []

        try:
            # 空查询 → 返回最近的记忆
            if not query or not query.strip():
                return self._list_recent(top_k)

            # 向量化查询
            query_vector = self._embedder.encode(query)
            if hasattr(query_vector, "tolist"):
                query_vector = query_vector.tolist()

            # 构建过滤条件
            where = None
            if category:
                where = {"category": category}
            # 只检索长期记忆
            if where:
                where["memory_type"] = "longterm"
            else:
                where = {"memory_type": "longterm"}

            # 执行语义搜索
            raw_results = self._qdrant.search_similar(
                query_vector=query_vector,
                limit=top_k,
                score_threshold=score_threshold,
                where=where,
            )

            # 格式化结果
            results = []
            for hit in raw_results:
                meta = hit.get("metadata", {})
                results.append({
                    "id": hit.get("id"),
                    "score": hit.get("score", 0.0),
                    "content": meta.get("content", ""),
                    "category": meta.get("category", "fact"),
                    "timestamp": meta.get("timestamp", 0),
                    "session_id": meta.get("session_id"),
                    "source": meta.get("source", ""),
                    "decay_score": meta.get("decay_score", 1.0),
                })

            logger.debug("记忆检索: query='%s' → %d 结果", query[:50], len(results))

            # 访问强化：重置被检索记忆的衰减计时器（用进废退）
            if results:
                retrieved_ids = [r["id"] for r in results if r.get("id")]
                self._reinforce_memories(retrieved_ids)

            return results

        except Exception as e:
            logger.error("记忆检索异常: %s", e, exc_info=True)
            return []

    def _list_recent(self, top_k: int = 20) -> List[Dict[str, Any]]:
        """返回最近的记忆（无查询词时的回退）"""
        # 用全零向量 + 低阈值拿最近的点（Qdrant 会按默认排序）
        # 实际用空字符串做向量查询更合理
        try:
            dummy_vec = self._embedder.encode("recent memories")
            if hasattr(dummy_vec, "tolist"):
                dummy_vec = dummy_vec.tolist()

            raw_results = self._qdrant.search_similar(
                query_vector=dummy_vec,
                limit=top_k,
                score_threshold=0.0,
                where={"memory_type": "longterm"},
            )

            results = []
            for hit in raw_results:
                meta = hit.get("metadata", {})
                results.append({
                    "id": hit.get("id"),
                    "score": hit.get("score", 0.0),
                    "content": meta.get("content", ""),
                    "category": meta.get("category", "fact"),
                    "timestamp": meta.get("timestamp", 0),
                    "session_id": meta.get("session_id"),
                    "source": meta.get("source", ""),
                    "decay_score": meta.get("decay_score", 1.0),
                })
            # 按 timestamp 降序排
            results.sort(key=lambda x: x.get("timestamp", 0), reverse=True)
            return results[:top_k]

        except Exception as e:
            logger.error("列出最近记忆失败: %s", e)
            return []

    # ── 删除 ────────────────────────────────────────────

    def delete_memories(self, memory_ids: List[str]) -> bool:
        """删除指定记忆

        Args:
            memory_ids: 要删除的 memory_id 列表

        Returns:
            是否成功
        """
        if not memory_ids:
            return True

        if not self.available:
            return False

        try:
            self._qdrant.delete_memories(memory_ids)
            logger.info("记忆删除成功: %d 条", len(memory_ids))
            return True
        except Exception as e:
            logger.error("记忆删除异常: %s", e, exc_info=True)
            return False

    def delete_by_id(self, memory_id: str) -> bool:
        """删除单条记忆"""
        return self.delete_memories([memory_id])

    # ── 遗忘机制（衰减式） ────────────────────────────────

    def process_decay(self) -> Dict[str, int]:
        """处理记忆衰减（懒策略）

        遍历所有记忆，根据分类对应的衰减速率计算当前衰减分数。
        衰减分数归零的记忆被删除，其余记忆更新分数和时间戳。

        机制设计：
        - 每条记忆有 ``decay_score``（初始 1.0）和 ``last_decay_ts``（上次衰减计算时间）
        - 每 ``DECAY_INTERVAL_DAYS`` 天为一个衰减周期，每周期按分类对应的速率衰减
        - 被检索命中的记忆会重置 ``last_decay_ts``（访问强化，用进废退）
        - 本方法只在程序启动或手动触发时执行，不随每轮对话触发

        Returns:
            ``{"deleted": int, "updated": int, "total": int}``
        """
        if not self.available:
            return {"deleted": 0, "updated": 0, "total": 0}

        from qdrant_client.http.models import PointIdsList

        now_ts = int(datetime.now().timestamp())
        interval_seconds = DECAY_INTERVAL_DAYS * 86400

        deleted_count = 0
        updated_count = 0
        total_count = 0

        to_delete: list = []
        to_update: list = []  # [(point_id, {"decay_score": float, "last_decay_ts": int}), ...]

        try:
            offset = None
            while True:
                points, next_offset = self._qdrant.client.scroll(
                    collection_name=self.collection_name,
                    limit=200,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                if not points:
                    break

                for point in points:
                    total_count += 1
                    payload = point.payload or {}

                    category = payload.get("category", "fact")
                    current_score = payload.get("decay_score", 1.0)
                    # 兼容旧记忆：没有 last_decay_ts 时回退到 timestamp 或 added_at
                    last_decay_ts = payload.get(
                        "last_decay_ts",
                        payload.get("timestamp", payload.get("added_at", now_ts)),
                    )

                    # 计算已过去的完整衰减周期数
                    elapsed = now_ts - last_decay_ts
                    if elapsed < interval_seconds:
                        continue  # 不足一个周期，跳过

                    periods = elapsed // interval_seconds
                    rate = CATEGORY_DECAY_RATES.get(category, DEFAULT_DECAY_RATE)
                    new_score = max(0.0, current_score - rate * periods)

                    if new_score <= 0.0:
                        # 衰减归零 → 删除
                        to_delete.append(point.id)
                    else:
                        # 更新分数，并将 last_decay_ts 推进到最近一个周期边界
                        new_last_decay_ts = last_decay_ts + periods * interval_seconds
                        to_update.append((point.id, {
                            "decay_score": round(new_score, 4),
                            "last_decay_ts": new_last_decay_ts,
                        }))

                offset = next_offset
                if not offset:
                    break

            # 批量删除归零记忆
            if to_delete:
                for i in range(0, len(to_delete), 500):
                    batch = to_delete[i:i + 500]
                    self._qdrant.client.delete(
                        collection_name=self.collection_name,
                        points_selector=PointIdsList(points=batch),
                        wait=True,
                    )
                deleted_count = len(to_delete)

            # 逐条更新衰减分数（每条分数不同，无法批量）
            for point_id, new_payload in to_update:
                try:
                    self._qdrant.client.set_payload(
                        collection_name=self.collection_name,
                        payload=new_payload,
                        points=[point_id],
                    )
                    updated_count += 1
                except Exception as e:
                    logger.warning("更新记忆衰减分数失败: id=%s err=%s", point_id, e)

            logger.info(
                "记忆衰减处理完成: 总计 %d 条，删除 %d 条，更新 %d 条",
                total_count, deleted_count, updated_count,
            )
            return {"deleted": deleted_count, "updated": updated_count, "total": total_count}

        except Exception as e:
            logger.warning("记忆衰减处理跳过（Qdrant 异常）: %s", e)
            return {"deleted": deleted_count, "updated": updated_count, "total": total_count}

    def cleanup_expired(self, days: Optional[int] = None) -> int:
        """向后兼容：触发一次衰减处理，返回删除数量

        ``days`` 参数已废弃（衰减周期由分类决定），保留仅为兼容旧调用方。

        Args:
            days: 已废弃，忽略

        Returns:
            被删除的记忆数量
        """
        result = self.process_decay()
        return result["deleted"]

    def _reinforce_memories(self, memory_ids: List[str]):
        """访问强化：重置被检索记忆的衰减计时器

        被检索命中的记忆说明仍然有用，将 ``last_decay_ts`` 重置为当前时间，
        给予另一个完整的衰减周期。频繁被访问的记忆将持久存在（用进废退）。

        这是轻量级操作——每次检索最多涉及 ``top_k`` 条记忆（通常 5 条），
        每条仅一次 ``set_payload`` 调用。强化失败不影响检索结果。
        """
        if not self.available or not memory_ids:
            return

        now_ts = int(datetime.now().timestamp())

        for mid in memory_ids:
            try:
                self._qdrant.client.set_payload(
                    collection_name=self.collection_name,
                    payload={"last_decay_ts": now_ts},
                    points=[mid],
                )
            except Exception:
                pass  # 强化失败不影响检索结果

    # ── 统计 ────────────────────────────────────────────

    def get_stats(self) -> Dict[str, Any]:
        """获取记忆统计信息

        Returns:
            包含 total_count, categories 等统计的字典
        """
        if not self.available:
            return {
                "total_count": 0,
                "categories": {},
                "collection_name": self.collection_name,
                "forget_days": self.forget_days,
                "available": False,
                "decay_interval_days": DECAY_INTERVAL_DAYS,
            }

        try:
            info = self._qdrant.get_collection_info()
            total = info.get("points_count", 0)

            # 统计各分类数量 + 衰减分数分布
            cat_counts: Dict[str, int] = {}
            decay_stats: Dict[str, Any] = {"avg_score": 0.0, "below_half": 0, "total_scanned": 0}
            score_sum = 0.0

            try:
                offset = None
                while True:
                    points, next_offset = self._qdrant.client.scroll(
                        collection_name=self.collection_name,
                        limit=200,
                        offset=offset,
                        with_payload=True,
                        with_vectors=False,
                    )
                    if not points:
                        break
                    for p in points:
                        payload = p.payload or {}
                        cat = payload.get("category", "unknown")
                        cat_counts[cat] = cat_counts.get(cat, 0) + 1

                        score = payload.get("decay_score", 1.0)
                        score_sum += score
                        decay_stats["total_scanned"] += 1
                        if score < 0.5:
                            decay_stats["below_half"] += 1

                    offset = next_offset
                    if not offset:
                        break
            except Exception:
                pass  # 统计分类失败不影响主流程

            scanned = decay_stats["total_scanned"]
            decay_stats["avg_score"] = round(score_sum / scanned, 4) if scanned > 0 else 0.0

            return {
                "total_count": total,
                "categories": cat_counts,
                "collection_name": self.collection_name,
                "forget_days": self.forget_days,
                "vector_dimension": self._dimension,
                "decay_interval_days": DECAY_INTERVAL_DAYS,
                "decay_rates": CATEGORY_DECAY_RATES,
                "decay_stats": decay_stats,
            }
        except Exception as e:
            logger.error("获取记忆统计失败: %s", e)
            return {
                "total_count": 0,
                "categories": {},
                "collection_name": self.collection_name,
                "forget_days": self.forget_days,
                "decay_interval_days": DECAY_INTERVAL_DAYS,
            }

    def health_check(self) -> bool:
        """健康检查"""
        if not self.available:
            return False
        return self._qdrant.health_check()
