"""L2.2 LLM-as-judge 单测（不依赖真实 LLM 的部分）。

重点覆盖：
1. build_digest 的脱敏与压缩（不含 violations/hard_pass/expect）
2. _parse 对各类畸形 LLM 输出的健壮性（绝不让评测崩溃）
3. summarize_judge 的汇总口径
4. TraceJudge 未配置时的优雅降级
"""

import json

import pytest

from evals.agent.harness.judge import (
    JUDGE_DIMENSIONS,
    JudgeConfig,
    JudgeResult,
    TraceDigest,
    TraceJudge,
    build_digest,
    summarize_judge,
)
from evals.agent.harness.types import Expectation, Scenario, StreamTrace


def _scenario():
    return Scenario(
        id="craft_search_then_edit",
        title="搜索后定点修改",
        message="找到折扣计算的 bug 并修复",
        mode="craft",
        expect=Expectation(require_tools=["search_content", "Edit"]),
    )


def _trace():
    return StreamTrace(
        events=[
            {"event": "step_start", "data": {"step": 1, "max_steps": 10}},
            {"event": "step_start", "data": {"step": 2, "max_steps": 10}},
        ],
        tool_starts=[
            {"tool": "search_content", "args": {"pattern": "discount"}},
            {"tool": "Edit", "args": {"path": "src/utils.py", "old": "x", "new": "y"}},
        ],
        tool_finishes=[
            {"tool": "search_content", "result": "utils.py:42"},
            {"tool": "Edit", "result": "已成功修改"},
        ],
        done_content="已定位并修复 utils.py:42 的折扣 bug。",
        errors=[],
        latency_ms=1234.5,
    )


# --------------------------------------------------------------------------
# build_digest
# --------------------------------------------------------------------------


def test_digest_contains_facts_only():
    d = build_digest(_scenario(), _trace())
    assert d.scenario_id == "craft_search_then_edit"
    assert d.user_request == "找到折扣计算的 bug 并修复"
    assert len(d.tool_calls) == 2
    assert d.steps_used == 2 and d.max_steps == 10
    assert d.final_answer == "已定位并修复 utils.py:42 的折扣 bug。"


def test_digest_never_leaks_assertion_result():
    """digest 必须不含 violations / hard_pass / expect 断言内容。"""
    d = build_digest(_scenario(), _trace())
    rendered = d.render()
    assert "hard_pass" not in rendered
    assert "violation" not in rendered
    assert "require_tools" not in rendered
    # expect 里的工具名不应作为评判依据泄漏
    assert "search_content" not in rendered.split("用户请求")[0]


def test_digest_truncates_long_result():
    long = "x" * 5000
    tr = StreamTrace(
        tool_starts=[{"tool": "Read", "args": {"path": "a.py"}}],
        tool_finishes=[{"tool": "Read", "result": long}],
        done_content="ok",
    )
    d = build_digest(_scenario(), tr)
    assert len(d.tool_calls[0].result) < 1500


def test_digest_marks_error_calls():
    tr = StreamTrace(
        tool_starts=[{"tool": "Read", "args": {"path": "a.py"}}],
        tool_finishes=[{"tool": "Read", "result": "Traceback (most recent call last): x"}],
    )
    d = build_digest(_scenario(), tr)
    assert d.tool_calls[0].is_error is True


# --------------------------------------------------------------------------
# TraceJudge 降级
# --------------------------------------------------------------------------


def test_judge_unavailable_without_config(monkeypatch):
    """无模型配置时，judge 应 unavailable 且 judge() 返回 ok=False，不抛异常。"""
    monkeypatch.delenv("EVAL_JUDGE_MODEL_ID", raising=False)
    monkeypatch.delenv("LLM_MODEL_ID", raising=False)
    monkeypatch.delenv("EVAL_JUDGE_API_KEY", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    j = TraceJudge()
    assert not j.available
    r = j.judge(build_digest(_scenario(), _trace()))
    assert r.ok is False
    assert r.error is not None


def test_judge_parse_returns_ok_for_valid_json():
    j = TraceJudge.__new__(TraceJudge)  # 绕过 __init__，不初始化 LLM
    j.model = "test-model"
    raw = json.dumps(
        {
            "scores": {d: {"score": 4, "reason": "还行"} for d in JUDGE_DIMENSIONS},
            "summary": "总体不错",
            "red_flags": [],
        },
        ensure_ascii=False,
    )
    r = j._parse(raw, JudgeConfig(enabled=True))
    assert r.ok is True
    assert r.judge_score is not None
    assert 1.0 <= r.judge_score <= 5.0


def test_judge_parse_handles_markdown_fence():
    j = TraceJudge.__new__(TraceJudge)
    j.model = "test"
    raw = '```json\n{"scores": {"task_completion": {"score": 5, "reason": "ok"}}, "summary": "好", "red_flags": []}\n```'
    r = j._parse(raw, JudgeConfig(enabled=True))
    assert r.ok is True
    assert r.judge_score is not None


def test_judge_parse_recovers_json_with_junk_prefix():
    j = TraceJudge.__new__(TraceJudge)
    j.model = "test"
    raw = '这是结果：{"scores": {"task_completion": {"score": 3, "reason": "部分"}}, "summary": "x", "red_flags": []} 结束'
    r = j._parse(raw, JudgeConfig(enabled=True))
    assert r.ok is True


def test_judge_parse_invalid_json_degrades_gracefully():
    j = TraceJudge.__new__(TraceJudge)
    j.model = "test"
    r = j._parse("这不是 JSON", JudgeConfig(enabled=True))
    assert r.ok is False
    assert "解析失败" in r.error


def test_judge_parse_no_scores_degrades():
    j = TraceJudge.__new__(TraceJudge)
    j.model = "test"
    r = j._parse('{"summary": "只有总结"}', JudgeConfig(enabled=True))
    assert r.ok is False


def test_judge_parse_clamps_out_of_range():
    j = TraceJudge.__new__(TraceJudge)
    j.model = "test"
    raw = json.dumps({"scores": {"task_completion": {"score": 99, "reason": "x"}}, "summary": "y", "red_flags": []})
    r = j._parse(raw, JudgeConfig(enabled=True))
    assert r.ok and r.scores["task_completion"]["score"] == 5.0


def test_judge_parse_ignores_unknown_dimensions():
    j = TraceJudge.__new__(TraceJudge)
    j.model = "test"
    raw = json.dumps(
        {
            "scores": {
                "task_completion": {"score": 4, "reason": "x"},
                "hallucinated_dim": {"score": 5, "reason": "不该存在"},
            },
            "summary": "y",
            "red_flags": [],
        }
    )
    r = j._parse(raw, JudgeConfig(enabled=True))
    assert "hallucinated_dim" not in r.scores


def test_judge_parse_collects_red_flags():
    j = TraceJudge.__new__(TraceJudge)
    j.model = "test"
    raw = json.dumps(
        {
            "scores": {"task_completion": {"score": 2, "reason": "x"}},
            "summary": "y",
            "red_flags": ["声称写入但轨迹无写入", ""],
        }
    )
    r = j._parse(raw, JudgeConfig(enabled=True))
    assert r.red_flags == ["声称写入但轨迹无写入"]


# --------------------------------------------------------------------------
# summarize_judge
# --------------------------------------------------------------------------


def test_summarize_judge_aggregates():
    results = [
        JudgeResult(ok=True, model="m", judge_score=4.0, scores={}, red_flags=[]),
        JudgeResult(ok=True, model="m", judge_score=2.0, scores={}, red_flags=["a"]),
        JudgeResult(ok=False, model="m", error="boom"),
    ]
    s = summarize_judge([r.to_dict() for r in results])
    assert s["scored"] == 2
    assert s["skipped"] == 1
    assert s["avg_judge_score"] == 3.0
    assert s["min_judge_score"] == 2.0
    assert s["max_judge_score"] == 4.0
    assert s["red_flags_count"] == 1
    assert s["errors"] == ["boom"]


def test_summarize_judge_empty():
    s = summarize_judge([])
    assert s["scored"] == 0
    assert s["avg_judge_score"] is None


# --------------------------------------------------------------------------
# digest 渲染（供 prompt 使用）
# --------------------------------------------------------------------------


def test_render_includes_user_request_and_trace():
    d = build_digest(_scenario(), _trace())
    rendered = d.render()
    assert "找到折扣计算的 bug 并修复" in rendered
    assert "search_content" in rendered
    assert "已成功修改" in rendered
