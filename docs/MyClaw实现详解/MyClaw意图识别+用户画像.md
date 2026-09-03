# Agent 核心能力升级设计方案

> 基于 Claude Code v2→v4、WorkBuddy、学术界 Memory 综述的调研，为 MyClaw 的意图识别、计划与执行、用户画像三个维度设计升级方案。
>
> _文档版本：v1.1（2026-07-30 更新，对齐实际实现）_

---

## 调研摘要

### Claude Code 的做法

Claude Code 是当前 AI 编程 Agent 领域的标杆。其核心设计在 agent loop v2 中实现：

| 机制 | 实现方式 |
|------|---------|
| **两阶段强制分离** | Planning Phase（禁止副作用工具）→ Execution Phase（锁定计划执行） |
| **TODO 系统** | 结构化计划：`{id, description, status, dependencies[], tools_required[]}`，构建 DAG |
| **依赖跟踪** | 仅当所有依赖项 `completed` 时任务才进入 `ready` 队列；失败时自动阻塞下游 |
| **计划 enforce** | 系统提示词硬约束 + 工具门控。在 Planning Phase 直接屏蔽 Write/Edit/Bash |
| **动态重新规划** | v3/v4 中增加：偏离计划自动触发重新规划子流程 |

关键设计哲学：**不是"建议 LLM 先规划"，而是"在代码层面强制 LLM 必须先规划后执行"**。

### WorkBuddy 的做法

| 模式 | 工具权限 | 执行方式 | 适用场景 |
|------|---------|---------|---------|
| **Ask** | 只读工具（Read/Search/WebFetch/Memory/RAG），禁止副作用工具（Write/Edit/Bash） | 直接回复 | 概念咨询、代码解释、只读分析 |
| **Plan** | 先只读（生成计划）→ 用户确认 → 解锁全部工具逐步执行 | 两阶段：规划 → 确认 → 执行 | 复杂任务拆分 |
| **Craft** | 全部工具 | 自动 ReAct 循环 | 全自动编程 |

关键设计哲学：**用模式切换来实现意图路由——用户显式选择模式，不同模式匹配不同的工具权限和交互流程**。

### 学术界的用户画像方案（2025）

Mercedes-Benz AG 和乌尔姆大学的论文描绘了完整画像框架：

```
Profile JSON（初始化空字段）
    ↓
LLM Agent 解析对话 → 提取新偏好 → 增量更新字段
    ↓
画像成熟后注入 Response Generator → 调整回复风格
    ↓
记忆-画像联动 → 长期记忆检索 + 画像引导 → 个性化输出
```

关键设计哲学：**画像不是一次性创建，而是对话的自然副产品——每次对话后自动更新**。

---

## 一、意图识别：从"零代码"到"用户显式模式选择"

### 1.1 设计理念

放弃正则自动分类（会误判），直接学习 WorkBuddy 的做法：**让用户自己选模式**。前端增加模式选择按钮，用户在发送消息前明确选择 Ask / Plan / Craft，后端根据模式过滤工具集和执行流程。

理由：
- 正则分类无法 100% 准确，误判比不分类更糟（用户预期被违背）
- 用户自己最清楚意图，把选择权交给用户零误判
- 实现更简单——不需要维护分类规则，只需要工具过滤 + 流程控制

### 1.2 三种模式定义

```
┌─────────────────────────────────────────────────────────────┐
│  Ask          │  Plan           │  Craft                   │
│  只读问答      │  规划→确认→执行  │  全自动编程               │
├───────────────┼─────────────────┼──────────────────────────┤
│ 工具：只读     │ 工具：先只读     │ 工具：全部                │
│ Read/Search   │ → 生成计划       │ Write/Edit/Bash          │
│ WebFetch      │ → 用户确认       │ Automation               │
│ Memory/RAG    │ → 解锁全部工具   │ 全部可用                  │
│ Skill/MCP     │ → 逐步执行       │                          │
│               │                  │                          │
│ 禁止：         │ 禁止（规划期）：  │ 执行方式：                │
│ Write/Edit    │ Write/Edit/Bash  │ 自动 ReAct 循环           │
│ Bash          │                  │ 无需用户确认              │
│ Automation    │                  │                          │
└───────────────┴─────────────────┴──────────────────────────┘
```

| 模式 | 允许的工具 | 禁止的工具 | 执行流程 |
|------|-----------|-----------|---------|
| **Ask** | read, search_content, search_file, list_dir, web_search, web_fetch, http_request, browser(只读action), memory_search, session_search, rag, skill, mcp | write, edit, execute_command, bash, automation, **memory_add** | 单轮 ReAct；turn_context 注入 Ask 只读指令；LLM 直接回复 |
| **Plan** | 规划期同 Ask；执行期全部 | 规划期禁止副作用工具（含 memory_add） | 两阶段：规划→用户确认→执行；规划指令含写操作禁令 |
| **Craft** | 全部 | 无 | 自动 ReAct 循环，无需确认 |

### 1.3 前端实现

#### 1.3.1 新增组件：`AgentModeSelector.vue`

放置在输入区域上方，使用 ant-design-vue 的 `Segmented` 组件：

- 三种模式选项：Ask 只读 / Plan 规划 / Craft 全自动
- 支持 `v-model` 双向绑定，导出 `AgentMode` 类型
- 默认值 `craft`，保持向后兼容

```typescript
// 核心接口
export type AgentMode = 'ask' | 'plan' | 'craft'
```

#### 1.3.2 集成到 ChatView.vue

**① 模式状态管理**：
```typescript
const agentMode = ref<AgentMode>('craft')
const planGenerated = ref(false)        // 是否展示 Plan 确认卡片
const pendingPlan = ref(null)           // 待确认的计划数据
const renderedPlan = ref('')            // 渲染后的计划 Markdown
const lastPlanMessage = ref('')         // 规划阶段的原始用户消息
```

**② 输入框 Placeholder 联动**（`computed` 属性 `inputPlaceholder`）：
- Ask: "只读模式：提问、搜索、分析，不能修改文件 (Enter 发送)"
- Plan: "规划模式：先分析计划，确认后执行 (Enter 发送)"
- Craft: "输入 / 使用技能 (Enter 发送, Shift+Enter 换行)"

**③ `runChatRequest` 选项增强**（`ChatRequestOptions` 接口新字段）：

| 字段 | 类型 | 说明 |
|------|------|------|
| `mode` | `AgentMode` | 不传时使用当前 `agentMode` 状态 |
| `planConfirmed` | `boolean` | Plan 确认后重新发送时传 `true` |
| `skipUserMessage` | `boolean` | Plan 确认场景跳过追加用户消息（规划阶段已显示） |

**④ Plan 确认卡片**：
- 由 `plan_generated` SSE 事件触发显示
- 渲染计划为 Markdown（通过 `renderMarkdown` + `DOMPurify` 安全渲染）
- 两个按钮："取消"（调用 `cancelPlan` 清理状态）/"确认，开始执行"（调用 `confirmPlan`）
- `confirmPlan` 以 `mode: 'craft'` + `planConfirmed: true` + `skipUserMessage: true` 重新发送原消息

**⑤ SSE 事件处理新增 `plan_generated` 分支**：
- 从 `event.plan`（`PlanTodoItem[]`）中提取计划的每个 TODO 项
- 渲染为 Markdown 格式：`- [ ] {description} *(依赖: ...)*  [工具: ...]`
- 设置 `planGenerated = true` 展示确认卡片

#### 1.3.3 修改 `chat.ts` 的 `SendMessageOptions`

```typescript
// api/chat.ts
export interface PlanTodoItem {
  id: string
  description: string
  dependencies: string[]
  tools_required: string[]
}

export interface SendMessageOptions {
  // ... 现有字段保持不变
  mode?: 'ask' | 'plan' | 'craft'
}

// SSE 流事件中新增
export interface StreamEvent {
  // ...
  /** plan_generated 事件的计划数据 */
  plan?: PlanTodoItem[]
}
```

### 1.4 后端实现

#### 1.4.1 新增文件：`agent/tool_mode_filter.py`

包装外部 `ToolRegistry`，根据 Agent 模式过滤可用工具：

```python
class ToolMode(Enum):
    READ_ONLY = "read_only"  # Ask / Plan 规划期
    FULL = "full"            # Craft / Plan 执行期

# 副作用工具集合（READ_ONLY 模式下被屏蔽；含大小写变体）
SIDE_EFFECT_TOOLS = frozenset({
    "write", "Write", "edit", "Edit",
    "bash", "execute_command", "automation", "memory_add",
})

# 提示词展示用规范名（与代码门控名单对齐，避免文案漂移）
SIDE_EFFECT_TOOL_LABELS = (
    "Write", "Edit", "execute_command", "bash", "automation", "memory_add",
)

def readonly_block_message(tool_name: str) -> str:
    """统一拒绝文案（须含「只读模式」「被禁用」，供评测 scorer 识别）。"""
    ...

class ToolModeFilter:
    """包装外部 ToolRegistry，根据模式过滤工具。

    设计要点：不修改外部包 ToolRegistry 源码，通过 wrapper 实现模式切换。
    """
    def set_mode(self, mode: ToolMode) -> None: ...
    def get_available_tool_names(self) -> List[str]: ...
    def get_filtered_schemas(self) -> List[dict]: ...  # READ_ONLY 下再按 function.name 过滤
    def get_tool(self, name: str) -> Optional[Tool]: ...  # 副作用工具返回 None
```

> Bash 真实注册名为 **`execute_command`**；`bash` 为兼容保留。Memory 在注册时 expandable 展开为 `memory_search` / `memory_add`，后者进入副作用名单。

#### 1.4.2 工具门控：三层执行拦截（不可只拦 FINISH 批量路径）

流式 ReAct 存在两条工具执行入口：

| 路径 | 时机 | 说明 |
|------|------|------|
| `_try_execute_ready_tool` | LLM 流式过程中参数 JSON 一旦完整即执行 | 「边收边跑」，历史上若未做门控会绕过只读限制 |
| `_execute_tools_batch` | `FINISH` 后批量执行剩余调用 | 原先主要在此做 `ToolModeFilter` 检查 |

当前实现在 **两条路径 + 底层执行** 均硬拦截：

```
待执行工具 tc：
├── _readonly_block_message_if_needed(name)
│   └── READ_ONLY 且 name ∈ SIDE_EFFECT_TOOLS
│       → 返回统一拒绝文案，不调用真实工具
│       → 早执行路径仍 emit tool_start / tool_finish（status=blocked_readonly）
├── _execute_tools_batch：过滤后按 has_side_effects 并行/串行
└── _execute_tool_call：入口再次检查（纵深防御，防止未来新路径漏网）
```

仅依赖 schema 过滤不够：模型可能凭 system/历史幻觉调用未暴露的工具名；因此 **执行层硬拦是 Ask/Plan 安全的底线**，提示词用于降低违规调用意愿。

#### 1.4.3 `achat()` 入口：模式分流 + turn_context 提示词

模式指令与相关记忆一样，走 ephemeral **`turn_context`**（经 `_compose_turn_context` 拼接），**不写入**冻结 system、**不入**会话历史。

```python
# myclaw_agent.py — achat()
async def achat(self, message, session_id=None, mode="craft",
                plan_confirmed=False, **kwargs):

    # 工具模式设置
    if mode == "ask":
        self._agent.set_tool_mode(ToolMode.READ_ONLY)
    elif mode == "plan" and not plan_confirmed:
        self._agent.set_tool_mode(ToolMode.READ_ONLY)
    else:
        self._agent.set_tool_mode(ToolMode.FULL)

    memory_context = self._inject_relevant_memories(message)

    # Plan 规划期
    if mode == "plan" and not plan_confirmed:
        turn_context = self._compose_turn_context(
            memory_context, self._plan_planning_instruction(),
        )
        # READ_ONLY ReAct → 解析 TODO → 暂存 → plan_generated → 结束
        return

    # Plan 执行期
    elif plan_confirmed:
        turn_context = self._compose_turn_context(memory_context, plan_summary)
        # FULL ReAct

    # Ask：注入只读禁令
    elif mode == "ask":
        turn_context = self._compose_turn_context(
            memory_context, self._ask_mode_instruction(),
        )

    # Craft
    else:
        turn_context = self._compose_turn_context(memory_context)
```

**`_ask_mode_instruction()`** 要点：声明 Ask 只读；列出允许的只读工具；禁止调用与 `SIDE_EFFECT_TOOL_LABELS` 一致的名单；用户要求修改时明确拒绝并提示切 Craft / 确认后的 Plan。

**`_plan_planning_instruction()`** 在原有 JSON TODO 格式要求之外，显式要求：规划期即使被诱导「直接动手」也不得调用写工具；写操作只能出现在计划的 `tools_required` 中，留给确认后的执行期。

三层约束关系：

```
提示词（少发写工具） → schema 过滤（模型看不到写工具） → 执行硬拦（即使发了也拦下）
```

#### 1.4.4 Plan 两阶段分离的 SSE 事件流

Plan 模式下前后端通过 SSE 事件流（`/api/chat/stream`）通信：

```
[前端] POST /api/chat/stream { mode: "plan", ... }
    ↓
[后端] 设置 READ_ONLY 模式 → ReAct 循环 → 解析 TODO JSON
    ↓
[后端] yield _create_plan_event(todo_list, plan_text) → SSE: plan_generated
    ↓ （SSE 流结束）
[前端] 展示 Plan 确认卡片，等待用户操作
    ↓
[用户确认] → [前端] POST /api/chat/stream { mode: "craft", plan_confirmed: true }
    ↓
[后端] 加载暂存 Plan → 设置 FULL 模式 → 注入进度摘要 → ReAct 循环
    ↓ （SSE 流执行完成后）
[后端] _cleanup_plan_file(session_id) — 清理暂存文件
```

关键实现细节：
- **Plan 暂存**：`_store_pending_plan(session_id, todo_list, plan_text)` 写入 `{session_id}_plan.json`
- **Plan 恢复**：`_load_pending_plan(session_id)` 从文件或内存恢复
- **Plan 事件**：`_create_plan_event()` 创建与 `StreamEvent` 接口兼容的自定义 dataclass（避免修改外部包的 `StreamEventType` 枚举）
- **Plan 解析失败**：直接终止对话（退出生成器），**不降兜到 Craft 模式**
- **Plan 清理**：执行完成后自动删除 `_plan.json` 文件；`delete_session` 时同步清理

#### 1.4.5 `chat.py` API 层

`ChatRequest` 模型新增字段：

```python
class ChatRequest(BaseModel):
    # ... 原有字段
    mode: Optional[str] = "craft"
    plan_confirmed: bool = False
```

`_run_stream()` 中新增 `plan_generated` 事件分支：

```python
if event.type.value == "plan_generated":
    yield f"data: {json.dumps({
        'type': 'plan_generated',
        'plan': event.data.get('plan', []),
        'content': event.data.get('content', ''),
    })}\n\n"
```

### 1.5 改造量（模式选择器 + 工具过滤）

| 新增/修改 | 文件 | 实际行数 |
|----------|------|---------|
| 新增 | `components/AgentModeSelector.vue` | ~90 行 |
| 修改 | `views/ChatView.vue`（集成选择器 + Plan 确认卡片 + placeholder 联动 + mode 透传） | ~130 行 |
| 修改 | `api/chat.ts`（`SendMessageOptions` 增加 `mode`/`planConfirmed` 字段 + `PlanTodoItem` 类型） | ~20 行 |
| 新增 | `agent/tool_mode_filter.py`（`ToolMode` 枚举 + `ToolModeFilter` wrapper） | ~160 行 |
| 修改 | `agent/myclaw_agent.py`（`achat()` 入口 mode 分流 + Plan 两阶段 + 暂存/恢复/清理） | ~120 行 |
| 修改 | `api/chat.py`（`ChatRequest` 增加 `mode`/`plan_confirmed` 字段 + SSE `plan_generated` 事件） | ~30 行 |
| 修改 | `agent/enhanced_simple_agent.py`（`set_tool_mode()` + `_execute_tools_batch` 工具门控） | ~60 行 |
| **小计** | | **~610 行** |

### 1.6 Plan 模式详细实现：从"被动存储"到"强制 Plan-Then-Execute"

上述 §1.2-1.4 描述了三种模式的入口和工具过滤。其中 **Ask 和 Craft 模式**只需工具过滤即可（§1.4 已覆盖），而 **Plan 模式**需要额外的两阶段执行机制——这就是本节的内容。

#### 设计理念

参考 Claude Code v2 的核心设计：**用工具门控（Tool Gate）强制两阶段分离**，而非仅依赖 prompt 软引导。

#### 方案设计

```
[Planning Phase] — READ_ONLY ReAct
  工具集：{read, search_content, search_file, web_search, web_fetch, skill, mcp}
  屏蔽：{write, edit, bash, automation}
  LLM 输出：自然语言分析 + 结构化 TODO JSON
  Plan 生成失败 → 终止对话（不降兜）

        ↓ plan_generated SSE 事件 → 前端展示确认卡片

[用户确认/取消]
  ├── 确认 → 新 POST /api/chat/stream { mode: "craft", plan_confirmed: true }
  └── 取消 → 前端清理状态，对话结束

[Execution Phase] — FULL ReAct
  工具集：全部
  从文件恢复 Plan → 注入进度摘要到系统提示词
  TodoScheduler 按 DAG 依赖顺序调度
  执行完成后 → 自动清理 plan 文件
```

#### TODO 数据结构升级

在现有 `TaskTracker` 的基础上增加 `dependencies` 和 `tools_required`：

```python
@dataclass
class TodoItem:
    id: str                    # UUID
    description: str           # 自然语言描述
    status: Literal["pending", "ready", "in_progress", "completed", "failed"]
    dependencies: list[str]    # 依赖的前置 todo_id 列表
    tools_required: list[str]  # 完成任务需要的工具列表
```

#### DAG 执行调度器

核心方法（~280 行）：

- `load_from_plan(plan_data)` — 从结构化 JSON 加载 TODO 列表，自动 `_update_ready_status()`
- `get_ready_tasks()` — 返回所有依赖已满足的 pending/ready 任务
- `mark_completed(todo_id)` — 标记完成，自动触发下游 `_update_ready_status()`
- `mark_failed(todo_id, reason)` — 标记失败，自动 `fail_downstream()`
- `fail_downstream(todo_id)` — 迭代式 BFS 阻塞下游（避免递归栈溢出）
- `get_progress_summary()` — 生成进度 Markdown（Plan 执行期由 TodoScheduler 经 turn_context 注入；TaskTracker 版本供工具/API）
- `check_plan_divergence(current_action)` — **[实验性]** 偏离检测（当前为简单关键词匹配，后续将升级为语义匹配）
- `is_all_completed()` / `has_failed()` — 完成度检查
- `to_plan_json()` — 序列化为前端可消费格式

**注意**：`TodoItem.is_ready` 属性仅适用于无依赖场景。完整的就绪判断应使用 `TodoScheduler.get_ready_tasks()`（检查所有依赖项是否已完成）。

#### 与现有架构的集成点

| 改动点 | 位置 | 说明 |
|--------|------|------|
| Planning Phase 启动 | `myclaw_agent.py` 的 `achat()` | 注入强化规划指令（含副作用禁令）+ READ_ONLY ReAct |
| Ask 提示词 | `myclaw_agent.py` 的 `_ask_mode_instruction()` | ephemeral turn_context，与 `SIDE_EFFECT_TOOL_LABELS` 对齐 |
| 工具门控 | `tool_mode_filter.py` + `enhanced_simple_agent.py` 的 `_try_execute_ready_tool` / `_execute_tools_batch` / `_execute_tool_call` | schema 过滤 + 流式早执行/批量/底层三处硬拦 |
| Plan 事件 | `myclaw_agent.py` 的 `_create_plan_event()` | 自定义 dataclass 模拟 StreamEvent 接口 |
| Plan 持久化 | `myclaw_agent.py` 的 `_store_pending_plan` / `_load_pending_plan` / `_cleanup_plan_file` | 文件 + 内存双通道 |
| Execute Phase | `myclaw_agent.py` 的 `achat()` plan_confirmed 分支 | 恢复 Plan → 注入进度 → FULL ReAct |
| Plan 文件清理 | `myclaw_agent.py` 的 `delete_session()` + Plan 执行完成后 | 防资源泄漏 |

#### 改造量（Plan 模式专用）

| 新增文件 | 修改文件 | 实际行数 |
|---------|---------|---------|
| `agent/todo_scheduler.py` | `agent/enhanced_simple_agent.py` | ~280 行 |
| | `agent/task_tracker.py` | ~30 行 |

---

## 二、用户画像：从"碎片化记忆"到"画像驱动行为"

### 2.1 设计理念

参考学术界的最佳实践：**画像不是静态配置，而是对话的自然副产品**——但前提是偏好等信息已通过显式路径写入 Memory。当前实现中，对话结束后 **不会** 用正则从用户原话自动抓取记忆；画像上游依赖 Agent `memory_add`、Memory Flush 与 HTTP `POST /api/memory/capture`。`ProfileAggregator` 再按条件从 Qdrant 中的 preference/entity 等记忆聚合到 `USER.md`。

### 2.2 方案设计

```
对话结束 (achat() 末尾)
  ↓
ProfileAggregator.should_trigger() — 检查是否需要聚合
  ↓
ProfileAggregator._collect_memories() — 从 Memory（Qdrant）检索 preference/entity/decision 分类
  ↓
ProfileAggregator._llm_aggregate() — LLM 聚合为结构化画像摘要（单次调用 ~2000 token）
   降兜：_text_aggregate() — 纯文本聚合并通过关键词区分技术栈/工作领域
  ↓
ProfileAggregator._update_user_md() — 更新到 ~/.helloclaw/identity/USER.md 的 AUTO 区域
   机制：HTML 注释标记区域边界（<!-- AUTO:xxx --> … <!-- /AUTO:xxx -->）
   写入方式：_atomic_save_user_md() — 临时文件 + os.replace 原子替换，降兜到 save_file()
   注入防护：清理 new_value 中的 <!-- AUTO:xxx --> 标记，防止 LLM 输出注入
  ↓
下次对话前 — _build_system_prompt() 自动注入 USER.md 到系统提示词（已有逻辑，无需改动）
```

**设计原则：用户画像跟随用户而非项目**。画像直接写入 `~/.helloclaw/identity/USER.md`，与工作区无关。

### 2.3 USER.md 模板重设计

现有 `USER.md` 模板仅有姓名/称呼/时区/备注 5 个静态字段。重设计后新增动态画像区域，由 `ProfileAggregator` 定期更新：

```markdown
# USER.md - 关于你的人类

## 基本信息

- **姓名：** 
- **称呼：** 
- **时区：** Asia/Shanghai

## 技术栈

<!-- AUTO:tech_stack -->
（暂无数据）
<!-- /AUTO:tech_stack -->

## 工作领域

<!-- AUTO:work_domain -->
（暂无数据）
<!-- /AUTO:work_domain -->

## 沟通偏好

<!-- AUTO:communication -->
（暂无数据）
<!-- /AUTO:communication -->

## 代码风格

<!-- AUTO:code_style -->
（暂无数据）
<!-- /AUTO:code_style -->

## 备注

_其他手动维护的信息_
```

**字段来源**：

| 区域 | 来源 | 更新方式 |
|------|------|---------|
| 基本信息 | 用户手动填写 / Agent 对话中主动 Edit | 手动 |
| 技术栈 / 工作领域 / 沟通偏好 / 代码风格 | Memory preference/entity 聚合 | 自动（ProfileAggregator） |
| 备注 | 用户手动填写 | 手动 |
| 聚合时间 / 版本 | ProfileAggregator 写入 | 自动 |

**聚合规则**：`ProfileAggregator` 读取 Memory → LLM 聚合为结构化文本 → 更新 USER.md 中对应 `<!-- AUTO:xxx -->` 区域（保留手动区域不变）。HTML 注释标记确保非自动区域不会被覆盖。

### 2.4 画像聚合触发机制

| 触发条件 | 实现 |
|---------|------|
| 每 10 轮对话后 | `should_trigger()` 检查 `turn_count - _last_aggregated_turn >= 10` |
| 手动触发 | 已移除 Agent 工具面；仅保留 `achat()` 末尾 `ProfileAggregator.aggregate()` 自动路径 |
| preference 记忆超过 20 条 | `should_trigger()` 检查 `preference_count >= 20`（通过 `_list_recent(top_k=200)` 精确统计） |

### 2.5 画像如何驱动行为

USER.md 已由 `_build_system_prompt()` 自动注入系统提示词（`myclaw_agent.py`），**无需额外注入逻辑**。Agent 在每轮对话中天然能看到画像内容。

**行为调节的操作化规则**（通过 AGENTS.md §1 会话启动清单引导）：

| 画像字段 | 驱动行为 |
|---------|---------|
| 沟通偏好 = "简洁" | 回复长度控制在 200 字内，优先给代码 |
| 技术栈 = "Python, FastAPI" | 代码示例优先用 Python |
| 代码风格 = "type-hinted" | 示例代码自动添加类型注解 |
| 工作领域 = "金融" | 领域术语保持原有表达，不逐字翻译 |

### 2.6 改造量

| 新增文件 | 修改文件 | 实际行数 |
|---------|---------|---------|
| `agent/profile_aggregator.py` | `agent/myclaw_agent.py`（画像聚合触发 + `profile_aggregator` 注入） | ~260 行 |
| | `tools/builtin/memory.py`（曾新增手动画像 action，现已精简为仅 search/add） | ~40 行 |
| | `workspace/templates/identity/USER.md`（模板重设计） | ~50 行 |
| **合计** | | **~350 行** |

### 2.7 画像聚合器的关键实现细节

**双接口设计**：
- `aggregate()` — 异步方法，供 `myclaw_agent.py` 的 `achat()` 末尾调用
- `aggregate_sync()` — 同步方法，供 `MemoryTool`（同步 `run()` 方法）直接调用，避免在现有事件循环中创建 `asyncio.new_event_loop()` 导致崩溃

**降兜方案**：
- LLM 可用 → `_llm_aggregate()` 调用轻量 LLM（`glm-4-flash`）聚合
- LLM 不可用 → `_text_aggregate()` 纯文本聚合：
  - 通过 `tech_keywords` 集合（Python/Java/Vue/React/FastAPI/Docker 等）区分 `tech_stack` 和 `work_domain`
  - `communication` 和 `code_style` 从 preference 记忆分配
  - `work_domain` 不会永远为空（用非技术关键词 + decision 记忆补充）

**原子写入保护**：
- `_atomic_save_user_md(content)` — 写入 `identity_dir/.user_md_*.tmp` 后 `os.replace()` 原子替换
- 失败时降兜到 `self.identity.save_file("USER.md", content)` 直接写入
- 写入前清理 `new_value` 中的 HTML 注释标记（防止 LLM 输出注入 `<!-- AUTO:xxx -->`）

---

## 总体改造评估

| 维度 | 现有成熟度 | 目标成熟度 | 新增代码 | 修改代码 | 核心复杂度 |
|------|-----------|-----------|---------|---------|-----------|
| 模式选择与执行 | 1/10 | 8/10 | ~530 行 | ~240 行 | 中 — 模式选择器(低) + Plan 两阶段(中) |
| 用户画像 | 6/10 | 8/10 | ~260 行 | ~90 行 | 中 — LLM 聚合 + 原子写入 |
| **合计** | | | **~790 行** | **~330 行** | |

### 实施顺序（按 ROI）

1. **模式选择器 + Ask/Craft 模式**（1-2 天）— 前端 Segmented + 后端工具过滤。立即可用，零误判 ✅
2. **Plan 模式两阶段执行**（3-5 天）— 在模式选择器基础上增加 DAG 调度器和工具门控 ✅
3. **用户画像**（2-3 天）— 依赖已有 Memory 系统，投入中等 ✅

### 核心风险与规避

| 风险 | 规避措施 |
|------|---------|
| 用户选错模式（如用 Ask 做需要写文件的任务） | Ask 模式下副作用工具被屏蔽时，LLM 会告知用户"当前为只读模式，请切换到 Craft 或 Plan 模式" |
| Plan 生成质量差 | Plan 生成失败直接终止对话（不降兜到 Craft，避免用户预期违背）。用户可重新描述任务或切换到 Craft 模式 |
| Plan 文件残留 | `delete_session` 和 Plan 执行完成后自动清理 `_plan.json` 文件 |
| 画像聚合消耗 token | 聚合使用轻量 LLM（如 glm-4-flash），每次 ~2000 token。10 轮触发一次，平均每轮仅 +200 token |
| USER.md 并发写入冲突 | 使用 `os.replace` 原子替换（临时文件 → 目标文件），降兜到直接写入 |
| 画像聚合触发条件不准 | 使用 `_list_recent(top_k=200)` 精确统计 preference 记忆数量，而非粗略估算 |

### 对比总结

| 能力 | Claude Code | WorkBuddy | 本方案 |
|------|------------|-----------|--------|
| 意图识别 | 隐式（全 ReAct） | **显式（Ask/Plan/Craft 用户选择）** | 显式（三模式选择器，学习 WorkBuddy） |
| 计划 enforcement | **硬约束（工具门控）** | 软约束（用户确认） | 硬约束（ToolModeFilter + 运行时工具门控）|
| 用户画像 | 无（依赖用户主动提供信息） | 无 | **依赖显式 Memory 写入后条件聚合，画像驱动行为** |

---

## 实现文件清单

### 新增文件（4 个）

| 文件 | 说明 | 行数 |
|------|------|------|
| `frontend/src/components/AgentModeSelector.vue` | 前端模式选择器组件 | ~90 |
| `backend/src/agent/tool_mode_filter.py` | 工具模式过滤器（ToolModeFilter wrapper） | ~160 |
| `backend/src/agent/todo_scheduler.py` | DAG 任务调度器 | ~280 |
| `backend/src/agent/profile_aggregator.py` | 用户画像聚合器 | ~260 |

### 修改文件（8 个）

| 文件 | 说明 | 改动 |
|------|------|------|
| `frontend/src/views/ChatView.vue` | 集成模式选择器 + Plan 确认卡片 + `skipUserMessage` | +130 行 |
| `frontend/src/api/chat.ts` | 新增 `AgentMode`/`PlanTodoItem` 类型 + 模式/planConfirmed 参数 | +20 行 |
| `backend/src/api/chat.py` | `ChatRequest` 新增 `mode`/`plan_confirmed` + SSE `plan_generated` 事件 | +30 行 |
| `backend/src/agent/myclaw_agent.py` | `achat()` mode 分流 + Plan 两阶段 + 暂存/恢复/清理 + 画像聚合触发 | +160 行 |
| `backend/src/agent/enhanced_simple_agent.py` | `set_tool_mode()` + `_execute_tools_batch` 工具门控 + `executed_ids` 参数 | +60 行 |
| `backend/src/agent/task_tracker.py` | `Task` 新增 `tools_required` 字段 | +20 行 |
| `backend/src/tools/builtin/memory.py` | 曾注入 `profile_aggregator` 与手动聚合 action；现 Agent 面仅保留 `memory_search` / `memory_add` | 精简 |
| `backend/src/workspace/templates/identity/USER.md` | 模板重设计，4 个 AUTO 自动区域 | 重写 |
