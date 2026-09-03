"""工具模式过滤器 — 根据 Agent 模式过滤可用工具集。

包装外部 ToolRegistry，在 Ask 模式和 Plan 规划期屏蔽副作用工具
（write/edit/bash/automation），仅暴露只读工具；Craft 模式和 Plan
执行期暴露全部工具。

设计参考 Claude Code v2 的工具门控（Tool Gate）机制：
通过代码层面强制两阶段分离，而非仅依赖 prompt 软引导。
"""

from __future__ import annotations

from enum import Enum
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from hello_agents.tools import Tool
    from hello_agents.tools.registry import ToolRegistry


# ============================================================================
# 模式定义
# ============================================================================


class ToolMode(Enum):
    """工具访问模式。

    - READ_ONLY: Ask 模式 / Plan 规划期 — 仅只读工具可用
    - FULL: Craft 模式 / Plan 执行期 — 全部工具可用
    """

    READ_ONLY = "read_only"
    FULL = "full"


# 副作用工具集合 — READ_ONLY 模式下被屏蔽
# 与设计文档 §1.2 一致：write/edit/execute_command/automation
# 注意：bash 工具的注册名为 "bash"（见 myclaw_agent.py:670）
SIDE_EFFECT_TOOLS: frozenset[str] = frozenset({
    "write",
    "Write",
    "edit",
    "Edit",
    "bash",
    "execute_command",
    "automation",
    "memory_add",
})

# 提示词展示用的规范名（与 SIDE_EFFECT_TOOLS 对齐，去重大小写变体）
SIDE_EFFECT_TOOL_LABELS: tuple[str, ...] = (
    "Write",
    "Edit",
    "execute_command",
    "bash",
    "automation",
    "memory_add",
)


def readonly_block_message(tool_name: str) -> str:
    """READ_ONLY 下拒绝副作用工具时的统一文案（须含「只读模式」「被禁用」供评测识别）。"""
    return (
        f"⚠️ 当前为只读模式，工具 '{tool_name}' 被禁用。"
        f"请切换到 Craft 或 Plan 模式执行修改操作。"
    )


# ============================================================================
# 工具模式过滤器
# ============================================================================


class ToolModeFilter:
    """工具模式过滤器 — 包装 ToolRegistry，根据模式过滤可用工具。

    Usage::

        filter = ToolModeFilter(tool_registry)
        filter.set_mode(ToolMode.READ_ONLY)  # Ask / Plan 规划期

        # 获取过滤后的工具名列表（用于暴露给 LLM）
        names = filter.get_available_tool_names()

        # 获取过滤后的工具 schema（用于 LLM function calling）
        schemas = filter.get_filtered_schemas()

        # 按名获取工具（执行时用）
        tool = filter.get_tool("read")  # 只读工具可用
        tool = filter.get_tool("write")  # READ_ONLY 模式下返回 None
    """

    def __init__(self, tool_registry: "ToolRegistry"):
        """初始化过滤器。

        Args:
            tool_registry: 原始 ToolRegistry 实例
        """
        self._registry = tool_registry
        self._mode: ToolMode = ToolMode.FULL

    # ------------------------------------------------------------------ #
    # 模式控制
    # ------------------------------------------------------------------ #

    def set_mode(self, mode: ToolMode) -> None:
        """设置当前工具模式。

        Args:
            mode: ToolMode.READ_ONLY（只读）或 ToolMode.FULL（全部）
        """
        self._mode = mode

    @property
    def mode(self) -> ToolMode:
        """当前工具模式。"""
        return self._mode

    # ------------------------------------------------------------------ #
    # 过滤查询
    # ------------------------------------------------------------------ #

    def get_available_tool_names(self) -> List[str]:
        """获取当前模式下可用的工具名列表。

        READ_ONLY 模式下过滤掉 SIDE_EFFECT_TOOLS 中的工具。
        """
        all_names = self._registry.list_tools()
        if self._mode == ToolMode.FULL:
            return all_names
        return [name for name in all_names if name not in SIDE_EFFECT_TOOLS]

    def get_available_tools(self) -> List["Tool"]:
        """获取当前模式下可用的工具实例列表。"""
        names = self.get_available_tool_names()
        tools: List[Tool] = []
        for name in names:
            tool = self._registry.get_tool(name)
            if tool is not None:
                tools.append(tool)
        return tools

    def get_filtered_schemas(self) -> List[Dict[str, Any]]:
        """获取当前模式下可用工具的 schema 列表（用于 LLM function calling）。

        schema 格式为 OpenAI function calling 标准（Tool.to_openai_schema()）。
        READ_ONLY 下额外按 function.name 过滤，防止 expandable 展开名漏网。
        """
        tools = self.get_available_tools()
        schemas: List[Dict[str, Any]] = []
        for tool in tools:
            if not hasattr(tool, "to_openai_schema"):
                continue
            schema = tool.to_openai_schema()
            if self._mode == ToolMode.READ_ONLY:
                fn = (schema.get("function") or {}) if isinstance(schema, dict) else {}
                name = fn.get("name") or getattr(tool, "name", "")
                if name in SIDE_EFFECT_TOOLS:
                    continue
            schemas.append(schema)
        return schemas

    def get_tool(self, name: str) -> Optional["Tool"]:
        """按名获取工具。

        READ_ONLY 模式下，副作用工具返回 None（即 LLM 即使生成了
        被禁用工具的调用，也无法获取到工具实例）。

        Args:
            name: 工具名

        Returns:
            工具实例，不存在或被屏蔽时返回 None
        """
        if self._mode == ToolMode.READ_ONLY and name in SIDE_EFFECT_TOOLS:
            return None
        return self._registry.get_tool(name)

    def is_tool_available(self, name: str) -> bool:
        """检查指定工具在当前模式下是否可用。

        Args:
            name: 工具名

        Returns:
            是否可用
        """
        if self._mode == ToolMode.READ_ONLY and name in SIDE_EFFECT_TOOLS:
            return False
        return self._registry.get_tool(name) is not None
