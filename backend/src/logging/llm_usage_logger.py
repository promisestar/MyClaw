"""LLM 调用用量日志（token 消耗 / 缓存命中）

设计目标：
- 与 ToolCallLogger 同源：相同日志目录、相同 trace_id 上下文，独立文件
  ``{log_dir}/llm-usage-YYYY-MM-DD.jsonl``
- 零外部依赖，写入加 threading.Lock 保证线程安全
- 归一化不同厂商的 usage 字段（OpenAI / MiniMax / 智谱等），
  重点提取 ``prompt_tokens_details.cached_tokens`` 用于验证 system 提示词冻结效果

使用方式：
    # API 层（与 tool_logger 一起）
    set_trace_id(generate_trace_id())
    set_session_id(session_id)

    # 流式链路：EnhancedHelloAgentsLLM 内部已自动记录
    # 非流式链路：手动记录
    LLMUsageLogger.log_response(resp, model="glm-4", call_site="context_compress")
"""

from __future__ import annotations

import json
import logging
import os
import threading
from contextvars import ContextVar
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from .tool_logger import ToolCallLogger, get_trace_id

logger = logging.getLogger(__name__)

# 文件前缀（与工具日志的 YYYY-MM-DD.jsonl 区分开）
FILE_PREFIX = "llm-usage-"

_session_id_ctx: ContextVar[Optional[str]] = ContextVar("llm_usage_session_id", default=None)
_agent_name_ctx: ContextVar[Optional[str]] = ContextVar("llm_usage_agent_name", default=None)


def set_session_id(session_id: Optional[str]) -> None:
    """设置当前异步/线程上下文的 session_id（供用量日志关联会话）。"""
    _session_id_ctx.set(session_id)


def get_session_id() -> Optional[str]:
    return _session_id_ctx.get()


def set_agent_name(agent_name: Optional[str]) -> None:
    """设置当前上下文的 Agent 名称（主代理 / 子代理）。"""
    _agent_name_ctx.set(agent_name)


def get_agent_name() -> Optional[str]:
    return _agent_name_ctx.get()


# ==================== usage 归一化 ====================

def _get(obj: Any, name: str) -> Any:
    """同时支持 dict 与对象两种 usage 形态。"""
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def normalize_usage(usage: Any) -> Optional[Dict[str, int]]:
    """把各家厂商的 usage 归一化成固定字段。

    Returns:
        None 表示本次调用没有拿到 usage（统计时应计为 0 次有效采样）。
    """
    if usage is None:
        return None

    prompt = _get(usage, "prompt_tokens") or 0
    completion = _get(usage, "completion_tokens") or 0
    total = _get(usage, "total_tokens") or (prompt + completion)

    # 缓存命中：OpenAI 规范在 prompt_tokens_details.cached_tokens，
    # 部分厂商（含 MiniMax）直接在顶层给 cached_tokens
    cached = _get(usage, "cached_tokens")
    if cached is None:
        details = _get(usage, "prompt_tokens_details")
        cached = _get(details, "cached_tokens") if details is not None else None

    # 思考型模型的推理 token
    reasoning = _get(usage, "reasoning_tokens")
    if reasoning is None:
        c_details = _get(usage, "completion_tokens_details")
        reasoning = _get(c_details, "reasoning_tokens") if c_details is not None else None

    if not (prompt or completion or total):
        return None

    return {
        "prompt_tokens": int(prompt or 0),
        "completion_tokens": int(completion or 0),
        "total_tokens": int(total or 0),
        "cached_tokens": int(cached or 0),
        "reasoning_tokens": int(reasoning or 0),
    }


def empty_usage() -> Dict[str, int]:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
    }


def merge_usage(target: Dict[str, int], usage: Optional[Dict[str, int]]) -> Dict[str, int]:
    """把一次调用的 usage 累加到汇总字典（原地修改并返回）。"""
    if not usage:
        return target
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens", "reasoning_tokens"):
        target[key] = int(target.get(key, 0)) + int(usage.get(key, 0))
    return target


def log_response_safe(
    response: Any,
    *,
    model: str = "",
    call_site: str = "unknown",
    duration_ms: float = 0.0,
    **kwargs,
) -> None:
    """记录非流式响应用量，且保证绝不抛异常。

    供子代理摘要、上下文压缩、用户画像等旁路调用点使用——
    这些位置不应因为遥测失败而影响主流程。
    """
    try:
        LLMUsageLogger.log_response(
            response,
            model=model,
            call_site=call_site,
            duration_ms=duration_ms,
            **kwargs,
        )
    except Exception:
        logger.debug("log_response_safe 失败", exc_info=True)


class LLMUsageLogger:
    """LLM 用量日志器（JSONL 单文件 / 按天切分）。"""

    _lock = threading.Lock()

    # ── 路径 ──────────────────────────────────────────────
    @classmethod
    def _log_dir(cls) -> Path:
        return ToolCallLogger._ensure_log_dir()

    @classmethod
    def _log_file_path(cls, date_str: Optional[str] = None) -> Path:
        d = date_str or datetime.now().strftime("%Y-%m-%d")
        return cls._log_dir() / f"{FILE_PREFIX}{d}.jsonl"

    # ── 写入 ──────────────────────────────────────────────
    @classmethod
    def log(
        cls,
        *,
        model: str = "",
        call_site: str = "unknown",
        usage: Optional[Dict[str, int]] = None,
        duration_ms: float = 0.0,
        stream: bool = False,
        iteration: Optional[int] = None,
        status: str = "ok",
        error: Optional[str] = None,
        session_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        agent_name: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """写入一条 LLM 调用用量记录。

        Args:
            model: 模型 ID
            call_site: 调用来源（main_loop / subagent_summary / context_compress / profile ...）
            usage: normalize_usage 的结果
            duration_ms: 该次调用耗时
            stream: 是否流式调用
            iteration: 主循环轮次（仅 main_loop 有）
            status: ok / error / no_usage
            error: 错误信息
        """
        u = usage or empty_usage()
        has_usage = bool(usage)
        entry = {
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "trace_id": trace_id or get_trace_id() or "",
            "session_id": session_id if session_id is not None else (get_session_id() or ""),
            "agent_name": agent_name if agent_name is not None else (get_agent_name() or ""),
            "call_site": call_site,
            "model": model,
            "stream": bool(stream),
            "iteration": iteration,
            "prompt_tokens": u.get("prompt_tokens", 0),
            "completion_tokens": u.get("completion_tokens", 0),
            "total_tokens": u.get("total_tokens", 0),
            "cached_tokens": u.get("cached_tokens", 0),
            "reasoning_tokens": u.get("reasoning_tokens", 0),
            "duration_ms": round(duration_ms, 2),
            "status": status if has_usage else (status if status != "ok" else "no_usage"),
        }
        if error:
            entry["error"] = str(error)[:500]

        try:
            line = json.dumps(entry, ensure_ascii=False) + "\n"
            with cls._lock:
                with open(cls._log_file_path(), "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception:
            logger.warning("写入 LLM 用量日志失败", exc_info=True)
        return entry

    @classmethod
    def log_response(
        cls,
        response: Any,
        *,
        model: str = "",
        call_site: str = "unknown",
        duration_ms: float = 0.0,
        **kwargs,
    ) -> Optional[Dict[str, Any]]:
        """从非流式响应对象中提取 usage 并记录。

        兼容 hello_agents 的 LLMResponse（.usage）与 OpenAI 原生响应（.usage）。
        取不到 usage 时仍写一条 status="no_usage" 记录，便于发现盲区。
        """
        usage = normalize_usage(_get(response, "usage"))
        model = model or str(_get(response, "model") or "")
        return cls.log(
            model=model,
            call_site=call_site,
            usage=usage,
            duration_ms=duration_ms,
            stream=False,
            **kwargs,
        )

    # ── 读取 ──────────────────────────────────────────────
    @classmethod
    def _read_day(cls, date_str: str) -> List[Dict[str, Any]]:
        path = cls._log_file_path(date_str)
        if not path.exists():
            return []
        entries: List[Dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entries.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
        except Exception:
            logger.warning("读取 LLM 用量日志失败: %s", path, exc_info=True)
        return entries

    @classmethod
    def list_files(cls) -> List[Dict[str, Any]]:
        """列出所有用量日志文件（按日期降序）。"""
        files: List[Dict[str, Any]] = []
        for fpath in sorted(cls._log_dir().glob(f"{FILE_PREFIX}*.jsonl"), reverse=True):
            try:
                stat = fpath.stat()
                count = 0
                with open(fpath, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            count += 1
                files.append({
                    "date_str": fpath.name[len(FILE_PREFIX):-len(".jsonl")],
                    "file_name": fpath.name,
                    "entry_count": count,
                    "size_bytes": stat.st_size,
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                })
            except Exception:
                continue
        return files

    @classmethod
    def recent(cls, date_str: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        """返回最近 N 条用量记录。"""
        entries = cls._read_day(date_str or datetime.now().strftime("%Y-%m-%d"))
        return entries[-limit:]

    # ── 聚合 ──────────────────────────────────────────────
    @staticmethod
    def _aggregate(entries: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
        total = empty_usage()
        calls = 0
        calls_with_usage = 0
        duration_sum = 0.0
        by_model: Dict[str, Dict[str, Any]] = {}
        by_site: Dict[str, Dict[str, Any]] = {}

        for e in entries:
            calls += 1
            has_usage = e.get("status") == "ok"
            if has_usage:
                calls_with_usage += 1
            duration_sum += float(e.get("duration_ms") or 0)

            for key in total:
                total[key] += int(e.get(key) or 0)

            for bucket, name in ((by_model, e.get("model") or "unknown"),
                                 (by_site, e.get("call_site") or "unknown")):
                item = bucket.setdefault(name, {
                    "name": name, "calls": 0, "duration_ms": 0.0, **empty_usage(),
                })
                item["calls"] += 1
                item["duration_ms"] += float(e.get("duration_ms") or 0)
                for key in total:
                    item[key] += int(e.get(key) or 0)

        def _finalize(bucket: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
            out = []
            for item in bucket.values():
                item["cache_hit_rate"] = (
                    round(item["cached_tokens"] / item["prompt_tokens"], 4)
                    if item["prompt_tokens"] else 0.0
                )
                item["avg_duration_ms"] = (
                    round(item["duration_ms"] / item["calls"], 2) if item["calls"] else 0.0
                )
                out.append(item)
            out.sort(key=lambda x: x["total_tokens"], reverse=True)
            return out

        return {
            "calls": calls,
            "calls_with_usage": calls_with_usage,
            "usage_coverage": round(calls_with_usage / calls, 4) if calls else 0.0,
            "total_tokens": total["total_tokens"],
            "prompt_tokens": total["prompt_tokens"],
            "completion_tokens": total["completion_tokens"],
            "cached_tokens": total["cached_tokens"],
            "reasoning_tokens": total["reasoning_tokens"],
            "cache_hit_rate": (
                round(total["cached_tokens"] / total["prompt_tokens"], 4)
                if total["prompt_tokens"] else 0.0
            ),
            "avg_duration_ms": round(duration_sum / calls, 2) if calls else 0.0,
            "by_model": _finalize(by_model),
            "by_call_site": _finalize(by_site),
        }

    @classmethod
    def daily_summary(cls, date_str: str) -> Dict[str, Any]:
        """单日汇总。"""
        summary = cls._aggregate(cls._read_day(date_str))
        summary["date_str"] = date_str
        return summary

    @classmethod
    def range_summary(cls, days: int = 7) -> Dict[str, Any]:
        """最近 N 天汇总（含按天趋势）。"""
        days = max(1, min(days, 90))
        today = datetime.now().date()
        by_day: List[Dict[str, Any]] = []
        all_entries: List[Dict[str, Any]] = []
        for i in range(days - 1, -1, -1):
            d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
            entries = cls._read_day(d)
            all_entries.extend(entries)
            day_summary = cls._aggregate(entries)
            day_summary["date_str"] = d
            by_day.append(day_summary)

        summary = cls._aggregate(all_entries)
        summary["days"] = days
        summary["by_day"] = by_day
        return summary
