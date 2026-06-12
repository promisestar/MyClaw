"""工具调用日志 API 路由"""
import re
import json
from pathlib import Path
from fastapi import APIRouter, HTTPException
from typing import Optional

router = APIRouter(prefix="/tool-logs", tags=["tool-logs"])

# 安全校验：date_str 只允许 YYYY-MM-DD 格式
_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _get_log_dir() -> Path:
    """获取日志目录。"""
    from ..logging.tool_logger import ToolCallLogger
    return ToolCallLogger.get_log_dir()


def _safe_log_path(date_str: str) -> Path:
    """安全拼接日志文件路径，防止路径穿越。"""
    if not _DATE_PATTERN.match(date_str):
        raise HTTPException(status_code=400, detail=f"无效的日期格式: {date_str}，应为 YYYY-MM-DD")
    log_dir = _get_log_dir()
    file_path = (log_dir / f"{date_str}.jsonl").resolve()
    # 确保解析后的路径仍在日志目录内
    if not str(file_path).startswith(str(log_dir.resolve())):
        raise HTTPException(status_code=403, detail="路径穿越检测")
    return file_path


@router.get("/list")
async def list_log_files():
    """获取工具日志文件列表（按日期降序）。
    
    Returns:
        files: 日志文件元信息列表，每项包含 date_str、file_name、entry_count、size_bytes、modified_at
    """
    from ..logging.tool_logger import ToolCallLogger
    files = ToolCallLogger.list_files()
    return {"files": files, "total": len(files)}


@router.get("/{date_str}")
async def get_log_file(date_str: str, limit: Optional[int] = None):
    """读取指定日期的工具日志文件内容。

    Args:
        date_str: 日期字符串，格式 YYYY-MM-DD
        limit: 可选，返回最近 N 条记录
    """
    file_path = _safe_log_path(date_str)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"日志文件不存在: {date_str}.jsonl")

    entries: list[dict] = []
    try:
        with open(file_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取日志文件失败: {e}")

    if limit and limit > 0:
        entries = entries[-limit:]

    return {"date_str": date_str, "entries": entries, "total": len(entries)}


@router.delete("/{date_str}")
async def delete_log_file(date_str: str):
    """删除指定日期的工具日志文件。

    Args:
        date_str: 日期字符串，格式 YYYY-MM-DD
    """
    file_path = _safe_log_path(date_str)
    if not file_path.exists():
        raise HTTPException(status_code=404, detail=f"日志文件不存在: {date_str}.jsonl")

    try:
        file_path.unlink()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除日志文件失败: {e}")

    return {"message": "日志文件已删除", "date_str": date_str}
