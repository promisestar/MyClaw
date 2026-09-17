"""轨迹 span 树聚合。

把同一天的两类 JSONL 日志 join 成「会话 → 轮次 → 模型调用 → 工具调用」四级
span 树，供前端轨迹页（TraceView）展示：

- 工具调用：``{log_dir}/YYYY-MM-DD.jsonl``（``ToolCallLogger``）
- LLM 用量：``{log_dir}/llm-usage-YYYY-MM-DD.jsonl``（``LLMUsageLogger``）

关联策略（按可靠性排序）：

1. **显式轮次**：工具日志的 ``iteration`` 与用量日志主循环的 ``iteration`` 对齐
   （新日志，精确）。
2. **时间窗兜底**：LLM span 起点取 ``timestamp - duration_ms``。
   把工具分配到「起点 ≤ 工具结束时刻」中起点最大的 LLM span——
   等价于「工具落在这一轮之后、下一轮之前」，对长耗时工具同样成立（旧日志，近似）。
3. **兜底挂载**：仍无法归属的工具挂到轮次根下的 ``unassigned_tools``，
   并在 ``attribution`` 中标注，前端据此降级展示。

为什么不能直接按时间戳排序分组：流式链路下工具是在流式过程中就执行的
（``enhanced_simple_agent`` 的 TOOL_CALL_START/DELTA/FINISH 三个早执行点），
而用量记录写在流式**结束之后**，因此「模型调用记录」的时间戳可能晚于其
自身工具的时间戳，必须依赖显式 ``iteration`` 才能精确归属。

本模块只做纯计算 + 文件读取，不做 HTTP/线程调度，便于单测。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple

from .llm_usage_logger import LLMUsageLogger
from .tool_logger import ToolCallLogger

logger = logging.getLogger(__name__)

#: trace_id 缺失时的兜底分组键（如 /chat/send/sync 未设置 trace_id）
NO_TRACE = "__no_trace__"

#: 视作「主循环模型调用」的 call_site；其余归入旁路 span（压缩 / 摘要 / 画像）
MAIN_LOOP_SITES = {"main_loop"}

DEFAULT_PREVIEW_LEN = 120

# 轮次摘要暴露给列表接口的字段（不含 span 明细）
_SUMMARY_KEYS = (
    "trace_id",
    "session_id",
    "index",
    "prompt_preview",
    "start_ts",
    "end_ts",
    "duration_ms",
    "model_calls",
    "tool_calls",
    "usage",
    "status",
    "agents",
    "attribution",
)

_USAGE_KEYS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cached_tokens",
    "reasoning_tokens",
)


# ══════════════════════════════════════════════════════════
# 基础工具
# ══════════════════════════════════════════════════════════

def _parse_ts(value: Any) -> Optional[float]:
    """把 ISO 时间字符串解析成 epoch 秒；失败返回 None。"""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).timestamp()
    except (ValueError, TypeError):
        return None


def _iso(ts: Optional[float]) -> str:
    """epoch 秒 → ISO 字符串（失败或 None 返回空串）。"""
    if ts is None:
        return ""
    try:
        return datetime.fromtimestamp(ts).isoformat(timespec="milliseconds")
    except (OverflowError, OSError, ValueError):
        return ""


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _preview(text: Any, limit: int = DEFAULT_PREVIEW_LEN) -> str:
    if not isinstance(text, str):
        return ""
    return text.replace("\n", " ").strip()[:limit]


def _prev_date(date_str: str) -> str:
    """前一天日期字符串（跨零点兜底用）；解析失败时原样返回。"""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").date() - timedelta(days=1)
        return d.strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return date_str


# ══════════════════════════════════════════════════════════
# 读取
# ══════════════════════════════════════════════════════════

def load_usage_entries(date_str: str) -> List[Dict[str, Any]]:
    """读取某日 LLM 用量日志条目（读不到返回空列表）。"""
    try:
        return LLMUsageLogger._read_day(date_str)
    except Exception:
        logger.warning("读取用量日志失败: %s", date_str, exc_info=True)
        return []


def load_tool_entries(date_str: str) -> List[Dict[str, Any]]:
    """读取某日工具调用日志条目（读不到返回空列表）。"""
    try:
        return ToolCallLogger.read_day(date_str)
    except Exception:
        logger.warning("读取工具日志失败: %s", date_str, exc_info=True)
        return []


# ══════════════════════════════════════════════════════════
# 分组
# ══════════════════════════════════════════════════════════

def _group_by_trace(
    usage_entries: List[Dict[str, Any]],
    tool_entries: List[Dict[str, Any]],
) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    """按 trace_id 把两类条目归入同一桶（保持首次出现顺序）。"""
    order: List[str] = []
    buckets: Dict[str, Dict[str, Any]] = {}

    def _bucket(entry: Dict[str, Any]) -> Dict[str, Any]:
        key = str(entry.get("trace_id") or "") or NO_TRACE
        bucket = buckets.get(key)
        if bucket is None:
            bucket = {"trace_id": key, "session_id": "", "usage": [], "tools": []}
            buckets[key] = bucket
            order.append(key)
        if not bucket["session_id"]:
            bucket["session_id"] = str(entry.get("session_id") or "")
        return bucket

    for entry in usage_entries:
        _bucket(entry)["usage"].append(entry)
    for entry in tool_entries:
        _bucket(entry)["tools"].append(entry)
    return order, buckets


def _bucket_sort_ts(bucket: Dict[str, Any]) -> float:
    """桶内所有条目的最早时间戳（用于轮次排序）。"""
    stamps = [
        ts
        for entry in (bucket["usage"] + bucket["tools"])
        for ts in (_parse_ts(entry.get("timestamp")),)
        if ts is not None
    ]
    return min(stamps) if stamps else 0.0


def _ordered_sessions(
    order: List[str],
    buckets: Dict[str, Dict[str, Any]],
) -> List[Tuple[str, List[str]]]:
    """按会话聚合 trace 键，并在会话内按时间升序排列。

    Returns:
        [(session_id, [trace_key, ...]), ...]
    """
    grouped: Dict[str, List[str]] = {}
    session_order: List[str] = []
    for key in order:
        sid = buckets[key]["session_id"] or ""
        if sid not in grouped:
            grouped[sid] = []
            session_order.append(sid)
        grouped[sid].append(key)

    for sid in session_order:
        grouped[sid].sort(key=lambda k: _bucket_sort_ts(buckets[k]))

    return [(sid, grouped[sid]) for sid in session_order]


# ══════════════════════════════════════════════════════════
# 工具 span 合并（重试去重）
# ══════════════════════════════════════════════════════════

def _merge_tool_attempts(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把同一 tool_call_id 的多条重试记录合并为单个工具 span。

    工具失败重试会在日志中产生多条记录：中间尝试 ``status="retry"``，
    最后一条为最终结果（``done`` / ``error``，带累计 ``retry_count`` 与累计耗时）。
    这里以最终记录为主 span，中间尝试折叠进 ``attempts``。
    """
    order: List[str] = []
    groups: Dict[str, List[Dict[str, Any]]] = {}

    for index, entry in enumerate(entries):
        key = str(entry.get("tool_call_id") or "") or f"__anon_{index}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(entry)

    merged: List[Dict[str, Any]] = []
    for key in order:
        items = groups[key]
        final = items[-1]
        for candidate in reversed(items):
            if candidate.get("status") != "retry":
                final = candidate
                break

        attempts = [
            {
                "status": e.get("status") or "",
                "duration_ms": round(_num(e.get("duration_ms")), 2),
                "retry_attempt": e.get("retry_attempt"),
                "error_type": e.get("error_type"),
                "timestamp": e.get("timestamp") or "",
            }
            for e in items
            if e is not final
        ]

        duration = _num(final.get("duration_ms"))
        end_ts = _parse_ts(final.get("timestamp"))
        retry_count = final.get("retry_count")
        if retry_count is None:
            retry_count = len(items) - 1

        merged.append(
            {
                "tool_call_id": str(final.get("tool_call_id") or ""),
                "tool_name": str(final.get("tool_name") or ""),
                "args": final.get("args") or {},
                "result": final.get("result") or "",
                "result_len": _int(final.get("result_len")),
                "status": str(final.get("status") or "done"),
                "error_type": final.get("error_type"),
                "agent_name": str(final.get("agent_name") or ""),
                "duration_ms": round(duration, 2),
                "start_ts": _iso(end_ts - duration / 1000.0 if end_ts is not None else None),
                "end_ts": _iso(end_ts),
                "retry_count": _int(retry_count),
                "attempts": attempts,
                "_start": end_ts - duration / 1000.0 if end_ts is not None else None,
                "_end": end_ts,
                "_iteration": final.get("iteration"),
            }
        )
    return merged


# ══════════════════════════════════════════════════════════
# span 构建
# ══════════════════════════════════════════════════════════

def _build_model_span(entry: Dict[str, Any]) -> Dict[str, Any]:
    """把一条用量记录转成模型调用 span。"""
    duration = _num(entry.get("duration_ms"))
    end_ts = _parse_ts(entry.get("timestamp"))
    start_ts = end_ts - duration / 1000.0 if end_ts is not None else None

    request = entry.get("request")
    response = entry.get("response")
    return {
        "seq": None,
        "iteration": entry.get("iteration"),
        "call_site": str(entry.get("call_site") or ""),
        "model": str(entry.get("model") or ""),
        "agent_name": str(entry.get("agent_name") or ""),
        "stream": bool(entry.get("stream")),
        "status": str(entry.get("status") or ""),
        "error": entry.get("error"),
        "duration_ms": round(duration, 2),
        "start_ts": _iso(start_ts),
        "end_ts": _iso(end_ts),
        "usage": {k: _int(entry.get(k)) for k in _USAGE_KEYS},
        "request": request if isinstance(request, dict) and request else None,
        "response": response if isinstance(response, dict) and response else None,
        "tools": [],
        "_start": start_ts,
        "_end": end_ts,
    }


def _pick_span_by_time(
    spans: List[Dict[str, Any]],
    ts: Optional[float],
) -> Optional[Dict[str, Any]]:
    """时间窗兜底：取「起点 ≤ ts」中起点最大的 span。

    传入的 ``ts`` 应为工具的**结束时刻**（而非开始时刻）：
    模型调用 span 按起点递增排列，「起点 ≤ 工具结束」中起点最大者
    即「该工具所属的最后一轮已开始的模型调用」，
    对长耗时工具、以及流结束后才执行的兜底工具都成立。
    """
    if ts is None:
        return None
    best: Optional[Dict[str, Any]] = None
    for span in spans:
        start = span["_start"]
        if start is None or start > ts:
            continue
        if best is None or start > best["_start"]:
            best = span
    return best


def _aggregate_usage(entries: List[Dict[str, Any]]) -> Dict[str, int]:
    total = {k: 0 for k in _USAGE_KEYS}
    for entry in entries:
        for key in _USAGE_KEYS:
            total[key] += _int(entry.get(key))
    return total


def _build_turn(
    bucket: Dict[str, Any],
    *,
    index: int,
    prompt_resolver: Optional[Callable[[str, int], Optional[str]]] = None,
) -> Dict[str, Any]:
    """把单个 trace 的原始条目构建成完整轮次 span 树。"""
    usage_entries = bucket["usage"]
    tool_entries = bucket["tools"]
    session_id = bucket["session_id"]

    main_spans: List[Dict[str, Any]] = []
    side_spans: List[Dict[str, Any]] = []
    for entry in usage_entries:
        span = _build_model_span(entry)
        if span["call_site"] in MAIN_LOOP_SITES:
            main_spans.append(span)
        else:
            side_spans.append(span)

    # 主循环 span 按开始时间升序（流式链路上时间戳与轮次同序）
    main_spans.sort(key=lambda s: (s["_start"] if s["_start"] is not None else 0.0, s["_end"] or 0.0))
    side_spans.sort(key=lambda s: (s["_start"] if s["_start"] is not None else 0.0, s["_end"] or 0.0))

    # 轮次编号：优先用日志里的 iteration，缺失时退化为顺序号
    for position, span in enumerate(main_spans, start=1):
        iteration = span["iteration"]
        span["seq"] = iteration if isinstance(iteration, int) and iteration > 0 else position

    merged_tools = _merge_tool_attempts(tool_entries)

    unassigned_tools: List[Dict[str, Any]] = []
    used_exact = False
    used_time_window = False
    for tool in merged_tools:
        target: Optional[Dict[str, Any]] = None
        iteration = tool["_iteration"]
        if isinstance(iteration, int):
            target = next((s for s in main_spans if s["seq"] == iteration), None)
            if target is not None:
                used_exact = True
        if target is None:
            # 用工具「结束时刻」匹配：对长耗时工具与流后兜底执行的工具都成立
            target = _pick_span_by_time(main_spans, tool["_end"] or tool["_start"])
            if target is not None:
                used_time_window = True
        if target is None:
            unassigned_tools.append(tool)
        else:
            target["tools"].append(tool)

    if unassigned_tools and (used_exact or used_time_window):
        attribution = "mixed"
    elif unassigned_tools:
        attribution = "unassigned"
    elif used_exact and used_time_window:
        attribution = "mixed"
    elif used_time_window:
        attribution = "time_window"
    elif used_exact:
        attribution = "exact"
    else:
        attribution = "exact"

    # 时间范围
    spans_all = main_spans + side_spans
    starts = [s["_start"] for s in spans_all if s["_start"] is not None]
    ends = [s["_end"] for s in spans_all if s["_end"] is not None]
    starts += [t["_start"] for t in merged_tools if t["_start"] is not None]
    ends += [t["_end"] for t in merged_tools if t["_end"] is not None]
    start_ts = min(starts) if starts else None
    end_ts = max(ends) if ends else None
    duration_ms = (end_ts - start_ts) * 1000 if start_ts is not None and end_ts is not None else 0.0

    # 状态：任一工具失败/超时或任一 LLM 调用报错 → error
    bad_tool = any(t["status"] in ("error", "timeout") for t in merged_tools)
    bad_llm = any(s["status"] == "error" for s in spans_all)
    status = "error" if (bad_tool or bad_llm) else "done"

    agents = sorted({s["agent_name"] for s in spans_all if s["agent_name"]})

    # 用户提问预览：新日志直接带 prompt_preview，旧日志回退到会话历史
    prompt_preview = ""
    for entry in usage_entries:
        candidate = _preview(entry.get("prompt_preview"))
        if candidate:
            prompt_preview = candidate
            break
    if not prompt_preview and prompt_resolver is not None and session_id:
        try:
            prompt_preview = _preview(prompt_resolver(session_id, index))
        except Exception:
            logger.debug("prompt_resolver 失败: %s#%s", session_id, index, exc_info=True)

    turn = {
        "trace_id": bucket["trace_id"],
        "session_id": session_id,
        "index": index,
        "prompt_preview": prompt_preview,
        "start_ts": _iso(start_ts),
        "end_ts": _iso(end_ts),
        "duration_ms": round(duration_ms, 2),
        "model_calls": len(main_spans),
        "tool_calls": len(merged_tools),
        "usage": _aggregate_usage(usage_entries),
        "status": status,
        "agents": agents,
        "attribution": attribution,
        "model_spans": main_spans,
        "side_spans": side_spans,
        "unassigned_tools": unassigned_tools,
    }

    # 清理内部排序字段
    for span in spans_all:
        span.pop("_start", None)
        span.pop("_end", None)
    for tool in merged_tools:
        tool.pop("_start", None)
        tool.pop("_end", None)
        tool.pop("_iteration", None)

    return turn


def _to_summary(turn: Dict[str, Any]) -> Dict[str, Any]:
    """从完整轮次中抽出列表接口需要的摘要字段。"""
    return {key: turn[key] for key in _SUMMARY_KEYS if key in turn}


# ══════════════════════════════════════════════════════════
# 对外接口
# ══════════════════════════════════════════════════════════

def build_day_summaries(
    date_str: str,
    *,
    session_id: Optional[str] = None,
    prompt_resolver: Optional[Callable[[str, int], Optional[str]]] = None,
) -> Dict[str, Any]:
    """构建某日全部轮次的摘要（供轨迹页左侧列表）。

    Args:
        date_str: 日期 YYYY-MM-DD
        session_id: 可选，只返回该会话
        prompt_resolver: 可选，(session_id, 轮次号) → 用户提问预览；用于旧日志兜底

    Returns:
        {"date_str", "sessions": [{"session_id", "turn_count", "turns": [...]}], "total_turns"}
    """
    usage_entries = load_usage_entries(date_str)
    tool_entries = load_tool_entries(date_str)
    order, buckets = _group_by_trace(usage_entries, tool_entries)

    sessions: List[Dict[str, Any]] = []
    total_turns = 0
    for sid, trace_keys in _ordered_sessions(order, buckets):
        if session_id and sid != session_id:
            continue
        turns: List[Dict[str, Any]] = []
        for index, trace_key in enumerate(trace_keys, start=1):
            turn = _build_turn(
                buckets[trace_key],
                index=index,
                prompt_resolver=prompt_resolver,
            )
            turns.append(_to_summary(turn))
        sessions.append({"session_id": sid, "turn_count": len(turns), "turns": turns})
        total_turns += len(turns)

    return {"date_str": date_str, "sessions": sessions, "total_turns": total_turns}


def build_turn_tree(
    trace_id: str,
    date_str: str,
    *,
    prompt_resolver: Optional[Callable[[str, int], Optional[str]]] = None,
) -> Optional[Dict[str, Any]]:
    """构建单个轮次的完整 span 树（供轨迹页右侧）。

    跨零点兜底：若该 trace 在当天文件中没有用量记录（请求始于前一天），
    会一并补读前一日日志。

    Returns:
        完整轮次 dict；找不到返回 None
    """
    key = trace_id or NO_TRACE

    usage_entries = load_usage_entries(date_str)
    tool_entries = load_tool_entries(date_str)

    if key != NO_TRACE and not any(str(e.get("trace_id") or "") == key for e in usage_entries):
        prev_date = _prev_date(date_str)
        if prev_date != date_str:
            usage_entries = load_usage_entries(prev_date) + usage_entries
            tool_entries = load_tool_entries(prev_date) + tool_entries

    order, buckets = _group_by_trace(usage_entries, tool_entries)
    bucket = buckets.get(key)
    if bucket is None:
        return None

    # 计算该轮次在所属会话内的序号（与列表接口保持一致）
    index = 1
    session_id = bucket["session_id"] or ""
    for sid, trace_keys in _ordered_sessions(order, buckets):
        if sid != session_id:
            continue
        if key in trace_keys:
            index = trace_keys.index(key) + 1
        break

    return _build_turn(bucket, index=index, prompt_resolver=prompt_resolver)


def list_log_dates() -> List[Dict[str, Any]]:
    """列出同时可用的日志日期（合并工具日志与用量日志的文件列表）。"""
    merged: Dict[str, Dict[str, Any]] = {}
    for files in (ToolCallLogger.list_files(), LLMUsageLogger.list_files()):
        for item in files or []:
            date_str = str(item.get("date_str") or "")
            if not date_str:
                continue
            current = merged.get(date_str)
            if current is None:
                merged[date_str] = {
                    "date_str": date_str,
                    "tool_entries": 0,
                    "usage_entries": 0,
                    "modified_at": item.get("modified_at") or "",
                }
                current = merged[date_str]
            if str(item.get("file_name") or "").startswith("llm-usage-"):
                current["usage_entries"] = int(item.get("entry_count") or 0)
            else:
                current["tool_entries"] = int(item.get("entry_count") or 0)
            if (item.get("modified_at") or "") > current["modified_at"]:
                current["modified_at"] = item.get("modified_at") or ""

    return [merged[key] for key in sorted(merged.keys(), reverse=True)]
