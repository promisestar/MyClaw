"""记忆工具 - 基于 Qdrant 向量数据库的长期记忆管理

Agent 可见子工具（expandable 展开）：
- memory_search: 语义检索记忆
- memory_add: 写入长期记忆

列表/按 ID 查询/衰减清理/删除/画像聚合仍由系统 lifespan、HTTP API、
ProfileAggregator 自动路径提供，不再暴露给 LLM，以免干扰工具选型。
"""

from typing import List, Dict, Any, Optional

from hello_agents.tools import Tool, ToolParameter, ToolResponse, tool_action


class MemoryTool(Tool):
    """记忆管理工具

    所有记忆操作基于 Qdrant 向量数据库，提供语义检索能力。
    记忆检索由 Agent 按需调用（与 RAGTool 使用方式一致）。
    """

    def __init__(self, memory_store=None, workspace_manager=None, profile_aggregator=None):
        """初始化记忆工具

        Args:
            memory_store: MemoryVectorStore 实例（优先使用）
            workspace_manager: WorkspaceManager 实例（过渡期回退）
            profile_aggregator: 保留参数以兼容旧调用方；画像聚合已改由
                MyClawAgent 自动触发，不再作为 Agent 工具暴露。
        """
        super().__init__(
            name="memory",
            description=(
                "长期记忆：memory_search 语义检索用户偏好/决策/实体等；"
                "memory_add 写入新的长期事实。"
                "需要回忆历史偏好或个人实体信息时优先 memory_search。"
            ),
            expandable=True,
        )
        self.memory_store = memory_store
        self.workspace = workspace_manager  # 过渡期回退
        # 兼容旧构造参数；Agent 工具面不再使用
        self.profile_aggregator = profile_aggregator
        # 父工具元数据（展开后由 get_expanded_tools 覆盖到各子工具）
        self.output_size_hint = 1000
        self.has_side_effects = False

    def _has_store(self) -> bool:
        return self.memory_store is not None

    def get_expanded_tools(self) -> Optional[List[Tool]]:
        """展开子工具，并为 memory_add 标记副作用。"""
        tools = super().get_expanded_tools()
        if not tools:
            return tools
        for t in tools:
            if t.name == "memory_add":
                t.has_side_effects = True
                t.output_size_hint = 200
            elif t.name == "memory_search":
                t.has_side_effects = False
                t.output_size_hint = 1000
        return tools

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        """默认执行：语义搜索记忆"""
        keyword = parameters.get("keyword", "")
        return self._search(keyword=keyword)

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="keyword",
                type="string",
                description="搜索关键词（对记忆进行语义检索）",
                required=True,
            )
        ]

    # ── memory_search: 语义检索 ──────────────────────────

    @tool_action("memory_search", "语义检索长期记忆（基于向量相似度）")
    def _search(
        self,
        keyword: str,
        top_k: int = 5,
        category: str = None,
    ) -> ToolResponse:
        """语义检索记忆

        Args:
            keyword: 检索关键词或问题
            top_k: 返回结果数量，默认 5
            category: 按分类过滤（preference/decision/entity/fact/plan/relationship/reference/rule），可选
        """
        if not keyword:
            return ToolResponse.error(
                code="INVALID_INPUT",
                message="请提供检索关键词",
            )

        if self._has_store():
            results = self.memory_store.search_memories(
                query=keyword,
                top_k=top_k,
                category=category,
            )
            return self._format_search_results(results, keyword)
        elif self.workspace:
            return self._fallback_file_search(keyword)
        else:
            return ToolResponse.error(
                code="NO_STORE",
                message="记忆存储未初始化",
            )

    def _format_search_results(self, results: List[dict], keyword: str) -> ToolResponse:
        """格式化语义检索结果"""
        if not results:
            return ToolResponse.success(
                text=f"未找到与 '{keyword}' 语义相关的记忆。",
                data={"results": [], "keyword": keyword, "count": 0},
            )

        from datetime import datetime

        lines = [f"找到 {len(results)} 条与 '{keyword}' 相关的长期记忆：\n"]
        for i, r in enumerate(results, 1):
            score = r.get("score", 0)
            score_str = f"{score:.3f}" if isinstance(score, float) and score > 0 else "?"

            ts = r.get("timestamp", 0)
            time_str = ""
            if ts:
                try:
                    dt = datetime.fromtimestamp(ts)
                    time_str = dt.strftime("%Y-%m-%d %H:%M")
                except Exception:
                    time_str = ""

            cat = r.get("category", "fact")
            content = r.get("content", "")
            mem_id = r.get("id", "?")
            lines.append(
                f"### 记忆 {i} [{cat}] (相似度: {score_str})"
                + (f" ({time_str})" if time_str else "")
                + f"\nID: `{mem_id}`\n{content}\n"
            )

        return ToolResponse.success(
            text="\n".join(lines),
            data={"results": results, "keyword": keyword, "count": len(results)},
        )

    def _fallback_file_search(self, keyword: str) -> ToolResponse:
        """回退到旧的文件搜索"""
        if not self.workspace:
            return ToolResponse.error(code="NO_STORE", message="记忆存储未初始化")

        results = self.workspace.search_memory_enhanced(keyword, context_lines=3)
        if not results:
            return ToolResponse.success(
                text=f"未找到与 '{keyword}' 相关的记忆",
                data={"results": [], "keyword": keyword},
            )

        formatted_parts = []
        total_matches = 0
        for r in results:
            source = r["source"]
            matches = r["matches"]
            total_matches += len(matches)
            for m in matches:
                start = m["start_line"]
                end = m["end_line"]
                content = m["content"]
                line_range = f"行 {start}" if start == end else f"行 {start}-{end}"
                formatted_parts.append(
                    f"**{source}** ({line_range}):\n```\n{content}\n```"
                )

        return ToolResponse.success(
            text=f"找到 {total_matches} 处匹配 '{keyword}':\n\n"
            + "\n\n".join(formatted_parts),
            data={"results": results, "count": total_matches, "keyword": keyword},
        )

    # ── memory_add: 写入长期记忆 ─────────────────────────

    @tool_action("memory_add", "写入一条新的长期记忆")
    def _add_memory(
        self,
        content: str,
        category: str = "fact",
        session_id: str = None,
    ) -> ToolResponse:
        """写入长期记忆

        Args:
            content: 记忆内容
            category: 分类标签（preference/decision/entity/fact/plan/relationship/reference/rule）
            session_id: 关联的会话 ID（可选）
        """
        if not content:
            return ToolResponse.error(code="INVALID_INPUT", message="请提供记忆内容")

        if self._has_store():
            memory_id = self.memory_store.add_memory(
                content=content,
                category=category,
                session_id=session_id,
                source="agent",
            )
            if memory_id:
                return ToolResponse.success(
                    text=f"已写入长期记忆 [{category}]: {content[:80]}...",
                    data={"memory_id": memory_id, "category": category},
                )
            return ToolResponse.error(code="WRITE_FAILED", message="记忆写入失败")

        elif self.workspace:
            self.workspace.append_classified_memory(content, category)
            return ToolResponse.success(
                text=f"已写入记忆 [{category}]: {content[:80]}...",
            )

        return ToolResponse.error(code="NO_STORE", message="记忆存储未初始化")
