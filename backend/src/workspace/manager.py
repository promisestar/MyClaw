"""工作空间管理器"""

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional


# 配置文件列表
CONFIG_FILES = [
    "BOOTSTRAP",
    "IDENTITY",
    "SOUL",
    "USER",
    "AGENTS",
    "HEARTBEAT",
]

# 模板目录（相对于当前文件）
TEMPLATES_DIR = Path(__file__).parent / "templates"


def get_default_global_config() -> dict:
    """获取默认全局配置（从模板文件读取）"""
    template_path = TEMPLATES_DIR / "config.json"
    if template_path.exists():
        with open(template_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return {
        "llm": {"model_id": "", "base_url": "", "api_key": ""},
        "mcp": {"enabled": True, "builtin_demo": True, "servers": []},
    }


class WorkspaceManager:
    """工作空间管理器

    负责：
    - 创建和管理工作空间目录结构
    - 加载和保存配置文件
    - 管理记忆文件（每日记忆、长期记忆）
    """

    def __init__(self, workspace_path: str):
        """初始化工作空间管理器

        Args:
            workspace_path: 工作空间根目录路径
        """
        self.workspace_path = os.path.expanduser(workspace_path)
        self.sessions_path = os.path.join(self.workspace_path, "sessions")

    # ==================== 全局配置读取 ====================

    def load_global_config(self) -> dict:
        """加载全局 config.json

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
        """获取 LLM 配置

        优先级：config.json 非空值 > 环境变量 > 默认值

        Returns:
            包含 model_id, api_key, base_url 的字典
        """
        global_config = self.load_global_config()
        llm_config = global_config.get("llm", {})

        return {
            "model_id": llm_config.get("model_id") or os.getenv("LLM_MODEL_ID") or "glm-4",
            "api_key": llm_config.get("api_key") or os.getenv("LLM_API_KEY"),
            "base_url": llm_config.get("base_url") or os.getenv("LLM_BASE_URL"),
        }

    def get_mcp_config(self) -> Dict[str, Any]:
        """读取 MCP 工具相关配置（来自 ~/.helloclaw/config.json 的 `mcp` 段）。

        字段说明：
        - enabled: 是否注册任何 MCP 工具（默认 True）
        - builtin_demo: 当 servers 为空时，是否注册内置演示 MCPTool（默认 True）
        - servers: 外部 MCP 服务列表；非空时按条目各注册一个 MCPTool（此时不再注册内置演示，除非自行再加一条）

        每条 server 支持：
        - name: 工具注册名（建议唯一，如 github、fs）
        - server_url: 远程 MCP 地址（推荐 GitHub 托管：https://api.githubcopilot.com/mcp/）
        - server_command: 本地启动命令，如 ["npx","-y","@modelcontextprotocol/server-github"]
          （与 server_url 二选一；GitHub 场景请优先 server_url，工具更全）
        - transport_type: 远程传输类型，http（默认）或 sse
        - headers: 远程 MCP 请求头（也可用 env_keys 自动注入 Bearer PAT）
        - server_args: 可选附加参数列表
        - env: 可选，传给子进程的环境变量字典
        - env_keys: 可选，从当前进程环境读取的变量名列表
        - auto_expand: 可选，是否启动时全量展开远端工具（默认 False，渐进披露）
        - 渐进披露：auto_expand=false 时通过 action=enable_tools 按需注册 mcp_{name}_* 子工具
        """
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
        """若不存在则创建全局配置文件 ~/.helloclaw/config.json。

        工作区内的 AGENTS.md 等与「全局」config.json 是两套配置：前者在 workspace 目录，
        后者供 LLM/MCP 等读取；首次初始化工作区时应一并生成，便于用户编辑。

        若文件已存在则不做覆盖，避免丢失用户修改。
        """
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

    # ==================== 入职状态检测 ====================

    def is_onboarding_completed(self) -> bool:
        """检查入职是否完成

        入职完成的标志：BOOTSTRAP.md 不存在。
        同时会检查身份是否已确定，如果是则自动删除 BOOTSTRAP.md。

        Returns:
            入职是否已完成
        """
        # 先检查是否需要删除 BOOTSTRAP（身份已确定但文件还在）
        self._check_and_delete_bootstrap()

        return not os.path.exists(self.get_config_path("BOOTSTRAP"))

    def ensure_workspace_exists(self):
        """确保工作空间存在

        如果工作空间不存在，创建默认目录和配置文件
        """
        # 全局配置（~/.helloclaw/config.json）与 workspace 内 *.md 分开管理；首次一并创建
        self.ensure_global_config_exists()

        # 创建目录
        os.makedirs(self.workspace_path, exist_ok=True)
        os.makedirs(self.sessions_path, exist_ok=True)

        # 创建默认配置文件
        for config_name in CONFIG_FILES:
            config_path = self.get_config_path(config_name)
            if not os.path.exists(config_path):
                self._create_default_config(config_name)

        # 检查是否需要删除 BOOTSTRAP（遗留工作空间迁移）
        self._check_and_delete_bootstrap()

    def get_config_path(self, name: str) -> str:
        """获取配置文件路径

        Args:
            name: 配置文件名称（不含扩展名）

        Returns:
            配置文件完整路径
        """
        return os.path.join(self.workspace_path, f"{name}.md")

    def load_config(self, name: str) -> Optional[str]:
        """加载配置文件内容

        Args:
            name: 配置文件名称

        Returns:
            配置文件内容，如果不存在返回 None
        """
        config_path = self.get_config_path(name)
        if os.path.exists(config_path):
            with open(config_path, "r", encoding="utf-8") as f:
                return f.read()
        return None

    def save_config(self, name: str, content: str):
        """保存配置文件

        Args:
            name: 配置文件名称
            content: 配置文件内容
        """
        config_path = self.get_config_path(name)
        with open(config_path, "w", encoding="utf-8") as f:
            f.write(content)

        # 如果保存的是 IDENTITY，检查是否需要删除 BOOTSTRAP
        if name == "IDENTITY":
            self._check_and_delete_bootstrap()

    def list_configs(self) -> list:
        """列出所有配置文件

        Returns:
            配置文件名称列表
        """
        configs = []
        for name in CONFIG_FILES:
            config_path = self.get_config_path(name)
            if os.path.exists(config_path):
                configs.append(name)
        return configs

    def _check_and_delete_bootstrap(self):
        """检查身份是否已确定，如果是则删除 BOOTSTRAP.md"""
        bootstrap_path = self.get_config_path("BOOTSTRAP")

        # BOOTSTRAP 不存在，无需处理
        if not os.path.exists(bootstrap_path):
            return

        # 检查身份是否已确定
        if self._is_identity_established():
            os.remove(bootstrap_path)

    def _is_identity_established(self) -> bool:
        """检查身份是否已确定（名称字段有实际内容）

        Returns:
            身份是否已确定
        """
        identity = self.load_config("IDENTITY")
        if not identity:
            return False

        # 尝试匹配名称字段
        # 格式: - **名称：** xxx 或 - **名称:** xxx
        match = re.search(r'\*\*名称[：:]\*\*\s*(.+?)(?:\n|$)', identity)
        if match:
            name = match.group(1).strip()
            # 如果名称不是占位符，则认为身份已确定
            # 占位符特征：以下划线开头、包含"选一个"、包含"（"
            if name and not name.startswith('_') and '选一个' not in name and '（' not in name:
                return True

        return False

    def _create_default_config(self, name: str):
        """创建默认配置文件

        从模板文件读取内容，如果模板不存在则使用基础模板

        Args:
            name: 配置文件名称
        """
        template_path = TEMPLATES_DIR / f"{name}.md"

        if template_path.exists():
            with open(template_path, "r", encoding="utf-8") as f:
                content = f.read()
        else:
            # 回退到基础模板
            content = f"# {name}\n\n（待配置）"

        # 替换日期占位符
        content = content.replace("{date}", datetime.now().strftime("%Y-%m-%d"))

        self.save_config(name, content)

    def reset_to_templates(self, reset_sessions: bool = False, reset_global_config: bool = False):
        """重置工作空间到初始模板

        Args:
            reset_sessions: 是否清除会话
            reset_global_config: 是否重置全局配置

        警告：这将覆盖所有配置文件！
        """
        # 重置配置文件（包括 BOOTSTRAP）
        for config_name in CONFIG_FILES:
            self._create_default_config(config_name)

        # 清除会话
        if reset_sessions:
            self._clear_sessions()

        # 重置全局配置
        if reset_global_config:
            self._reset_global_config()

    def _clear_sessions(self):
        """清除所有会话"""
        if os.path.exists(self.sessions_path):
            for filename in os.listdir(self.sessions_path):
                if filename.endswith(".json"):
                    filepath = os.path.join(self.sessions_path, filename)
                    os.remove(filepath)

    def _reset_global_config(self):
        """重置全局配置文件"""
        config_path = os.path.expanduser("~/.helloclaw/config.json")
        os.makedirs(os.path.dirname(config_path), exist_ok=True)

        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(get_default_global_config(), f, indent=2, ensure_ascii=False)
