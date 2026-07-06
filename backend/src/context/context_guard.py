"""Context Guard —— 工具执行前的智能路由引擎。

在工具实际执行之前，根据预估值表判断输出规模，决定执行策略：
- inline  → 直接执行，结果放入主上下文（小工具：calculator、memory_add）
- snip    → 正常执行，但输出自动截断后放入上下文（中等：execute_command、web_search）
- delegate → 委托给子代理，主上下文只收到摘要（大工具：read_file、web_fetch、rag_ask）

核心理念：
    不让臃肿的工具输出进入主 Agent 的上下文。在数据进入之前就拦截它。

使用方式：
    guard = ContextGuard(orchestrator=subagent_orchestrator)
    strategy = guard.decide("read_file")
    if strategy == "delegate":
        result = await guard.delegate_tool("read_file", {"path": "src/main.py"})
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

    # ── 阈值配置 ──
    SMALL_THRESHOLD = 2000    # tokens：低于此值直接执行
    LARGE_THRESHOLD = 8000    # tokens：高于此值委托子代理

    # ── 工具 → 预估输出大小(tokens) ──
    # 预估值来源于工具语义分析（不是实时计算）：
    # - read_file 天然产出大量文本 → 5000
    # - web_fetch 抓取网页 → 8000
    # - calculator 几乎永远短输出 → 100
    TOOL_ESTIMATES: Dict[str, int] = {
        # 入口在 subagent_orchestrator.py，此处为副本（解耦）
        # 如果你改了 orchestrator 中的值，记得同步这里
        "read_file": 5000,
        "write_file": 500,
        "edit_file": 800,
        "execute_command": 3000,
        "web_search": 4000,
        "web_fetch": 8000,
        "memory_search": 1000,
        "memory_add": 200,
        "memory_list": 1500,
        "memory_get": 800,
        "memory_delete": 200,
        "memory_cleanup": 500,
        "calculator": 100,
        "Skill": 2000,
        "mcp": 5000,
        "rag_ask": 5000,
        # subagent 工具本身输出是摘要，很小
        "subagent": 500,
        "task": 300,
    }

    # ── 不适合委托的工具 ──
    # 这些工具即使预估输出大，也不能委托给子代理，因为：
    # - write_file / edit_file：有副作用，必须在主上下文中执行
    # - memory_add / memory_delete：副作用操作
    # - task / subagent：元工具，委托会导致无限递归
    # - Skill：需要加载到主 Agent 的上下文中才有意义
    NO_DELEGATE_TOOLS = {
        "write_file",
        "edit_file",
        "memory_add",
        "memory_delete",
        "memory_cleanup",
        "task",
        "subagent",
        "Skill",
        "calculator",
    }

    def __init__(self, orchestrator: Optional["SubAgentOrchestrator"] = None):
        """初始化上下文守卫。

        Args:
            orchestrator: SubAgentOrchestrator 实例。如果为 None，delegate 策略不可用，
                          会降级为 snip。
        """
        self._orchestrator = orchestrator

    # ------------------------------------------------------------------ #
    # 公开 API
    # ------------------------------------------------------------------ #

    def decide(self, tool_name: str) -> str:
        """根据工具名判断执行策略。

        Returns:
            "inline" | "snip" | "delegate"
        """
        # 副作用工具不能委托
        if tool_name in self.NO_DELEGATE_TOOLS:
            estimate = self.TOOL_ESTIMATES.get(tool_name, 2000)
            return "snip" if estimate > self.SMALL_THRESHOLD else "inline"

        estimate = self.TOOL_ESTIMATES.get(tool_name, 2000)

        if estimate < self.SMALL_THRESHOLD:
            return "inline"
        elif estimate < self.LARGE_THRESHOLD:
            return "snip"
        else:
            # 如果 orchestrator 不可用，降级为 snip
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

        # 构造子代理的自然语言任务描述
        task_desc = self._format_task_description(tool_name, arguments)

        task = SubAgentTask(
            description=task_desc,
            tools=[tool_name],              # 子代理只能用这一个工具
            result_mode=SubAgentResultMode.SUMMARY,
            max_iterations=3,               # 子代理少迭代（通常 1 次工具调用就够）
            timeout_seconds=30,
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
        """将 LLM 工具调用转为子代理可理解的自然语言任务描述。

        例如：read_file(path="src/main.py", limit=100)
        → "使用 read_file 工具读取文件 'src/main.py'（限制 100 行），"
          "只输出文件内容，不要解释。"
        """
        # 针对常见工具做语义化描述
        formatters = {
            "read_file": lambda args: (
                f"使用 read_file 工具读取文件 '{args.get('path', 'unknown')}'"
                + (f"（限制 {args.get('limit', 'all')} 行）" if args.get('limit') else "")
                + "。只输出文件的完整内容，不要做任何解释或总结。"
            ),
            "web_search": lambda args: (
                f"使用 web_search 工具搜索关键词 '{args.get('query', 'unknown')}'"
                + (f"（返回 {args.get('count', 5)} 条结果）" if args.get('count') else "")
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
            "rag_ask": lambda args: (
                f"使用 rag_ask 工具查询知识库 '{args.get('query', 'unknown')}'"
                + "。只输出检索结果，不要做任何解释或总结。"
            ),
        }

        formatter = formatters.get(tool_name)
        if formatter:
            return formatter(arguments)

        # 通用兜底：JSON 化参数
        return (
            f"使用 {tool_name} 工具，参数为：{json.dumps(arguments, ensure_ascii=False)}。"
            f"只输出工具返回的结果，不要做任何解释或总结。"
        )
