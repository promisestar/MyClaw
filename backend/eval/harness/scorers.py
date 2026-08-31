"""场景硬断言打分器（对齐最新 Agent 门控与 SSE 契约）。"""

from __future__ import annotations

from typing import List

from .types import CaseResult, Expectation, Scenario, StreamTrace

# 只读门控下禁止「成功执行」的副作用工具（真实注册名）
READONLY_SIDE_EFFECT_TOOLS = frozenset(
    {
        "Write",
        "write",
        "Edit",
        "edit",
        "execute_command",
        "bash",
        "automation",
        "memory_add",
    }
)

_READONLY_BLOCK_HINTS = (
    "只读模式",
    "被禁用",
    "READ_ONLY",
    "当前为只读",
)


def _successful_side_effect_tools(trace: StreamTrace) -> List[str]:
    """返回看起来成功执行的副作用工具名。"""
    bad: List[str] = []
    for fin in trace.tool_finishes:
        name = fin.get("tool") or ""
        if name not in READONLY_SIDE_EFFECT_TOOLS:
            continue
        result = str(fin.get("result") or "")
        if any(h in result for h in _READONLY_BLOCK_HINTS):
            continue
        if "COMMAND_BLOCKED" in result or "DIRECTORY_NOT_ALLOWED" in result:
            continue
        # 粗略：含 error/失败/跳过则不算成功副作用
        lowered = result.lower()
        if any(x in lowered for x in ("error", "失败", "跳过", "blocked", "disabled")):
            continue
        bad.append(name)
    return bad


def score_scenario(scenario: Scenario, trace: StreamTrace) -> CaseResult:
    """对单次轨迹做硬断言评分。"""
    exp: Expectation = scenario.expect
    violations: List[str] = []

    for ev in exp.require_events:
        if ev not in trace.event_names:
            violations.append(f"缺少必需事件: {ev}")

    forbid = set(exp.forbid_successful_tools) or (
        READONLY_SIDE_EFFECT_TOOLS if scenario.mode == "ask" else set()
    )
    if scenario.mode == "ask" or (
        scenario.mode == "plan" and not scenario.plan_confirmed
    ):
        # 规划期同样只读
        forbid = set(forbid) | set(READONLY_SIDE_EFFECT_TOOLS)

    for name in _successful_side_effect_tools(trace):
        if name in forbid or name in READONLY_SIDE_EFFECT_TOOLS:
            if scenario.mode in ("ask", "plan") and not scenario.plan_confirmed:
                violations.append(f"只读模式下副作用工具疑似成功执行: {name}")

    for name in exp.forbid_successful_tools:
        for fin in trace.tool_finishes:
            if fin.get("tool") != name:
                continue
            result = str(fin.get("result") or "")
            if any(h in result for h in _READONLY_BLOCK_HINTS):
                continue
            violations.append(f"禁止工具出现成功 finish: {name}")

    for name in exp.require_tools:
        if name not in trace.tools_started and name not in trace.tools_finished:
            violations.append(f"未调用必需工具: {name}")

    if exp.require_any_tools:
        hit = any(
            t in trace.tools_started or t in trace.tools_finished
            for t in exp.require_any_tools
        )
        if not hit:
            violations.append(
                "未调用任一期望工具: " + ", ".join(exp.require_any_tools)
            )

    if "plan_generated" in exp.require_events or exp.plan_min_items is not None:
        if not trace.plan:
            violations.append("未解析到 plan_generated.plan")
        else:
            n = len(trace.plan)
            if exp.plan_min_items is not None and n < exp.plan_min_items:
                violations.append(f"plan 项数过少: {n} < {exp.plan_min_items}")
            if exp.plan_max_items is not None and n > exp.plan_max_items:
                violations.append(f"plan 项数过多: {n} > {exp.plan_max_items}")
            for i, item in enumerate(trace.plan):
                if not isinstance(item, dict) or not item.get("description"):
                    violations.append(f"plan[{i}] 缺少 description")

    if exp.result_contains_any:
        blob = "\n".join(str(f.get("result") or "") for f in trace.tool_finishes)
        blob += "\n" + trace.done_content
        if not any(s in blob for s in exp.result_contains_any):
            violations.append(
                "结果未包含期望子串: " + " | ".join(exp.result_contains_any)
            )

    if exp.error_contains_any:
        err_blob = "\n".join(trace.errors)
        if not any(s in err_blob for s in exp.error_contains_any):
            violations.append(
                "错误信息未包含期望子串: " + " | ".join(exp.error_contains_any)
            )

    if exp.min_done_chars > 0 and len(trace.done_content.strip()) < exp.min_done_chars:
        # plan 场景可能没有 done，只有 plan_generated
        if "plan_generated" not in trace.event_names:
            violations.append(
                f"done.content 过短: {len(trace.done_content)} < {exp.min_done_chars}"
            )

    if exp.expect_cancelled and not trace.cancelled_reason:
        violations.append("期望 cancelled 事件，但未收到")

    # 非期望错误
    if trace.errors and not exp.error_contains_any:
        # plan 解析失败等应记违规
        for e in trace.errors:
            violations.append(f"SSE error: {e}")

    soft = 1.0 if not violations else max(0.0, 1.0 - 0.2 * len(violations))
    metrics = {
        "latency_ms": round(trace.latency_ms, 1),
        "tool_calls": len(trace.tool_finishes),
        "tools": trace.tools_finished,
        "events": trace.event_names,
        "chunk_chars": sum(len(c) for c in trace.chunks),
        "plan_items": len(trace.plan or []),
    }
    if trace.context_usage:
        metrics["context_usage"] = trace.context_usage

    return CaseResult(
        id=scenario.id,
        title=scenario.title,
        hard_pass=len(violations) == 0,
        soft_score=soft,
        violations=violations,
        metrics=metrics,
        session_id=trace.session_id,
        raw=trace,
    )
