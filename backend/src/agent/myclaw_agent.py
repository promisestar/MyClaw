"""HelloClaw Agent - 基于 HelloAgents SimpleAgent 的个性化 AI 助手"""

import os
import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

from hello_agents import Config
from hello_agents.core.message import Message
from .enhanced_simple_agent import EnhancedSimpleAgent
from .cancel_token import CancellationToken
from .enhanced_llm import EnhancedHelloAgentsLLM  # HelloClaw 专用 LLM（支持流式工具调用）
from .multimodal_bridge import (
    encode_multimodal_content,
    decode_multimodal_content,
    is_encoded_multimodal,
    install_simple_agent_multimodal_patch,
)
from ..memory.memory_flush import MemoryFlushManager
from ..memory.capture import MemoryCaptureManager
from ..memory.vector_store import MemoryVectorStore
from ..multimodal import MultimodalConfig, build_user_content

# 安装一次性 patch：让 EnhancedSimpleAgent 在 _build_messages 时解码多模态内容
install_simple_agent_multimodal_patch()
from hello_agents.tools import (
    ToolRegistry,
    ReadTool,
    WriteTool,
    EditTool,
    CalculatorTool,
)

from ..workspace.manager import WorkspaceManager
from ..workspace.identity import IdentityManager
from ..tools import MemoryTool, BashTool, WebSearchTool, WebFetchTool, RAGTool, MCPTool
from ..tools import SearchContentTool, SearchFileTool, ListDirTool
from ..tools import HttpRequestTool
from ..tools import BrowserTool, BrowserSession
from ..tools import AutomationTool
from ..automation import AutomationStore
from ..tools.builtin.mcp_tool import reset_all_mcp_disclosed_tools
from ..tools.builtin.skill_tool import SkillTool
from ..skills.loader import SkillLoader
from ..core.timeouts import TimeoutConfig, get_timeout_config

# SubAgent 编排器 + 任务追踪器
from .subagent_orchestrator import SubAgentOrchestrator, SubAgentTask, SubAgentResultMode
from .task_tracker import TaskTracker
from ..tools.builtin.subagent_tool import SubAgentTool
from ..tools.builtin.task_tool import TaskTool


class MyClawAgent:
    """HelloClaw Agent - 个性化 AI 助手

    基于 HelloAgents SimpleAgent，增加了：
    - 工作空间管理（配置文件、记忆文件）
    - 从 AGENTS.md 读取系统提示词
    - HelloClaw 专属工具集
    """

    def __init__(
        self,
        home_path: str = "~/.helloclaw",
        workspace_path: str = "~",
        name: str = None,
        model_id: str = None,
        api_key: str = None,
        base_url: str = None,
        max_tool_iterations: int = 15,
        max_tool_retries: Optional[int] = None,
    ):
        """初始化 HelloClaw Agent

        Args:
            home_path: Agent 基座目录，默认 ~/.helloclaw（进程固定，永不可切换）
            workspace_path: 默认工作区路径，默认 ~（用户家目录）
            name: Agent 名称（从 IDENTITY.md 读取，无需手动指定）
            model_id: LLM 模型 ID
            api_key: API Key
            base_url: API Base URL
            max_tool_iterations: 最大工具调用迭代次数
            max_tool_retries: 工具调用失败最大重试次数（None=从 config.json 读取，默认 2）
        """
        # Agent 基座目录（进程固定，存放 identity/全局配置）
        self.home_path = os.path.expanduser(home_path)

        # Phase 1：初始化身份管理器并部署 identity 基座（含 V1→V2 迁移）
        self.identity = IdentityManager(self.home_path)
        old_ws = os.path.expanduser("~/.helloclaw/workspace")
        self.identity.ensure_exists(
            old_workspace=old_ws if os.path.isdir(old_ws) else None
        )

        # 确保基座 AGENTS.md fallback 存在（工作区无 AGENTS.md 时的 prompt 骨架）
        self._ensure_base_agents_md()

        # 当前工作区（运行时可通过 bind_workspace 切换）
        self._current_workspace = os.path.abspath(
            os.path.expanduser(workspace_path or "~")
        )

        # 初始化工作空间管理器（管理 .myclaw/ 子目录）
        self.workspace = WorkspaceManager(self._current_workspace)
        self.workspace.ensure_global_config_exists()
        self.workspace.ensure_project_workspace()  # Phase 2 部署

        # 保留 workspace_path 属性（供 SubAgentOrchestrator 等外部引用，指向当前工作区）
        self.workspace_path = self._current_workspace

        # 编辑/重新生成时暂存该轮之后的对话，跑完新回复后再拼回
        self._resend_suffix: List[Message] = []

        # 加载模块级超时配置（env > config.json > 默认值）
        self._timeout_config = get_timeout_config()

        # 从 IDENTITY.md 读取名称（从基座 identity 目录），如果没有则使用默认值
        self.name = name or self._read_identity_name() or "HelloClaw"

        # 保存传入的参数（用于热加载时的优先级判断）
        self._override_model_id = model_id
        self._override_api_key = api_key
        self._override_base_url = base_url

        # 构建系统提示词（从 AGENTS.md 读取）
        system_prompt = self._build_system_prompt()

        # 初始化 LLM（从 config.json 读取配置）
        self._init_llm()

        # ── 读取工具重试配置（从 config.json） ──
        if max_tool_retries is None:
            global_config = self.workspace.load_global_config()
            retry_cfg = global_config.get("tool_retry", {})
            max_tool_retries = retry_cfg.get("max_retries", 2)
            tool_retry_base_delay = retry_cfg.get("base_delay", 1.0)
            tool_retry_max_delay = retry_cfg.get("max_delay", 15.0)
            tool_retry_backoff = retry_cfg.get("backoff", 2.0)
            tool_retry_jitter = retry_cfg.get("jitter", 0.2)
        else:
            tool_retry_base_delay = 1.0
            tool_retry_max_delay = 15.0
            tool_retry_backoff = 2.0
            tool_retry_jitter = 0.2

        # 初始化配置
        self.config = Config(
            session_enabled=True,
            session_dir=self.workspace.sessions_path,
            compression_threshold=0.8,
            min_retain_rounds=10,
            enable_smart_compression=False,
            context_window=128000,
            trace_enabled=False,
            skills_enabled=False,  # 使用自实现的 SkillLoader（不依赖 hello_agents）
            skills_auto_register=False,
            todowrite_enabled=False,
            devlog_enabled=False,
            subagent_enabled=False,  # 使用自实现的 SubAgentOrchestrator（不依赖 hello_agents）
        )

        # 初始化自实现的 Skill 系统（支持全局 + 工作区双目录）
        global_skills_dir = Path(self.home_path) / "skills"
        self.skill_loader = SkillLoader(
            skills_dir=Path(self.workspace.skills_path),
            global_dir=global_skills_dir,
        )

        # 初始化 MemoryVectorStore（长期记忆的 Qdrant 存储层）
        # 必须在 _setup_tools() 之前，因为 MemoryTool 依赖它
        self._memory_store = MemoryVectorStore()

        # 初始化子代理编排器（延迟创建，在 _setup_tools 中实例化）
        self._subagent_orchestrator = None

        # 初始化任务追踪器（带持久化）
        self._task_tracker = TaskTracker(persist_dir=self.workspace.tasks_path)

        # 浏览器会话（在 _setup_tools 中实际创建，此处预声明供 shutdown 安全引用）
        self._browser_session = None

        # 定时任务存储（在 _setup_tools 中实际创建，此处预声明供 main.py 引用）
        self._automation_store = None

        # 初始化工具注册表
        self.tool_registry = self._setup_tools()

        # 初始化底层 EnhancedSimpleAgent
        self._agent = EnhancedSimpleAgent(
            name=self.name,  # 使用已读取的名字
            llm=self._llm,
            tool_registry=self.tool_registry,
            system_prompt=system_prompt,
            config=self.config,
            enable_tool_calling=True,
            max_tool_iterations=max_tool_iterations,
            workspace_root=self._current_workspace,
            auto_cleanup_temp_files=True,
            max_tool_retries=max_tool_retries,
            tool_retry_base_delay=tool_retry_base_delay,
            tool_retry_max_delay=tool_retry_max_delay,
            tool_retry_backoff=tool_retry_backoff,
            tool_retry_jitter=tool_retry_jitter,
            max_tools_per_round=5,
            subagent_orchestrator=self._subagent_orchestrator,
            timeout_config=self._timeout_config,
        )

        # 注入模型信息到 ContextManager，启用精确 token 统计
        if hasattr(self._agent, "context_manager") and self._agent.context_manager:
            self._agent.context_manager.set_model_context(
                model=self._model_id,
                base_url=self._base_url,
            )

        # 更新 ContextGuard 的工具元数据（元数据驱动路由）
        if hasattr(self._agent, '_context_guard') and self._agent._context_guard:
            self._agent._context_guard.set_tool_registry(self.tool_registry)

        # 初始化 Memory Flush 管理器
        self._memory_flush_manager = MemoryFlushManager(
            context_window=self.config.context_window,
            compression_threshold=self.config.compression_threshold,
            soft_threshold_tokens=4000,
            enabled=True,
        )

        # 此时 config / ContextManager / MemoryFlushManager 均已就绪，
        # 同步动态上下文窗口（覆盖 _init_llm 中因 config 尚未创建而跳过的首次调用）
        self._sync_context_window()

        # 初始化 Memory Capture 管理器（传入 memory_store）
        self._memory_capture_manager = MemoryCaptureManager(
            memory_store=self._memory_store,
            workspace_manager=self.workspace,  # 过渡期回退
        )

    def _ensure_base_agents_md(self):
        """确保基座 AGENTS.md fallback 存在。

        从 templates/workspace/AGENTS.md 复制一份到 ~/.helloclaw/AGENTS.md，
        作为工作区无 AGENTS.md 时的 prompt 骨架。不覆盖已有。
        """
        base_agents_path = os.path.join(self.home_path, "AGENTS.md")
        if os.path.exists(base_agents_path):
            return
        from ..workspace.manager import WORKSPACE_TEMPLATES_DIR
        src = WORKSPACE_TEMPLATES_DIR / "AGENTS.md"
        if src.exists():
            os.makedirs(self.home_path, exist_ok=True)
            import shutil
            shutil.copy2(src, base_agents_path)
            print(f"📝 已部署基座 AGENTS.md fallback: {base_agents_path}")

    def _read_identity_name(self) -> str:
        """从 IDENTITY.md 读取助手名称（从基座 identity 目录）。

        Returns:
            助手名称，如果未设置则返回 None
        """
        return self.identity.read_name()

    def _init_llm(self):
        """初始化 LLM（从 config.json 读取配置）

        配置优先级：构造函数参数 > config.json > 环境变量 > 默认值
        """
        llm_config = self.workspace.get_llm_config()

        self._model_id = self._override_model_id or llm_config.get("model_id") or "glm-4"
        self._api_key = self._override_api_key or llm_config.get("api_key")
        self._base_url = self._override_base_url or llm_config.get("base_url")

        self._llm = EnhancedHelloAgentsLLM(
            model=self._model_id,
            api_key=self._api_key,
            base_url=self._base_url,
            timeout_config=self._timeout_config,
        )

        # 动态感知模型的实际上下文窗口
        self._sync_context_window()

    def _reload_llm_if_changed(self) -> bool:
        """检查配置变化并重新加载 LLM

        如果 config.json 中的配置发生变化，重新创建 LLM 实例。

        Returns:
            是否发生了重新加载
        """
        llm_config = self.workspace.get_llm_config()

        new_model_id = self._override_model_id or llm_config.get("model_id") or "glm-4"
        new_api_key = self._override_api_key or llm_config.get("api_key")
        new_base_url = self._override_base_url or llm_config.get("base_url")

        if (new_model_id != self._model_id or
            new_api_key != self._api_key or
            new_base_url != self._base_url):

            print(f"🔄 检测到配置变化，重新加载 LLM: {self._model_id} -> {new_model_id}")

            # 关闭旧 LLM 的异步客户端（释放 httpx 连接池）
            if self._llm and hasattr(self._llm, 'close_async_client'):
                try:
                    asyncio.get_event_loop().create_task(self._llm.close_async_client())
                except Exception:
                    pass

            self._model_id = new_model_id
            self._api_key = new_api_key
            self._base_url = new_base_url

            self._llm = EnhancedHelloAgentsLLM(
                model=self._model_id,
                api_key=self._api_key,
                base_url=self._base_url,
                timeout_config=self._timeout_config,
            )

            # 更新 Agent 的 LLM 引用
            if hasattr(self, '_agent'):
                self._agent.llm = self._llm

            # 同步 tokenizer 模型信息
            if hasattr(self._agent, "context_manager") and self._agent.context_manager:
                self._agent.context_manager.set_model_context(
                    model=self._model_id,
                    base_url=self._base_url,
                )

            # 动态感知新模型的上下文窗口
            self._sync_context_window()

            return True
        return False

    def _sync_context_window(self):
        """根据当前 LLM 模型动态感知上下文窗口大小。

        调用 tokenizer.get_context_window() 查询模型注册表，
        将结果同步到 Config、ContextManager 和 MemoryFlushManager。

        可在 Agent 生命周期的任何阶段调用（初始化/热加载后均安全）。
        """
        from ..context.tokenizer import get_context_window

        # 首次调用时 config 可能尚未创建，用默认值
        default_window = (
            self.config.context_window
            if hasattr(self, "config") and self.config
            else 128_000
        )

        new_window = get_context_window(
            model=self._model_id,
            base_url=self._base_url,
            default=default_window,
        )

        # 更新 Config（dataclass，直接赋值）
        if hasattr(self, "config") and self.config:
            old_window = self.config.context_window
            if new_window != old_window:
                self.config.context_window = new_window
                print(f"📐 模型 '{self._model_id}' 上下文窗口: {old_window:,} → {new_window:,} tokens")

        # 同步到 ContextManager（更新压缩阈值）— 仅在 _agent 已创建时
        if hasattr(self, "_agent") and hasattr(self._agent, "context_manager") and self._agent.context_manager:
            self._agent.context_manager.update_context_window(new_window)

        # 同步到 MemoryFlushManager（更新 flush 触发点）— 仅在已创建时
        if hasattr(self, "_memory_flush_manager") and self._memory_flush_manager:
            self._memory_flush_manager.context_window = new_window

    def _build_system_prompt(self) -> str:
        """构建系统提示词

        从 AGENTS.md 读取主要内容，附加其他配置文件作为上下文。
        如果入职未完成，注入 BOOTSTRAP.md 引导内容。

        Raises:
            RuntimeError: 如果 AGENTS.md 不存在
        """
        # AGENTS.md：优先用工作区 .myclaw/AGENTS.md，不存在则用基座 fallback
        agents_content = self.workspace.load_config("AGENTS")
        if not agents_content:
            # 基座 AGENTS.md fallback（~/.helloclaw/AGENTS.md）
            base_agents_path = os.path.join(self.home_path, "AGENTS.md")
            if os.path.exists(base_agents_path):
                with open(base_agents_path, "r", encoding="utf-8") as f:
                    agents_content = f.read()
        if not agents_content:
            raise RuntimeError("AGENTS.md 不存在（工作区与基座均无），请检查部署")

        base_prompt = agents_content

        # 加载其他配置文件作为上下文
        context_parts = []

        # 检查入职是否完成（从 identity 基座读取）
        if not self.identity.is_onboarding_completed():
            bootstrap = self.identity.bootstrap
            if bootstrap:
                context_parts.append(f"\n## 初始化引导\n\n{bootstrap}")

        # 身份信息（从 identity 基座读取）
        identity = self.identity.identity
        if identity:
            context_parts.append(f"\n## 你的身份信息\n{identity}")

        # 用户信息（从 identity 基座读取）
        user_info = self.identity.user
        if user_info:
            context_parts.append(f"\n## 用户信息\n{user_info}")

        # 人格模板（从 identity 基座读取）
        soul = self.identity.soul
        if soul:
            context_parts.append(f"\n## 人格模板\n{soul}")

        # 长期记忆使用指引（自动注入 + 主动检索）
        context_parts.append(
            "\n## 长期记忆\n"
            "你拥有长期记忆能力，所有历史记忆存储在向量数据库中。\n"
            "每轮对话开始时，系统会自动检索与你当前消息相关的记忆并注入上下文"
            "（标记为「相关记忆（自动注入）」）。\n"
            "如果自动注入的记忆不够，你可以使用 memory_search 工具进行更深入的语义检索。\n"
            "使用 memory_add 写入新的长期记忆。"
        )

        # ══════════════════════════════════════════════════════════
        # 子代理使用指引
        # ══════════════════════════════════════════════════════════
        context_parts.append(
            "\n## 子代理（SubAgent）\n"
            "你拥有启动子代理的能力（subagent 工具）。子代理在隔离的上下文中"
            "独立完成任务，它们的工具输出不会污染你的主上下文。\n\n"
            "**使用原则**：\n"
            "1. 需要搜索/读取大量文件 → 委托给子代理（用 execute_command + read_file）\n"
            "2. 需要多步数据处理 → 委托给子代理\n"
            "3. 多个可并行的独立子任务 → 用 parallel_spawn 并行启动\n"
            "4. 简单的单次工具调用（读一个小文件、一次计算）→ 不用子代理，直接调用\n\n"
            "**注意**：子代理的结果以摘要形式返回。如果需要看原始数据，"
            "可以要求子代理将结果写入文件，然后用 read_file 读取。"
        )

        # ══════════════════════════════════════════════════════════
        # 任务管理指引
        # ══════════════════════════════════════════════════════════
        context_parts.append(
            "\n## 任务管理\n"
            "对于包含 3 个以上独立步骤的复杂请求，你必须：\n"
            "1. 用 task_create 创建任务列表（每个步骤一个任务）\n"
            "2. 用 task_start 标记当前正在做的任务\n"
            "3. 用 task_complete 标记已完成的任务\n"
            "4. 如果某个任务依赖其他任务的输出，在创建时指定 depends_on\n"
            "5. 完成任务后自动检查 task_list，推进下一个可开始的任务\n\n"
            "这能确保你不会遗漏任何步骤。"
        )

        # ══════════════════════════════════════════════════════════
        # P0 工具使用指引
        # ══════════════════════════════════════════════════════════
        context_parts.append(
            "\n## 代码检索\n"
            "你拥有三个代码检索工具，优先使用它们而非 execute_command 拼 shell 命令：\n"
            "- search_content：在文件内容中搜索正则匹配（类似 grep）\n"
            "- search_file：按文件名 glob 模式搜索文件（如 *.py）\n"
            "- list_dir：列出目录内容（单层，带类型标注）\n\n"
            "这三个工具自动跳过 .git/node_modules/.venv 等忽略目录，跨平台通用。"
        )

        context_parts.append(
            "\n## HTTP 请求\n"
            "使用 http_request 工具调用 REST API 或 JSON 接口（不要绕道 bash curl）。\n"
            "支持任意 HTTP 方法、自定义 headers、body（dict 自动 JSON 序列化）、"
            "basic/bearer 认证。\n"
            "return_format=auto 时自动美化 JSON 响应。"
        )

        context_parts.append(
            "\n## 浏览器自动化\n"
            "使用 browser 工具操控真实浏览器（Playwright headless）。\n"
            "通过 action 参数执行操作：navigate/click/type/fill/screenshot/evaluate/text/press/scroll/wait/close。\n"
            "用于处理 JS 渲染的 SPA 页面、表单交互、截图等 web_fetch 无法处理的场景。\n"
            "selector 使用 CSS 选择器语法。浏览器状态在会话内持久化。"
        )

        context_parts.append(
            "\n## 定时任务\n"
            "使用 automation 工具创建定时任务（每日简报、定时提醒、周期巡检等）。\n"
            "action=create 创建任务，需要 name/prompt/schedule_type/schedule_config。\n"
            "调度类型：once（一次性）、interval（间隔循环）、rrule（RFC 5545 RRULE）。\n"
            "可选 webhook_url 投递执行结果。时间均为 UTC ISO 8601。"
        )

        if context_parts:
            return base_prompt + "\n" + "\n".join(context_parts)

        return base_prompt

    # ==================== 工作区动态切换 ====================

    @property
    def current_workspace(self) -> str:
        """当前工作区绝对路径。"""
        return self._current_workspace

    def bind_workspace(self, workspace_path: str):
        """切换到指定工作区（运行时动态重绑工具根目录 + 部署 .myclaw/ 结构）。

        调用时机：
        - POST /api/workspace/switch 接口
        - 前端发送消息时附带 workspace_path 参数

        全量重绑所有依赖 workspace_path 的引用点，确保切换后
        session/tasks/skills/uploads/tools 都落在新工作区。

        Args:
            workspace_path: 用户选择的工作区目录

        Raises:
            ValueError: 工作区未授权或路径无效
        """
        from ..workspace.auth import is_allowed

        if not is_allowed(workspace_path):
            raise ValueError(f"工作区未授权: {workspace_path}")

        abs_path = os.path.abspath(os.path.expanduser(workspace_path))
        self._current_workspace = abs_path

        # Phase 2 部署：确保 .myclaw/ 子目录结构存在
        self.workspace = WorkspaceManager(abs_path)
        self.workspace.ensure_project_workspace()

        # 保留 workspace_path 属性（外部引用，如 SubAgentOrchestrator）
        self.workspace_path = abs_path

        # 重绑工具沙箱根目录（Read/Write/Edit/Bash/RAG）
        self._rebind_workspace_tools(abs_path)

        # 重绑定时任务存储（指向新工作区的 automations 目录）
        if hasattr(self, '_automation_store') and self._automation_store is not None:
            try:
                from ..automation import AutomationStore
                self._automation_store = AutomationStore(
                    automations_dir=self.workspace.automations_path,
                )
            except Exception:
                pass

        # 重绑浏览器会话的上传目录（截图保存到新工作区）
        if hasattr(self, '_browser_session') and self._browser_session is not None:
            try:
                self._browser_session._uploads_dir = Path(self.workspace.uploads_path)
            except Exception:
                pass

        # 重绑 config.session_dir
        self.config.session_dir = self.workspace.sessions_path

        # 同步更新 hello_agents 底层 SessionStore 的存储目录
        # SessionStore.__init__() 缓存了 Path(self.config.session_dir) 到 self.session_dir，
        # bind_workspace 只改了 Config 对象，底层 store 仍指向旧工作区，导致 save/list/load 写错位置
        if self._agent is not None and hasattr(self._agent, 'session_store') and self._agent.session_store:
            self._agent.session_store.session_dir = Path(self.workspace.sessions_path)

        # 重绑 SkillLoader（仅切工作区目录，全局目录不变）
        self.skill_loader.update_workspace_dir(Path(self.workspace.skills_path))
        self.refresh_skill_tool()

        # 重绑 TaskTracker（私有属性 _persist_dir）
        self._task_tracker._persist_dir = self.workspace.tasks_path

        # 重绑 SubAgentOrchestrator
        if self._subagent_orchestrator is not None:
            self._subagent_orchestrator.workspace_path = abs_path

        # 重绑 EnhancedSimpleAgent workspace_root（Path 对象）
        if self._agent is not None and hasattr(self._agent, "workspace_root"):
            self._agent.workspace_root = Path(abs_path).resolve()

        # 更新 MemoryCaptureManager 的 workspace 引用
        if hasattr(self, "_memory_capture_manager") and self._memory_capture_manager:
            self._memory_capture_manager.workspace_manager = self.workspace

        # 重建系统提示词（identity 不变 + 新工作区 AGENTS 叠加）
        if self._agent is not None:
            self._agent.system_prompt = self._build_system_prompt()

        print(f"🔄 已切换工作区: {abs_path}")

    def _rebind_workspace_tools(self, abs_path: str):
        """重绑工作区相关工具的根目录。

        Args:
            abs_path: 新工作区绝对路径
        """
        if not self.tool_registry:
            return

        for tool in self.tool_registry.get_all_tools():
            tool_name = getattr(tool, "name", "")

            # ReadTool / WriteTool / EditTool / MultiEditTool
            if hasattr(tool, "project_root"):
                tool.project_root = abs_path
            # Read/Write/Edit/MultiEdit 的相对路径解析用的是 working_dir（file_tools.py:268
            # `return self.working_dir / path`），而非 project_root。仅重绑 project_root
            # 会导致切换工作区后 Read('.') 仍指向旧工作区，看不到新工作区文件。
            # 必须同步重绑 working_dir（Path 对象，因 `/` 运算需 Path）。
            if hasattr(tool, "working_dir"):
                tool.working_dir = Path(abs_path).resolve()

            # BashTool（name 为 execute_command）：重置白名单 + 默认工作目录 + cd 历史（防跨工作区逃逸）
            if tool_name == "execute_command" and hasattr(tool, "allowed_directories"):
                tool.allowed_directories = [abs_path]
                tool.default_workdir = abs_path
                tool._cwd = None

            # RAGTool（属性名为 _workspace_root）
            if hasattr(tool, "_workspace_root"):
                tool._workspace_root = os.path.normpath(abs_path)

            # MemoryTool：更新 workspace_manager 引用（memory_store 不可用时回退到文件搜索）
            if hasattr(tool, "workspace_manager"):
                tool.workspace_manager = self.workspace

    def _inject_relevant_memories(self, user_message: str) -> str:
        """检索与用户消息相关的记忆，返回格式化的记忆上下文文本。

        每轮用户消息到达时后台语义检索 top-K 记忆，作为 system context 静默注入。
        如果记忆系统不可用或无相关记忆，返回空字符串。

        配置项（config.json 的 memory 段）：
        - auto_inject: bool, 是否启用自动注入（默认 True）
        - auto_inject_top_k: int, 返回结果数量（默认 3）
        - auto_inject_threshold: float, 相似度阈值（默认 0.3）

        Args:
            user_message: 用户消息文本（可能是多模态编码字符串）

        Returns:
            格式化的记忆上下文文本，无相关记忆时返回空字符串
        """
        if not self._memory_store or not self._memory_store.available:
            return ''

        # 读取配置
        try:
            global_config = self.workspace.load_global_config()
            memory_cfg = global_config.get('memory', {})
        except Exception:
            memory_cfg = {}

        if not memory_cfg.get('auto_inject', True):
            return ''

        top_k = memory_cfg.get('auto_inject_top_k', 3)
        score_threshold = memory_cfg.get('auto_inject_threshold', 0.3)

        try:
            # 从多模态编码中提取纯文本用于检索
            query_text = user_message
            try:
                from .multimodal_bridge import (
                    is_encoded_multimodal,
                    decode_multimodal_content,
                )
                from ..multimodal import flatten_content_to_text
                if is_encoded_multimodal(user_message):
                    decoded = decode_multimodal_content(user_message)
                    query_text = flatten_content_to_text(decoded)
            except Exception:
                pass

            if not query_text or not query_text.strip():
                return ''

            memories = self._memory_store.search_memories(
                query=query_text,
                top_k=top_k,
                score_threshold=score_threshold,
            )

            if not memories:
                return ''

            # 格式化记忆上下文
            lines = [
                '\n## 相关记忆（自动注入）',
                '以下是从长期记忆中检索到的与当前对话相关的信息：\n',
            ]
            for i, m in enumerate(memories, 1):
                content = m.get('content', '')
                category = m.get('category', '')
                lines.append(f'{i}. [{category}] {content}')

            lines.append(
                '\n（以上记忆仅供参考，如需更多历史信息请使用 memory_search 工具）'
            )

            print(f'🧠 自动注入 {len(memories)} 条相关记忆')
            return '\n'.join(lines)
        except Exception as e:
            print(f'⚠️ 记忆自动注入失败: {e}')
            return ''

    def _setup_tools(self) -> ToolRegistry:
        """设置工具集"""
        registry = ToolRegistry()

        # HelloAgents 内置工具（设置元数据属性供 ContextGuard 动态路由）
        read_tool = ReadTool(project_root=self.workspace_path)
        read_tool.output_size_hint = 5000
        read_tool.has_side_effects = False
        registry.register_tool(read_tool)

        write_tool = WriteTool(project_root=self.workspace_path)
        write_tool.output_size_hint = 500
        write_tool.has_side_effects = True
        registry.register_tool(write_tool)

        edit_tool = EditTool(project_root=self.workspace_path)
        edit_tool.output_size_hint = 800
        edit_tool.has_side_effects = True
        registry.register_tool(edit_tool)

        calc_tool = CalculatorTool()
        calc_tool.output_size_hint = 100
        calc_tool.has_side_effects = True  # 不可委托（输出太小无需委托）
        registry.register_tool(calc_tool)

        # HelloClaw 自定义工具
        registry.register_tool(MemoryTool(
            memory_store=self._memory_store,
            workspace_manager=self.workspace,  # 过渡期回退
        ))
        registry.register_tool(BashTool(
            allowed_directories=[self.workspace_path],  # 限制在工作空间目录
            default_workdir=self.workspace_path,  # 与 Read/Write 根目录一致，避免 uvicorn CWD 下找不到脚本
            timeout_config=self._timeout_config,
        ))
        registry.register_tool(WebFetchTool())   # 网页抓取工具

        # MyClaw自定义工具
        registry.register_tool(WebSearchTool())  # 网页搜索工具
        registry.register_tool(RAGTool(workspace_root=self.workspace_path))  # RAG：相对路径相对工作空间根

        # 代码检索三件套（P0 补全）— project_root 属性自动被 _rebind_workspace_tools 处理
        registry.register_tool(SearchContentTool(project_root=self.workspace_path))
        registry.register_tool(SearchFileTool(project_root=self.workspace_path))
        registry.register_tool(ListDirTool(project_root=self.workspace_path))

        # 结构化 HTTP 请求工具（P0 补全）
        registry.register_tool(HttpRequestTool())

        # Playwright 浏览器自动化工具（P0 补全）— 线程隔离 + 懒初始化
        self._browser_session = BrowserSession(
            timeout_config=self._timeout_config,
            uploads_dir=self.workspace.uploads_path,
        )
        registry.register_tool(BrowserTool(session=self._browser_session))

        # 定时任务工具（P0 补全）— store 在此处创建，供 main.py 调度器引用
        self._automation_store = AutomationStore(
            automations_dir=self.workspace.automations_path,
        )
        registry.register_tool(AutomationTool(
            store_getter=lambda: self._automation_store,
        ))

        # 自实现 Skill 工具
        self._skill_tool = SkillTool(skill_loader=self.skill_loader)
        registry.register_tool(self._skill_tool)

        self._register_mcp_tools(registry)

        # ══════════════════════════════════════════════════════════
        # 子代理编排器（延迟初始化，因为此时 LLM 已就绪）
        # ══════════════════════════════════════════════════════════
        if self._subagent_orchestrator is None:
            self._subagent_orchestrator = SubAgentOrchestrator(
                llm=self._llm,
                master_tool_registry=registry,
                workspace_path=self.workspace_path,
            )

        # 注册 SubAgent 工具
        registry.register_tool(SubAgentTool(orchestrator=self._subagent_orchestrator))

        # 注册 Task 管理工具
        registry.register_tool(TaskTool(tracker=self._task_tracker))

        return registry

    def refresh_skill_tool(self) -> None:
        """刷新 SkillTool 的描述（技能列表变化时调用）"""
        if hasattr(self, '_skill_tool') and self._skill_tool:
            self._skill_tool.refresh_description()

    def _reset_mcp_disclosed_tools(self) -> None:
        """切换会话或关闭时清理 MCP 动态披露的子工具。"""
        if hasattr(self, "tool_registry") and self.tool_registry:
            reset_all_mcp_disclosed_tools(self.tool_registry)

    def _register_mcp_tools(self, registry: ToolRegistry) -> None:
        """按 ~/.helloclaw/config.json 的 `mcp` 段注册 MCP 工具。

        - servers 非空：为每条配置注册一个 MCPTool（外部命令/鉴权等）
        - servers 为空且 builtin_demo：注册内置演示 MCPTool()
        - mcp.enabled=false：不注册任何 MCP
        """
        cfg = self.workspace.get_mcp_config()
        if not cfg.get("enabled", True):
            print("ℹ️ MCP 已在 config.json 中禁用（mcp.enabled=false）")
            return

        servers = cfg.get("servers") or []
        if servers:
            for item in servers:
                if not isinstance(item, dict):
                    print("⚠️ MCP servers 项格式无效（应为对象），已跳过")
                    continue
                name = (item.get("name") or "mcp").strip() or "mcp"
                url = (item.get("server_url") or "").strip()
                cmd = item.get("server_command")
                has_cmd = (
                    cmd
                    and isinstance(cmd, list)
                    and all(isinstance(x, str) for x in cmd)
                )
                if not url and not has_cmd:
                    print(
                        f"⚠️ MCP「{name}」需配置 server_url 或 server_command，已跳过"
                    )
                    continue
                try:
                    registry.register_tool(
                        MCPTool(
                            name=name,
                            server_url=url or None,
                            server_command=cmd if has_cmd else None,
                            server_args=item.get("server_args") or [],
                            transport_type=item.get("transport_type"),
                            headers=item.get("headers"),
                            env=item.get("env"),
                            env_keys=item.get("env_keys"),
                            auto_expand=item.get("auto_expand", False),
                            tool_registry=registry,
                        )
                    )
                    print(f"✅ MCP 工具已注册: {name}")
                except Exception as e:
                    print(f"⚠️ MCP 工具注册失败 ({name}): {e}")
            return

        if cfg.get("builtin_demo", True):
            registry.register_tool(MCPTool(tool_registry=registry))
            print("✅ MCP 工具已注册（内置演示服务器）")
        else:
            print("ℹ️ 未配置 mcp.servers 且 builtin_demo=false，未注册 MCP 工具")

    @staticmethod
    def _split_history_at_user_turn(
        history: List[Message],
        user_turn_index: int,
    ) -> tuple[List[Message], List[Message]]:
        """将历史拆为 prefix + suffix，中间为该轮用户消息及其旧回复（将被替换）。"""
        if user_turn_index < 0:
            raise ValueError("user_turn_index 不能为负数")

        user_count = 0
        for i, msg in enumerate(history):
            if msg.role != "user":
                continue
            if user_count == user_turn_index:
                next_user = len(history)
                for k in range(i + 1, len(history)):
                    if history[k].role == "user":
                        next_user = k
                        break
                return history[:i], history[next_user:]
            user_count += 1

        raise ValueError(
            f"user_turn_index={user_turn_index} 超出会话中的用户消息数量（共 {user_count} 条用户消息）"
        )

    def _prepare_session_turn_replace(self, session_id: str, user_turn_index: int) -> None:
        """加载会话：保留该轮之前的上下文与之后的对话，仅替换该轮回复。"""
        session_file = os.path.join(self.workspace.sessions_path, f"{session_id}.json")
        if not os.path.exists(session_file):
            self._agent.clear_history()
            raise ValueError(f"会话 {session_id} 不存在")

        self._agent.load_session(session_file)

        # 恢复被截断的历史内容（非破坏性压缩的逆向操作）
        if hasattr(self._agent, "context_manager"):
            self._agent.context_manager.restore_original_content()

        history = list(self._agent.get_history())
        prefix, suffix = self._split_history_at_user_turn(history, user_turn_index)
        self._agent._history = prefix
        self._resend_suffix = suffix
        self._current_session_id = session_id
        if hasattr(self._agent, "context_manager"):
            self._agent.context_manager.recalculate_history_tokens()

    def activate_session(self, session_id: str) -> None:
        """将 Agent 内存切换到指定会话（打开历史会话 / 开始对话前调用）。

        从会话文件加载历史并设置 ``_current_session_id``，使内存状态与前端当前会话一致。
        """
        # 当内存中的session_id与传入的session_id一致时，直接返回
        if getattr(self, "_current_session_id", None) == session_id:
            return
        self._reset_mcp_disclosed_tools()
        # 尝试加载该会话的任务列表
        if hasattr(self, '_task_tracker') and self._task_tracker:
            self._task_tracker.clear()
            self._task_tracker.load(session_id)
        session_file = os.path.join(self.workspace.sessions_path, f"{session_id}.json")
        self._resend_suffix = []
        if os.path.exists(session_file):
            self._agent.load_session(session_file)
        else:
            self._agent.clear_history()
            if hasattr(self, "_memory_flush_manager"):
                self._memory_flush_manager.reset()
        self._current_session_id = session_id
        if hasattr(self._agent, "context_manager"):
            self._agent.context_manager.recalculate_history_tokens()

    def _finalize_turn_replace_if_needed(self) -> None:
        """将保留的后续对话拼回历史（在保存会话前调用）。"""
        suffix = getattr(self, "_resend_suffix", None)
        if not suffix:
            return
        history = list(self._agent.get_history())
        self._agent._history = history + suffix
        self._resend_suffix = []

    def _build_multimodal_config(self) -> MultimodalConfig:
        """从环境变量构造多模态配置（每轮对话读取一次，便于热改 env）。"""
        image_mode = (os.getenv("MULTIMODAL_IMAGE_MODE", "base64").strip().lower()
                      or "base64")
        if image_mode not in ("base64", "url"):
            image_mode = "base64"
        try:
            max_image_mb = float(os.getenv("MULTIMODAL_MAX_IMAGE_MB", "5"))
        except ValueError:
            max_image_mb = 5.0
        uploads_root = self.workspace.uploads_path
        return MultimodalConfig(
            image_mode=image_mode,  # type: ignore[arg-type]
            public_base_url=(os.getenv("MULTIMODAL_PUBLIC_BASE_URL") or None),
            uploads_root=uploads_root,
            max_image_mb=max_image_mb,
        )

    def _materialize_image_ref_for_display(self, url: str) -> str:
        """会话历史中的 image_url 反序列化为前端可显示的 URL。

        历史中 image_url 以三种形式存在：
        - ``@FILE:<abs_path>``：base64 模式下的路径引用，需要即时读盘构造 data URL
        - ``http(s)://...``：URL 模式直接透传
        - ``data:...``：极少见的旧数据 / 用户手动注入；原样返回

        文件已被删除时返回空串（调用方据此跳过该附件项）。
        """
        if not isinstance(url, str) or not url:
            return ""
        if url.startswith("@FILE:"):
            local_path = url[len("@FILE:"):]
            if not os.path.isfile(local_path):
                return ""
            try:
                from ..multimodal.image import load_image_as_data_url
                # 复用上传配置中的图片大小上限，保持与首轮一致
                cfg = self._build_multimodal_config()
                return load_image_as_data_url(local_path, max_mb=cfg.max_image_mb)
            except Exception as exc:
                print(f"⚠️ 历史图片还原失败 path={local_path} err={exc}")
                return ""
        return url

    def _prepare_message_with_attachments(
        self,
        message: str,
        attachments: Optional[List[Dict[str, Any]]],
    ) -> str:
        """构造发给底层 Agent 的输入。

        - 无附件：原样返回字符串
        - 有附件：构造 OpenAI 多模态 list-content，再用 encode_multimodal_content
          包装成可放入 hello_agents.Message 的字符串。
        """
        if not attachments:
            return message
        cfg = self._build_multimodal_config()
        content = build_user_content(
            message,
            attachments,
            workspace_root=self._current_workspace,
            config=cfg,
        )
        if isinstance(content, str):
            return content
        return encode_multimodal_content(content)

    def chat(
        self,
        message: str,
        session_id: str = None,
        *,
        user_turn_index: Optional[int] = None,
        regenerate: bool = False,
        attachments: Optional[List[Dict[str, Any]]] = None,
        cancel_token: Optional['CancellationToken'] = None,
    ) -> str:
        """同步聊天

        Args:
            cancel_token: 取消令牌，用于中断 Agent 循环
        """
        # 热加载配置（检测 config.json 变化）
        self._reload_llm_if_changed()

        # 动态更新系统提示词（检查 BOOTSTRAP 状态、读取最新配置）
        self._agent.system_prompt = self._build_system_prompt()

        # 自动注入相关记忆（后台语义检索 top-K）
        memory_context = self._inject_relevant_memories(message)
        if memory_context:
            self._agent.system_prompt += memory_context

        if session_id:
            if user_turn_index is not None:
                self._prepare_session_turn_replace(session_id, user_turn_index)
            else:
                self.activate_session(session_id)
        else:
            self._agent.clear_history()
            self._current_session_id = None

        # LLM 调用参数（防止重复循环）
        llm_kwargs = {
            "frequency_penalty": 0.5,  # 降低重复相同内容的概率
            "presence_penalty": 0.3,   # 鼓励谈论新话题
        }

        # 构造（可能含多模态附件）输入并运行 Agent
        agent_input = self._prepare_message_with_attachments(message, attachments)
        response = self._agent.run(agent_input, cancel_token=cancel_token, **llm_kwargs)
        self._finalize_turn_replace_if_needed()

        # 保存会话
        save_id = session_id or self.create_session()
        try:
            self._agent.save_session(save_id)
        except Exception as e:
            print(f"⚠️ 保存会话失败: {e}")

        return response

    async def achat(
        self,
        message: str,
        session_id: str = None,
        *,
        user_turn_index: Optional[int] = None,
        regenerate: bool = False,
        attachments: Optional[List[Dict[str, Any]]] = None,
        cancel_token: Optional['CancellationToken'] = None,
    ):
        """异步聊天（支持流式输出）

        Args:
            message: 用户消息
            session_id: 会话 ID，如果为 None 则创建新会话
            user_turn_index: 要替换回复的用户轮次（0 起）；保留该轮之后的对话
            regenerate: 是否为重新生成（与编辑共用替换逻辑）
            cancel_token: 取消令牌，用于中断 Agent 循环

        Yields:
            StreamEvent: 流式事件
        """
        import uuid
        import time

        t0 = time.time()
        print(f"[⏱️ {t0:.3f}] achat 开始")

        # 热加载配置（检测 config.json 变化）
        self._reload_llm_if_changed()

        # 动态更新系统提示词（检查 BOOTSTRAP 状态、读取最新配置）
        self._agent.system_prompt = self._build_system_prompt()

        # 自动注入相关记忆（后台语义检索 top-K）
        memory_context = self._inject_relevant_memories(message)
        if memory_context:
            self._agent.system_prompt += memory_context

        print(f"[⏱️ {time.time():.3f}] 系统提示词构建完成 (+{time.time()-t0:.3f}s)")

        if not session_id:
            session_id = str(uuid.uuid4())[:8]

        if user_turn_index is not None:
            try:
                self._prepare_session_turn_replace(session_id, user_turn_index)
            except ValueError as exc:
                from hello_agents.core.streaming import StreamEvent, StreamEventType
                yield StreamEvent.create(
                    StreamEventType.ERROR,
                    self._agent.name,
                    error=str(exc),
                )
                return
        else:
            self.activate_session(session_id)
        print(f"[⏱️ {time.time():.3f}] 会话加载完成 (+{time.time()-t0:.3f}s)")

        # LLM 调用参数（防止重复循环）
        llm_kwargs = {
            "frequency_penalty": 0.5,  # 降低重复相同内容的概率
            "presence_penalty": 0.3,   # 鼓励谈论新话题
        }

        t_llm = time.time()
        print(f"[⏱️ {t_llm:.3f}] 开始调用 LLM ({self._model_id})...")
        first_chunk = True

        # 构造（可能含多模态附件）输入
        agent_input = self._prepare_message_with_attachments(message, attachments)

        async for event in self._agent.arun_stream_with_tools(agent_input, cancel_token=cancel_token, **llm_kwargs):
            if first_chunk and event.type.value == "llm_chunk":
                print(f"[⏱️ {time.time():.3f}] 首个 token 到达 (LLM 延迟: {time.time()-t_llm:.3f}s)")
                first_chunk = False
            yield event

        print(f"[⏱️ {time.time():.3f}] LLM 调用完成 (总耗时: {time.time()-t0:.3f}s)")

        # 对话结束后自动捕获记忆（异步执行，不阻塞用户）
        await self._capture_memories(message)

        # 对话结束后检查是否需要触发 Memory Flush（异步执行，不阻塞用户）
        await self._check_and_run_memory_flush()

    async def _capture_memories(self, user_message: str):
        """自动捕获对话中的记忆

        Args:
            user_message: 用户消息
        """
        try:
            session_id = getattr(self, "_current_session_id", None)
            # 使用 MemoryCaptureManager 分析并存储记忆到 Qdrant
            memories = await self._memory_capture_manager.acapture_and_store(
                user_message, session_id=session_id
            )

            if memories:
                print(f"📝 自动捕获 {len(memories)} 条记忆")
                for m in memories:
                    print(f"   - [{m['category']}] {m['content'][:50]}...")
        except Exception as e:
            print(f"⚠️ 记忆捕获失败: {e}")

    async def _check_and_run_memory_flush(self):
        """检查并执行 Memory Flush

        如果当前 token 数接近压缩阈值，触发一个静默回合提醒 Agent 保存记忆。
        """
        # 估算当前 token 数（简单估算：字符数 / 4）
        estimated_tokens = self._estimate_tokens()

        if self._memory_flush_manager.should_trigger_flush(estimated_tokens):
            print(f"\n🔄 触发 Memory Flush（估算 token: {estimated_tokens}）")

            # 获取 flush 提示词
            flush_prompt = self._memory_flush_manager.get_flush_prompt()

            # 执行静默回合
            try:
                # 使用同步方法执行（不返回给用户）
                response = self._agent.run(flush_prompt)

                # 检查是否是静默响应
                if self._memory_flush_manager.is_silent_response(response):
                    print("📝 Agent 选择不保存记忆")
                else:
                    print(f"📝 Agent 已保存记忆")

            except Exception as e:
                print(f"⚠️ Memory Flush 失败: {e}")

    def _estimate_tokens(self) -> int:
        """估算当前上下文的 token 数（优先使用 ContextManager 的精确计数）。"""
        agent = self._agent
        if hasattr(agent, "context_manager"):
            total = agent.context_manager.history_token_count
            if agent.system_prompt:
                total += len(agent.system_prompt) // 3
            return total

        total_chars = 0
        if agent.system_prompt:
            total_chars += len(agent.system_prompt)
        for msg in agent._history:
            if msg.content:
                total_chars += len(msg.content)
        return total_chars // 3

    def _count_system_prompt_tokens(self) -> int:
        """系统提示词 token 数。"""
        prompt = self._agent.system_prompt or ""
        if not prompt:
            return 0
        if hasattr(self._agent, "token_counter"):
            return self._agent.token_counter.count_text(prompt)
        return len(prompt) // 3

    def _count_messages_tokens(self, messages: List[Message]) -> int:
        """消息列表 token 数（含 tool_calls 元数据粗略估算）。"""
        if hasattr(self._agent, "token_counter"):
            total = self._agent.token_counter.count_messages(messages)
            for msg in messages:
                metadata = getattr(msg, "metadata", None) or {}
                tool_calls = metadata.get("tool_calls")
                if tool_calls:
                    total += len(str(tool_calls)) // 3
            return total

        total_chars = 0
        for msg in messages:
            if msg.content:
                total_chars += len(msg.content)
            metadata = getattr(msg, "metadata", None) or {}
            if metadata.get("tool_calls"):
                total_chars += len(str(metadata["tool_calls"]))
        return total_chars // 3

    def _estimate_session_file_tokens(self, session_id: str) -> int:
        """从会话文件估算历史 token（未加载到内存时使用）。"""
        import json
        from hello_agents.core.message import Message

        filepath = os.path.join(self.workspace.sessions_path, f"{session_id}.json")
        if not os.path.exists(filepath):
            return 0

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            return 0

        messages: List[Message] = []
        for msg_data in data.get("history", []):
            try:
                messages.append(Message.from_dict(msg_data))
            except Exception:
                role = msg_data.get("role", "user")
                content = msg_data.get("content", "") or ""
                messages.append(Message(content, role))

        return self._count_messages_tokens(messages)

    def get_context_usage(self, session_id: Optional[str] = None) -> dict:
        """返回上下文窗口使用情况。

        若 session_id 与当前内存会话一致，使用内存中的 token 计数；否则读会话文件估算。
        """
        context_window = self.config.context_window
        system_tokens = self._count_system_prompt_tokens()

        in_memory_session = (
            session_id is not None
            and getattr(self, "_current_session_id", None) == session_id
        )
        if in_memory_session:
            history_tokens = (
                self._agent.context_manager.history_token_count
                if hasattr(self._agent, "context_manager")
                else self._count_messages_tokens(self._agent.get_history())
            )
        elif session_id:
            history_tokens = self._estimate_session_file_tokens(session_id)
        else:
            history_tokens = (
                self._agent.context_manager.history_token_count
                if hasattr(self._agent, "context_manager")
                else self._count_messages_tokens(self._agent.get_history())
            )

        used_tokens = system_tokens + history_tokens
        if context_window > 0:
            used_percent = min(100.0, (used_tokens / context_window) * 100.0)
        else:
            used_percent = 0.0

        return {
            "session_id": session_id,
            "context_window": context_window,
            "used_tokens": used_tokens,
            "system_tokens": system_tokens,
            "history_tokens": history_tokens,
            "used_percent": round(used_percent, 2),
        }

    def save_current_session(self):
        """保存当前会话"""
        self._finalize_turn_replace_if_needed()
        if hasattr(self, '_current_session_id') and self._current_session_id:
            try:
                self._agent.save_session(self._current_session_id)
                # 持久化任务追踪器
                if hasattr(self, '_task_tracker') and self._task_tracker:
                    self._task_tracker.save(self._current_session_id)
                return self._current_session_id
            except Exception as e:
                print(f"⚠️ 保存会话失败: {e}")
        return None

    def create_session(self) -> str:
        """创建新会话"""
        import uuid
        session_id = str(uuid.uuid4())[:8]
        return session_id

    def list_sessions(self) -> List[dict]:
        """列出所有会话"""
        sessions_dir = self.workspace.sessions_path
        if not os.path.exists(sessions_dir):
            return []

        sessions = []
        for filename in os.listdir(sessions_dir):
            if filename.endswith(".json"):
                filepath = os.path.join(sessions_dir, filename)
                stat = os.stat(filepath)
                sessions.append({
                    "id": filename[:-5],
                    "created_at": stat.st_ctime,
                    "updated_at": stat.st_mtime,
                })

        return sorted(sessions, key=lambda x: x["updated_at"], reverse=True)

    def delete_session(self, session_id: str) -> bool:
        """删除会话"""
        filepath = os.path.join(self.workspace.sessions_path, f"{session_id}.json")
        if os.path.exists(filepath):
            os.remove(filepath)
            return True
        return False

    def get_session_history(self, session_id: str) -> List[dict]:
        """获取会话历史消息（兼容多模态 list-content 与编码字符串形式）。

        返回结构：
            [
                {"role": "user", "content": "文本", "attachments": [...], "metadata": {...}},
                ...
            ]
        其中 ``attachments`` 仅在该条消息含图片 part 时存在，元素形如
        ``{"kind": "image", "url": "<data:...|http://...>"}``，便于前端直接渲染缩略图。
        """
        import json
        filepath = os.path.join(self.workspace.sessions_path, f"{session_id}.json")
        if not os.path.exists(filepath):
            return []

        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)

            messages = []
            raw_history = data.get("history", [])
            for msg in raw_history:
                role = msg.get("role", "")
                # 支持 user, assistant, tool 三种角色
                if role not in ("user", "assistant", "tool"):
                    continue

                content = msg.get("content", "")

                # 1) 编码字符串：先解码回 list-content
                if is_encoded_multimodal(content):
                    content = decode_multimodal_content(content)

                # 2) list-content：拍平为文本 + 提取附件元数据
                #    image_url 在历史中以 @FILE:<abs_path> 引用形式保存，
                #    这里按需即时读盘构造 data URL 供前端缩略图渲染；URL 模式保持原样。
                attachments_meta: List[dict] = []
                if isinstance(content, list):
                    text_parts: List[str] = []
                    for part in content:
                        if isinstance(part, dict):
                            ptype = part.get("type")
                            if ptype == "text":
                                text_parts.append(part.get("text") or "")
                            elif ptype == "image_url":
                                url = (part.get("image_url") or {}).get("url") or ""
                                display_url = self._materialize_image_ref_for_display(url)
                                if display_url:
                                    attachments_meta.append({"kind": "image", "url": display_url})
                        elif isinstance(part, str):
                            text_parts.append(part)
                    content = "\n".join(text_parts)

                message_obj: dict = {"role": role, "content": content}
                if attachments_meta:
                    message_obj["attachments"] = attachments_meta
                if "metadata" in msg:
                    message_obj["metadata"] = msg["metadata"]

                messages.append(message_obj)

            return messages
        except Exception as e:
            print(f"Error loading session history: {e}")
            return []

    def clear_all_history(self):
        """清除 Agent 内存中的所有历史记录

        用于初始化时重置 Agent 状态。
        """
        self._agent.clear_history()
        self._current_session_id = None
        self._reset_mcp_disclosed_tools()

        # 重置 MemoryFlushManager 状态
        if hasattr(self, '_memory_flush_manager'):
            self._memory_flush_manager.reset()

        # 重新读取 name（因为 IDENTITY.md 可能已被重置）
        self.name = self._read_identity_name() or "HelloClaw"

    async def shutdown(self):
        """异步关闭 Agent 持有的外部连接与可释放资源。

        由 main.py 的 lifespan finally 块调用（Ctrl+C / 正常退出时均会触发）。
        """
        # 0) 关闭 LLM 异步客户端（释放 httpx 连接池）
        if self._llm and hasattr(self._llm, 'close_async_client'):
            try:
                await self._llm.close_async_client()
            except Exception as e:
                print(f"⚠️ 关闭 LLM 客户端失败: {e}")

        self._reset_mcp_disclosed_tools()
        # 1) 尝试让各工具自行释放资源（如 RAG/Qdrant、HTTP client 等）
        try:
            if hasattr(self, "tool_registry") and self.tool_registry:
                for tool in self.tool_registry.get_all_tools():
                    for method_name in ("shutdown", "close"):
                        method = getattr(tool, method_name, None)
                        if callable(method):
                            try:
                                method()
                            except Exception as e:
                                print(f"⚠️ 释放工具资源失败 ({tool.name}.{method_name}): {e}")
                            break
        except Exception as e:
            print(f"⚠️ 清理工具资源失败: {e}")

        # 2) 清空工具注册表，避免残留引用
        try:
            if hasattr(self, "tool_registry") and self.tool_registry:
                self.tool_registry.clear()
        except Exception as e:
            print(f"⚠️ 清理工具注册表失败: {e}")
