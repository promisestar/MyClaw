"""Skill Manage Tool — Agent 驱动的 Skill 创建 / 修补 / 归档。

把成功经验沉淀为程序性记忆（SKILL.md），并在使用中持续改写。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from hello_agents.tools.base import Tool, ToolParameter
from hello_agents.tools.response import ToolResponse
from hello_agents.tools.errors import ToolErrorCode

from ...skills.loader import SkillLoader, _lint_skill_content
from ...skills.exceptions import SkillError
from ...skills.provenance import is_background_review


class SkillManageTool(Tool):
    """skill_manage：create / patch / edit / write_file / remove_file / delete / view"""

    def __init__(
        self,
        skill_loader: SkillLoader,
        *,
        on_changed: Optional[Callable[[], None]] = None,
        agent_created_default: bool = False,
    ):
        self.skill_loader = skill_loader
        self._on_changed = on_changed
        # 前台对话默认 False（用户资产）；Curator fork 可设 True
        self.agent_created_default = agent_created_default
        self._viewed_paths: set[str] = set()

        super().__init__(
            name="skill_manage",
            description=(
                "创建或更新可复用 Skill（程序性记忆：如何完成某类任务）。\n\n"
                "Create when：复杂任务成功（多轮工具）、错误被克服、用户纠正后路径有效、"
                "发现非平凡工作流、用户要求记住流程。\n"
                "Update when：说明过时/错误、平台相关失败、缺步骤或缺坑点——"
                "用了就立刻 patch（优先于整篇 edit）。\n"
                "Skip：简单一次性任务不必建 Skill；删除前宜与用户确认。\n\n"
                "动作：create | patch | edit | write_file | remove_file | delete | view。\n"
                "delete 默认归档（可恢复），不硬删。pin 的技能禁止 delete。"
            ),
            expandable=False,
        )
        self.output_size_hint = 800
        self.has_side_effects = True

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="action",
                type="string",
                description="create|patch|edit|write_file|remove_file|delete|view",
                required=True,
            ),
            ToolParameter(
                name="name",
                type="string",
                description="技能名（与 frontmatter.name / 目录名一致）",
                required=True,
            ),
            ToolParameter(
                name="content",
                type="string",
                description="create/edit 的 SKILL.md 全文，或 write_file 的文件内容",
                required=False,
                default="",
            ),
            ToolParameter(
                name="old_string",
                type="string",
                description="patch：要查找的原文",
                required=False,
                default="",
            ),
            ToolParameter(
                name="new_string",
                type="string",
                description="patch：替换后的文本",
                required=False,
                default="",
            ),
            ToolParameter(
                name="file_path",
                type="string",
                description=(
                    "相对技能目录的路径；默认 SKILL.md；"
                    "配套文件须在 scripts/references/examples/assets/templates 下"
                ),
                required=False,
                default="SKILL.md",
            ),
            ToolParameter(
                name="replace_all",
                type="boolean",
                description="patch 时是否替换全部匹配",
                required=False,
                default=False,
            ),
            ToolParameter(
                name="scope",
                type="string",
                description="create 层级：workspace（默认）或 global",
                required=False,
                default="workspace",
            ),
            ToolParameter(
                name="absorbed_into",
                type="string",
                description="Curator 合并删除时填写目标 umbrella 技能名；普通删除可省略",
                required=False,
                default="",
            ),
        ]

    def _refresh(self) -> None:
        if self._on_changed:
            try:
                self._on_changed()
            except Exception:
                pass

    def _guard_background_write(self, name: str, *, allow_create: bool = False) -> Optional[ToolResponse]:
        """Curator/后台路径写守卫。"""
        if not (self.agent_created_default or is_background_review()):
            return None
        if allow_create:
            return None
        if name not in self.skill_loader.metadata_cache:
            return self._err(f"技能 '{name}' 不存在", code="NOT_FOUND")
        store = self.skill_loader.usage_store_for(name)
        if store.is_pinned(name):
            return self._err(f"技能 '{name}' 已 pin，自主维护不可改写", code="PINNED")
        if not store.is_curator_managed(name):
            return self._err(
                f"技能 '{name}' 非 curator-managed；请先 adopt",
                code="NOT_CURATOR_MANAGED",
            )
        return None

    def _ok(self, payload: Dict[str, Any]) -> ToolResponse:
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        return ToolResponse.success(text=text, data=payload)

    def _err(
        self, message: str, *, code: str = "SKILL_ERROR", detail: Any = None
    ) -> ToolResponse:
        ctx: Dict[str, Any] = {"code": code}
        if detail is not None:
            ctx["detail"] = detail
        err_code = (
            ToolErrorCode.NOT_FOUND if code == "NOT_FOUND" else ToolErrorCode.INTERNAL_ERROR
        )
        if code in ("INVALID_PARAM", "INVALID_ACTION", "EMPTY_OLD_STRING"):
            err_code = ToolErrorCode.INVALID_PARAM
        return ToolResponse.error(code=err_code, message=message, context=ctx)

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        action = str(parameters.get("action") or "").strip().lower()
        name = str(parameters.get("name") or "").strip()
        if not action:
            return self._err("必须指定 action", code="INVALID_PARAM")
        if not name:
            return self._err("必须指定 name", code="INVALID_PARAM")

        try:
            if action == "view":
                return self._view(name, parameters.get("file_path") or "SKILL.md")
            if action == "create":
                return self._create(name, parameters)
            if action == "edit":
                return self._edit(name, parameters)
            if action == "patch":
                return self._patch(name, parameters)
            if action == "write_file":
                return self._write_file(name, parameters)
            if action == "remove_file":
                return self._remove_file(name, parameters)
            if action == "delete":
                return self._delete(name, parameters)
            return self._err(
                f"未知 action '{action}'；支持 create/patch/edit/write_file/remove_file/delete/view",
                code="INVALID_ACTION",
            )
        except SkillError as e:
            return self._err(e.message, code=e.code, detail=e.detail)
        except Exception as e:
            return self._err(f"skill_manage 失败：{e}", code="INTERNAL_ERROR", detail=str(e))

    def _view(self, name: str, file_path: str) -> ToolResponse:
        meta = self.skill_loader.metadata_cache.get(name)
        if not meta:
            return self._err(f"技能 '{name}' 不存在", code="NOT_FOUND")

        skill_dir = Path(meta["dir"])
        if not file_path or file_path in ("SKILL.md", ".", ""):
            target = skill_dir / "SKILL.md"
            rel = "SKILL.md"
        else:
            target = skill_dir / str(file_path)
            rel = str(file_path).replace("\\", "/")
            try:
                target.resolve().relative_to(skill_dir.resolve())
            except ValueError:
                return self._err("路径穿越被拒绝", code="PATH_TRAVERSAL")

        if not target.exists() or not target.is_file():
            return self._err(f"文件不存在：{rel}", code="NOT_FOUND")

        content = target.read_text(encoding="utf-8")
        self._viewed_paths.add(str(target.resolve()))
        self.skill_loader.usage_store_for(name).bump_view(name)

        resources: List[str] = []
        for folder in ("scripts", "references", "examples", "assets", "templates"):
            d = skill_dir / folder
            if d.is_dir():
                for f in d.rglob("*"):
                    if f.is_file():
                        resources.append(str(f.relative_to(skill_dir)).replace("\\", "/"))

        return self._ok(
            {
                "success": True,
                "action": "view",
                "name": name,
                "file_path": rel,
                "content": content,
                "resources": resources[:50],
                "source": meta.get("source"),
            }
        )

    def _create(self, name: str, parameters: Dict[str, Any]) -> ToolResponse:
        content = parameters.get("content") or ""
        if not str(content).strip():
            return self._err("create 需要 content（完整 SKILL.md）", code="INVALID_PARAM")
        scope = str(parameters.get("scope") or "workspace").strip().lower()
        if scope not in ("workspace", "global"):
            return self._err("scope 须为 workspace 或 global", code="INVALID_PARAM")

        agent_created = self.agent_created_default or is_background_review()
        created_name, lint = self.skill_loader.create_skill(name, content, scope=scope)
        store = self.skill_loader.usage_store_for(created_name, scope=scope)
        store.record_created(created_name, agent_created=agent_created)
        self._refresh()
        return self._ok(
            {
                "success": True,
                "action": "create",
                "name": created_name,
                "scope": scope,
                "agent_created": agent_created,
                "lint_warnings": lint,
            }
        )

    def _edit(self, name: str, parameters: Dict[str, Any]) -> ToolResponse:
        blocked = self._guard_background_write(name)
        if blocked:
            return blocked
        content = parameters.get("content") or ""
        if not str(content).strip():
            return self._err("edit 需要 content（完整 SKILL.md）", code="INVALID_PARAM")
        new_name = self.skill_loader.set_skill_content(name, content)
        self.skill_loader.usage_store_for(new_name).bump_patch(new_name)
        self._refresh()
        return self._ok(
            {
                "success": True,
                "action": "edit",
                "name": new_name,
                "lint_warnings": _lint_skill_content(content),
            }
        )

    def _patch(self, name: str, parameters: Dict[str, Any]) -> ToolResponse:
        blocked = self._guard_background_write(name)
        if blocked:
            return blocked
        old_string = parameters.get("old_string") or ""
        new_string = parameters.get("new_string")
        if new_string is None:
            new_string = ""
        if not old_string:
            return self._err("patch 需要 old_string", code="INVALID_PARAM")
        file_path = parameters.get("file_path") or "SKILL.md"
        replace_all = bool(parameters.get("replace_all") or False)

        before_names = set(self.skill_loader.metadata_cache.keys())
        rel, count, lint = self.skill_loader.patch_skill_file(
            name,
            old_string,
            new_string,
            file_path=file_path,
            replace_all=replace_all,
        )
        after_names = set(self.skill_loader.metadata_cache.keys())
        final_name = name
        if name not in after_names:
            added = after_names - before_names
            if len(added) == 1:
                final_name = next(iter(added))

        self.skill_loader.usage_store_for(final_name).bump_patch(final_name)
        self._refresh()
        return self._ok(
            {
                "success": True,
                "action": "patch",
                "name": final_name,
                "file_path": rel,
                "replacements": count,
                "lint_warnings": lint,
            }
        )

    def _write_file(self, name: str, parameters: Dict[str, Any]) -> ToolResponse:
        blocked = self._guard_background_write(name)
        if blocked:
            return blocked
        file_path = parameters.get("file_path") or ""
        content = parameters.get("content")
        if content is None:
            content = ""
        rel = self.skill_loader.write_skill_file(name, file_path, content)
        self.skill_loader.usage_store_for(name).bump_patch(name)
        self._refresh()
        return self._ok(
            {
                "success": True,
                "action": "write_file",
                "name": name,
                "file_path": rel,
            }
        )

    def _remove_file(self, name: str, parameters: Dict[str, Any]) -> ToolResponse:
        blocked = self._guard_background_write(name)
        if blocked:
            return blocked
        file_path = parameters.get("file_path") or ""
        rel = self.skill_loader.remove_skill_file(name, file_path)
        self.skill_loader.usage_store_for(name).bump_patch(name)
        self._refresh()
        return self._ok(
            {
                "success": True,
                "action": "remove_file",
                "name": name,
                "file_path": rel,
            }
        )

    def _delete(self, name: str, parameters: Dict[str, Any]) -> ToolResponse:
        absorbed = parameters.get("absorbed_into")
        background = self.agent_created_default or is_background_review()
        if background:
            if "absorbed_into" not in parameters:
                return self._err(
                    "自主维护删除必须提供 absorbed_into（目标 umbrella 名，或空字符串表示 prune）",
                    code="ABSORBED_INTO_REQUIRED",
                )
            if absorbed and absorbed not in self.skill_loader.metadata_cache:
                return self._err(
                    f"absorbed_into 目标 '{absorbed}' 不存在",
                    code="ABSORB_TARGET_MISSING",
                )
            blocked = self._guard_background_write(name)
            if blocked:
                return blocked

        archived_path = self.skill_loader.archive_skill(name)
        self._refresh()
        payload: Dict[str, Any] = {
            "success": True,
            "action": "delete",
            "name": name,
            "archived_to": archived_path,
        }
        if background:
            payload["absorbed_into"] = absorbed if absorbed is not None else ""
        return self._ok(payload)
