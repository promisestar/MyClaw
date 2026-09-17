"""轨迹 span 树 API 路由

数据来源（与工具日志 / 用量日志同目录、按天切分，靠 trace_id 关联）：

- ``{log_dir}/YYYY-MM-DD.jsonl``             工具调用
- ``{log_dir}/llm-usage-YYYY-MM-DD.jsonl``   LLM 调用用量

提供视图：

- ``/trace/dates``                   可用日期列表（合并两类日志）
- ``/trace/turns?date=``             某日全部轮次摘要（轨迹页左侧列表）
- ``/trace/turns/{trace_id}?date=``  单个轮次的完整 span 树（轨迹页右侧）

性能说明：本地文件 IO，读取与聚合在线程池中执行，避免阻塞事件循环。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

router = APIRouter(prefix="/trace", tags=["trace"])

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# 会话 JSON 目录提供者（由 main.py 在启动时注入），用于旧日志的轮次标题兜底
_sessions_dir_provider: Optional[Callable[[], Optional[str]]] = None


def set_sessions_dir_provider(provider: Callable[[], Optional[str]]) -> None:
    """注入「当前工作区 sessions 目录」的取值函数。"""
    global _sessions_dir_provider
    _sessions_dir_provider = provider


def _sessions_dir() -> Optional[str]:
    if _sessions_dir_provider is None:
        return None
    try:
        value = _sessions_dir_provider()
    except Exception:
        return None
    return value or None


def _flatten_content(content: Any) -> str:
    """把 message content 拍平成纯文本（兼容多模态 list / 编码字符串）。"""
    if isinstance(content, list):
        try:
            from ..multimodal import flatten_content_to_text

            return flatten_content_to_text(content) or ""
        except Exception:
            parts = [
                item.get("text")
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            ]
            return "\n".join([p for p in parts if isinstance(p, str)])
    if isinstance(content, str):
        try:
            from ..agent.multimodal_bridge import (
                decode_multimodal_content,
                is_encoded_multimodal,
            )

            if is_encoded_multimodal(content):
                decoded = decode_multimodal_content(content)
                if not isinstance(decoded, str):
                    from ..multimodal import flatten_content_to_text

                    return flatten_content_to_text(decoded) or ""
                return decoded
        except Exception:
            pass
        return content
    return str(content or "")


def _resolve_prompt_from_session(session_id: str, turn_index: int) -> Optional[str]:
    """旧日志缺 ``prompt_preview`` 时，从会话 JSON 的 history 取第 N 条 user 消息。

    会话文件位于 ``{workspace}/sessions/{session_id}.json``，history 按时间顺序，
    与同一会话内按时间排序的轮次一一对应。
    """
    base = _sessions_dir()
    if not base or not session_id or turn_index < 1:
        return None

    path = Path(base) / f"{session_id}.json"
    if not path.is_file():
        return None

    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None

    history = data.get("history") if isinstance(data, dict) else None
    if not isinstance(history, list):
        return None

    users = [
        msg for msg in history
        if isinstance(msg, dict) and str(msg.get("role") or "") == "user"
    ]
    if turn_index > len(users):
        return None

    target = users[turn_index - 1]
    metadata = target.get("metadata") if isinstance(target.get("metadata"), dict) else {}
    original = metadata.get("original_content")
    raw = original if original is not None else target.get("content")
    return _flatten_content(raw)


def _validate_date(date_str: str) -> str:
    if not _DATE_PATTERN.match(date_str or ""):
        raise HTTPException(
            status_code=400,
            detail=f"无效的日期格式: {date_str}，应为 YYYY-MM-DD",
        )
    return date_str


@router.get("/dates")
async def list_dates():
    """列出可用的轨迹日期（合并工具日志与用量日志）。"""
    from ..logging.trace_tree import list_log_dates

    try:
        dates = await run_in_threadpool(list_log_dates)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取日志文件列表失败: {e}")
    return {"dates": dates, "total": len(dates)}


@router.get("/turns")
async def list_turns(
    date_str: str = Query(..., alias="date", description="日期 YYYY-MM-DD"),
    session_id: Optional[str] = Query(None, description="可选，仅返回该会话"),
):
    """某日全部轮次摘要（含每轮模型调用 / 工具调用次数、token 与状态）。"""
    from ..logging.trace_tree import build_day_summaries

    _validate_date(date_str)
    try:
        return await run_in_threadpool(
            build_day_summaries,
            date_str,
            session_id=session_id,
            prompt_resolver=_resolve_prompt_from_session,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"构建轨迹列表失败: {e}")


@router.get("/turns/{trace_id}")
async def get_turn_tree(
    trace_id: str,
    date_str: str = Query(..., alias="date", description="日期 YYYY-MM-DD"),
):
    """单个轮次的完整 span 树：模型调用分组 + 组内工具调用 + 旁路调用。

    ``trace_id`` 传 ``__no_trace__`` 可查看未关联 trace 的记录
    （如未设置 trace_id 的 ``/chat/send/sync`` 链路）。
    """
    from ..logging.trace_tree import build_turn_tree

    _validate_date(date_str)
    try:
        turn = await run_in_threadpool(
            build_turn_tree,
            trace_id,
            date_str,
            prompt_resolver=_resolve_prompt_from_session,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"构建轨迹失败: {e}")

    if turn is None:
        raise HTTPException(
            status_code=404,
            detail=f"未找到 trace {trace_id} 在 {date_str} 的记录",
        )
    return turn
