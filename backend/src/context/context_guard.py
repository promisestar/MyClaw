"""Context Guard —— 工具执行前的智能路由引擎。

在工具实际执行之前，根据预估值表判断输出规模，决定执行策略：
- inline  → 直接执行，结果放入主上下文（小工具：calculator、memory_add）
- snip    → 正常执行，但输出由 ContextManager 在压缩层按 tool_snip_chars 截断（中等）
- delegate → 委托给子代理，主上下文只收到摘要（大工具：web_fetch 等）

核心理念：
    不让臃肿的工具输出进入主 Agent 的上下文。在数据进入之前就拦截它。

阈值（small/large）按当前 LLM 上下文窗口比例动态计算；
工具 output_size_hint / TOOL_ESTIMATES 仍是「预估输出 token 绝对值」，不随窗口缩放
（否则阈值与预估同比例放大会导致路由结果不变）。

使用方式：
    guard = ContextGuard(orchestrator=subagent_orchestrator)
    guard.update_context_window(128_000)
    strategy = guard.decide("Read")
    if strategy == "delegate":
        result = await guard.delegate_tool("Read", {"path": "src/main.py"})
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..agent.subagent_orchestrator import SubAgentOrchestrator

logger = logging.getLogger(__name__)


class ContextGuard:
    """上下文守卫 —— 工具执行前预判 + 路由。

    在 EnhancedSimpleAgent._try_execute_ready_tool 中拦截，
    决定每个工具调用是直接执行还是委托给子代理。
    """

    # ── 相对 128K 基准窗口的额度比例 ──
    REFERENCE_WINDOW = 128_000
    SMALL_RATIO = 2000 / 128_000   # ≈ 1.56%
    LARGE_RATIO = 8000 / 128_000   # ≈ 6.25%
    MIN_SMALL = 500

    # 兼容旧代码/文档引用的类常量（128K 基准下的绝对值）
    SMALL_THRESHOLD = 2000
    LARGE_THRESHOLD = 8000

    # ── 工具 → 预估输出大小(tokens，绝对值，不随窗口缩放) ──
    # 键名与实际注册的工具 name 对齐；registry 的 output_size_hint 优先。
    TOOL_ESTIMATES: Dict[str, int] = {
        "Read": 5000,
        "Write": 500,
        "Edit": 800,
        "execute_command": 3000,
        "web_search": 4000,
        "web_fetch": 8000,
        "memory": 1000,
        "memory_search": 1000,
        "memory_add": 200,
        "memory_list": 1500,
        "memory_get": 800,
        "memory_delete": 200,
        "memory_cleanup": 500,
        "calculator": 100,
        "Skill": 2000,
        "skill_manage": 800,
        "mcp": 5000,
        "rag": 5000,
        "rag_ask": 5000,
        "subagent": 500,
        "task": 300,
        "search_content": 3000,
        "search_file": 1000,
        "list_dir": 1500,
        "http_request": 4000,
        "browser": 4000,
        "automation": 500,
        "session_search": 4000,
    }

    # ── 不适合委托的工具 ──
    NO_DELEGATE_TOOLS = {
        "Write",
        "Edit",
        "write_file",  # 旧名兼容
        "edit_file",
        "memory_add",
        "memory_delete",
        "memory_cleanup",
        "task",
        "subagent",
        "Skill",
        "skill_manage",
        "calculator",
        "browser",
        "automation",
    }

    def __init__(
        self,
        orchestrator: Optional["SubAgentOrchestrator"] = None,
        tool_registry: Optional[Any] = None,
        context_window: int = REFERENCE_WINDOW,
    ):
        """初始化上下文守卫。

        Args:
            orchestrator: SubAgentOrchestrator 实例。如果为 None，delegate 策略不可用，
                          会降级为 snip。
            tool_registry: ToolRegistry 实例。如果提供，decide() 会优先从工具元数据
                          (output_size_hint, has_side_effects) 读取，而非硬编码字典。
            context_window: 当前模型上下文窗口（token），用于重算 small/large 阈值。
        """
        self._orchestrator = orchestrator
        self._tool_registry = tool_registry
        self._dynamic_estimates: Dict[str, int] = {}
        self._dynamic_no_delegate: set = set()
        self.context_window = context_window
        self.small_threshold = self.SMALL_THRESHOLD
        self.large_threshold = self.LARGE_THRESHOLD
        self.update_context_window(context_window)
        if tool_registry is not None:
            self._build_metadata_from_registry()

    def update_context_window(self, window: int) -> None:
        """按上下文窗口比例重算 inline/snip/delegate 阈值。

        在 128K 基准下与历史常量一致（2000 / 8000）。
        更大窗口提高 large 阈值，避免 web_fetch 等被不必要地委派。
        """
        if window <= 0:
            window = self.REFERENCE_WINDOW
        self.context_window = window
        small = max(self.MIN_SMALL, int(window * self.SMALL_RATIO))
        large = max(small + 1, int(window * self.LARGE_RATIO))
        self.small_threshold = small
        self.large_threshold = large

    def set_tool_registry(self, tool_registry: Any) -> None:
        """设置或更新工具注册表（工具注册后调用）。"""
        self._tool_registry = tool_registry
        self._build_metadata_from_registry()

    def _build_metadata_from_registry(self) -> None:
        """从工具注册表构建动态元数据映射。"""
        if self._tool_registry is None:
            return
        try:
            for tool in self._tool_registry.get_all_tools():
                name = getattr(tool, "name", None)
                if name is None:
                    continue
                hint = getattr(tool, "output_size_hint", None)
                if hint is not None:
                    self._dynamic_estimates[name] = hint
                if getattr(tool, "has_side_effects", False):
                    self._dynamic_no_delegate.add(name)
        except Exception as e:
            logger.warning(f"构建工具元数据失败，回退到硬编码: {e}")

    # ------------------------------------------------------------------ #
    # 公开 API
    # ------------------------------------------------------------------ #

    def decide(self, tool_name: str) -> str:
        """根据工具名判断执行策略。

        优先从工具元数据 (output_size_hint, has_side_effects) 读取，
        回退到硬编码 TOOL_ESTIMATES / NO_DELEGATE_TOOLS。
        与 small_threshold / large_threshold（随窗口动态）比较。

        Returns:
            "inline" | "snip" | "delegate"
        """
        no_delegate = self._dynamic_no_delegate | self.NO_DELEGATE_TOOLS
        estimates = {**self.TOOL_ESTIMATES, **self._dynamic_estimates}

        if tool_name in no_delegate:
            estimate = estimates.get(tool_name, 2000)
            return "snip" if estimate > self.small_threshold else "inline"

        estimate = estimates.get(tool_name, 2000)

        if estimate < self.small_threshold:
            return "inline"
        elif estimate < self.large_threshold:
            return "snip"
        else:
            if self._orchestrator is None:
                return "snip"
            return "delegate"

    def should_delegate(self, tool_name: str) -> bool:
        """工具是否应该委托给子代理。"""
        return self.decide(tool_name) == "delegate"

    async def delegate_tool(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
    ) -> str:
        """将工具调用委托给子代理执行，返回摘要结果。

        子代理在隔离上下文中调用工具，结果以 LLM 摘要形式返回。
        主上下文永远不会看到原始的工具输出。

        Args:
            tool_name: 工具名称
            arguments: 工具参数（LLM 产生的原始参数）

        Returns:
            格式化的摘要文本，可直接作为 tool result 放入主上下文

        Raises:
            RuntimeError: 如果 orchestrator 不可用
        """
        if self._orchestrator is None:
            raise RuntimeError(
                f"无法委托工具 '{tool_name}'：orchestrator 未初始化。"
                f"降级为直接执行。"
            )

        from ..agent.subagent_orchestrator import SubAgentTask, SubAgentResultMode

        task_desc = self._format_task_description(tool_name, arguments)

        task = SubAgentTask(
            description=task_desc,
            tools=[tool_name],
            result_mode=SubAgentResultMode.SUMMARY,
            max_iterations=3,
        )

        result = await self._orchestrator.run_task(task)

        if result.success:
            return (
                f"[自动委托给子代理 {result.task_id}]\n"
                f"工具: {tool_name}\n"
                f"工具调用: {result.tool_calls_count} 次\n"
                f"耗时: {result.duration_ms:.0f}ms\n"
                f"结果摘要:\n{result.summary}"
            )
        else:
            return (
                f"[自动委托失败 {result.task_id}]\n"
                f"工具: {tool_name}\n"
                f"错误: {result.error}\n"
                f"请尝试直接调用该工具或调整参数后重试。"
            )

    # ------------------------------------------------------------------ #
    # 内部
    # ------------------------------------------------------------------ #

    @staticmethod
    def _format_task_description(tool_name: str, arguments: Dict[str, Any]) -> str:
        """将 LLM 工具调用转为子代理可理解的自然语言任务描述。"""
        formatters = {
            "Read": lambda args: (
                f"使用 Read 工具读取文件 '{args.get('path', 'unknown')}'"
                + (f"（限制 {args.get('limit', 'all')} 行）" if args.get("limit") else "")
                + "。只输出文件的完整内容，不要做任何解释或总结。"
            ),
            "read_file": lambda args: (
                f"使用 Read 工具读取文件 '{args.get('path', 'unknown')}'"
                + (f"（限制 {args.get('limit', 'all')} 行）" if args.get("limit") else "")
                + "。只输出文件的完整内容，不要做任何解释或总结。"
            ),
            "web_search": lambda args: (
                f"使用 web_search 工具搜索关键词 '{args.get('query', 'unknown')}'"
                + (f"（返回 {args.get('count', 5)} 条结果）" if args.get("count") else "")
                + "。只输出搜索结果，不要做任何解释或总结。"
            ),
            "web_fetch": lambda args: (
                f"使用 web_fetch 工具抓取 URL '{args.get('url', 'unknown')}'"
                + "。返回网页内容，不要做任何解释或总结。"
            ),
            "execute_command": lambda args: (
                f"使用 execute_command 工具执行命令 '{args.get('command', 'unknown')}'"
                + "。只输出命令执行结果，不要做任何解释或总结。"
            ),
            "rag": lambda args: (
                f"使用 rag 工具查询知识库 '{args.get('question') or args.get('query', 'unknown')}'"
                + "。只输出检索结果，不要做任何解释或总结。"
            ),
            "rag_ask": lambda args: (
                f"使用 rag 工具查询知识库 '{args.get('query', 'unknown')}'"
                + "。只输出检索结果，不要做任何解释或总结。"
            ),
            "search_content": lambda args: (
                f"使用 search_content 工具搜索内容，正则 '{args.get('pattern', 'unknown')}'"
                + (f"，目录 {args.get('path', '工作空间根')}" if args.get("path") else "")
                + "。只输出搜索结果，不要做任何解释或总结。"
            ),
            "search_file": lambda args: (
                f"使用 search_file 工具搜索文件，glob '{args.get('pattern', 'unknown')}'"
                + "。只输出文件列表，不要做任何解释或总结。"
            ),
            "list_dir": lambda args: (
                f"使用 list_dir 工具列出目录"
                + (f" {args.get('target_directory', '')}" if args.get("target_directory") else "")
                + "。只输出目录内容，不要做任何解释或总结。"
            ),
            "http_request": lambda args: (
                f"使用 http_request 工具请求 {args.get('method', 'GET')} {args.get('url', 'unknown')}"
                + "。只输出响应内容，不要做任何解释或总结。"
            ),
            "session_search": lambda args: (
                f"使用 session_search 工具检索历史会话，参数为："
                f"{json.dumps(arguments, ensure_ascii=False)}。"
                "只输出检索结果，不要做任何解释或总结。"
            ),
        }

        formatter = formatters.get(tool_name)
        if formatter:
            return formatter(arguments)

        return (
            f"使用 {tool_name} 工具，参数为：{json.dumps(arguments, ensure_ascii=False)}。"
            f"只输出工具返回的结果，不要做任何解释或总结。"
        )
