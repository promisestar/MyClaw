"""记忆工具 - 基于 Qdrant 向量数据库的长期记忆管理

子动作：
- memory_search: 语义检索记忆
- memory_get: 按 ID 查询记忆
- memory_add: 写入长期记忆（含 memory_update_longterm 的语义）
- memory_list: 列出最近记忆
- memory_cleanup: 清除过期记忆
- memory_delete: 删除指定记忆
"""

from typing import List, Dict, Any, Optional

from hello_agents.tools import Tool, ToolParameter, ToolResponse, tool_action


class MemoryTool(Tool):
    """记忆管理工具

    所有记忆操作基于 Qdrant 向量数据库，提供语义检索能力。
    记忆检索由 Agent 按需调用（与 RAGTool 使用方式一致）。
    """

    def __init__(self, memory_store=None, workspace_manager=None):
        """初始化记忆工具

        Args:
            memory_store: MemoryVectorStore 实例（优先使用）
            workspace_manager: WorkspaceManager 实例（过渡期回退）
        """
        super().__init__(
            name="memory",
            description="长期记忆管理工具：支持语义检索(memory_search)、按ID查询(memory_get)、"
                        "写入记忆(memory_add)、列出近期记忆(memory_list)、清除过期记忆(memory_cleanup)、"
                        "删除指定记忆(memory_delete)。"
                        "当需要回忆之前的对话内容、用户偏好、历史决策、个人实体信息时，"
                        "优先使用 memory_search 进行语义检索。",
            expandable=True,
        )
        self.memory_store = memory_store
        self.workspace = workspace_manager  # 过渡期回退

    def _has_store(self) -> bool:
        return self.memory_store is not None

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        """默认执行：语义搜索记忆"""
        keyword = parameters.get("keyword", "")
        return self._search_memory(keyword)

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
            # 回退到旧的文件搜索
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

            # 格式化时间
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
                formatted_parts.append(f"**{source}** ({line_range}):\n```\n{content}\n```")

        return ToolResponse.success(
            text=f"找到 {total_matches} 处匹配 '{keyword}':\n\n" + "\n\n".join(formatted_parts),
            data={"results": results, "count": total_matches, "keyword": keyword},
        )

    # ── memory_get: 按 ID 查询 ──────────────────────────

    @tool_action("memory_get", "按记忆 ID 查询具体内容")
    def _get_memory(self, memory_id: str) -> ToolResponse:
        """按记忆 ID 查询具体内容

        Args:
            memory_id: 记忆的唯一标识符（UUID 格式）
        """
        if not memory_id:
            return ToolResponse.error(code="INVALID_INPUT", message="请提供 memory_id")

        if self._has_store():
            # 用特定 ID 检索（Qdrant 不支持 batch get by id，用搜索兜底）
            results = self.memory_store.search_memories(
                query=memory_id, top_k=20, score_threshold=0.0
            )
            for r in results:
                if r.get("id") == memory_id:
                    from datetime import datetime
                    ts = r.get("timestamp", 0)
                    time_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""
                    content = r.get("content", "")
                    cat = r.get("category", "fact")
                    return ToolResponse.success(
                        text=f"### 记忆 [{cat}] ({time_str})\nID: `{memory_id}`\n\n{content}",
                        data=r,
                    )
            return ToolResponse.error(
                code="NOT_FOUND",
                message=f"未找到 ID 为 '{memory_id}' 的记忆",
            )
        elif self.workspace:
            # 回退
            content = self.workspace.read_memory_lines(memory_id)
            if content:
                return ToolResponse.success(text=content)
            return ToolResponse.error(code="NOT_FOUND", message=f"未找到记忆 '{memory_id}'")

        return ToolResponse.error(code="NO_STORE", message="记忆存储未初始化")

    # ── memory_add: 写入长期记忆 ─────────────────────────

    @tool_action("memory_add", "写入一条新的长期记忆")
    def _add_memory(
        self,
        content: str,
        category: str = "fact",
        session_id: str = None,
    ) -> ToolResponse:
        """写入长期记忆（合并了旧 memory_update_longterm 的功能）

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
            # 回退到旧存储
            self.workspace.append_classified_memory(content, category)
            return ToolResponse.success(
                text=f"已写入记忆 [{category}]: {content[:80]}...",
            )

        return ToolResponse.error(code="NO_STORE", message="记忆存储未初始化")

    @tool_action(
        "memory_update_longterm",
        "更新长期记忆（已弃用，请使用 memory_add 代替）",
    )
    def _update_longterm(self, content: str) -> ToolResponse:
        """更新长期记忆（已弃用，自动转发到 memory_add）"""
        return self._add_memory(content=content, category="fact")

    # ── memory_list: 列出近期记忆 ────────────────────────

    @tool_action("memory_list", "列出最近的长期记忆")
    def _list(self, top_k: int = 20) -> ToolResponse:
        """列出最近的长期记忆

        Args:
            top_k: 返回条数，默认 20
        """
        if self._has_store():
            results = self.memory_store._list_recent(top_k)
            if not results:
                return ToolResponse.success(text="暂无长期记忆")

            from datetime import datetime

            lines = [f"# 最近 {len(results)} 条长期记忆\n"]
            for i, r in enumerate(results, 1):
                ts = r.get("timestamp", 0)
                time_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else ""
                cat = r.get("category", "fact")
                mem_id = r.get("id", "?")
                content = r.get("content", "")
                lines.append(f"### {i}. [{cat}] ({time_str})\nID: `{mem_id}`\n{content}\n")

            return ToolResponse.success(
                text="\n".join(lines),
                data={"memories": results, "count": len(results)},
            )

        elif self.workspace:
            # 回退
            files = self.workspace.list_memory_files()
            if not files:
                return ToolResponse.success(text="暂无记忆文件")
            lines = ["# 记忆文件列表\n"]
            for f in files:
                size_kb = f["size"] / 1024
                lines.append(f"- **{f['name']}** ({f['type']}, {size_kb:.1f} KB)")
            return ToolResponse.success(text="\n".join(lines))

        return ToolResponse.error(code="NO_STORE", message="记忆存储未初始化")

    # ── memory_cleanup: 衰减处理（删除归零记忆） ──────────

    @tool_action("memory_cleanup", "处理记忆衰减，删除衰减分数归零的长期记忆")
    def _cleanup(self) -> ToolResponse:
        """处理记忆衰减（懒策略）

        遍历所有记忆，根据分类对应的衰减速率计算当前衰减分数。
        衰减分数归零的记忆被删除，其余记忆更新分数。
        被检索命中的记忆会重置衰减计时器（访问强化）。
        """
        if self._has_store():
            result = self.memory_store.process_decay()
            return ToolResponse.success(
                text=f"衰减处理完成: 总计 {result['total']} 条，删除 {result['deleted']} 条，更新 {result['updated']} 条",
                data=result,
            )

        elif self.workspace:
            # 回退
            deleted = self.workspace.cleanup_old_memories(7)
            if not deleted:
                return ToolResponse.success(text="没有需要清理的记忆")
            return ToolResponse.success(
                text=f"已清理 {len(deleted)} 个过期记忆文件",
                data={"deleted": deleted},
            )

        return ToolResponse.error(code="NO_STORE", message="记忆存储未初始化")

    # ── memory_delete: 删除指定记忆 ──────────────────────

    @tool_action("memory_delete", "删除指定的长期记忆（按 ID）")
    def _delete(self, memory_id: str = None, memory_ids: str = None) -> ToolResponse:
        """删除指定长期记忆

        Args:
            memory_id: 单个记忆 ID
            memory_ids: 多个记忆 ID，用逗号分隔
        """
        ids: List[str] = []
        if memory_ids:
            ids = [i.strip() for i in memory_ids.split(",") if i.strip()]
        if memory_id:
            ids.append(memory_id.strip())

        if not ids:
            return ToolResponse.error(code="INVALID_INPUT", message="请提供要删除的记忆 ID")

        if self._has_store():
            success = self.memory_store.delete_memories(ids)
            if success:
                return ToolResponse.success(
                    text=f"已删除 {len(ids)} 条记忆",
                    data={"deleted_ids": ids},
                )
            return ToolResponse.error(code="DELETE_FAILED", message="记忆删除失败")

        elif self.workspace:
            return ToolResponse.success(
                text="文件存储模式下不支持按 ID 删除，请使用 memory_cleanup 清理过期记忆",
            )

        return ToolResponse.error(code="NO_STORE", message="记忆存储未初始化")
