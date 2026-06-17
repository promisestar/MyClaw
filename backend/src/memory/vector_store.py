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
# 默认遗忘天数
DEFAULT_FORGET_DAYS = 7


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
            payload = {
                "content": content,
                "category": category,
                "memory_type": "longterm",
                "memory_id": memory_id,
                "timestamp": int(now.timestamp()),
                "added_at": int(now.timestamp()),
                "source": source,
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
                })

            logger.debug("记忆检索: query='%s' → %d 结果", query[:50], len(results))
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

    # ── 遗忘机制 ────────────────────────────────────────

    def cleanup_expired(self, days: Optional[int] = None) -> int:
        """清除超过 N 天的记忆（遗忘机制）

        利用 Qdrant 的 timestamp 整数索引按时间范围过滤删除。

        Args:
            days: 保留天数，默认使用初始化时的 forget_days

        Returns:
            删除的记忆数量（估算值，Qdrant delete_by_filter 不返回精确计数）
        """
        if not self.available:
            return 0

        retain_days = days if days is not None else self.forget_days
        cutoff_ts = int(datetime.now().timestamp()) - retain_days * 86400

        try:
            count = self._cleanup_by_scroll(cutoff_ts)
            logger.info("遗忘清理完成: 删除 %d 条超过 %d 天的记忆", count, retain_days)
            return count
        except Exception as e:
            logger.warning("遗忘清理跳过（Qdrant 连接异常）: %s", e)
            return 0

    def _cleanup_by_scroll(self, cutoff_ts: int) -> int:
        """通过 scroll 遍历找到过期记忆并批量删除"""
        if not self.available:
            return 0

        from qdrant_client.http.models import (
            Filter, FieldCondition, Range, PointIdsList,
        )
        from qdrant_client.http import models

        try:
            # 使用 Range 条件过滤 timestamp < cutoff_ts
            expired_filter = Filter(
                must=[
                    FieldCondition(
                        key="timestamp",
                        range=Range(lt=cutoff_ts),
                    ),
                    FieldCondition(
                        key="memory_type",
                        match=models.MatchValue(value="longterm"),
                    ),
                ]
            )

            # 先 scroll 获取所有过期点的 ID
            expired_ids = []
            offset = None
            while True:
                points, next_offset = self._qdrant.client.scroll(
                    collection_name=self.collection_name,
                    scroll_filter=expired_filter,
                    limit=100,
                    offset=offset,
                    with_payload=False,
                    with_vectors=False,
                )
                if not points:
                    break
                for p in points:
                    expired_ids.append(p.id)
                offset = next_offset
                if not offset:
                    break

            if not expired_ids:
                return 0

            # 批量删除
            self._qdrant.client.delete(
                collection_name=self.collection_name,
                points_selector=PointIdsList(points=expired_ids),
                wait=True,
            )

            logger.info("遗忘清理: 删除 %d 条过期记忆 (cutoff_ts=%d)", len(expired_ids), cutoff_ts)
            return len(expired_ids)

        except Exception as e:
            logger.error("scroll 清理异常: %s", e, exc_info=True)
            raise

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
            }

        try:
            info = self._qdrant.get_collection_info()
            total = info.get("points_count", 0)

            # 统计各分类数量
            cat_counts: Dict[str, int] = {}
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
                        cat = (p.payload or {}).get("category", "unknown")
                        cat_counts[cat] = cat_counts.get(cat, 0) + 1
                    offset = next_offset
                    if not offset:
                        break
            except Exception:
                pass  # 统计分类失败不影响主流程

            return {
                "total_count": total,
                "categories": cat_counts,
                "collection_name": self.collection_name,
                "forget_days": self.forget_days,
                "vector_dimension": self._dimension,
            }
        except Exception as e:
            logger.error("获取记忆统计失败: %s", e)
            return {
                "total_count": 0,
                "categories": {},
                "collection_name": self.collection_name,
                "forget_days": self.forget_days,
            }

    def health_check(self) -> bool:
        """健康检查"""
        if not self.available:
            return False
        return self._qdrant.health_check()
