"""任务管理 API 路由 — 查询当前会话的任务进度

提供只读端点供前端任务面板实时展示任务状态。
任务的创建/推进由 Agent 通过 TaskTool 完成，本路由不做写入。
"""

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from typing import List, Optional

router = APIRouter(prefix="/agent", tags=["agent"])

# 全局 Agent 引用（由 main.py 在启动时调用 set_agent 注入）
_agent = None


def set_agent(agent):
    """设置全局 Agent 实例引用"""
    global _agent
    _agent = agent


def get_agent():
    """获取 Agent 实例"""
    return _agent


class TaskItem(BaseModel):
    """单个任务项"""
    id: str
    subject: str
    description: str = ""
    status: str
    blocked_by: List[str] = []
    blocks: List[str] = []
    created_at: float = 0
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    notes: str = ""


class TaskProgressResponse(BaseModel):
    """任务进度响应"""
    session_id: Optional[str] = None
    total: int = 0
    completed: int = 0
    in_progress: int = 0
    pending: int = 0
    failed: int = 0
    cancelled: int = 0
    blocked: int = 0
    summary: str = ""
    tasks: List[TaskItem] = []


@router.get("/task-progress", response_model=TaskProgressResponse)
async def get_task_progress(session_id: str = ""):
    """获取当前会话的任务进度。

    Args:
        session_id: 会话 ID。为空时使用 Agent 当前会话。
    """
    agent = get_agent()
    if not agent:
        raise HTTPException(status_code=503, detail="Agent 未就绪")

    tracker = getattr(agent, "_task_tracker", None)
    if not tracker:
        return TaskProgressResponse()

    # 指定 session_id 且与当前不同 → 从磁盘加载
    sid = session_id.strip() or getattr(agent, "_current_session_id", None)
    if session_id.strip() and sid != getattr(agent, "_current_session_id", None):
        # 临时加载指定会话的任务快照（不影响内存状态）
        from ..agent.task_tracker import TaskTracker

        tasks_dir = getattr(tracker, "_persist_dir", None)
        if tasks_dir:
            tmp_tracker = TaskTracker(persist_dir=tasks_dir)
            tmp_tracker.load(session_id.strip())
            return _build_response(session_id.strip(), tmp_tracker)

    # 默认返回当前内存中的任务
    return _build_response(sid, tracker)


def _build_response(session_id: Optional[str], tracker) -> TaskProgressResponse:
    """从 TaskTracker 构建响应。"""
    all_tasks = tracker.list_all()
    if not all_tasks:
        return TaskProgressResponse(session_id=session_id)

    from ..agent.task_tracker import TaskStatus

    items = [
        TaskItem(
            id=t.id,
            subject=t.subject,
            description=t.description,
            status=t.status.value,
            blocked_by=list(t.blocked_by),
            blocks=list(t.blocks),
            created_at=t.created_at,
            started_at=t.started_at,
            completed_at=t.completed_at,
            notes=t.notes,
        )
        for t in all_tasks
    ]

    completed = sum(1 for t in all_tasks if t.status == TaskStatus.COMPLETED)
    in_progress = sum(1 for t in all_tasks if t.status == TaskStatus.IN_PROGRESS)
    pending = sum(1 for t in all_tasks if t.status == TaskStatus.PENDING)
    failed = sum(1 for t in all_tasks if t.status == TaskStatus.FAILED)
    cancelled = sum(1 for t in all_tasks if t.status == TaskStatus.CANCELLED)
    blocked = sum(1 for t in all_tasks if t.is_blocked)

    return TaskProgressResponse(
        session_id=session_id,
        total=len(all_tasks),
        completed=completed,
        in_progress=in_progress,
        pending=pending,
        failed=failed,
        cancelled=cancelled,
        blocked=blocked,
        summary=tracker.get_progress_summary(),
        tasks=items,
    )
