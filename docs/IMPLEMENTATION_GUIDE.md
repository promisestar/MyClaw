# MyClaw 架构改造实施指南

> 本文档用于指导 AI coding Agent 完成以下两项改造：
>
> **改造 1：SubAgent 上下文隔离 + 并行执行**
> **改造 2：结构化任务管理系统**
>
> 已创建的新文件（可直接使用，无需再写）：
> - `backend/src/agent/subagent_orchestrator.py`
> - `backend/src/tools/builtin/subagent_tool.py`
> - `backend/src/agent/task_tracker.py`
> - `backend/src/tools/builtin/task_tool.py`
> - `backend/src/tools/__init__.py`（已更新导出）
>
> 你需要按以下顺序修改现有文件。

---

## 一、修改 `backend/src/agent/myclaw_agent.py`

### 1.1 新增导入（文件顶部）

在现有 import 块末尾添加：

```python
# 在 from ..skills.loader import SkillLoader 之后追加

# SubAgent 编排器 + 任务追踪器
from .subagent_orchestrator import SubAgentOrchestrator, SubAgentTask, SubAgentResultMode
from .task_tracker import TaskTracker
from ..tools.builtin.subagent_tool import SubAgentTool
from ..tools.builtin.task_tool import TaskTool
```

### 1.2 `__init__` 中新增编排器和追踪器初始化

⚠️ **关键**：这两个初始化必须在 `_setup_tools()` 调用（约第 136 行）之前完成。

在 `self._memory_store = MemoryVectorStore()` (约第 133 行) 之后、`self.tool_registry = self._setup_tools()` (约第 136 行) 之前插入：

```python
        # 初始化子代理编排器（延迟创建，在 _setup_tools 中实例化）
        self._subagent_orchestrator = None

        # 初始化任务追踪器（带持久化）
        tasks_dir = os.path.join(self.workspace_path, "tasks")
        self._task_tracker = TaskTracker(persist_dir=tasks_dir)
```

### 1.3 延迟创建编排器（避免循环依赖）

在 `__init__` 中，`SubAgentTool` 和 `TaskTool` 的注册会触发工具创建，但此时 `_subagent_orchestrator` 尚未准备好。改为在 `_setup_tools` 中延迟创建。

修改 `_setup_tools` 方法（约第 362 行），在整个方法末尾 `return registry` 之前追加：

```python
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
```

### 1.4 系统提示词注入（`_build_system_prompt` 方法）

在 `_build_system_prompt` 方法中（约第 308 行），在所有 context_parts 的最后追加两段：

```python
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
```

### 1.5 会话切换时重置任务追踪器（`activate_session` 方法）

在 `activate_session` 方法中（约第 504 行），在 `self._reset_mcp_disclosed_tools()` 之后追加：

```python
        # 尝试加载该会话的任务列表
        if hasattr(self, '_task_tracker') and self._task_tracker:
            self._task_tracker.clear()
            self._task_tracker.load(session_id)
```

### 1.6 会话保存时持久化任务（`save_current_session` 方法附近）

在 `save_current_session` 方法中（约第 887 行），在保存会话之后追加：

```python
        # 持久化任务追踪器
        if hasattr(self, '_task_tracker') and self._task_tracker and self._current_session_id:
            self._task_tracker.save(self._current_session_id)
```

---

## 二、修改 `backend/src/agent/enhanced_simple_agent.py`

### 2.1 新增可选参数 `subagent_orchestrator`

在 `__init__` 方法的参数列表中新增：

```python
        subagent_orchestrator=None,   # SubAgentOrchestrator 引用（可选）
```

在 `__init__` 方法体中存储引用（在 `self.context_manager = ContextManager(...)` 之前或之后）：

```python
        self._subagent_orchestrator = subagent_orchestrator
```

### 2.2 检测到需要子代理时自动委托

在实际使用中，**不需要在 EnhancedSimpleAgent 中做自动委托判断**——这个决策权交给主 Agent 的 LLM。SubAgentTool 已经作为一个可调用工具注册，LLM 会自动判断何时使用。

如果未来想做自动委托（不需要 LLM 判断），可以在 `_try_execute_ready_tool` 调用前加一层拦截，但**本文档不推荐在 Phase 1 做这个**，因为引入复杂度高，收益不确定。

---

## 三、系统提示词注入时机验证

检查 `_build_system_prompt` 被调用的地方，确保每次对话都注入最新指引：

| 调用位置 | 约第几行 | 是否正确 |
|----------|----------|----------|
| `__init__` | ~90 行 | ✅ 初始化时注入 |
| `chat`（同步） | ~616 行 | ✅ 每次同步对话前重建 |
| `achat`（流式） | ~677 行 | ✅ 每次流式对话前重建 |

无需额外修改。

---

## 四、修改 `backend/src/main.py`（可选 — 持久化增强）

如果需要自动恢复任务列表，在 `lifespan` 的 shutdown 阶段保存：

```python
# 在 lifespan 的 yield 之后（shutdown 阶段）
if _agent and hasattr(_agent, '_task_tracker'):
    try:
        session_id = getattr(_agent, '_current_session_id', None)
        if session_id:
            _agent._task_tracker.save(session_id)
    except Exception:
        pass
```

---

## 五、测试清单

改造完成后，按以下 checklist 逐项验证：

### 5.1 SubAgent 功能验证

- [ ] 发送："搜索 backend/src/agent/ 目录下所有 Python 文件中包含 'asyncio' 的行"
  - 预期：Agent 调用 `subagent` 工具 spawn 子代理 → 子代理用 execute_command (grep) 搜索 → 返回摘要 → 主 Agent 基于摘要回复
  - 检查：主对话的消息列表中不应出现几千行 grep 输出

- [ ] 发送："并行检查以下 3 件事：1) 列出 frontend/src/views/ 下的所有文件 2) 检查 backend/src/tools/ 的代码风格 3) 查看 backend/pyproject.toml 的内容"
  - 预期：Agent 调用 `subagent` 的 parallel_spawn → 3 个并行子代理 → 主 Agent 看到 3 个摘要

### 5.2 Task 功能验证

- [ ] 发送："帮我完成以下任务：1) 在 backend/src/ 下创建一个 utils/helper.py 2) 在里面写一个时间格式化函数 3) 写一个测试用例 4) 运行测试"
  - 预期：Agent 先 task_create 4 个任务 → 逐个完成 → 不遗漏步骤

- [ ] 检查：工具调用日志中应出现 task_create / task_start / task_complete 调用

### 5.3 回归验证

- [ ] 正常对话（无复杂任务）：不应出现 SubAgent 或 Task 工具调用
- [ ] MCP 工具：应正常工作，不受影响
- [ ] 技能加载：SkillTool 应正常工作
- [ ] 记忆系统：MemoryCapture / MemoryFlush 应正常触发

---

## 六、回滚方案

如果出现严重问题，回滚仅需两步：

1. 将 `backend/src/tools/__init__.py` 恢复到修改前的版本（移除 SubAgentTool 和 TaskTool 的导入和导出）

2. 在 `backend/src/agent/myclaw_agent.py` 的 `_setup_tools` 中移除三行注册代码：
   ```python
   # 注释掉以下三行
   # registry.register_tool(SubAgentTool(orchestrator=self._subagent_orchestrator))
   # registry.register_tool(TaskTool(tracker=self._task_tracker))
   ```

新创建的 4 个 .py 文件不需要删除，它们的存在不影响运行。

---

## 七、后续优化方向

| 优先级 | 方向 | 说明 |
|--------|------|------|
| P1 | 子代理的 summary_llm | 当前复用主 LLM 做摘要，后续可配置独立的轻量模型（如 glm-4-flash）降成本 |
| P2 | Context Guard 自动路由 | 在 `_try_execute_ready_tool` 中根据工具名自动判断是否委托 |
| P3 | 任务模板 | 为常见任务类型（代码重构、项目初始化、文档生成）预定义任务模板 |
| P4 | 子代理超时策略 | 基于任务复杂度动态调整 timeout_seconds |

---

*本文档与以下源文件配套使用：*
- `backend/src/agent/subagent_orchestrator.py`
- `backend/src/tools/builtin/subagent_tool.py`
- `backend/src/agent/task_tracker.py`
- `backend/src/tools/builtin/task_tool.py`
