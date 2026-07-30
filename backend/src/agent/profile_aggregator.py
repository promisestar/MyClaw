"""用户画像聚合器 — 从 Memory 记忆自动聚合用户画像。

参考学术界用户画像方案：画像不是一次性创建，而是对话的自然副产品。
每次对话后检查触发条件，从 Memory 的 preference/entity/decision 记忆中
聚合为结构化文本，更新到 ~/.helloclaw/identity/USER.md 的自动区域。

设计原则：
- 画像跟随用户而非项目（写入 ~/.helloclaw/identity/USER.md）
- 保留手动区域不被覆盖（使用 HTML 注释标记区域边界）
- 使用轻量 LLM 调用（glm-4-flash），控制 token 消耗
"""

from __future__ import annotations

import logging
import os
import re
import tempfile
from typing import Optional

logger = logging.getLogger(__name__)


# ============================================================================
# 自动区域定义
# ============================================================================

# 自动聚合区域配置（区域名 → 分类标签列表）
AUTO_REGIONS = {
    "tech_stack": ["preference", "entity"],
    "work_domain": ["preference", "entity"],
    "communication": ["preference"],
    "code_style": ["preference"],
}

# 区域中文标题
REGION_TITLES = {
    "tech_stack": "技术栈",
    "work_domain": "工作领域",
    "communication": "沟通偏好",
    "code_style": "代码风格",
}


# ============================================================================
# 画像聚合器
# ============================================================================


class ProfileAggregator:
    """用户画像聚合器。

    从 Memory 的 preference/entity/decision 记忆中聚合为结构化画像，
    更新到 USER.md 的自动区域（保留手动区域）。

    Usage::

        aggregator = ProfileAggregator(memory_store, identity_manager, llm)
        if aggregator.should_trigger(turn_count, preference_count):
            await aggregator.aggregate()
    """

    # 触发阈值
    TURNS_PER_AGGREGATION = 10  # 每 10 轮对话触发一次
    PREFERENCE_THRESHOLD = 20   # preference 记忆超过 20 条触发

    def __init__(
        self,
        memory_store=None,
        identity_manager=None,
        llm=None,
    ):
        """初始化画像聚合器。

        Args:
            memory_store: MemoryVectorStore 实例（Qdrant）
            identity_manager: IdentityManager 实例（读写 USER.md）
            llm: 轻量 LLM 实例（推荐 glm-4-flash），None 时使用纯文本聚合
        """
        self.memory_store = memory_store
        self.identity = identity_manager
        self.llm = llm
        self._last_aggregated_turn = 0

    # ------------------------------------------------------------------ #
    # 触发判断
    # ------------------------------------------------------------------ #

    def should_trigger(
        self,
        turn_count: int,
        preference_count: Optional[int] = None,
    ) -> bool:
        """判断是否满足画像聚合触发条件。

        Args:
            turn_count: 当前对话轮次（自上次聚合以来的增量）
            preference_count: preference 分类记忆总数（可选）

        Returns:
            True 表示应触发聚合
        """
        # 条件 1：每 N 轮触发
        if turn_count - self._last_aggregated_turn >= self.TURNS_PER_AGGREGATION:
            return True

        # 条件 2：preference 记忆超过阈值
        if preference_count is not None and preference_count >= self.PREFERENCE_THRESHOLD:
            return True

        return False

    def update_turn(self, turn_count: int) -> None:
        """更新已聚合的轮次计数。"""
        self._last_aggregated_turn = turn_count

    # ------------------------------------------------------------------ #
    # 聚合主流程
    # ------------------------------------------------------------------ #

    def aggregate_sync(self) -> dict:
        """同步版画像聚合（供同步上下文如 MemoryTool 调用）。

        与 aggregate() 逻辑相同，但使用同步 LLM 调用。
        """
        return self._do_aggregate(sync_llm=True)

    async def aggregate(self) -> dict:
        """执行画像聚合。

        1. 从 Memory 检索 preference/entity/decision 记忆
        2. 调用轻量 LLM 聚合为结构化文本
        3. 更新 USER.md 自动区域（保留手动区域）

        Returns:
            聚合结果摘要 {"updated_regions": [...], "memory_count": N}
        """
        return await self._do_aggregate_async()

    async def _do_aggregate_async(self) -> dict:
        """异步聚合实现。"""
        if not self.memory_store:
            logger.warning("memory_store 未初始化，跳过画像聚合")
            return {"updated_regions": [], "memory_count": 0}

        if not self.identity:
            logger.warning("identity_manager 未初始化，跳过画像聚合")
            return {"updated_regions": [], "memory_count": 0}

        memories = self._collect_memories()
        if not memories:
            logger.info("无可用记忆，跳过画像聚合")
            return {"updated_regions": [], "memory_count": 0}

        logger.info("画像聚合：检索到 %d 条记忆", len(memories))
        aggregated = await self._llm_aggregate(memories)
        updated_regions = self._update_user_md(aggregated)

        return {
            "updated_regions": updated_regions,
            "memory_count": len(memories),
        }

    def _do_aggregate(self, sync_llm: bool = False) -> dict:
        """同步聚合实现（供 aggregate_sync 使用）。"""
        if not self.memory_store:
            logger.warning("memory_store 未初始化，跳过画像聚合")
            return {"updated_regions": [], "memory_count": 0}

        if not self.identity:
            logger.warning("identity_manager 未初始化，跳过画像聚合")
            return {"updated_regions": [], "memory_count": 0}

        memories = self._collect_memories()
        if not memories:
            logger.info("无可用记忆，跳过画像聚合")
            return {"updated_regions": [], "memory_count": 0}

        logger.info("画像聚合：检索到 %d 条记忆", len(memories))
        aggregated = self._llm_aggregate_sync(memories)
        updated_regions = self._update_user_md(aggregated)

        return {
            "updated_regions": updated_regions,
            "memory_count": len(memories),
        }

    # ------------------------------------------------------------------ #
    # 记忆收集
    # ------------------------------------------------------------------ #

    def _collect_memories(self) -> list:
        """从 Memory 检索 preference/entity/decision 记忆。"""
        all_memories: list = []

        for category in ["preference", "entity", "decision"]:
            try:
                results = self.memory_store.search_memories(
                    query=category,
                    top_k=30,
                    category=category,
                )
                all_memories.extend(results)
            except Exception as e:
                logger.warning("检索 %s 记忆失败: %s", category, e)

        # 去重（按 content）
        seen = set()
        unique: list = []
        for m in all_memories:
            content = m.get("content", "")
            if content and content not in seen:
                seen.add(content)
                unique.append(m)

        return unique

    # ------------------------------------------------------------------ #
    # LLM 聚合
    # ------------------------------------------------------------------ #

    def _llm_aggregate_sync(self, memories: list) -> dict:
        """同步版 LLM 聚合（内部 llm.invoke 本身就是同步调用）。

        与 _llm_aggregate 逻辑相同，但可在同步上下文中直接调用。
        """
        return self._do_llm_aggregate(memories)

    async def _llm_aggregate(self, memories: list) -> dict:
        """调用轻量 LLM 将记忆聚合为结构化画像文本。

        Args:
            memories: 记忆列表（每项含 content/category）

        Returns:
            {"tech_stack": "...", "work_domain": "...", ...}
        """
        return self._do_llm_aggregate(memories)

    def _do_llm_aggregate(self, memories: list) -> dict:
        """LLM 聚合核心实现（同步）。"""
        # 构建记忆摘要
        memory_lines = []
        for m in memories:
            cat = m.get("category", "fact")
            content = m.get("content", "")
            memory_lines.append(f"[{cat}] {content}")

        memory_text = "\n".join(memory_lines)

        # 如果没有 LLM，使用纯文本聚合（降兜）
        if not self.llm:
            return self._text_aggregate(memories)

        # LLM 聚合提示词
        prompt = f"""请根据以下用户记忆，聚合为结构化的用户画像摘要。

用户记忆：
{memory_text}

请输出 JSON 格式的画像摘要，只包含以下字段（如果某字段无相关信息则留空字符串）：
```json
{{
  "tech_stack": "用户使用的技术栈，逗号分隔",
  "work_domain": "用户的工作领域",
  "communication": "用户的沟通偏好（如简洁/详细、中文/英文等）",
  "code_style": "用户的代码风格偏好（如类型注解、函数式等）"
}}
```

只输出 JSON，不要其他解释。"""

        try:
            from hello_agents.core.message import Message
            response = self.llm.invoke([Message(prompt, "user")])
            response_text = (
                response.content if hasattr(response, "content") else str(response)
            )

            # 解析 JSON
            import json
            import re
            json_match = re.search(r'```json\s*\n(.*?)\n```', response_text, re.DOTALL)
            if json_match:
                data = json.loads(json_match.group(1).strip())
            else:
                # 尝试直接解析
                data = json.loads(response_text.strip())

            return {
                "tech_stack": data.get("tech_stack", ""),
                "work_domain": data.get("work_domain", ""),
                "communication": data.get("communication", ""),
                "code_style": data.get("code_style", ""),
            }

        except Exception as e:
            logger.warning("LLM 聚合失败，降兜到文本聚合: %s", e)
            return self._text_aggregate(memories)

    def _text_aggregate(self, memories: list) -> dict:
        """纯文本聚合（无 LLM 时的降兜方案）。

        按分类简单分组，提取关键词。
        """
        from collections import defaultdict

        by_category: dict[str, list[str]] = defaultdict(list)
        for m in memories:
            cat = m.get("category", "fact")
            content = m.get("content", "")
            by_category[cat].append(content)

        preferences = by_category.get("preference", [])
        entities = by_category.get("entity", [])
        decisions = by_category.get("decision", [])

        # 通过关键词将 entities 分为技术栈和工作领域
        tech_keywords = {
            "python", "java", "javascript", "typescript", "vue", "react",
            "angular", "node", "fastapi", "django", "flask", "sql", "mysql",
            "postgres", "redis", "docker", "kubernetes", "git", "linux",
            "css", "html", "webpack", "vite", "rust", "go", "c++", "c#",
            "swift", "kotlin", "flutter", "mongodb", "graphql", "api",
        }
        tech_items: list[str] = []
        domain_items: list[str] = []
        for entity in entities:
            lower = entity.lower()
            if any(kw in lower for kw in tech_keywords):
                tech_items.append(entity)
            else:
                domain_items.append(entity)

        # 将 decisions 前半作为工作领域补充
        if decisions and not domain_items:
            domain_items = decisions[:3]

        return {
            "tech_stack": "; ".join(tech_items[:5]) if tech_items else "",
            "work_domain": "; ".join(domain_items[:5]) if domain_items else "",
            "communication": "; ".join(preferences[:3]) if preferences else "",
            "code_style": "; ".join(preferences[3:6]) if len(preferences) > 3 else "",
        }

    # ------------------------------------------------------------------ #
    # USER.md 更新
    # ------------------------------------------------------------------ #

    def _update_user_md(self, aggregated: dict) -> list:
        """更新 USER.md 的自动区域（保留手动区域）。

        Args:
            aggregated: {"tech_stack": "...", "work_domain": "...", ...}

        Returns:
            已更新的区域名列表
        """
        # 读取当前 USER.md
        current = self.identity.user
        if not current:
            current = self._default_template()

        updated_regions: list[str] = []

        for region_key, region_title in REGION_TITLES.items():
            new_content = aggregated.get(region_key, "").strip()
            if not new_content:
                continue

            # 替换或插入自动区域
            current = self._replace_region(
                current, region_key, region_title, new_content
            )
            updated_regions.append(region_key)

        # 原子化保存：写入临时文件后 rename，避免并发读写冲突
        if updated_regions:
            self._atomic_save_user_md(current)
            logger.info("USER.md 更新完成，更新区域: %s", updated_regions)

        return updated_regions

    def _atomic_save_user_md(self, content: str) -> None:
        """原子化写入 USER.md，防止并发读写冲突。

        先写入同目录临时文件，再 os.replace 原子替换。
        """
        user_md_path = os.path.join(self.identity.identity_dir, "USER.md")

        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=self.identity.identity_dir, prefix=".user_md_", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                os.replace(tmp_path, user_md_path)
            except Exception:
                # 清理临时文件
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise
        except Exception as e:
            logger.error("原子写入 USER.md 失败: %s", e)
            # 降兜到直接写入
            self.identity.save_file("USER.md", content)

    def _replace_region(
        self,
        content: str,
        region_key: str,
        region_title: str,
        new_value: str,
    ) -> str:
        """替换 USER.md 中的自动区域。

        使用 HTML 注释标记区域边界：
        <!-- AUTO:tech_stack -->
        - Python, FastAPI, Vue 3
        <!-- /AUTO:tech_stack -->
        """
        open_marker = f"<!-- AUTO:{region_key} -->"
        close_marker = f"<!-- /AUTO:{region_key} -->"

        # 清理 new_value 中可能包含的区域标记，防止注入
        new_value = re.sub(r'<!--\s*/?AUTO:[^>]*-->', '', new_value).strip()

        # 构建新区域内容
        new_section = f"{open_marker}\n{new_value}\n{close_marker}"

        # 尝试匹配现有区域
        pattern = re.compile(
            re.escape(open_marker) + r".*?" + re.escape(close_marker),
            re.DOTALL,
        )

        if pattern.search(content):
            # 替换现有区域
            return pattern.sub(new_section, content)
        else:
            # 区域不存在 → 在文件末尾插入
            return content.rstrip() + f"\n\n## {region_title}\n\n{new_section}\n"

    def _default_template(self) -> str:
        """默认 USER.md 模板。"""
        return """# USER.md - 关于你的人类

## 基本信息

- **姓名：**
- **称呼：**
- **时区：** Asia/Shanghai

## 技术栈

<!-- AUTO:tech_stack -->
（暂无数据）
<!-- /AUTO:tech_stack -->

## 工作领域

<!-- AUTO:work_domain -->
（暂无数据）
<!-- /AUTO:work_domain -->

## 沟通偏好

<!-- AUTO:communication -->
（暂无数据）
<!-- /AUTO:communication -->

## 代码风格

<!-- AUTO:code_style -->
（暂无数据）
<!-- /AUTO:code_style -->

## 备注

_其他手动维护的信息_
"""
