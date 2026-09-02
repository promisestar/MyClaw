"""场景断言工具名 ↔ SSE 实际上报名（expandable / MCP 网关）。"""

from __future__ import annotations

from typing import AbstractSet, FrozenSet, Iterable, Set

# 场景 YAML 常用「父工具名」→ SSE tool_start/tool_finish 可能出现的名称
_TOOL_ALIASES: dict[str, FrozenSet[str]] = {
    "web_search": frozenset({"web_search", "search_web"}),
    "search_web": frozenset({"web_search", "search_web"}),
    "web_fetch": frozenset({"web_fetch", "fetch_url"}),
    "fetch_url": frozenset({"web_fetch", "fetch_url"}),
    "memory": frozenset({"memory", "memory_search", "memory_add"}),
    "memory_search": frozenset({"memory", "memory_search"}),
    "memory_add": frozenset({"memory", "memory_add"}),
    "rag": frozenset(
        {
            "rag",
            "rag_add_document",
            "rag_add_text",
            "rag_search",
            "rag_ask",
            "rag_stats",
        }
    ),
    "python_calculator": frozenset({"python_calculator", "calculator"}),
    "calculator": frozenset({"python_calculator", "calculator"}),
    "bash": frozenset({"bash", "execute_command"}),
    "execute_command": frozenset({"bash", "execute_command"}),
    "Write": frozenset({"Write", "write"}),
    "write": frozenset({"Write", "write"}),
    "Edit": frozenset({"Edit", "edit"}),
    "edit": frozenset({"Edit", "edit"}),
    "Read": frozenset({"Read", "read"}),
    "read": frozenset({"Read", "read"}),
    "Skill": frozenset({"Skill", "skill"}),
    "skill": frozenset({"Skill", "skill"}),
}

# MCP 网关以 config.json mcp.servers[].name 注册（如 github），子工具为 mcp_{name}_*
_MCP_GATEWAY_NAMES = frozenset(
    {
        "mcp",
        "github",
        "slack",
        "filesystem",
        "postgres",
        "sqlite",
        "google-drive",
        "google_drive",
    }
)


def expand_tool_assertion(name: str) -> FrozenSet[str]:
    """场景里写的工具名 → 可匹配的 SSE 工具名集合。"""
    if name == "mcp":
        return frozenset({"mcp"})
    return _TOOL_ALIASES.get(name, frozenset({name}))


def is_mcp_related_tool(name: str) -> bool:
    """是否算作 MCP 能力调用（网关或 mcp_* 子工具）。"""
    if not name:
        return False
    if name == "mcp" or name.startswith("mcp_"):
        return True
    return name in _MCP_GATEWAY_NAMES


def tool_assertion_matches(expected: str, observed: str) -> bool:
    """单个 observed 工具名是否满足对 expected 的断言。"""
    if expected == "mcp":
        return is_mcp_related_tool(observed)
    return observed in expand_tool_assertion(expected)


def trace_has_tool(observed_tools: Iterable[str], expected: str) -> bool:
    """轨迹中是否出现过满足 expected 的工具。"""
    observed = set(observed_tools)
    if expected == "mcp":
        return any(is_mcp_related_tool(t) for t in observed)
    aliases = expand_tool_assertion(expected)
    return bool(observed & aliases)


def expand_forbid_set(names: AbstractSet[str]) -> Set[str]:
    """forbid_successful_tools 展开为 SSE 可能出现的全部名称。"""
    out: Set[str] = set()
    for name in names:
        if name == "mcp":
            out.add("mcp")
            out.update(_MCP_GATEWAY_NAMES)
            continue
        out.update(expand_tool_assertion(str(name)))
    return out
