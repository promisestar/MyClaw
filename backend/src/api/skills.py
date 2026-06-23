"""技能管理 API 路由

提供技能的 CRUD、导入、状态管理接口。
"""

import os
from pathlib import Path
from typing import Optional, List

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/skills", tags=["skills"])


# ── 全局 skill_loader 引用 ──
_skill_loader = None


def set_skill_loader(loader):
    """设置全局 SkillLoader 实例（在 main.py lifespan 中调用）"""
    global _skill_loader
    _skill_loader = loader


def get_skill_loader():
    """获取全局 SkillLoader 实例"""
    if _skill_loader is None:
        raise HTTPException(status_code=500, detail="Skill 系统未初始化")
    return _skill_loader


def _refresh_skill_tool():
    """刷新 Agent 的 SkillTool 描述（技能列表变化时调用）"""
    try:
        from ..main import get_agent
        agent = get_agent()
        if agent and hasattr(agent, 'refresh_skill_tool'):
            agent.refresh_skill_tool()
    except Exception:
        pass


# ── Pydantic 模型 ──

class SkillInfo(BaseModel):
    """技能信息"""
    name: str
    description: str
    enabled: bool
    dir: str


class SkillListResponse(BaseModel):
    """技能列表响应"""
    skills: List[SkillInfo]
    total: int
    enabled_count: int


class ImportRequest(BaseModel):
    """导入技能请求"""
    source_type: str  # "path" 或 "git"
    source: str       # 目录路径或 Git URL


class ImportResponse(BaseModel):
    """导入技能响应"""
    message: str
    skill: Optional[SkillInfo] = None
    error: Optional[str] = None


class SkillContentResponse(BaseModel):
    """技能内容响应"""
    name: str
    content: str


class SkillContentUpdateRequest(BaseModel):
    """更新技能内容请求"""
    content: str


class SkillDetailResponse(BaseModel):
    """技能详情响应"""
    name: str
    description: str
    body: str
    enabled: bool
    dir: str


# ── API 端点 ──

@router.get("", response_model=SkillListResponse)
async def list_skills():
    """列出所有技能"""
    loader = get_skill_loader()
    infos = loader.list_skill_infos()
    return SkillListResponse(
        skills=[SkillInfo(**info) for info in infos],
        total=loader.total_count,
        enabled_count=loader.enabled_count,
    )


@router.get("/{name}", response_model=SkillDetailResponse)
async def get_skill(name: str):
    """获取技能详情"""
    loader = get_skill_loader()
    skill = loader.get_skill(name)
    if not skill:
        raise HTTPException(status_code=404, detail=f"技能 '{name}' 不存在")
    return SkillDetailResponse(
        name=skill.name,
        description=skill.description,
        body=skill.body,
        enabled=loader.is_enabled(name),
        dir=str(skill.dir),
    )


@router.get("/{name}/content", response_model=SkillContentResponse)
async def get_skill_content(name: str):
    """获取 SKILL.md 原始内容"""
    loader = get_skill_loader()
    content = loader.get_skill_content(name)
    if content is None:
        raise HTTPException(status_code=404, detail=f"技能 '{name}' 不存在")
    return SkillContentResponse(name=name, content=content)


@router.put("/{name}/content")
async def update_skill_content(name: str, request: SkillContentUpdateRequest):
    """更新 SKILL.md 内容"""
    loader = get_skill_loader()
    success = loader.set_skill_content(name, request.content)
    if not success:
        raise HTTPException(status_code=404, detail=f"技能 '{name}' 不存在")
    _refresh_skill_tool()
    return {"message": f"技能 '{name}' 内容已更新"}


@router.delete("/{name}")
async def delete_skill(name: str):
    """删除技能"""
    loader = get_skill_loader()
    success = loader.delete_skill(name)
    if not success:
        raise HTTPException(status_code=404, detail=f"技能 '{name}' 不存在")
    _refresh_skill_tool()
    return {"message": f"技能 '{name}' 已删除"}


@router.post("/{name}/toggle")
async def toggle_skill(name: str):
    """切换技能启用状态"""
    loader = get_skill_loader()
    if name not in loader.list_skills():
        raise HTTPException(status_code=404, detail=f"技能 '{name}' 不存在")
    current = loader.is_enabled(name)
    loader.set_enabled(name, not current)
    _refresh_skill_tool()
    return {
        "message": f"技能 '{name}' 已{'禁用' if current else '启用'}",
        "enabled": not current,
    }


@router.post("/import", response_model=ImportResponse)
async def import_skill(request: ImportRequest):
    """导入技能

    支持两种导入方式：
    - source_type="path": 从本地目录复制
    - source_type="git": 从 Git 仓库克隆
    """
    loader = get_skill_loader()

    if not request.source or not request.source.strip():
        raise HTTPException(status_code=400, detail="source 不能为空")

    source = request.source.strip()

    if request.source_type == "path":
        # 展开 ~ 路径
        source = os.path.expanduser(source)
        skill = loader.import_from_path(source)
        if not skill:
            raise HTTPException(status_code=400, detail=f"无法从路径导入技能: {source}")
    elif request.source_type == "git":
        skill = loader.import_from_git(source)
        if not skill:
            raise HTTPException(
                status_code=400,
                detail=f"无法从 Git 仓库导入技能: {source}。请确保 Git 已安装且仓库地址正确。"
            )
    else:
        raise HTTPException(
            status_code=400,
            detail=f"不支持的导入类型: {request.source_type}，支持 'path' 和 'git'"
        )

    _refresh_skill_tool()

    return ImportResponse(
        message=f"技能 '{skill.name}' 导入成功",
        skill=SkillInfo(
            name=skill.name,
            description=skill.description,
            enabled=True,
            dir=str(skill.dir),
        ),
    )


@router.post("/reload")
async def reload_skills():
    """重新加载技能列表（热重载）"""
    loader = get_skill_loader()
    loader.reload()
    _refresh_skill_tool()
    return {"message": "技能列表已重新加载", "total": loader.total_count}
