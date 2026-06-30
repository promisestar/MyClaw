"""MemoryVectorStore - 记忆专用 Qdrant 向量存储封装

统一的长期记忆存储层，基于 Qdrant 向量数据库 + embedding 语义检索。
支持写入、检索、删除、过期清理，并在写入路径上做两层去重：

- **L1 字面去重（默认启用）**：写入前用 ``content_hash``（sha1 of
  normalized content）+ ``category`` 精确匹配。命中则直接强化旧记忆
  （重置 ``last_decay_ts`` + ``access_count`` 累加）并复用其 memory_id。
  零误判，专门拦截"反复说同一句话"类重复。

- **L2 语义去重（默认关闭）**：用新内容 embedding 在同分类范围内查
  top-1，若相似度 ≥ ``MEMORY_DEDUPE_THRESHOLD`` 判定为重复 → 强化旧记忆，
  不新建。

  默认 ``MEMORY_DEDUPE_THRESHOLD=1.0`` 关闭，因为当前默认 embedding
  ``all-MiniLM-L6-v2`` 对中文的反义/同义判别力不足（实测反义对相似度
  有时反高于同义对），无法用固定阈值兼顾"接受同义 + 拒绝反义"。
  换用 ``BAAI/bge-small-zh-v1.5`` 等中文 embedding 后，把阈值调到
  0.90~0.93 即可启用。

两层去重均不抛异常，失败时回退为正常写入，不会阻塞主对话流程。
"""

import hashlib
import logging
import os
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Any, Tuple

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

# ── 写入路径去重配置 ───────────────────────────────────
# L2 语义去重相似度阈值：余弦相似度 ≥ 阈值则判定为重复，强化旧记忆而非新建。
#
# 默认 1.0 = **关闭 L2**。原因：当前默认 embedding 模型 ``all-MiniLM-L6-v2``
# 对中文的语义场判别力不足，实测反义对（"喜欢" vs "讨厌"）的余弦相似度
# 反而高于同义改写对，无法用固定阈值同时满足"接受同义 + 拒绝反义"。
# 因此默认只启用 L1 字面去重（完全无误判）。
#
# 启用 L2 的前提：换用对中文友好的 embedding 模型（如 ``BAAI/bge-small-zh-v1.5``
# 或 ``text-embedding-3-small``）。届时把阈值降到 0.90~0.93 即可生效。
#
# 经验值参考（bge-small-zh-v1.5）：
#   - "我喜欢简洁回复" vs "我偏好简洁的回复风格"  ~0.93-0.96
#   - "我喜欢简洁回复" vs "我喜欢长篇大论详细回复" ~0.82-0.88
try:
    MEMORY_DEDUPE_THRESHOLD = float(os.getenv("MEMORY_DEDUPE_THRESHOLD", "1.0"))
except ValueError:
    MEMORY_DEDUPE_THRESHOLD = 1.0

# L2 是否启用：阈值 >= 1.0 视为关闭（任何余弦相似度都不可能命中）
_L2_ENABLED = MEMORY_DEDUPE_THRESHOLD < 1.0


def _normalize_content(content: str) -> str:
    """归一化文本用于 L1 字面去重的 hash 计算。

    规则：
    - 转小写
    - strip 两端空白
    - 内部连续空白合并为单个空格

    这样"我喜欢简洁回复"和"我喜欢简洁回复 "在 hash 上等价。
    """
    if not content:
        return ""
    return " ".join(content.lower().split())


def _compute_content_hash(content: str) -> str:
    """计算归一化后内容的 sha1[:16]，用作 Qdrant payload 中的 content_hash 字段。

    截取前 16 个字符（64 bit）即可：记忆库总量预期 << 2^32，碰撞概率可忽略。
    """
    norm = _normalize_content(content)
    return hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


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
            # 确保去重路径所需的 payload index 存在（Qdrant 云端服务要求
            # 所有用于 filter 的字段都必须建立 keyword index，否则 search/scroll
            # 会返回 400 Bad Request）
            self._ensure_dedupe_indexes()
        except Exception as e:
            logger.warning(
                "⚠️ Qdrant 连接失败，记忆向量存储不可用，将回退到文件模式: %s", e
            )
            self._qdrant = None
            self._available = False

    def _ensure_dedupe_indexes(self) -> None:
        """为 L1/L2 去重所需的 payload 字段创建 keyword index。

        共享的 ``QdrantVectorStore._ensure_payload_indexes`` 已经覆盖了 memory_type
        / memory_id / source 等通用字段，但没有 ``category`` 与 ``content_hash``——
        这两个是 MemoryVectorStore 独有的过滤字段，需要在这里补建。

        重复调用安全：``create_payload_index`` 在索引已存在时会抛异常，被静默忽略。
        """
        if not self._qdrant or not self._qdrant.client:
            return
        try:
            from qdrant_client.http import models as qmodels
        except Exception:
            return

        fields = [
            ("category", qmodels.PayloadSchemaType.KEYWORD),
            ("content_hash", qmodels.PayloadSchemaType.KEYWORD),
        ]
        for field_name, schema_type in fields:
            try:
                self._qdrant.client.create_payload_index(
                    collection_name=self.collection_name,
                    field_name=field_name,
                    field_schema=schema_type,
                )
                logger.debug("payload index 已创建: %s", field_name)
            except Exception as ie:
                # 索引已存在 → 抛错，安全忽略
                logger.debug("payload index %s 已存在或创建失败: %s", field_name, ie)

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
        """向量化并写入一条长期记忆，写入前自动做 L1（+L2，可选）去重。

        写入流程：

        1. **L1 字面去重（默认启用）**：把归一化后的 ``content`` 求 sha1，
           按 ``content_hash`` + ``category`` 在 Qdrant 中精确匹配；命中
           则强化旧记忆并返回其 memory_id（**不新建**）。
        2. **L2 语义去重（仅当 MEMORY_DEDUPE_THRESHOLD < 1.0 时启用）**：
           L1 未命中时，用 ``content`` 的 embedding 在 ``memory_type=longterm``
           + 同 ``category`` 范围内查 top-1，若相似度 ≥ 阈值则强化旧记忆并
           返回其 memory_id（**不新建**）。默认阈值 1.0 即关闭——见模块
           docstring 中的说明。
        3. 以上都未命中 → 正常写入新记忆。

        强化策略（B 策略）：重置 ``last_decay_ts`` 为当前时间 +
        ``access_count`` 累加。**不写 aliases / 不合并内容**。

        去重检查失败（Qdrant 异常）时静默回退到普通写入，不阻塞主流程。

        Args:
            content: 记忆文本内容
            category: 分类标签（preference/decision/entity/fact/plan/relationship/reference）
            session_id: 关联的会话 ID
            source: 来源（capture/agent/flush）

        Returns:
            memory_id 字符串（可能是复用的旧 ID），失败返回 None
        """
        if not self.available:
            logger.warning("Qdrant 不可用，跳过记忆写入")
            return None

        if not content or not content.strip():
            return None

        try:
            # ── L1：字面去重（sha1 精确匹配，限定同分类） ────
            # 跨分类即使字面相同也应作为两条独立记忆（preference 与 fact 下
            # 的 "我喜欢简洁回复" 语义指向不同——前者是偏好声明，后者是事实记录）
            content_hash = _compute_content_hash(content)
            dup_id = self._find_by_hash(content_hash, category=category)
            if dup_id:
                self._reinforce_single(dup_id)
                logger.info(
                    "L1 字面去重命中：复用旧记忆 id=%s category=%s",
                    dup_id, category,
                )
                return dup_id

            # 向量化（正常写入路径用；若启用 L2 则也会复用同一份向量）
            vector = self._embedder.encode(content)
            if hasattr(vector, "tolist"):
                vector = vector.tolist()

            # ── L2：语义去重（同分类内 top-1 相似度判定） ────
            # 仅在 MEMORY_DEDUPE_THRESHOLD < 1.0 时启用——默认配置下 L2 关闭，
            # 这里短路跳过，避免每次写入都白做一次 Qdrant 查询。
            if _L2_ENABLED:
                dup_id, dup_score = self._find_semantic_duplicate(
                    vector=vector,
                    category=category,
                    threshold=MEMORY_DEDUPE_THRESHOLD,
                )
                if dup_id:
                    self._reinforce_single(dup_id)
                    logger.info(
                        "L2 语义去重命中：复用旧记忆 id=%s score=%.3f category=%s "
                        "new='%s'",
                        dup_id, dup_score, category, content[:60],
                    )
                    return dup_id

            # ── 正常写入 ─────────────────────────────────
            memory_id = str(uuid.uuid4())
            now = datetime.now()
            now_ts = int(now.timestamp())
            payload = {
                "content": content,
                "content_hash": content_hash,
                "category": category,
                "memory_type": "longterm",
                "memory_id": memory_id,
                "timestamp": now_ts,
                "added_at": now_ts,
                "source": source,
                "decay_score": 1.0,           # 衰减分数：初始满分
                "last_decay_ts": now_ts,       # 上次衰减计算时间戳
                "access_count": 0,             # 命中复用计数（每次去重命中 +1）
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

    # ── 内部：写入路径去重辅助方法 ────────────────────────

    def _find_by_hash(
        self,
        content_hash: str,
        category: Optional[str] = None,
    ) -> Optional[str]:
        """L1：按 content_hash + category 精确匹配查找已有记忆。

        使用 Qdrant 的 scroll + filter 实现（不需要向量），开销 < 10ms。
        失败时返回 None，不抛异常，保证主流程可继续走 L2 / 正常写入。

        Args:
            content_hash: 待查 hash
            category: 限定分类，None 表示跨分类查找（一般不用）

        Returns:
            命中时返回 memory_id（即 point id），未命中返回 None
        """
        if not self.available or not content_hash:
            return None
        try:
            from qdrant_client.http.models import Filter, FieldCondition
            from qdrant_client.http import models as qmodels

            must = [
                FieldCondition(
                    key="content_hash",
                    match=qmodels.MatchValue(value=content_hash),
                ),
                FieldCondition(
                    key="memory_type",
                    match=qmodels.MatchValue(value="longterm"),
                ),
            ]
            if category:
                must.append(FieldCondition(
                    key="category",
                    match=qmodels.MatchValue(value=category),
                ))

            points, _ = self._qdrant.client.scroll(
                collection_name=self.collection_name,
                scroll_filter=Filter(must=must),
                limit=1,
                with_payload=False,
                with_vectors=False,
            )
            if points:
                # point.id 即 memory_id（写入时使用同一 UUID）
                return str(points[0].id)
            return None
        except Exception as exc:
            logger.warning("L1 字面去重查询失败（回退到正常写入）: %s", exc)
            return None

    def _find_semantic_duplicate(
        self,
        vector: List[float],
        category: str,
        threshold: float,
    ) -> Tuple[Optional[str], float]:
        """L2：在同分类内做向量 top-1 检索，相似度 ≥ threshold 视为重复。

        Returns:
            (memory_id, score) — 未命中时返回 (None, 0.0)。
        """
        if not self.available:
            return (None, 0.0)
        try:
            hits = self._qdrant.search_similar(
                query_vector=vector,
                limit=1,
                score_threshold=threshold,
                where={"category": category, "memory_type": "longterm"},
            )
            if hits:
                top = hits[0]
                return (str(top.get("id")), float(top.get("score", 0.0)))
            return (None, 0.0)
        except Exception as exc:
            logger.warning("L2 语义去重查询失败（回退到正常写入）: %s", exc)
            return (None, 0.0)

    def _reinforce_single(self, memory_id: str) -> None:
        """命中去重时强化单条记忆：重置 last_decay_ts + access_count++。

        采用 read-modify-write 模式：先 retrieve 当前 access_count，
        再 set_payload 写回。失败仅记录日志，不影响调用方。
        """
        if not self.available or not memory_id:
            return
        try:
            now_ts = int(datetime.now().timestamp())
            # 读取当前 access_count
            current_count = 0
            try:
                points = self._qdrant.client.retrieve(
                    collection_name=self.collection_name,
                    ids=[memory_id],
                    with_payload=True,
                    with_vectors=False,
                )
                if points:
                    current_count = int((points[0].payload or {}).get("access_count", 0))
            except Exception:
                # 读取失败仍尝试写入（access_count 起算为 1）
                pass

            self._qdrant.client.set_payload(
                collection_name=self.collection_name,
                payload={
                    "last_decay_ts": now_ts,
                    "access_count": current_count + 1,
                },
                points=[memory_id],
            )
        except Exception as exc:
            logger.warning("强化记忆 %s 失败: %s", memory_id, exc)

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

        from qdrant_client.http.models import PointIdsList, Filter, FieldCondition
        from qdrant_client.http import models

        now_ts = int(datetime.now().timestamp())
        interval_seconds = DECAY_INTERVAL_DAYS * 86400

        deleted_count = 0
        updated_count = 0
        total_count = 0

        to_delete: list = []
        to_update: list = []  # [(point_id, {"decay_score": float, "last_decay_ts": int}), ...]

        # 仅处理记忆 chunk（memory_type=longterm），不触碰同 collection 中的 RAG chunk
        memory_filter = Filter(must=[
            FieldCondition(
                key="memory_type",
                match=models.MatchValue(value="longterm"),
            )
        ])

        try:
            offset = None
            while True:
                points, next_offset = self._qdrant.client.scroll(
                    collection_name=self.collection_name,
                    scroll_filter=memory_filter,
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
                from qdrant_client.http.models import Filter, FieldCondition
                from qdrant_client.http import models

                # 仅统计记忆 chunk，不包含 RAG chunk
                memory_filter = Filter(must=[
                    FieldCondition(
                        key="memory_type",
                        match=models.MatchValue(value="longterm"),
                    )
                ])

                offset = None
                while True:
                    points, next_offset = self._qdrant.client.scroll(
                        collection_name=self.collection_name,
                        scroll_filter=memory_filter,
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
