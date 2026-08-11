"""跨会话原文搜索 API（调试 / 后续 UI）。

同步 SQLite 查询经 run_in_threadpool，避免阻塞事件循环。
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query
from fastapi.concurrency import run_in_threadpool

router = APIRouter(prefix="/session-search", tags=["session-search"])

_agent_getter = None


def set_agent_getter(getter) -> None:
    global _agent_getter
    _agent_getter = getter


def _get_agent():
    if _agent_getter is None:
        raise HTTPException(status_code=503, detail="Agent 未初始化")
    agent = _agent_getter()
    if agent is None:
        raise HTTPException(status_code=503, detail="Agent 未初始化")
    return agent


def _require_search(agent):
    search = getattr(agent, "_session_search", None)
    if search is None:
        raise HTTPException(status_code=503, detail="session_recall 未启用")
    return search


@router.get("")
async def discover(
    q: str = Query(..., min_length=1, description="检索关键词"),
    limit: int = Query(3, ge=1, le=20),
    window: int = Query(5, ge=0, le=50),
    sort: str = Query("relevance"),
    workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    agent = _get_agent()
    search = _require_search(agent)
    current = getattr(agent, "_current_session_id", None)

    def _run():
        return search.discover(
            q,
            limit=limit,
            window=window,
            sort=sort,
            current_session_id=current,
            workspace_id=workspace_id,
        )

    return await run_in_threadpool(_run)


@router.get("/{session_id}/around/{msg_id}")
async def scroll(
    session_id: str,
    msg_id: int,
    window: int = Query(5, ge=0, le=50),
) -> Dict[str, Any]:
    agent = _get_agent()
    search = _require_search(agent)

    def _run():
        return search.scroll(session_id, msg_id, window=window)

    return await run_in_threadpool(_run)


@router.post("/rebuild")
async def rebuild() -> Dict[str, Any]:
    agent = _get_agent()
    indexer = getattr(agent, "_session_indexer", None)
    if indexer is None:
        raise HTTPException(status_code=503, detail="session_recall 未启用")

    def _run():
        return indexer.rebuild_all()

    result = await run_in_threadpool(_run)
    return {"status": "ok", **result}
