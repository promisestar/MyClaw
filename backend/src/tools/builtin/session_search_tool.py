"""session_search — 跨会话原文回忆（按需工具，不自动注入）。

调用形态（参数推断，无显式 mode）：
- query → discover
- session_id + around_message_id → scroll
- 仅 session_id → read
- 无参 / 仅 limit → browse
"""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from hello_agents.tools import Tool, ToolParameter, ToolResponse


class SessionSearchTool(Tool):
    """跨会话原文检索工具（SQLite FTS / LIKE）。"""

    def __init__(
        self,
        search,
        *,
        current_session_id_getter: Optional[Callable[[], Optional[str]]] = None,
        default_limit: int = 3,
        default_window: int = 5,
    ):
        super().__init__(
            name="session_search",
            description=(
                "跨会话原文回忆：按关键词/短语检索历史对话原文，或浏览近期会话、"
                "锚定滚动阅读。问偏好/事实请用 memory_search；问「上次具体怎么说的」用本工具。"
                "不要把大段 transcript 用 memory_add 整段写入长期记忆。"
            ),
        )
        self._search = search
        self._current_session_id_getter = current_session_id_getter
        self.default_limit = default_limit
        self.default_window = default_window
        self.output_size_hint = 4000
        self.has_side_effects = False

    def _current_session_id(self) -> Optional[str]:
        if not self._current_session_id_getter:
            return None
        try:
            return self._current_session_id_getter()
        except Exception:  # noqa: BLE001
            return None

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="query",
                type="string",
                description="FTS/关键词查询（discover）。支持简单词与引号短语。",
                required=False,
            ),
            ToolParameter(
                name="session_id",
                type="string",
                description="会话 ID（scroll / read）",
                required=False,
            ),
            ToolParameter(
                name="around_message_id",
                type="integer",
                description="锚定消息 ID（scroll）",
                required=False,
            ),
            ToolParameter(
                name="window",
                type="integer",
                description=f"锚定窗口半径，默认 {self.default_window}",
                required=False,
            ),
            ToolParameter(
                name="limit",
                type="integer",
                description=f"discover/browse 返回条数，默认 {self.default_limit}",
                required=False,
            ),
            ToolParameter(
                name="role_filter",
                type="string",
                description="角色过滤，默认 user,assistant",
                required=False,
            ),
            ToolParameter(
                name="sort",
                type="string",
                description="relevance | newest | oldest",
                required=False,
            ),
            ToolParameter(
                name="include_current",
                type="boolean",
                description="是否包含当前会话 live 命中，默认 false",
                required=False,
            ),
            ToolParameter(
                name="workspace_id",
                type="string",
                description="可选：按工作区收窄",
                required=False,
            ),
        ]

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        return self._dispatch(parameters or {})

    def _dispatch(self, params: Dict[str, Any]) -> ToolResponse:
        query = params.get("query")
        session_id = params.get("session_id")
        around = params.get("around_message_id")
        window = params.get("window")
        limit = params.get("limit")
        role_filter = params.get("role_filter") or "user,assistant"
        sort = params.get("sort") or "relevance"
        include_current = bool(params.get("include_current", False))
        workspace_id = params.get("workspace_id")

        try:
            window_i = int(window) if window is not None else self.default_window
        except (TypeError, ValueError):
            window_i = self.default_window
        try:
            limit_i = int(limit) if limit is not None else self.default_limit
        except (TypeError, ValueError):
            limit_i = self.default_limit

        current = self._current_session_id()

        try:
            if query:
                result = self._search.discover(
                    str(query),
                    limit=limit_i,
                    window=window_i,
                    role_filter=str(role_filter),
                    sort=str(sort),
                    current_session_id=current,
                    workspace_id=workspace_id,
                    include_current=include_current,
                )
            elif session_id and around is not None:
                try:
                    around_i = int(around)
                except (TypeError, ValueError):
                    return ToolResponse.error(
                        code="INVALID_INPUT",
                        message="around_message_id 必须是整数",
                    )
                result = self._search.scroll(
                    str(session_id),
                    around_i,
                    window=window_i,
                    current_session_id=current,
                    include_current=include_current,
                )
            elif session_id:
                result = self._search.read_session(str(session_id))
            else:
                result = self._search.browse(limit=limit_i, workspace_id=workspace_id)
        except Exception as e:  # noqa: BLE001
            return ToolResponse.error(
                code="SESSION_SEARCH_FAILED",
                message=f"session_search 失败: {e}",
            )

        return ToolResponse.success(json.dumps(result, ensure_ascii=False))
