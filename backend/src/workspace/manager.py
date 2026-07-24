"""工作空间管理器 (V2) — 仅管理项目工作区的 .myclaw/ 子目录。

身份/人格文件（IDENTITY/SOUL/USER/BOOTSTRAP）已迁移到 IdentityManager，
本模块只负责工作区级文件（AGENTS/HEARTBEAT）与子目录（sessions/tasks/uploads/skills）。
所有工作区文件统一归拢到 `<workspace>/.myclaw/` 隐藏子目录，不污染项目根目录。
"""

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


# 模板目录（相对于当前文件：workspace/templates/）
TEMPLATES_DIR = Path(__file__).parent / "templates"

# 工作区模板目录
WORKSPACE_TEMPLATES_DIR = TEMPLATES_DIR / "workspace"

# 工作区级 .myclaw/ 子目录
WORKSPACE_SUBDIRS = ["sessions", "tasks", "uploads", "skills"]

# 工作区级必需配置文件（不存在时从模板部署）
WORKSPACE_CONFIG_FILES = ["AGENTS"]

# 工作区级可选配置文件（不存在不报错，存在时从模板部署）
WORKSPACE_OPTIONAL_FILES = ["HEARTBEAT"]

# .gitignore 中自动追加的条目（幂等）
_GITIGNORE_ENTRIES = [
    "# MyClaw Agent 工作区文件 (自动生成)",
    ".myclaw/sessions/",
    ".myclaw/tasks/",
    ".myclaw/uploads/",
    ".myclaw/HEARTBEAT.md",
    "# 以下文件可选择提交到 Git 以与团队共享：",
    "# .myclaw/AGENTS.md      <- 项目行为规范，可选择性提交",
    "# .myclaw/skills/         <- 项目专属 Skill，可选择性提交",
]


def get_default_global_config() -> dict:
    """获取默认全局配置（从模板文件读取）。"""
    template_path = TEMPLATES_DIR / "config.json"
    if template_path.exists():
        with open(template_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "llm": {"model_id": "", "base_url": "", "api_key": ""},
        "mcp": {"enabled": True, "builtin_demo": True, "servers": []},
    }


class WorkspaceManager:
    """管理单个项目工作区的 .myclaw/ 子目录。

    负责：
    - 创建和管理 .myclaw/ 目录结构（sessions/tasks/uploads/skills）
    - 加载和保存工作区级配置文件（AGENTS.md / HEARTBEAT.md）
    - 全局 config.json 的读取（LLM/MCP 配置，与工作区无关）
    """

    def __init__(self, workspace_path: str):
        """初始化工作空间管理器。

        Args:
            workspace_path: 项目工作区根目录路径
        """
        self.workspace_path = os.path.abspath(os.path.expanduser(workspace_path))
        # 所有 Agent 文件归拢到 .myclaw/ 隐藏子目录
        self.claw_dir = os.path.join(self.workspace_path, ".myclaw")

    # ==================== 路径属性 ====================

    @property
    def sessions_path(self) -> str:
        return os.path.join(self.claw_dir, "sessions")

    @property
    def tasks_path(self) -> str:
        return os.path.join(self.claw_dir, "tasks")

    @property
    def uploads_path(self) -> str:
        return os.path.join(self.claw_dir, "uploads")

    @property
    def skills_path(self) -> str:
        return os.path.join(self.claw_dir, "skills")

    # ==================== 全局配置读取 ====================

    def load_global_config(self) -> dict:
        """加载全局 config.json（~/.helloclaw/config.json，与工作区无关）。

        Returns:
            配置字典，如果文件不存在返回空字典
        """
        config_path = os.path.expanduser("~/.helloclaw/config.json")
        if os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, IOError):
                return {}
        return {}

    def get_llm_config(self) -> dict:
        """获取 LLM 配置。

        优先级：config.json 非空值 > 环境变量 > 默认值
        """
        global_config = self.load_global_config()
        llm_config = global_config.get("llm", {})

        return {
            "model_id": llm_config.get("model_id") or os.getenv("LLM_MODEL_ID") or "glm-4",
            "api_key": llm_config.get("api_key") or os.getenv("LLM_API_KEY"),
            "base_url": llm_config.get("base_url") or os.getenv("LLM_BASE_URL"),
        }

    def get_mcp_config(self) -> Dict[str, Any]:
        """读取 MCP 工具相关配置（来自 ~/.helloclaw/config.json 的 `mcp` 段）。"""
        defaults: Dict[str, Any] = {
            "enabled": True,
            "builtin_demo": True,
            "servers": [],
        }
        global_config = self.load_global_config()
        raw = global_config.get("mcp")
        if not isinstance(raw, dict):
            return dict(defaults)
        merged = {**defaults, **raw}
        if not isinstance(merged.get("servers"), list):
            merged["servers"] = []
        return merged

    def ensure_global_config_exists(self) -> None:
        """若不存在则创建全局配置文件 ~/.helloclaw/config.json（不覆盖已有）。"""
        config_path = os.path.expanduser("~/.helloclaw/config.json")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        if os.path.exists(config_path):
            return
        try:
            with open(config_path, "w", encoding="utf-8") as f:
                json.dump(get_default_global_config(), f, indent=2, ensure_ascii=False)
            print(f"📝 已创建全局配置: {config_path}")
        except OSError as e:
            print(f"⚠️ 无法写入全局配置 {config_path}: {e}")

    # ==================== 工作区部署 ====================

    def _deploy_from_template(self, name: str):
        """从 workspace 模板部署单个文件到 .myclaw/（不覆盖已有）。

        Args:
            name: 配置文件名（不含 .md 后缀）
        """
        target = os.path.join(self.claw_dir, f"{name}.md")
        if os.path.exists(target):
            return
        src = WORKSPACE_TEMPLATES_DIR / f"{name}.md"
        if src.exists():
            with open(src, "r", encoding="utf-8") as f:
                content = f.read()
            # 替换日期占位符
            content = content.replace("{date}", datetime.now().strftime("%Y-%m-%d"))
            os.makedirs(self.claw_dir, exist_ok=True)
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"📝 已从模板部署: {target}")
        else:
            # 模板缺失时跳过（工作区配置文件是可选的）
            print(f"⚠️ 模板不存在，跳过部署: {src}")

    def ensure_project_workspace(self):
        """Phase 2 部署：确保 .myclaw/ 目录结构存在。

        每次切换到新工作区时调用（首次部署模板文件，后续仅确保目录存在）。
        """
        # 创建 .myclaw/ 子目录
        for subdir in WORKSPACE_SUBDIRS:
            os.makedirs(os.path.join(self.claw_dir, subdir), exist_ok=True)

        # 部署必需的工作区配置文件
        for name in WORKSPACE_CONFIG_FILES:
            self._deploy_from_template(name)

        # 部署可选配置文件
        for name in WORKSPACE_OPTIONAL_FILES:
            self._deploy_from_template(name)

        # 自动注入 .gitignore
        self._ensure_gitignore()

    def _ensure_gitignore(self):
        """幂等追加 .myclaw/ 相关条目到项目根目录的 .gitignore。"""
        gitignore_path = os.path.join(self.workspace_path, ".gitignore")
        existing = ""
        if os.path.exists(gitignore_path):
            with open(gitignore_path, "r", encoding="utf-8") as f:
                existing = f.read()

        # 找出尚未存在的条目
        missing = [line for line in _GITIGNORE_ENTRIES if line not in existing]
        if not missing:
            return

        # 追加（前面确保有空行分隔）
        with open(gitignore_path, "a", encoding="utf-8") as f:
            if existing and not existing.endswith("\n"):
                f.write("\n")
            if existing and not existing.endswith("\n\n"):
                f.write("\n")
            f.write("\n".join(missing) + "\n")
        print(f"📝 已更新 .gitignore: {gitignore_path}")

    # ==================== 工作区配置读写 ====================

    def get_config_path(self, name: str) -> str:
        """获取工作区配置文件路径（.myclaw/<name>.md）。"""
        return os.path.join(self.claw_dir, f"{name}.md")

    def load_config(self, name: str) -> Optional[str]:
        """加载工作区配置文件内容。

        Args:
            name: 配置文件名称（不含扩展名）

        Returns:
            配置文件内容，如果不存在返回 None（工作区 AGENTS.md 是可选的）
        """
        config_path = self.get_config_path(name)
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                return f.read()
        return None

    def save_config(self, name: str, content: str):
        """保存工作区配置文件。"""
        os.makedirs(self.claw_dir, exist_ok=True)
        config_path = self.get_config_path(name)
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(content)

    def list_configs(self) -> list:
        """列出当前工作区 .myclaw/ 下已存在的配置文件名。"""
        configs = []
        for name in WORKSPACE_CONFIG_FILES + WORKSPACE_OPTIONAL_FILES:
            if os.path.exists(self.get_config_path(name)):
                configs.append(name)
        return configs

    # ==================== 重置与清理 ====================

    def _clear_sessions(self):
        """清除 .myclaw/sessions/ 下所有会话。"""
        if os.path.exists(self.sessions_path):
            for filename in os.listdir(self.sessions_path):
                if filename.endswith(".json"):
                    filepath = os.path.join(self.sessions_path, filename)
                    os.remove(filepath)

    def reset_to_templates(self, reset_sessions: bool = False, reset_global_config: bool = False):
        """重置当前工作区的 .myclaw/ 配置文件到初始模板。

        Args:
            reset_sessions: 是否清除会话
            reset_global_config: 是否重置全局配置（~/.helloclaw/config.json）

        警告：这将覆盖工作区配置文件！
        """
        # 重置工作区配置文件
        for name in WORKSPACE_CONFIG_FILES + WORKSPACE_OPTIONAL_FILES:
            target = os.path.join(self.claw_dir, f"{name}.md")
            if os.path.exists(target):
                os.remove(target)
            self._deploy_from_template(name)

        # 清除会话
        if reset_sessions:
            self._clear_sessions()

        # 重置全局配置
        if reset_global_config:
            self._reset_global_config()

    def _reset_global_config(self):
        """重置全局配置文件。"""
        config_path = os.path.expanduser("~/.helloclaw/config.json")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(get_default_global_config(), f, indent=2, ensure_ascii=False)
