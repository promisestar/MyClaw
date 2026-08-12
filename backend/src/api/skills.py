"""技能管理 API 路由

提供技能的 CRUD、导入、状态管理接口。

所有业务异常（SkillError 及其子类）会被自动转换为带结构化错误体的 HTTPException：
{
    "code": "INVALID_NAME" | "NAME_CONFLICT" | ...,
    "message": "用户可读消息",
    "detail": "可选细节"
}
"""

import logging
import os
from typing import Optional, List, Literal

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..skills import (
    SkillError,
    SkillImportError,
    SkillLoadError,
    SkillNameError,
    SkillConflictError,
    SkillNotFoundError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/skills", tags=["skills"])


# ── 全局 skill_loader / curator 引用 ──
_skill_loader = None
_skill_curator = None


def set_skill_loader(loader):
    """设置全局 SkillLoader 实例（在 main.py lifespan 中调用）"""
    global _skill_loader
    _skill_loader = loader


def set_skill_curator(curator):
    global _skill_curator
    _skill_curator = curator


def get_skill_loader():
    """获取全局 SkillLoader 实例"""
    if _skill_loader is None:
        raise HTTPException(status_code=500, detail="Skill 系统未初始化")
    return _skill_loader


def get_skill_curator():
    if _skill_curator is None:
        raise HTTPException(status_code=500, detail="Curator 未初始化")
    return _skill_curator


def _refresh_skill_tool():
    """刷新 Agent 的 SkillTool 描述（技能列表变化时调用）"""
    try:
        from ..main import get_agent
        agent = get_agent()
        if agent and hasattr(agent, "refresh_skill_tool"):
            agent.refresh_skill_tool()
    except Exception:
        logger.exception("刷新 SkillTool 描述失败（不影响主流程）")


# 异常 → HTTP 状态码映射
_EXC_STATUS_MAP = {
    SkillNotFoundError: 404,
    SkillNameError: 400,
    SkillConflictError: 409,
    SkillImportError: 400,
    SkillLoadError: 500,
}


def _raise_http_from_skill_error(exc: SkillError):
    """将 SkillError 转换为 HTTPException

    detail 字段使用 dict，保留结构化的 code / message / detail，前端可解析。
    """
    status = _EXC_STATUS_MAP.get(type(exc), 500)
    if exc.code in ("PINNED", "INVALID_PATH", "PATH_TRAVERSAL", "EMPTY_OLD_STRING", "AMBIGUOUS_MATCH"):
        status = 400
    logger.warning(
        "Skill API 业务异常 status=%s code=%s message=%s detail=%s",
        status, exc.code, exc.message, exc.detail,
    )
    raise HTTPException(status_code=status, detail=exc.to_dict())


# ── Pydantic 模型 ──

class SkillInfo(BaseModel):
    """技能信息"""
    name: str
    description: str
    enabled: bool
    dir: str
    source: str = "workspace"  # "global"（跨工作区共享）/ "workspace"（项目专属）
    has_venv: bool = False
    has_dependencies: bool = False
    python_path: Optional[str] = None
    use_count: int = 0
    patch_count: int = 0
    pinned: bool = False
    curator_managed: bool = False
    lifecycle_state: str = "active"


class InstallEnvResponse(BaseModel):
    """安装环境响应"""
    success: bool
    message: str
    python_path: Optional[str] = None
    log: str = ""


class SkillListResponse(BaseModel):
    """技能列表响应"""
    skills: List[SkillInfo]
    total: int
    enabled_count: int


class ImportRequest(BaseModel):
    """导入技能请求"""
    source_type: str  # "path" 或 "git"
    source: str       # 目录路径或 Git URL
    scope: Literal["global", "workspace"] = "workspace"  # "global"（用户级 ~/.helloclaw/skills/）或 "workspace"（项目级 .myclaw/skills/）


class ImportResponse(BaseModel):
    """导入技能响应"""
    message: str
    skill: Optional[SkillInfo] = None


class SkillContentResponse(BaseModel):
    """技能内容响应"""
    name: str
    content: str


class SkillContentUpdateRequest(BaseModel):
    """更新技能内容请求"""
    content: str


class SkillContentUpdateResponse(BaseModel):
    """更新技能内容响应"""
    message: str
    name: str  # 更新后的 name（如改名则为新 name）
    renamed: bool = False


class SkillDetailResponse(BaseModel):
    """技能详情响应"""
    name: str
    description: str
    body: str
    enabled: bool
    dir: str


class CuratorRunRequest(BaseModel):
    force: bool = False
    dry_run: bool = False


class ScopeRequest(BaseModel):
    scope: Literal["global", "workspace"] = "workspace"


# ── API 端点 ──

@router.get("", response_model=SkillListResponse)
async def list_skills():
    """列出所有技能"""
    loader = get_skill_loader()
    infos = loader.list_skill_infos()
    skills_out: List[SkillInfo] = []
    for info in infos:
        name = info["name"]
        store = loader.usage_store_for(name)
        rec = store.get_record(name)
        skills_out.append(
            SkillInfo(
                **info,
                use_count=int(rec.get("use_count") or 0),
                patch_count=int(rec.get("patch_count") or 0),
                pinned=bool(rec.get("pinned")),
                curator_managed=store.is_curator_managed(name),
                lifecycle_state=str(rec.get("state") or "active"),
            )
        )
    return SkillListResponse(
        skills=skills_out,
        total=loader.total_count,
        enabled_count=loader.enabled_count,
    )


@router.get("/usage")
async def get_usage():
    """返回工作区 + 全局 usage 报告。"""
    loader = get_skill_loader()
    workspace = loader._usage_store.usage_report()
    for row in workspace:
        row["scope"] = "workspace"
    global_rows = []
    if loader._global_usage_store:
        global_rows = loader._global_usage_store.usage_report()
        for row in global_rows:
            row["scope"] = "global"
    return {"usage": workspace + global_rows}


@router.get("/archived")
async def list_archived(scope: Literal["global", "workspace"] = "workspace"):
    loader = get_skill_loader()
    return {"archived": loader.list_archived(scope=scope), "scope": scope}


@router.get("/curator/status")
async def curator_status():
    return get_skill_curator().status()


@router.post("/curator/run")
async def curator_run(request: CuratorRunRequest):
    result = get_skill_curator().run(force=request.force, dry_run=request.dry_run)
    _refresh_skill_tool()
    return result


@router.post("/curator/pause")
async def curator_pause():
    get_skill_curator().set_paused(True)
    return {"paused": True}


@router.post("/curator/resume")
async def curator_resume():
    get_skill_curator().set_paused(False)
    return {"paused": False}


@router.post("/{name}/pin")
async def pin_skill(name: str):
    loader = get_skill_loader()
    if name not in loader.metadata_cache:
        _raise_http_from_skill_error(SkillNotFoundError(f"技能 '{name}' 不存在"))
    loader.usage_store_for(name).set_pinned(name, True)
    return {"message": f"技能 '{name}' 已 pin", "pinned": True}


@router.post("/{name}/unpin")
async def unpin_skill(name: str):
    loader = get_skill_loader()
    if name not in loader.metadata_cache:
        _raise_http_from_skill_error(SkillNotFoundError(f"技能 '{name}' 不存在"))
    loader.usage_store_for(name).set_pinned(name, False)
    return {"message": f"技能 '{name}' 已 unpin", "pinned": False}


@router.post("/{name}/adopt")
async def adopt_skill(name: str):
    """将用户技能纳入 Curator 自主维护（created_by=agent）。"""
    loader = get_skill_loader()
    if name not in loader.metadata_cache:
        _raise_http_from_skill_error(SkillNotFoundError(f"技能 '{name}' 不存在"))
    loader.usage_store_for(name).mark_agent_created(name)
    return {"message": f"技能 '{name}' 已 adopt 为 curator-managed", "curator_managed": True}


@router.post("/{name}/archive")
async def archive_skill_api(name: str):
    loader = get_skill_loader()
    try:
        path = loader.archive_skill(name)
    except SkillError as e:
        _raise_http_from_skill_error(e)
    _refresh_skill_tool()
    return {"message": f"技能 '{name}' 已归档", "archived_to": path}


@router.post("/{name}/restore")
async def restore_skill_api(name: str, request: ScopeRequest = ScopeRequest()):
    loader = get_skill_loader()
    try:
        path = loader.restore_skill(name, scope=request.scope)
    except SkillError as e:
        _raise_http_from_skill_error(e)
    _refresh_skill_tool()
    return {"message": f"技能 '{name}' 已恢复", "restored_to": path, "scope": request.scope}


@router.get("/{name}", response_model=SkillDetailResponse)
async def get_skill(name: str):
    """获取技能详情"""
    loader = get_skill_loader()
    skill = loader.get_skill(name)
    if not skill:
        _raise_http_from_skill_error(SkillNotFoundError(f"技能 '{name}' 不存在"))
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
    try:
        content = loader.get_skill_content(name)
    except SkillError as e:
        _raise_http_from_skill_error(e)
    return SkillContentResponse(name=name, content=content)


@router.put("/{name}/content", response_model=SkillContentUpdateResponse)
async def update_skill_content(name: str, request: SkillContentUpdateRequest):
    """更新 SKILL.md 内容

    如果新内容的 frontmatter.name 改变，会触发目录重命名。
    返回的 name 字段为更新后的最终名称。
    """
    loader = get_skill_loader()
    try:
        new_name = loader.set_skill_content(name, request.content)
    except SkillError as e:
        _raise_http_from_skill_error(e)

    _refresh_skill_tool()
    renamed = new_name != name
    msg = (
        f"技能内容已更新，并重命名为 '{new_name}'"
        if renamed else f"技能 '{name}' 内容已更新"
    )
    return SkillContentUpdateResponse(message=msg, name=new_name, renamed=renamed)


@router.delete("/{name}")
async def delete_skill(name: str):
    """硬删除技能（人类 UI）。pin 技能拒绝；全局技能仍由 loader 拒绝。"""
    loader = get_skill_loader()
    if name not in loader.list_skills():
        _raise_http_from_skill_error(SkillNotFoundError(f"技能 '{name}' 不存在"))

    store = loader.usage_store_for(name)
    if store.is_pinned(name):
        _raise_http_from_skill_error(
            SkillError(f"技能 '{name}' 已 pin，禁止删除", code="PINNED")
        )

    success = loader.delete_skill(name)
    if not success:
        raise HTTPException(
            status_code=500,
            detail={
                "code": "DELETE_FAILED",
                "message": f"删除技能 '{name}' 失败，详见后端日志",
                "detail": None,
            },
        )

    store.forget(name)
    _refresh_skill_tool()
    return {"message": f"技能 '{name}' 已删除"}


@router.post("/{name}/toggle")
async def toggle_skill(name: str):
    """切换技能启用状态"""
    loader = get_skill_loader()
    if name not in loader.list_skills():
        _raise_http_from_skill_error(SkillNotFoundError(f"技能 '{name}' 不存在"))
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
        raise HTTPException(
            status_code=400,
            detail={
                "code": "EMPTY_SOURCE",
                "message": "source 不能为空",
                "detail": None,
            },
        )

    source = request.source.strip()

    try:
        if request.source_type == "path":
            source = os.path.expanduser(source)
            skill = loader.import_from_path(source, scope=request.scope)
        elif request.source_type == "git":
            skill = loader.import_from_git(source, scope=request.scope)
        else:
            raise HTTPException(
                status_code=400,
                detail={
                    "code": "INVALID_SOURCE_TYPE",
                    "message": f"不支持的导入类型 '{request.source_type}'，支持 'path' 和 'git'",
                    "detail": None,
                },
            )
    except SkillError as e:
        _raise_http_from_skill_error(e)
    except Exception as e:
        # 兜底：未预期的异常
        logger.exception("导入技能时发生未预期错误 source=%s", source)
        raise HTTPException(
            status_code=500,
            detail={
                "code": "UNEXPECTED_ERROR",
                "message": "导入失败，详见后端日志",
                "detail": str(e),
            },
        )

    _refresh_skill_tool()

    return ImportResponse(
        message=f"技能 '{skill.name}' 导入成功",
        skill=SkillInfo(
            name=skill.name,
            description=skill.description,
            enabled=True,
            dir=str(skill.dir),
            source=request.scope,
            has_venv=skill.python_path is not None,
            has_dependencies=skill.has_dependencies,
            python_path=str(skill.python_path) if skill.python_path else None,
        ),
    )


@router.post("/reload")
async def reload_skills():
    """重新加载技能列表（热重载）"""
    loader = get_skill_loader()
    loader.reload()
    _refresh_skill_tool()
    return {"message": "技能列表已重新加载", "total": loader.total_count}


@router.post("/{name}/install-env", response_model=InstallEnvResponse)
async def install_skill_env(name: str):
    """为指定技能创建/重建专属 venv 并安装依赖"""
    loader = get_skill_loader()
    try:
        ok, log, python_path = loader.install_skill_env(name)
    except SkillError as e:
        _raise_http_from_skill_error(e)

    return InstallEnvResponse(
        success=ok,
        message=f"技能 '{name}' 环境{'安装成功' if ok else '安装失败'}",
        python_path=str(python_path) if python_path else None,
        log=log,
    )
