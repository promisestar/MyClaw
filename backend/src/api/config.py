"""配置 API 路由 (V2) — 读写按文件类型分发到 IdentityManager 或 WorkspaceManager。

IDENTITY/SOUL/USER/BOOTSTRAP 从 IdentityManager（基座 ~/.helloclaw/identity/）读写；
AGENTS/HEARTBEAT 从 WorkspaceManager（当前工作区 .myclaw/）读写；
CONFIG (config.json) 从 ~/.helloclaw/config.json 读写。
"""

import json
import os
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..workspace.manager import WorkspaceManager, get_default_global_config

router = APIRouter(prefix="/config", tags=["config"])

# identity 文件名集合（从 IdentityManager 读写）
IDENTITY_NAMES = {"IDENTITY", "SOUL", "USER", "BOOTSTRAP"}

# 工作区文件名集合（从 WorkspaceManager 读写）
WORKSPACE_NAMES = {"AGENTS", "HEARTBEAT"}


class ConfigUpdateRequest(BaseModel):
    """配置更新请求"""
    content: str


class AgentInfo(BaseModel):
    """助手信息"""
    name: str


# 全局 workspace 实例（由 main.py 在启动时设置，作为兜底）
_workspace: Optional[WorkspaceManager] = None


def set_workspace(ws: WorkspaceManager):
    """设置全局 workspace 实例（兜底用）。"""
    global _workspace
    _workspace = ws


def get_agent():
    """获取全局 Agent 实例。"""
    from ..main import get_agent as _get_agent
    return _get_agent()


def get_workspace() -> WorkspaceManager:
    """获取当前 workspace 实例。

    优先返回 agent 当前工作区（实时反映 bind_workspace 切换结果），
    兜底用 main.py 启动时注入的全局 workspace。
    """
    agent = get_agent()
    if agent and agent.workspace:
        return agent.workspace
    if _workspace is not None:
        return _workspace
    # 兜底：创建默认工作区
    ws = WorkspaceManager(os.getenv("WORKSPACE_PATH", "~"))
    ws.ensure_global_config_exists()
    ws.ensure_project_workspace()
    set_workspace(ws)
    return _workspace


def get_identity():
    """获取 IdentityManager（从 agent）。"""
    agent = get_agent()
    if agent and agent.identity:
        return agent.identity
    return None


def get_config_json_path() -> str:
    """获取全局 config.json 路径。"""
    return os.path.expanduser("~/.helloclaw/config.json")


def ensure_config_json_exists():
    """确保 config.json 存在。"""
    config_path = get_config_json_path()
    if not os.path.exists(config_path):
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(get_default_global_config(), f, indent=2, ensure_ascii=False)


@router.get("/list")
async def list_configs():
    """获取配置文件列表（合并 CONFIG + identity + workspace）。"""
    configs = ["CONFIG"]

    # identity 目录下的文件
    identity = get_identity()
    if identity:
        configs.extend(identity.list_configs())

    # 当前工作区 .myclaw/ 下的文件
    ws = get_workspace()
    configs.extend(ws.list_configs())

    return {"configs": configs}


@router.get("/{name}")
async def get_config(name: str):
    """获取指定配置文件内容（按文件类型分流）。"""
    # 特殊处理 CONFIG (config.json)
    if name == "CONFIG":
        ensure_config_json_exists()
        config_path = get_config_json_path()
        with open(config_path, "r", encoding="utf-8") as f:
            content = f.read()
        return {"name": name, "content": content}

    # identity 文件
    if name in IDENTITY_NAMES:
        identity = get_identity()
        if not identity:
            raise HTTPException(status_code=500, detail="Identity 管理器未初始化")
        content = identity.load_name(name)
        if content is None:
            raise HTTPException(status_code=404, detail=f"配置文件 {name} 不存在")
        return {"name": name, "content": content}

    # 工作区文件
    if name in WORKSPACE_NAMES:
        ws = get_workspace()
        content = ws.load_config(name)
        if content is None:
            raise HTTPException(status_code=404, detail=f"配置文件 {name} 不存在")
        return {"name": name, "content": content}

    raise HTTPException(status_code=404, detail=f"未知配置: {name}")


@router.put("/{name}")
async def update_config(name: str, request: ConfigUpdateRequest):
    """更新配置文件（按文件类型分流）。"""
    # 特殊处理 CONFIG (config.json)
    if name == "CONFIG":
        ensure_config_json_exists()
        try:
            config_data = json.loads(request.content)
        except json.JSONDecodeError as e:
            raise HTTPException(status_code=400, detail=f"无效的 JSON 格式: {str(e)}")
        if not isinstance(config_data, dict):
            raise HTTPException(status_code=400, detail="配置必须是 JSON 对象")
        if "llm" not in config_data:
            raise HTTPException(status_code=400, detail="缺少必需字段: llm")
        llm_config = config_data.get("llm", {})
        required_fields = ["model_id", "api_key", "base_url"]
        missing_fields = [f for f in required_fields if f not in llm_config]
        if missing_fields:
            raise HTTPException(status_code=400, detail=f"llm 配置缺少必需字段: {', '.join(missing_fields)}")
        config_path = get_config_json_path()
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(request.content)
        return {"name": name, "status": "updated"}

    # identity 文件
    if name in IDENTITY_NAMES:
        identity = get_identity()
        if not identity:
            raise HTTPException(status_code=500, detail="Identity 管理器未初始化")
        identity.save_file(f"{name}.md", request.content)
        # 保存 IDENTITY 后检查是否可完成入职（自动删除 BOOTSTRAP.md）
        if name == "IDENTITY":
            identity.try_complete_onboarding()
        return {"name": name, "status": "updated"}

    # 工作区文件
    if name in WORKSPACE_NAMES:
        ws = get_workspace()
        ws.save_config(name, request.content)
        return {"name": name, "status": "updated"}

    raise HTTPException(status_code=404, detail=f"未知配置: {name}")


@router.post("/reset")
async def reset_workspace(
    reset_sessions: bool = False,
    reset_global_config: bool = False,
):
    """重置当前工作区配置文件到初始模板（仅工作区文件，不含 identity）。"""
    try:
        ws = get_workspace()
        ws.reset_to_templates(
            reset_sessions=reset_sessions,
            reset_global_config=reset_global_config,
        )

        # 如果清除了会话，也要清除 Agent 内存中的历史记录
        if reset_sessions:
            agent = get_agent()
            if agent:
                agent.clear_all_history()

        messages = ["工作区配置文件已重置"]
        if reset_sessions:
            messages.append("会话已清除")
        if reset_global_config:
            messages.append("全局配置已重置")

        return {"status": "success", "message": "，".join(messages)}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"重置失败: {str(e)}")


@router.get("/agent/info", response_model=AgentInfo)
async def get_agent_info():
    """获取助手信息（从基座 identity 读取名字，每次重新读取最新值）。"""
    identity = get_identity()
    name = "HelloClaw"
    if identity:
        n = identity.read_name()
        if n:
            name = n
    return AgentInfo(name=name)
