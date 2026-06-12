"""工具调用结构化日志

设计目标：
- 每次工具执行记录一条 JSON 行（JSONL），用 contextvars 传递 trace_id 贯穿请求链路
- 零外部依赖，文件写入加了 threading.Lock 保证线程安全
- 日志路径：{workspace}/tool_logs/YYYY-MM-DD.jsonl

使用方式：
    # 在 API 层设置 trace_id
    set_trace_id(generate_trace_id())

    # 在工具执行处记录
    ToolCallLogger.log(...)
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_trace_id_ctx: ContextVar[Optional[str]] = ContextVar("tool_trace_id", default=None)


def generate_trace_id() -> str:
    """生成短 trace_id（8 位 hex），适合肉眼识别。"""
    return uuid.uuid4().hex[:8]


def set_trace_id(trace_id: str) -> None:
    """设置当前异步/线程上下文的 trace_id。"""
    _trace_id_ctx.set(trace_id)


def get_trace_id() -> Optional[str]:
    """获取当前上下文的 trace_id。"""
    return _trace_id_ctx.get()


class ToolCallLogger:
    """工具调用结构化日志器（JSONL 单文件）。"""

    _lock = threading.Lock()
    _log_dir: Optional[Path] = None

    @classmethod
    def _ensure_log_dir(cls) -> Path:
        if cls._log_dir is not None:
            return cls._log_dir
        base = os.getenv(
            "TOOL_LOG_DIR",
            os.path.join(os.path.expanduser("~"), ".helloclaw", "tool_logs"),
        )
        cls._log_dir = Path(base)
        cls._log_dir.mkdir(parents=True, exist_ok=True)
        return cls._log_dir

    @classmethod
    def _log_file_path(cls) -> Path:
        date_str = datetime.now().strftime("%Y-%m-%d")
        return cls._ensure_log_dir() / f"{date_str}.jsonl"

    @classmethod
    def log(
        cls,
        tool_name: str,
        tool_call_id: str,
        args: dict,
        result: str,
        session_id: Optional[str] = None,
        trace_id: Optional[str] = None,
        status: str = "done",
        duration_ms: float = 0.0,
    ) -> None:
        """写入一条工具调用日志。

        Args:
            tool_name: 工具名称
            tool_call_id: 工具调用 ID
            args: 调用参数
            result: 返回结果（长结果会被截断到 2000 字符）
            session_id: 会话 ID
            trace_id: 跟踪 ID（不传则自动从 contextvar 读取）
            status: 执行状态（done / error / timeout）
            duration_ms: 耗时（毫秒）
        """
        trace = trace_id or get_trace_id()
        entry = {
            "timestamp": datetime.now().isoformat(timespec="milliseconds"),
            "trace_id": trace,
            "session_id": session_id or "",
            "tool_name": tool_name,
            "tool_call_id": tool_call_id,
            "args": _sanitize_args(args),
            "result": result[:2000] if result else "",
            "result_len": len(result) if result else 0,
            "status": status,
            "duration_ms": round(duration_ms, 2),
        }

        try:
            line = json.dumps(entry, ensure_ascii=False) + "\n"
            with cls._lock:
                with open(cls._log_file_path(), "a", encoding="utf-8") as f:
                    f.write(line)
        except Exception:
            logger.warning("写入工具日志失败", exc_info=True)

    @classmethod
    def get_log_dir(cls) -> Path:
        """公开获取日志目录（供 API 使用）。"""
        return cls._ensure_log_dir()

    @classmethod
    def list_files(cls) -> list[dict]:
        """扫描日志目录，返回所有 JSONL 文件元信息列表（按日期降序）。"""
        log_dir = cls._ensure_log_dir()
        files: list[dict] = []
        for fpath in sorted(log_dir.glob("*.jsonl"), reverse=True):
            date_str = fpath.stem  # e.g. "2026-06-12"
            stat = fpath.stat()
            # 统计记录条数（非空 JSON 行）
            entry_count = 0
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    for line in f:
                        if line.strip():
                            entry_count += 1
            except Exception:
                entry_count = 0
            files.append({
                "date_str": date_str,
                "file_name": fpath.name,
                "entry_count": entry_count,
                "size_bytes": stat.st_size,
                "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
            })
        return files

    @classmethod
    def query(
        cls,
        date_str: Optional[str] = None,
        trace_id: Optional[str] = None,
        tool_name: Optional[str] = None,
        status: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        """查询最近的工具调用日志（供 API 读取）。"""
        if date_str:
            log_path = cls._ensure_log_dir() / f"{date_str}.jsonl"
        else:
            log_path = cls._log_file_path()

        if not log_path.exists():
            return []

        entries: list[dict] = []
        try:
            with open(log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                    except json.JSONDecodeError:
                        continue

                    # 过滤
                    if trace_id and entry.get("trace_id") != trace_id:
                        continue
                    if tool_name and entry.get("tool_name") != tool_name:
                        continue
                    if status and entry.get("status") != status:
                        continue

                    entries.append(entry)

            # 返回最近的 limit 条
            return entries[-limit:]

        except Exception:
            logger.warning("查询工具日志失败", exc_info=True)
            return []


def _sanitize_args(args: dict) -> dict:
    """清理参数中的过长值，避免日志爆炸。"""
    safe = {}
    for k, v in args.items():
        if isinstance(v, str) and len(v) > 500:
            safe[k] = v[:500] + f"...<{len(v) - 500} chars truncated>"
        else:
            safe[k] = v
    return safe
