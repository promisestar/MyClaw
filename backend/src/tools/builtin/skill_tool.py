"""Skill Tool - 技能工具（自实现版）

允许 Agent 按需加载领域知识。

特性：
- 渐进式披露：仅在需要时加载完整技能
- 缓存友好：作为 tool_result 注入，不修改 system_prompt
- 资源提示：自动列出可用的脚本、文档、示例等
- 参数替换：支持 $ARGUMENTS 占位符
"""

from typing import Dict, Any, List

from hello_agents.tools.base import Tool, ToolParameter
from hello_agents.tools.response import ToolResponse
from hello_agents.tools.errors import ToolErrorCode

from ...skills.loader import SkillLoader


class SkillTool(Tool):
    """技能工具

    允许模型按需加载领域知识。
    """

    def __init__(self, skill_loader: SkillLoader):
        """初始化技能工具

        Args:
            skill_loader: 技能加载器实例
        """
        self.skill_loader = skill_loader

        # 生成动态描述
        descriptions = skill_loader.get_descriptions(only_enabled=True)

        super().__init__(
            name="Skill",
            description=f"""加载技能获取专业知识。

可用技能：
{descriptions}

何时使用：
- 任务明确匹配某个技能描述时，立即使用
- 开始领域特定工作之前
- 需要模型不具备的专业知识时

注意：加载技能后，请严格遵循技能说明来完成用户任务。""",
            expandable=False,
        )
        self.skill_loader = skill_loader

    def get_parameters(self) -> List[ToolParameter]:
        return [
            ToolParameter(
                name="skill",
                type="string",
                description="要加载的技能名称",
                required=True,
            ),
            ToolParameter(
                name="args",
                type="string",
                description="可选参数，将替换 SKILL.md 中的 $ARGUMENTS 占位符",
                required=False,
                default="",
            ),
        ]

    def run(self, parameters: Dict[str, Any]) -> ToolResponse:
        """执行技能加载

        Args:
            parameters: 包含 skill 和可选 args 的参数字典

        Returns:
            ToolResponse: 包含完整技能内容的响应
        """
        skill_name = parameters.get("skill", "")
        args = parameters.get("args", "")

        if not skill_name:
            return ToolResponse.error(
                code=ToolErrorCode.INVALID_PARAM,
                message="必须指定技能名称",
                context={"params_input": parameters},
            )

        if not self.skill_loader.is_enabled(skill_name):
            return ToolResponse.error(
                code=ToolErrorCode.NOT_FOUND,
                message=f"技能 '{skill_name}' 已被禁用。",
                context={
                    "params_input": parameters,
                    "available_skills": self.skill_loader.list_skills(only_enabled=True),
                },
            )

        try:
            skill = self.skill_loader.get_skill(skill_name)

            if not skill:
                available = ", ".join(
                    self.skill_loader.list_skills(only_enabled=True)
                )
                return ToolResponse.error(
                    code=ToolErrorCode.NOT_FOUND,
                    message=f"技能 '{skill_name}' 不存在。可用技能：{available}",
                    context={
                        "params_input": parameters,
                        "available_skills": self.skill_loader.list_skills(only_enabled=True),
                    },
                )

            # 替换 $ARGUMENTS 占位符
            content = skill.body.replace("$ARGUMENTS", args)

            # 列出可用资源（含完整路径）
            resources_hint = self._get_resources_hint(skill)

            # 构造执行环境提示
            python_hint = self._get_python_hint(skill)

            # 构造完整技能内容
            full_content = f"""<skill-loaded name="{skill_name}">
{content}
{resources_hint}
</skill-loaded>

✅ 技能已加载：{skill.name}
📝 描述：{skill.description}
📁 技能目录：{skill.dir}
{python_hint}

⚠️ 执行规则（必须遵守）：
1. 「可用资源」中已列出所有脚本和文档的完整路径，请直接使用这些路径，不要搜索文件系统。
2. 执行 Python 脚本时，**必须使用上方指定的 Python 解释器**，不要用 `python` 或 `python3` 这种依赖 PATH 的写法。
3. 如果脚本报 ModuleNotFoundError，说明依赖未安装到本技能的专属环境，请告知用户而不是自行 pip install。"""

            return ToolResponse.success(
                text=full_content,
                data={
                    "name": skill.name,
                    "description": skill.description,
                    "loaded": True,
                    "token_estimate": len(full_content),
                    "has_resources": bool(resources_hint),
                    "python_path": str(skill.python_path) if skill.python_path else None,
                },
            )

        except Exception as e:
            return ToolResponse.error(
                code=ToolErrorCode.INTERNAL_ERROR,
                message=f"加载技能失败：{str(e)}",
                context={"params_input": parameters, "error": str(e)},
            )

    def _get_python_hint(self, skill) -> str:
        """生成 Python 解释器使用提示

        Args:
            skill: Skill 对象

        Returns:
            提示文本（含完整解释器路径或回退说明）
        """
        if skill.python_path:
            return (
                f"🐍 专属 Python 解释器（已安装该技能所需依赖）：\n"
                f"    {skill.python_path}\n"
                f"    执行示例：\"{skill.python_path}\" \"{skill.dir}\\scripts\\<脚本名>.py\" [参数]"
            )
        if skill.has_dependencies:
            return (
                "⚠️ 该技能声明了依赖但未安装专属环境。\n"
                "    请通知用户调用技能管理页面的「重装依赖」按钮，或使用系统 Python 但可能缺少依赖。"
            )
        return "🐍 该技能未声明依赖，可使用系统 Python 执行（命令：python 或 python3）。"

    def _get_resources_hint(self, skill) -> str:
        """生成资源提示文本（含完整路径）

        Args:
            skill: Skill 对象

        Returns:
            格式化的资源提示文本，每个文件附带绝对路径
        """
        resources = []

        for folder, label in [
            ("scripts", "脚本"),
            ("references", "参考文档"),
            ("assets", "资源"),
            ("examples", "示例"),
        ]:
            folder_path = skill.dir / folder
            if folder_path.exists():
                files = [f for f in folder_path.rglob("*") if f.is_file()]
                if files:
                    # 列出完整路径，便于 Agent 直接使用
                    file_list = "\n".join(
                        f"    - {f}" for f in files[:10]
                    )
                    if len(files) > 10:
                        file_list += f"\n    - ... 等 {len(files)} 个文件"
                    resources.append(f"  - {label}（{folder_path}）：\n{file_list}")

        if not resources:
            return ""

        return "\n\n**可用资源**（以下为完整路径，请直接使用）：\n" + "\n".join(resources)

    def refresh_description(self):
        """刷新工具描述（技能列表变化时调用）"""
        descriptions = self.skill_loader.get_descriptions(only_enabled=True)
        self.description = f"""加载技能获取专业知识。

可用技能：
{descriptions}

何时使用：
- 任务明确匹配某个技能描述时，立即使用
- 开始领域特定工作之前
- 需要模型不具备的专业知识时

注意：加载技能后，请严格遵循技能说明来完成用户任务。"""
