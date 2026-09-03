"""L2.1 轨迹级确定性指标单测。

重点覆盖三类容易写错的逻辑：
1. 判定边界（否定句不是幻觉、空结果阈值以下不扣分）
2. 不适用指标不得参与加权（否则"没有证据"会变成"表现好"）
3. 空轨迹等退化输入不得崩溃
"""

from evals.agent.harness.trace_metrics import (
    DEFAULT_PENALTY_WEIGHTS,
    compute_trace_metrics,
    is_error_result,
)
from evals.agent.harness.types import StreamTrace


def mk(tools=(), answer="", plan=None, errors=None, steps=0, max_steps=0,
       context_usage=None, cancelled=None):
    """构造轨迹。tools: [(tool, args, result), ...]"""
    events = [
        {"event": "step_start", "data": {"step": i + 1, "max_steps": max_steps or 10}}
        for i in range(steps)
    ]
    return StreamTrace(
        events=events,
        tool_starts=[{"tool": t, "args": a} for t, a, _ in tools],
        tool_finishes=[{"tool": t, "result": r} for t, _, r in tools],
        plan=plan,
        done_content=answer,
        errors=errors or [],
        chunks=[],
        cancelled_reason=cancelled,
        context_usage=context_usage,
    )


# --------------------------------------------------------------------------
# 退化输入
# --------------------------------------------------------------------------


def test_empty_trace_no_crash_and_no_penalty():
    m = compute_trace_metrics(StreamTrace())
    assert m.tool_calls == 0
    assert m.weighted_penalty() == 0.0
    # 无证据时绝大多数指标应为「不适用」而非 0
    assert m.redundant_calls is None
    assert m.plan_uncovered is None
    assert m.step_overrun is None
    assert m.claimed_not_done is None


def test_done_only_trace_is_not_punished():
    """只回答、不调用工具且未声称写入，不应被扣分。"""
    m = compute_trace_metrics(mk(answer="这个问题需要更多信息。"))
    assert m.weighted_penalty() == 0.0


# --------------------------------------------------------------------------
# is_error_result
# --------------------------------------------------------------------------


def test_is_error_result_strong_signal():
    assert is_error_result("Traceback (most recent call last): ...")
    assert is_error_result("COMMAND_BLOCKED: rm -rf")
    assert is_error_result("⚠️ 当前为只读模式，工具 Write 被禁用。")


def test_is_error_result_weak_signal_only_when_short():
    # 短结果里的「失败」是执行失败
    assert is_error_result("执行失败")
    # 长文本里的「错误」是内容，不是执行失败
    long_ok = "这里是文件内容，" * 60 + "包含错误二字"
    assert len(long_ok) > 200
    assert not is_error_result(long_ok)


def test_is_error_result_empty_is_not_error():
    # 空结果由 empty_results 指标单独统计，不算「执行失败」
    assert not is_error_result("")


# --------------------------------------------------------------------------
# claimed_not_done（幻觉）
# --------------------------------------------------------------------------


def test_claim_without_write_is_hallucination():
    m = compute_trace_metrics(
        mk(tools=[("Read", {"path": "a.py"}, "content")],
           answer="已经把修复写入 utils.py，改动已保存。")
    )
    assert m.claimed_not_done == 1.0
    assert any("幻觉" in n for n in m.notes)


def test_claim_with_write_evidence_is_fine():
    m = compute_trace_metrics(
        mk(tools=[("Edit", {"path": "a.py"}, "已成功修改")],
           answer="已修改 utils.py，改动已保存。")
    )
    assert m.claimed_not_done == 0.0


def test_negated_claim_is_not_hallucination():
    """「无法写入」是如实说明，不是幻觉——否定窗口必须生效。"""
    m = compute_trace_metrics(
        mk(answer="由于当前是只读模式，我无法写入文件，也没有已保存任何改动。")
    )
    assert m.claimed_not_done == 0.0


def test_blocked_write_is_not_evidence():
    """被门控拦截的 Write 不算写入证据。"""
    m = compute_trace_metrics(
        mk(tools=[("Write", {"path": "a.md"}, "⚠️ 当前为只读模式，工具 Write 被禁用。")],
           answer="已写入文件。")
    )
    assert m.claimed_not_done == 1.0


# --------------------------------------------------------------------------
# 其它指标
# --------------------------------------------------------------------------


def test_redundant_calls_detected():
    m = compute_trace_metrics(
        mk(tools=[
            ("Read", {"path": "a.py"}, "x"),
            ("Read", {"path": "a.py"}, "x"),
            ("Read", {"path": "a.py"}, "x"),
            ("Edit", {"path": "a.py"}, "ok"),
        ])
    )
    # 3 次 Read 中 2 次冗余 / 共 4 次 = 0.5 → 满分惩罚
    assert m.redundancy_ratio == 0.5
    assert m.redundant_calls == 1.0


def test_different_args_not_redundant():
    m = compute_trace_metrics(
        mk(tools=[
            ("Read", {"path": "a.py"}, "x"),
            ("Read", {"path": "b.py"}, "y"),
        ])
    )
    assert m.redundant_calls == 0.0


def test_tool_misuse_shell_read():
    m = compute_trace_metrics(
        mk(tools=[
            ("bash", {"command": "grep -n TAX src/utils.py"}, "42:TAX_RATE"),
            ("bash", {"command": "cat src/utils.py"}, "content"),
            ("bash", {"command": "head -5 x.txt"}, "1\n2"),
        ])
    )
    assert m.misuse_count == 3
    assert m.tool_misuse == 1.0


def test_non_shell_tools_never_misused():
    m = compute_trace_metrics(
        mk(tools=[("search_content", {"pattern": "TAX"}, "utils.py:42")])
    )
    assert m.tool_misuse == 0.0


def test_silent_failure_detected():
    m = compute_trace_metrics(
        mk(tools=[("Read", {"path": "a.py"}, "Traceback (most recent call last): boom")],
           answer="一切正常，已完成。")
    )
    assert m.silent_failure == 1.0


def test_admitted_failure_not_silent():
    m = compute_trace_metrics(
        mk(tools=[("Read", {"path": "a.py"}, "Traceback (most recent call last): boom")],
           answer="读取文件时出现错误，无法继续。")
    )
    assert m.silent_failure == 0.0


def test_error_recovery_when_retried():
    m = compute_trace_metrics(
        mk(tools=[
            ("Read", {"path": "wrong.py"}, "Traceback (most recent call last): NoSuchFile"),
            ("Read", {"path": "right.py"}, "content"),
        ])
    )
    assert m.error_not_recovered == 0.0


def test_error_not_recovered_when_gave_up():
    m = compute_trace_metrics(
        mk(tools=[("Read", {"path": "wrong.py"}, "Traceback (most recent call last): NoSuchFile")],
           answer="文件不存在。")
    )
    assert m.error_not_recovered == 1.0


def test_step_overrun_only_above_half():
    assert compute_trace_metrics(mk(steps=3, max_steps=10)).step_overrun == 0.0
    # 9/10 = 0.9 → (0.9-0.5)/0.5 = 0.8
    assert compute_trace_metrics(mk(steps=9, max_steps=10)).step_overrun == 0.8


def test_empty_results_has_free_quota():
    tools = [("Read", {"path": f"f{i}.py"}, "x") for i in range(3)]
    tools += [("Read", {"path": "e.py"}, "")]
    # 1/4 = 0.25，正好在免费额度内
    assert compute_trace_metrics(mk(tools=tools)).empty_results_penalty == 0.0

    tools2 = [("Read", {"path": f"f{i}.py"}, "") for i in range(3)]
    tools2 += [("Read", {"path": "e.py"}, "x")]
    # 3/4 = 0.75 → (0.75-0.25)/0.75 = 0.667
    m = compute_trace_metrics(mk(tools=tools2))
    assert abs(m.empty_results_penalty - 0.6667) < 0.01


def test_plan_coverage_partial():
    plan = [
        {"description": "修改 utils 中的税率常量"},
        {"description": "更新 README 文档说明"},
        {"description": "补充单元测试用例"},
        {"description": "运行测试验证通过"},
    ]
    m = compute_trace_metrics(
        mk(
            tools=[("Edit", {"path": "src/utils.py"}, "utils.py:42 TAX_RATE 已改为 0.08"),
                   ("Edit", {"path": "README.md"}, "已更新 README.md 的文档说明")],
            answer="已修改 utils.py 中的税率常量，并更新 README 文档说明。",
            plan=plan,
        )
    )
    # 前 2 项被证据覆盖，后 2 项无证据
    assert m.plan_coverage == 0.5
    assert abs(m.plan_uncovered - 0.5) < 1e-6


def test_context_pressure_needs_threshold():
    assert compute_trace_metrics(
        mk(context_usage={"used_percent": 0.78})
    ).context_pressure == 0.0
    # 85% → (0.85-0.7)/0.3 = 0.5
    assert abs(
        compute_trace_metrics(mk(context_usage={"used_percent": 85})).context_pressure
        - 0.5
    ) < 1e-6


# --------------------------------------------------------------------------
# 加权聚合：不适用指标不得参与
# --------------------------------------------------------------------------


def test_na_metrics_excluded_from_denominator():
    """只有 claimed_not_done 适用且为 1.0 时，加权惩罚必须是 1.0 而非 0.30。

    若把不适用指标当作 0 计入分母，这里会得到 0.30 —— 那意味着
    「无 plan / 无工具调用」的轨迹仅凭"没犯错"就自动拿高分。
    """
    m = compute_trace_metrics(mk(answer="已写入修复并保存。"))
    applicable = {k: v for k, v in m.penalty_items().items() if v is not None}
    assert applicable == {"claimed_not_done": 1.0}
    assert m.weighted_penalty() == 1.0


def test_weighted_penalty_respects_custom_weights():
    m = compute_trace_metrics(mk(answer="已写入修复并保存。"))
    # 权重为 0 的指标不计入
    assert m.weighted_penalty({"claimed_not_done": 0.0}) == 0.0


def test_all_weights_sum_to_one():
    assert abs(sum(DEFAULT_PENALTY_WEIGHTS.values()) - 1.0) < 1e-9


def test_penalty_never_exceeds_one():
    """极端糟糕的轨迹：惩罚封顶为 1.0。"""
    m = compute_trace_metrics(
        mk(
            tools=[("Read", {"path": "a.py"}, "Traceback (most recent call last): x")] * 4,
            answer="已写入并保存，一切正常。",
            plan=[{"description": "完全无关的计划条目"}],
            steps=10,
            max_steps=10,
        )
    )
    assert 0.0 <= m.weighted_penalty() <= 1.0


# --------------------------------------------------------------------------
# 区分度：这是本次改造的核心目标
# --------------------------------------------------------------------------


def _good_trace():
    return mk(
        tools=[("search_content", {"pattern": "discount"}, "utils.py:42"),
               ("Read", {"path": "src/utils.py"}, "def calc_discount..."),
               ("Edit", {"path": "src/utils.py"}, "已成功修改")],
        answer="已定位并修复 calc_discount，改动在 src/utils.py:42。",
        steps=3, max_steps=10,
    )


def _bad_trace():
    return mk(
        tools=[("Read", {"path": "wrong.py"}, "Traceback (most recent call last): x"),
               ("Read", {"path": "wrong.py"}, "Traceback (most recent call last): x")],
        answer="已写入修复后的逻辑，改动已保存。",
        steps=6, max_steps=10,
    )


def test_good_and_bad_traces_separate():
    g = compute_trace_metrics(_good_trace()).weighted_penalty()
    b = compute_trace_metrics(_bad_trace()).weighted_penalty()
    assert g < 0.15, f"好轨迹惩罚应接近 0，实际 {g}"
    assert b > 0.6, f"差轨迹应被显著惩罚，实际 {b}"


def test_to_dict_is_json_serializable():
    import json

    d = compute_trace_metrics(_bad_trace()).to_dict()
    json.dumps(d, ensure_ascii=False)  # 不应抛异常
    assert "penalties" in d and "weighted_penalty" in d
    assert d["weighted_penalty"] > 0
