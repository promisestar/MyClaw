"""轨迹 span 树聚合验证：显式 iteration 归属 + 时间窗兜底 + 重试去重 + 兜底分组。

不依赖真实 LLM / FastAPI，直接向临时日志目录写入 JSONL 后验证聚合结果。
运行：./.venv/Scripts/python.exe -m tests.test_trace_tree
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # backend/ 根

DATE = "2026-09-16"


def _redirect_log_dir(tmp_dir: Path) -> None:
    """把两类日志重定向到同一个临时目录。"""
    os.environ["TOOL_LOG_DIR"] = str(tmp_dir)
    from src.logging.tool_logger import ToolCallLogger

    ToolCallLogger._log_dir = tmp_dir


def _write_lines(path: Path, entries: List[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        for entry in entries:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _usage(ts: str, *, iteration: int, duration_ms: float, call_site: str = "main_loop",
           trace_id: str = "t1", session_id: str = "s1",
           prompt_preview: str = "") -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "timestamp": ts,
        "trace_id": trace_id,
        "session_id": session_id,
        "agent_name": "myclaw",
        "call_site": call_site,
        "model": "test-model",
        "stream": True,
        "iteration": iteration,
        "prompt_tokens": 1000,
        "completion_tokens": 50,
        "total_tokens": 1050,
        "cached_tokens": 800,
        "reasoning_tokens": 0,
        "duration_ms": duration_ms,
        "status": "ok",
        "request": {"system_count": 1, "message_count": 3, "tool_count": 32},
        "response": {"content_preview": "hello", "content_len": 5, "tool_call_count": 1},
    }
    if prompt_preview:
        entry["prompt_preview"] = prompt_preview
    return entry


def _tool(ts: str, *, name: str = "web_search", call_id: str = "c1",
          duration_ms: float = 3000.0, status: str = "done",
          iteration: int | None = None, trace_id: str = "t1",
          session_id: str = "s1", **extra: Any) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "timestamp": ts,
        "trace_id": trace_id,
        "session_id": session_id,
        "tool_name": name,
        "tool_call_id": call_id,
        "args": {"query": "x"},
        "result": "ok",
        "result_len": 2,
        "status": status,
        "duration_ms": duration_ms,
    }
    if iteration is not None:
        entry["iteration"] = iteration
    entry.update(extra)
    return entry


def test_exact_attribution(tmp_dir: Path):
    """工具日志带 iteration 时，应精确挂到同轮次的模型调用 span 上。"""
    _redirect_log_dir(tmp_dir)
    from src.logging.trace_tree import build_turn_tree

    _write_lines(
        tmp_dir / f"llm-usage-{DATE}.jsonl",
        [
            _usage("2026-09-16T10:00:00.000", iteration=1, duration_ms=3000),
            _usage("2026-09-16T10:00:10.000", iteration=2, duration_ms=2000),
        ],
    )
    _write_lines(
        tmp_dir / f"{DATE}.jsonl",
        [
            _tool("2026-09-16T10:00:01.000", call_id="c1", iteration=1),
            _tool("2026-09-16T10:00:02.000", call_id="c2", iteration=1),
            _tool("2026-09-16T10:00:09.000", call_id="c3", iteration=2),
        ],
    )

    turn = build_turn_tree("t1", DATE)
    assert turn is not None, "turn 不应为空"
    assert turn["model_calls"] == 2 and turn["tool_calls"] == 3, turn
    assert turn["attribution"] == "exact", turn["attribution"]

    by_seq = {span["seq"]: span for span in turn["model_spans"]}
    assert len(by_seq[1]["tools"]) == 2, by_seq[1]["tools"]
    assert len(by_seq[2]["tools"]) == 1, by_seq[2]["tools"]
    assert by_seq[1]["tools"][0]["tool_call_id"] == "c1"
    assert turn["usage"]["total_tokens"] == 2100, turn["usage"]
    assert turn["status"] == "done"
    print("✅ 显式 iteration 精确归属 通过")


def test_time_window_fallback(tmp_dir: Path):
    """工具日志缺 iteration 时，按时间窗落到对应模型调用 span。"""
    _redirect_log_dir(tmp_dir)
    from src.logging.trace_tree import build_turn_tree

    _write_lines(
        tmp_dir / f"llm-usage-{DATE}.jsonl",
        [
            _usage("2026-09-16T10:00:00.000", iteration=1, duration_ms=3000),
            _usage("2026-09-16T10:00:10.000", iteration=2, duration_ms=2000),
        ],
    )
    _write_lines(
        tmp_dir / f"{DATE}.jsonl",
        [
            # span1 窗口 [09:59:57, 10:00:00]，span2 窗口 [10:00:08, 10:00:10]
            _tool("2026-09-16T10:00:02.000", call_id="c1"),
            _tool("2026-09-16T10:00:09.000", call_id="c2"),
        ],
    )

    turn = build_turn_tree("t1", DATE)
    assert turn is not None
    assert turn["attribution"] == "time_window", turn["attribution"]
    by_seq = {span["seq"]: span for span in turn["model_spans"]}
    assert len(by_seq[1]["tools"]) == 1 and by_seq[1]["tools"][0]["tool_call_id"] == "c1"
    assert len(by_seq[2]["tools"]) == 1 and by_seq[2]["tools"][0]["tool_call_id"] == "c2"
    print("✅ 时间窗兜底归属 通过")


def test_retry_dedup(tmp_dir: Path):
    """同一 tool_call_id 的重试记录应合并为一个工具 span。"""
    _redirect_log_dir(tmp_dir)
    from src.logging.trace_tree import build_turn_tree

    _write_lines(
        tmp_dir / f"llm-usage-{DATE}.jsonl",
        [_usage("2026-09-16T10:00:00.000", iteration=1, duration_ms=500)],
    )
    _write_lines(
        tmp_dir / f"{DATE}.jsonl",
        [
            _tool("2026-09-16T10:00:00.500", call_id="c1", duration_ms=100,
                  status="retry", retry_attempt=1, iteration=1,
                  error_type="rate_limit"),
            _tool("2026-09-16T10:00:03.000", call_id="c1", duration_ms=2600,
                  status="done", retry_count=1, iteration=1),
        ],
    )

    turn = build_turn_tree("t1", DATE)
    assert turn is not None
    assert turn["tool_calls"] == 1, turn["tool_calls"]
    tool = turn["model_spans"][0]["tools"][0]
    assert tool["status"] == "done"
    assert tool["retry_count"] == 1
    assert len(tool["attempts"]) == 1 and tool["attempts"][0]["status"] == "retry"
    assert tool["duration_ms"] == 2600, tool["duration_ms"]
    print("✅ 重试记录合并去重 通过")


def test_unassigned_and_no_trace(tmp_dir: Path):
    """早于任何模型调用的工具 → unassigned；无 trace_id → __no_trace__ 分组。"""
    _redirect_log_dir(tmp_dir)
    from src.logging.trace_tree import NO_TRACE, build_day_summaries, build_turn_tree

    _write_lines(
        tmp_dir / f"llm-usage-{DATE}.jsonl",
        [
            _usage("2026-09-16T10:00:00.000", iteration=1, duration_ms=600),
            _usage("2026-09-16T10:05:00.000", iteration=1, duration_ms=100,
                   call_site="context_compress", trace_id="", session_id=""),
        ],
    )
    _write_lines(
        tmp_dir / f"{DATE}.jsonl",
        [
            # 09:00 早于 span 起点（09:59:59.400）→ 无法归属
            _tool("2026-09-16T09:00:00.000", call_id="c1"),
            _tool("2026-09-16T10:00:01.000", call_id="c2", trace_id="", session_id=""),
        ],
    )

    turn = build_turn_tree("t1", DATE)
    assert turn is not None
    assert len(turn["unassigned_tools"]) == 1, turn["unassigned_tools"]
    assert turn["unassigned_tools"][0]["tool_call_id"] == "c1"
    assert turn["attribution"] == "unassigned", turn["attribution"]

    # 旁路调用（context_compress）不应计入 model_calls
    assert turn["model_calls"] == 1, turn["model_calls"]

    summaries = build_day_summaries(DATE)
    assert summaries["total_turns"] == 2, summaries
    assert {s["session_id"] for s in summaries["sessions"]} == {"s1", ""}, summaries

    no_trace_turn = build_turn_tree(NO_TRACE, DATE)
    assert no_trace_turn is not None
    assert no_trace_turn["model_calls"] == 0
    assert len(no_trace_turn["side_spans"]) == 1, no_trace_turn["side_spans"]
    assert len(no_trace_turn["unassigned_tools"]) == 1
    print("✅ 兜底分组与旁路调用 通过")


def test_prompt_resolver_and_summary(tmp_dir: Path):
    """旧日志缺 prompt_preview 时回退到会话历史；列表接口按会话分组。"""
    _redirect_log_dir(tmp_dir)
    from src.logging.trace_tree import build_day_summaries, build_turn_tree

    _write_lines(
        tmp_dir / f"llm-usage-{DATE}.jsonl",
        [
            _usage("2026-09-16T10:00:00.000", iteration=1, duration_ms=600),
            _usage("2026-09-16T10:10:00.000", iteration=1, duration_ms=600, trace_id="t2"),
        ],
    )
    _write_lines(tmp_dir / f"{DATE}.jsonl", [])

    calls: List[Any] = []

    def resolver(session_id: str, index: int):
        calls.append((session_id, index))
        return f"提问-{index}"

    turn = build_turn_tree("t1", DATE, prompt_resolver=resolver)
    assert turn is not None
    assert turn["prompt_preview"] == "提问-1", turn["prompt_preview"]
    assert calls == [("s1", 1)], calls

    summaries = build_day_summaries(DATE, prompt_resolver=resolver)
    assert summaries["total_turns"] == 2, summaries
    assert len(summaries["sessions"]) == 1
    session = summaries["sessions"][0]
    assert session["session_id"] == "s1" and session["turn_count"] == 2
    assert [t["index"] for t in session["turns"]] == [1, 2]
    assert [t["prompt_preview"] for t in session["turns"]] == ["提问-1", "提问-2"]
    # 摘要不应带上 span 明细
    assert "model_spans" not in session["turns"][0]
    print("✅ 提问预览兜底与会话分组 通过")


def test_entry_fields_survive(tmp_dir: Path):
    """新增埋点字段（request/response/prompt_preview）应完整透传到 span。"""
    _redirect_log_dir(tmp_dir)
    from src.logging.trace_tree import build_turn_tree

    _write_lines(
        tmp_dir / f"llm-usage-{DATE}.jsonl",
        [_usage("2026-09-16T10:00:00.000", iteration=1, duration_ms=600,
                prompt_preview="轩链的编程宇宙这个B站UP主最近10期视频")],
    )
    _write_lines(tmp_dir / f"{DATE}.jsonl", [_tool("2026-09-16T10:00:01.000", iteration=1)])

    turn = build_turn_tree("t1", DATE)
    assert turn is not None
    assert turn["prompt_preview"].startswith("轩链的编程宇宙")
    span = turn["model_spans"][0]
    assert span["request"]["tool_count"] == 32, span["request"]
    assert span["request"]["message_count"] == 3
    assert span["response"]["tool_call_count"] == 1
    print("✅ 请求/响应元数据透传 通过")


def main():
    tmp = Path(tempfile.mkdtemp(prefix="trace_tree_test_"))
    test_exact_attribution(tmp)
    test_time_window_fallback(tmp)
    test_retry_dedup(tmp)
    test_unassigned_and_no_trace(tmp)
    test_prompt_resolver_and_summary(tmp)
    test_entry_fields_survive(tmp)
    print("\n全部轨迹 span 树验证通过 ✅")
    print(f"临时日志目录: {tmp}")


if __name__ == "__main__":
    main()
