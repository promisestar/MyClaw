"""工具名别名与 scorer 匹配单测。"""

from evals.agent.harness.scorers import score_scenario
from evals.agent.harness.tool_aliases import (
    expand_tool_assertion,
    is_mcp_related_tool,
    trace_has_tool,
)
from evals.agent.harness.types import Expectation, Scenario, StreamTrace


def test_web_search_aliases():
    assert trace_has_tool(["search_web"], "web_search")
    assert trace_has_tool(["web_search"], "web_search")
    assert not trace_has_tool(["Read"], "web_search")


def test_web_fetch_aliases():
    assert trace_has_tool(["fetch_url"], "web_fetch")


def test_mcp_gateway_and_prefixed_tools():
    assert is_mcp_related_tool("github")
    assert is_mcp_related_tool("mcp_github_search_repositories")
    assert not is_mcp_related_tool("execute_command")


def test_memory_action_aliases():
    assert "memory_search" in expand_tool_assertion("memory")
    assert trace_has_tool(["memory_search"], "memory")


def test_scorer_accepts_search_web_for_web_search_requirement():
    scenario = Scenario(
        id="net_smoke",
        title="net",
        message="x",
        expect=Expectation(require_any_tools=["web_search"]),
    )
    trace = StreamTrace(
        tool_starts=[{"tool": "search_web"}],
        tool_finishes=[{"tool": "search_web", "result": "ok"}],
        done_content="summary " * 10,
    )
    result = score_scenario(scenario, trace)
    assert result.hard_pass is True


def test_scorer_accepts_github_for_mcp_requirement():
    scenario = Scenario(
        id="mcp_smoke",
        title="mcp",
        message="x",
        expect=Expectation(require_any_tools=["mcp"], min_done_chars=5),
    )
    trace = StreamTrace(
        tool_starts=[{"tool": "github"}],
        tool_finishes=[{"tool": "github", "result": "servers listed"}],
        done_content="MCP servers: github",
    )
    result = score_scenario(scenario, trace)
    assert result.hard_pass is True
