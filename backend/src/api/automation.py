"""定时任务 REST API 路由 — CRUD + 启停 + 运行历史。

依赖注入：set_automation_store() 在 main.py lifespan 中调用。
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..automation.models import AutomationTask
from ..automation.store import AutomationStore

router = APIRouter(prefix="/automation", tags=["automation"])

# 全局 store_getter 引用（由 main.py lifespan 注入）
# 动态取值：工作区切换后 agent 重建 store，API 始终引用当前 store（C2）
_store_getter: Optional[Callable[[], "AutomationStore"]] = None


def set_automation_store(store: AutomationStore) -> None:
    """注入 AutomationStore 实例（兼容旧调用，内部转为返回该实例的 getter）。"""
    global _store_getter
    _store_getter = (lambda s=store: s)


def set_automation_store_getter(getter: Callable[[], AutomationStore]) -> None:
    """注入 AutomationStore getter（推荐，支持工作区切换后动态跟随，C2）。"""
    global _store_getter
    _store_getter = getter


def _get_store() -> AutomationStore:
    if _store_getter is None:
        raise HTTPException(status_code=503, detail="AutomationStore 未初始化")
    store = _store_getter()
    if store is None:
        raise HTTPException(status_code=503, detail="AutomationStore 未初始化")
    return store


# ── Pydantic 模型 ──


class CreateAutomationRequest(BaseModel):
    name: str = Field(description="任务名称")
    prompt: str = Field(description="要执行的 prompt")
    schedule_type: str = Field(description="once | interval | rrule")
    schedule_config: Dict[str, Any] = Field(
        default_factory=dict,
        description="调度配置",
    )
    webhook_url: Optional[str] = Field(default=None, description="webhook URL")
    workspace_path: Optional[str] = Field(default=None, description="工作区路径")
    enabled: bool = Field(default=True, description="是否启用")
    max_runs: Optional[int] = Field(default=None, description="最大运行次数")
    max_duration_minutes: Optional[int] = Field(default=None, description="单次最大时长")


class UpdateAutomationRequest(BaseModel):
    name: Optional[str] = None
    prompt: Optional[str] = None
    schedule_type: Optional[str] = None
    schedule_config: Optional[Dict[str, Any]] = None
    webhook_url: Optional[str] = None
    workspace_path: Optional[str] = None
    enabled: Optional[bool] = None
    max_runs: Optional[int] = None
    max_duration_minutes: Optional[int] = None


class AutomationResponse(BaseModel):
    id: str
    name: str
    prompt: str
    schedule_type: str
    schedule_config: Dict[str, Any]
    webhook_url: Optional[str] = None
    enabled: bool
    workspace_path: str = ""
    created_at: str = ""
    last_run_at: Optional[str] = None
    next_run_at: Optional[str] = None
    run_count: int = 0


class RunResponse(BaseModel):
    run_id: str
    task_id: str
    prompt: str
    result: str = ""
    success: bool = False
    error: Optional[str] = None
    started_at: str = ""
    finished_at: str = ""


# ── 路由 ──


@router.post("/", response_model=AutomationResponse)
async def create_automation(req: CreateAutomationRequest):
    """创建定时任务。"""
    store = _get_store()

    if req.schedule_type not in ("once", "interval", "rrule"):
        raise HTTPException(status_code=400, detail="schedule_type 必须是 once/interval/rrule")

    if req.webhook_url and not req.webhook_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="webhook_url 必须以 http:// 或 https:// 开头")

    task = AutomationTask(
        name=req.name,
        prompt=req.prompt,
        schedule_type=req.schedule_type,
        schedule_config=req.schedule_config,
        webhook_url=req.webhook_url,
        enabled=req.enabled,
        workspace_path=req.workspace_path or "",
        max_runs=req.max_runs,
        max_duration_minutes=req.max_duration_minutes,
    )

    next_run = task.compute_next_run()
    if next_run is None:
        raise HTTPException(status_code=400, detail="无法计算下次运行时间，请检查 schedule_config")
    task.next_run_at = next_run.isoformat()

    store.create_task(task)
    return AutomationResponse(**task.to_dict())


@router.get("/", response_model=List[AutomationResponse])
async def list_automations(enabled_only: bool = False):
    """列出所有定时任务。"""
    store = _get_store()
    tasks = store.list_tasks(enabled_only=enabled_only)
    return [AutomationResponse(**t.to_dict()) for t in tasks]


@router.get("/{task_id}", response_model=AutomationResponse)
async def get_automation(task_id: str):
    """获取定时任务详情。"""
    store = _get_store()
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    return AutomationResponse(**task.to_dict())


@router.put("/{task_id}", response_model=AutomationResponse)
async def update_automation(task_id: str, req: UpdateAutomationRequest):
    """更新定时任务。"""
    store = _get_store()
    task = store.get_task(task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 部分更新
    update_data = req.model_dump(exclude_none=True)
    for key, value in update_data.items():
        if hasattr(task, key):
            setattr(task, key, value)

    # 重新计算下次运行时间
    if "schedule_type" in update_data or "schedule_config" in update_data:
        next_run = task.compute_next_run()
        task.next_run_at = next_run.isoformat() if next_run else None

    store.update_task(task)
    return AutomationResponse(**task.to_dict())


@router.delete("/{task_id}")
async def delete_automation(task_id: str):
    """删除定时任务。"""
    store = _get_store()
    if not store.delete_task(task_id):
        raise HTTPException(status_code=404, detail="任务不存在")
    return {"deleted": task_id}


@router.post("/{task_id}/enable", response_model=AutomationResponse)
async def enable_automation(task_id: str):
    """启用定时任务。"""
    store = _get_store()
    if not store.set_enabled(task_id, True):
        raise HTTPException(status_code=404, detail="任务不存在")
    task = store.get_task(task_id)
    return AutomationResponse(**task.to_dict())


@router.post("/{task_id}/disable", response_model=AutomationResponse)
async def disable_automation(task_id: str):
    """禁用定时任务。"""
    store = _get_store()
    if not store.set_enabled(task_id, False):
        raise HTTPException(status_code=404, detail="任务不存在")
    task = store.get_task(task_id)
    return AutomationResponse(**task.to_dict())


@router.get("/{task_id}/runs", response_model=List[RunResponse])
async def list_automation_runs(task_id: str, limit: int = 20):
    """获取任务的运行历史。"""
    store = _get_store()
    if store.get_task(task_id) is None:
        raise HTTPException(status_code=404, detail="任务不存在")
    runs = store.list_runs(task_id, limit=limit)
    return [RunResponse(**r.to_dict()) for r in runs]
