"""LLM 用量统计 API 路由

数据来源：``{log_dir}/llm-usage-YYYY-MM-DD.jsonl``
（由 EnhancedHelloAgentsLLM / 主循环 / 子代理摘要 / 上下文压缩等调用点写入）

提供三类视图：
- ``/usage/summary``  最近 N 天汇总 + 按天趋势
- ``/usage/day/{date}`` 单日汇总
- ``/usage/recent``  最近若干条原始记录（排障用）
- ``/usage/files``   日志文件列表

性能说明：本地文件 IO，读取在线程池中执行，避免阻塞事件循环。
"""
import re
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

router = APIRouter(prefix="/usage", tags=["usage"])

_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


@router.get("/summary")
async def get_summary(days: int = Query(7, ge=1, le=90, description="统计最近 N 天")):
    """最近 N 天的 token 用量汇总（含按天趋势、按模型、按调用点分布）。"""
    from ..logging.llm_usage_logger import LLMUsageLogger

    try:
        return await run_in_threadpool(LLMUsageLogger.range_summary, days)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取用量统计失败: {e}")


@router.get("/recent")
async def get_recent(
    date: Optional[str] = Query(None, description="日期 YYYY-MM-DD，默认今天"),
    limit: int = Query(100, ge=1, le=2000),
):
    """返回最近的原始用量记录（排障用）。"""
    from ..logging.llm_usage_logger import LLMUsageLogger

    if date and not _DATE_PATTERN.match(date):
        raise HTTPException(status_code=400, detail=f"无效的日期格式: {date}")
    try:
        entries = await run_in_threadpool(LLMUsageLogger.recent, date, limit)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取用量记录失败: {e}")
    return {"date_str": date or "today", "entries": entries, "total": len(entries)}


@router.get("/files")
async def list_files():
    """列出所有用量日志文件。"""
    from ..logging.llm_usage_logger import LLMUsageLogger

    try:
        files = await run_in_threadpool(LLMUsageLogger.list_files)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"列出用量日志失败: {e}")
    return {"files": files, "total": len(files)}


@router.get("/day/{date_str}")
async def get_day_summary(date_str: str):
    """单日用量汇总。"""
    from ..logging.llm_usage_logger import LLMUsageLogger

    if not _DATE_PATTERN.match(date_str):
        raise HTTPException(status_code=400, detail=f"无效的日期格式: {date_str}")
    try:
        return await run_in_threadpool(LLMUsageLogger.daily_summary, date_str)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取用量统计失败: {e}")


@router.delete("/day/{date_str}")
async def delete_day_log(date_str: str):
    """删除指定日期的用量日志文件。"""
    from ..logging.llm_usage_logger import LLMUsageLogger

    if not _DATE_PATTERN.match(date_str):
        raise HTTPException(status_code=400, detail=f"无效的日期格式: {date_str}")
    path = LLMUsageLogger._log_file_path(date_str)
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"用量日志不存在: {date_str}")
    try:
        path.unlink()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"删除用量日志失败: {e}")
    return {"message": "用量日志已删除", "date_str": date_str}
