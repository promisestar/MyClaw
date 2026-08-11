"""工作区管理 API 路由 — 工作区列表、切换、授权、撤销。"""

import os
from typing import List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..workspace import auth as workspace_auth


def get_agent_lock():
    """获取全局 Agent 锁（避免与正在进行的对话竞态）。"""
    from ..main import get_agent_lock as _get_agent_lock
    return _get_agent_lock()

router = APIRouter(prefix="/workspace", tags=["workspace"])


class WorkspaceSwitchRequest(BaseModel):
    """工作区切换请求"""
    path: str


class WorkspaceAuthorizeRequest(BaseModel):
    """工作区授权请求"""
    path: str


class WorkspaceRevokeRequest(BaseModel):
    """工作区撤销请求"""
    path: str


class WorkspacePickResponse(BaseModel):
    """工作区选择响应（通过系统文件夹选择对话框）"""
    path: Optional[str] = None  # None 表示用户取消
    cancelled: bool = False


class WorkspaceListResponse(BaseModel):
    """工作区列表响应"""
    workspaces: List[str]
    current: str


class WorkspaceSwitchResponse(BaseModel):
    """工作区切换响应"""
    status: str
    workspace: str


def get_agent():
    """获取全局 Agent 实例"""
    from ..main import get_agent as _get_agent
    return _get_agent()


@router.get("/list", response_model=WorkspaceListResponse)
async def list_workspaces():
    """列出所有已授权工作区及当前工作区。

    若当前工作区不在白名单中（例如旧进程、手工删了 workspaces.json），
    自动补授权，避免 UI 显示「当前目录却未授权」。
    """
    agent = get_agent()
    current = agent.current_workspace if agent else ""
    if current:
        workspace_auth.ensure_authorized(current)
    return WorkspaceListResponse(
        workspaces=workspace_auth.list_allowed_workspaces(),
        current=current,
    )


@router.post("/switch", response_model=WorkspaceSwitchResponse)
async def switch_workspace(data: WorkspaceSwitchRequest):
    """切换到指定工作区（须已授权）。"""
    agent = get_agent()
    if not agent:
        raise HTTPException(status_code=500, detail="Agent not initialized")
    if not workspace_auth.is_allowed(data.path):
        raise HTTPException(status_code=403, detail="工作区未授权，请先授权")
    # 在 agent_lock 内执行切换，避免与正在进行的对话竞态
    lock = get_agent_lock()
    try:
        if lock:
            async with lock:
                agent.bind_workspace(data.path)
        else:
            agent.bind_workspace(data.path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return WorkspaceSwitchResponse(status="ok", workspace=agent.current_workspace)


@router.post("/authorize")
async def authorize_workspace(data: WorkspaceAuthorizeRequest):
    """授权一个新工作区。"""
    ok = workspace_auth.authorize(data.path)
    if not ok:
        raise HTTPException(status_code=400, detail="工作区路径不存在或无效")
    return {"status": "ok", "workspace": os.path.realpath(os.path.expanduser(data.path))}


@router.post("/pick_folder", response_model=WorkspacePickResponse)
async def pick_folder():
    """弹出系统原生文件夹选择对话框，让用户选择本地目录。

    适用于 H5 端无法直接获取本地文件系统绝对路径的情况。
    后端必须运行在用户本地机器上。需阻塞 GUI 调用，在线程池执行。

    Returns:
        WorkspacePickResponse: path 为用户选择的绝对路径，cancelled=True 表示用户取消
    """
    import asyncio
    try:
        from tkinter import Tk, filedialog
    except ImportError:
        raise HTTPException(
            status_code=501,
            detail="当前环境不支持 tkinter 文件夹选择对话框，请手动输入路径"
        )

    def _ask():
        root = Tk()
        root.withdraw()
        root.attributes('-topmost', True)
        path = filedialog.askdirectory(title="选择工作区目录", mustexist=True)
        root.destroy()
        return path

    try:
        path = await asyncio.to_thread(_ask)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"打开文件夹选择对话框失败: {e}")

    if not path:
        return WorkspacePickResponse(path=None, cancelled=True)
    return WorkspacePickResponse(
        path=os.path.realpath(os.path.expanduser(path)),
        cancelled=False,
    )


@router.delete("/revoke")
async def revoke_workspace(data: WorkspaceRevokeRequest):
    """撤销一个工作区授权。"""
    workspace_auth.revoke(data.path)
    return {"status": "ok"}
