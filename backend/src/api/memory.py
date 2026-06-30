"""记忆 API 路由 — 基于 Qdrant 向量数据库

性能说明：
- 本路由的核心操作（search_memories / get_stats / add_memory / process_decay）
  均为同步阻塞调用：embedding 计算 + Qdrant HTTP 请求，单次耗时数百 ms 到数秒。
- 为避免阻塞 FastAPI 事件循环（进而拖累其它接口），所有同步 Qdrant 调用通过
  ``starlette.concurrency.run_in_threadpool`` 派发到线程池执行。
"""

import os
from fastapi import APIRouter, HTTPException, Depends, Query
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel
from typing import Optional, List, Dict

router = APIRouter(prefix="/memory", tags=["memory"])


class MemoryEntry(BaseModel):
    """记忆条目"""
    id: str
    content: str
    category: str = "fact"
    timestamp: int = 0
    source: str = ""


class MemoryListResponse(BaseModel):
    """记忆列表响应"""
    memories: List[MemoryEntry]
    total: int


class MemoryStatsResponse(BaseModel):
    """记忆统计响应"""
    total_count: int
    categories: Dict[str, int]


class MemoryCaptureRequest(BaseModel):
    """记忆捕获请求"""
    content: str
    category: str = "fact"


class MemoryCaptureResponse(BaseModel):
    """记忆捕获响应"""
    status: str
    message: str
    category: str


class MemoryCleanupResponse(BaseModel):
    """记忆清理响应"""
    status: str
    deleted: int
    message: str


# 全局 memory_store 实例（由 main.py 在启动时设置）
_memory_store = None


def set_memory_store(store):
    """设置全局 memory_store 实例"""
    global _memory_store
    _memory_store = store


def get_memory_store():
    """获取 memory_store 实例"""
    return _memory_store


def set_workspace(ws):
    """兼容旧 API — 不再需要 workspace，保留空实现"""
    pass


@router.get("/list", response_model=MemoryListResponse)
async def list_memories(
    keyword: Optional[str] = Query(None, description="搜索关键词"),
    category: Optional[str] = Query(None, description="按分类过滤"),
    top_k: int = Query(50, description="返回条数"),
):
    """获取记忆列表（语义检索或全量列出）"""
    store = get_memory_store()
    if not store:
        return MemoryListResponse(memories=[], total=0)

    # 同步 Qdrant 调用派发到线程池，避免阻塞事件循环
    if keyword:
        results = await run_in_threadpool(
            store.search_memories, query=keyword, top_k=top_k, category=category
        )
    else:
        results = await run_in_threadpool(store._list_recent, top_k)

    memories = [
        MemoryEntry(
            id=r.get("id", ""),
            content=r.get("content", ""),
            category=r.get("category", "fact"),
            timestamp=r.get("timestamp", 0),
            source=r.get("source", ""),
        )
        for r in results
    ]

    return MemoryListResponse(memories=memories, total=len(memories))


@router.get("/stats", response_model=MemoryStatsResponse)
async def get_memory_stats():
    """获取记忆统计"""
    store = get_memory_store()
    if not store:
        return MemoryStatsResponse(total_count=0, categories={})

    stats = await run_in_threadpool(store.get_stats)
    return MemoryStatsResponse(
        total_count=stats.get("total_count", 0),
        categories=stats.get("categories", {}),
    )


@router.post("/capture", response_model=MemoryCaptureResponse)
async def capture_memory(request: MemoryCaptureRequest):
    """手动添加记忆（带分类）"""
    valid_categories = [
        "preference", "decision", "entity", "fact",
        "plan", "relationship", "reference", "rule",
    ]
    if request.category not in valid_categories:
        raise HTTPException(
            status_code=400,
            detail=f"无效的分类: {request.category}，有效值: {valid_categories}",
        )

    store = get_memory_store()
    if not store:
        raise HTTPException(status_code=503, detail="记忆存储未就绪")

    memory_id = await run_in_threadpool(
        store.add_memory,
        content=request.content,
        category=request.category,
        source="api",
    )

    if memory_id:
        return MemoryCaptureResponse(
            status="ok",
            message=f"已添加 [{request.category}] 记忆",
            category=request.category,
        )
    else:
        return MemoryCaptureResponse(
            status="error",
            message="记忆写入失败",
            category=request.category,
        )


@router.post("/cleanup", response_model=MemoryCleanupResponse)
async def cleanup_memories():
    """处理记忆衰减（懒策略：计算衰减分数，删除归零记忆）"""
    store = get_memory_store()
    if not store:
        raise HTTPException(status_code=503, detail="记忆存储未就绪")

    # process_decay 可能很重（scroll + batch update），必须放线程池
    result = await run_in_threadpool(store.process_decay)

    return MemoryCleanupResponse(
        status="ok",
        deleted=result["deleted"],
        message=f"衰减处理完成: 总计 {result['total']} 条，删除 {result['deleted']} 条，更新 {result['updated']} 条",
    )
