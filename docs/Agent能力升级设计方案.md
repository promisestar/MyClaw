# Agent 核心能力升级设计方案

> 基于 Claude Code v2→v4、WorkBuddy、学术界 Memory 综述的调研，为 MyClaw 的意图识别、计划与执行、用户画像三个维度设计升级方案。

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
| **Ask** | read, search_content, search_file, list_dir, web_search, web_fetch, http_request, browser(只读action), memory_*, rag, skill, mcp | write, edit, execute_command, automation | 单轮 ReAct，LLM 直接回复 |
| **Plan** | 规划期同 Ask；执行期全部 | 规划期禁止副作用工具 | 两阶段：规划→用户确认→执行 |
| **Craft** | 全部 | 无 | 自动 ReAct 循环，无需确认 |

### 1.3 前端实现

#### 1.3.1 新增组件：`AgentModeSelector.vue`

放置在输入区域上方（`chat-input-wrapper` 内，`chat-input` div 之前），使用 ant-design-vue 的 `Segmented` 组件：

```vue
<!-- components/AgentModeSelector.vue -->
<script setup lang="ts">
import { Segmented } from 'ant-design-vue'
import { EditOutlined, FileSearchOutlined, ThunderboltOutlined } from '@ant-design/icons-vue'

export type AgentMode = 'ask' | 'plan' | 'craft'

const props = defineProps<{
  modelValue: AgentMode
}>()

const emit = defineEmits<{
  'update:modelValue': [value: AgentMode]
}>()

const options = [
  { value: 'ask',   payload: 'ask',   label: 'Ask 只读' },
  { value: 'plan',  payload: 'plan',  label: 'Plan 规划' },
  { value: 'craft', payload: 'craft', label: 'Craft 全自动' },
]
</script>

<template>
  <Segmented
    :value="props.modelValue"
    :options="options"
    block
    size="small"
    @change="(val: string | number) => emit('update:modelValue', val as AgentMode)"
  />
</template>
```

#### 1.3.2 集成到 ChatView.vue

**① 新增 state**（在 `ChatView.vue` 的 `<script setup>` 中）：

```typescript
import AgentModeSelector from '@/components/AgentModeSelector.vue'
import type { AgentMode } from '@/components/AgentModeSelector.vue'

const agentMode = ref<AgentMode>('craft')
```

**② 模板改动** — 在 `chat-input-wrapper` 中，`chat-input` div 之前插入：

```html
<div class="chat-input-wrapper">
  <TaskPanel ... />

  <!-- 技能下拉 → 保持现有行为 -->
  <Transition name="skill-dropdown-fade">
    <div v-if="skillDropdownVisible" class="skill-dropdown">...</div>
  </Transition>

  <!-- 新增：模式选择器 -->
  <AgentModeSelector v-model="agentMode" />

  <!-- 附件预览条 → 保持现有行为 -->
  <div v-if="pendingAttachments.length > 0" class="chat-attachments-preview">...</div>

  <div class="chat-input" ...>
    <!-- 输入框：placeholder 根据模式变化 -->
    <Input.TextArea
      :placeholder="agentMode === 'ask'
        ? '只读模式：提问、搜索、分析，不能修改文件 (Enter 发送)'
        : agentMode === 'plan'
        ? '规划模式：先分析计划，确认后执行 (Enter 发送)'
        : '输入 / 使用技能 (Enter 发送, Shift+Enter 换行)'"
      ...
    />

    <div class="input-actions">
      <!-- 发送按钮：Plan 模式下文字变为"规划" -->
      <button
        v-else-if="inputMessage.trim()"
        class="send-btn active"
        @click="sendMessage"
      >
        {{ agentMode === 'plan' ? '📋' : '' }}
        <SendOutlined />
      </button>
    </div>
  </div>
</div>
```

**③ 发送时附带 mode** — 修改 `sendMessage` 方法中的 `sendMessageStream` 调用：

```typescript
const response = await chatApi.sendMessageStream(
  inputMessage.value.trim(),
  onStreamEvent,
  {
    sessionId: currentSessionId.value,
    skill: activeSkill.value || undefined,
    attachments: chatAttachments,
    workspacePath: workspaceStore.currentPath || undefined,
    mode: agentMode.value,          // ← 新增
    signal: abortController.value?.signal,
  }
)
```

**④ Plan 模式确认卡片** — 新增 inline 组件（或用 Modal）：

```html
<!-- Plan 确认卡片（Plan 模式下 planGenerated=true 时显示） -->
<Transition name="plan-card-fade">
  <div v-if="agentMode === 'plan' && planGenerated" class="plan-card">
    <div class="plan-card-header">
      <FileSearchOutlined /> 执行计划
    </div>
    <div class="plan-card-body" v-html="renderedPlan" />
    <div class="plan-card-actions">
      <Button @click="cancelPlan">取消</Button>
      <Button type="primary" @click="confirmPlan">确认，开始执行</Button>
    </div>
  </div>
</Transition>
```

`planGenerated` / `renderedPlan` 由 `onStreamEvent` 中新增的 `plan_generated` 事件类型驱动。

#### 1.3.3 修改 `chat.ts` 的 `SendMessageOptions`

```typescript
// api/chat.ts
export interface SendMessageOptions {
  // ... 现有字段保持不变
  /** Agent 模式：ask（只读）| plan（规划→确认→执行）| craft（全自动），默认 craft */
  mode?: 'ask' | 'plan' | 'craft'
}
```

并在 `sendMessageStream` 方法中：

```typescript
if (options.mode) {
  body.mode = options.mode
}
```

#### 1.3.4 视觉效果

```
┌─────────────────────────────────────────────────────────────────┐
│  ┌──────────┬──────────┬──────────┐                             │
│  │ Ask 只读 │ Plan 规划│ Craft 全自动 │  ← Segmented 组件       │
│  └──────────┴──────────┴──────────┘                             │
│                                                                 │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │ 📎  [只读模式：提问、搜索、分析，不能修改文件]                ││
│  │                                        [⚪] [+] [▶ 发送]    ││
│  └─────────────────────────────────────────────────────────────┘│
│  Enter 发送 · Shift+Enter 换行 · 支持拖拽文件                     │
└─────────────────────────────────────────────────────────────────┘

### 1.4 后端实现

```python
# myclaw_agent.py — achat() 入口增加 mode 参数
async def achat(self, message, mode: str = "craft", **kwargs):
    if mode == "ask":
        # 过滤工具集：移除副作用工具
        self._tool_registry.set_mode(ToolMode.READ_ONLY)
    elif mode == "plan":
        # Phase 1: 只读工具 → 生成计划
        self._tool_registry.set_mode(ToolMode.READ_ONLY)
        plan = await self._generate_plan(message)
        yield PlanGeneratedEvent(plan)  # 前端展示计划确认卡片
        # 等待用户确认（前端发 confirm 请求）
        # Phase 2: 解锁全部工具 → 按计划执行
        self._tool_registry.set_mode(ToolMode.FULL)
    else:  # craft
        self._tool_registry.set_mode(ToolMode.FULL)

    # 进入正常 ReAct 循环
    ...
```

```python
# tool_registry.py — 新增模式过滤
class ToolMode(Enum):
    READ_ONLY = "read_only"  # Ask / Plan 规划期
    FULL = "full"            # Craft / Plan 执行期

SIDE_EFFECT_TOOLS = {"write", "edit", "execute_command", "automation"}

class ToolRegistry:
    def set_mode(self, mode: ToolMode):
        self._mode = mode

    def get_available_tools(self) -> list[Tool]:
        if self._mode == ToolMode.READ_ONLY:
            return [t for t in self._tools if t.name not in SIDE_EFFECT_TOOLS]
        return self._tools
```

### 1.5 改造量（模式选择器 + 工具过滤）

| 新增/修改 | 文件 | 估计行数 |
|----------|------|---------|
| 新增 | `components/AgentModeSelector.vue` | ~40 行 |
| 修改 | `views/ChatView.vue`（集成选择器 + Plan 确认卡片 + placeholder 联动 + mode 透传） | ~80 行 |
| 修改 | `api/chat.ts`（`SendMessageOptions` 增加 `mode` 字段 + 透传） | ~8 行 |
| 新增 | `agent/tool_registry.py`（`ToolMode` 枚举 + `set_mode()` 过滤） | ~30 行 |
| 修改 | `agent/myclaw_agent.py`（`achat()` 入口 mode 分流） | ~40 行 |
| 修改 | `api/chat.py`（`ChatRequest` 增加 `mode` 字段） | ~8 行 |
| 修改 | `agent/enhanced_simple_agent.py`（工具调用前检查模式） | ~15 行 |
| **小计** | | **~221 行** |

### 1.6 Plan 模式详细实现：从"被动存储"到"强制 Plan-Then-Execute"

上述 §1.2-1.4 描述了三种模式的入口和工具过滤。其中 **Ask 和 Craft 模式**只需工具过滤即可（§1.4 已覆盖），而 **Plan 模式**需要额外的两阶段执行机制——这就是本节的内容。

#### 设计理念

参考 Claude Code v2 的核心设计：**用工具门控（Tool Gate）强制两阶段分离**，而非仅依赖 prompt 软引导。

#### 方案设计

```
[Planning Phase] — §1.4 中 Plan 模式的 Phase 1
  工具集：{read, search_content, search_file, web_search, web_fetch, skill, mcp}
  屏蔽：{write, edit, bash, automation}
  输出：结构化 TODO 清单（JSON）

        ↓ 用户可确认/修改计划（§1.3.2 的确认卡片）

[Execution Phase] — §1.4 中 Plan 模式的 Phase 2
  工具集：全部
  约束：必须按 TODO 顺序执行，只做 pending 任务
  偏离计划 → 自动标记为 failed + 触发重新规划
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

```python
# 新增文件：agent/todo_scheduler.py
class TodoScheduler:
    def get_ready_tasks(self) -> list[TodoItem]:
        """返回所有依赖已满足的 pending 任务"""
        completed = {t.id for t in self.todos if t.status == "completed"}
        return [
            t for t in self.todos
            if t.status == "pending"
            and all(dep in completed for dep in t.dependencies)
        ]

    def fail_downstream(self, todo_id: str):
        """任务失败 → 阻塞所有下游依赖"""
        for t in self.todos:
            if todo_id in t.dependencies:
                t.status = "pending"  # 阻塞，不给 ready

    def check_plan_divergence(self, current_action: str) -> bool:
        """检测当前行动是否偏离计划"""
        ready = self.get_ready_tasks()
        return all(not self._match_action(t, current_action) for t in ready)
```

#### 与现有架构的集成点

| 改动点 | 位置 | 说明 |
|--------|------|------|
| Planning Phase 启动 | `enhanced_simple_agent.py` 的 `_arun_stream_with_tools` | 新增 `planner_agent.run()` 调用（纯推理 LLM，无工具） |
| 工具门控 | `ToolRegistry` 新增 `get_filtered_registry(phase)` | 根据 phase 返回不同的工具子集（§1.4 已设计） |
| Execute Phase | `enhanced_simple_agent.py` 的 `_arun_stream_with_tools` | 每轮工具调用前检查 plan 进度 |
| 降兜 | Plan 生成失败或用户拒绝计划 | 退回 Craft 模式的标准 ReAct |

#### 改造量（Plan 模式专用）

| 新增文件 | 修改文件 | 估计行数 |
|---------|---------|---------|
| `agent/todo_scheduler.py` | `agent/enhanced_simple_agent.py` | ~400 行 |
| | `agent/task_tracker.py` | |

---

## 二、用户画像：从"碎片化记忆"到"画像驱动行为"

### 2.1 设计理念

参考学术界的最佳实践：**画像不是静态配置，而是对话的自然副产品**。每次对话后自动从中提取更新的偏好信息并归入画像中。

### 2.2 方案设计

```
对话结束
  ↓
ProfileAggregator.aggregate(session_id)
  ↓
从 Memory（Qdrant，全局）检索 preference/entity/decision 分类
  ↓
LLM 聚合为结构化画像摘要（单次调用）
  ↓
更新到 ~/.helloclaw/USER_PROFILE.md（用户全局，不随工作区变化）
  ↓
下次对话前注入系统提示词
```

**设计原则：用户画像跟随用户而非项目**。画像直接写入 `~/.helloclaw/identity/USER.md`（已由 `myclaw_agent.py:414-417` 自动注入系统提示词），与工作区无关。工作区相关的上下文（如当前项目的技术栈分布）属于工作区上下文，不纳入用户画像。画像仅依赖 Memory 系统中自动捕获的 preference/entity/decision 记忆聚合生成，不引入额外的数据源。

### 2.3 USER.md 模板重设计

现有 `USER.md` 模板仅有姓名/称呼/时区/备注 5 个静态字段。重设计后新增动态画像区域，由 `ProfileAggregator` 定期更新：

```markdown
# USER.md - 关于你的人类

## 基本信息

- **姓名：** 张三
- **称呼：** 老张
- **时区：** Asia/Shanghai

## 技术栈

_Preference 记忆聚合，由系统自动更新_

- Python, FastAPI, Vue 3

## 工作领域

_Preference 记忆聚合，由系统自动更新_

- 金融科技、证券交易系统

## 沟通偏好

_Preference 记忆聚合，由系统自动更新_

- 喜欢简洁直接的回复，不需要长篇解释
- 偏好直接给出代码而非先描述思路
- 通常给出可复制运行的示例

## 代码风格

_Preference 记忆聚合，由系统自动更新_

- 偏好 TypeScript 类型注解
- 函数式风格优先

## 备注

_其他手动维护的信息_

---

_画像聚合时间：2026-07-30T14:30:00 | 版本：3_
```

**字段来源**：

| 区域 | 来源 | 更新方式 |
|------|------|---------|
| 基本信息 | 用户手动填写 / Agent 对话中主动 Edit | 手动 |
| 技术栈 / 工作领域 / 沟通偏好 / 代码风格 | Memory preference/entity 聚合 | 自动（ProfileAggregator） |
| 备注 | 用户手动填写 | 手动 |
| 聚合时间 / 版本 | ProfileAggregator 写入 | 自动 |

**聚合规则**：`ProfileAggregator` 读取 Memory 中的 preference/entity/decision 记忆 → LLM 聚合为结构化文本 → 用 Edit 工具替换 USER.md 中对应区域（保留手动区域不变）。

### 2.4 画像聚合触发机制

| 触发条件 | 说明 |
|---------|------|
| 每 10 轮对话后 | 增量更新（轻量：仅扫描新增记忆） |
| 手动触发 | `memory aggregate_profile` 命令 |
| preference 记忆超过 20 条 | 自动触发（防止画像过期） |

### 2.5 画像如何驱动行为

USER.md 已由 `_build_system_prompt()` 自动注入系统提示词（`myclaw_agent.py:414-417`），**无需额外注入逻辑**。Agent 在每轮对话中天然能看到画像内容。

**行为调节的操作化规则**（通过 AGENTS.md §1 会话启动清单引导）：

| 画像字段 | 驱动行为 |
|---------|---------|
| 沟通偏好 = "简洁" | 回复长度控制在 200 字内，优先给代码 |
| 技术栈 = "Python, FastAPI" | 代码示例优先用 Python |
| 代码风格 = "type-hinted" | 示例代码自动添加类型注解 |
| 工作领域 = "金融" | 领域术语保持原有表达，不逐字翻译 |

### 2.6 改造量

| 新增文件 | 修改文件 | 估计行数 |
|---------|---------|---------|
| `agent/profile_aggregator.py` | `workspace/templates/identity/USER.md`（模板重设计） | ~20 行 |
| | `agent/memory/capture.py`（新增 `aggregate_profile` action） | ~30 行 |
| | `tools/builtin/memory_tool.py`（注册新 action） | ~10 行 |
| **合计** | | **~300 行 + ~60 行** |

---

## 总体改造评估

| 维度 | 现有成熟度 | 目标成熟度 | 新增代码 | 修改代码 | 核心复杂度 |
|------|-----------|-----------|---------|---------|-----------|
| 模式选择与执行 | 1/10 | 8/10 | ~470 行 | ~373 行 | 中 — 模式选择器(低) + Plan 两阶段(中) |
| 用户画像 | 6/10 | 8/10 | ~350 行 | ~80 行 | 中 — LLM 聚合 + 注入策略 |
| **合计** | | | **~820 行** | **~453 行** | |

### 实施顺序（按 ROI）

1. **模式选择器 + Ask/Craft 模式**（1-2 天）— 前端 Segmented + 后端工具过滤。立即可用，零误判
2. **Plan 模式两阶段执行**（3-5 天）— 在模式选择器基础上增加 DAG 调度器和工具门控
3. **用户画像**（2-3 天）— 依赖已有 Memory 系统，投入中等

### 核心风险与规避

| 风险 | 规避措施 |
|------|---------|
| 用户选错模式（如用 Ask 做需要写文件的任务） | Ask 模式下副作用工具被屏蔽时，LLM 会告知用户"当前为只读模式，请切换到 Craft 或 Plan 模式" |
| Plan 生成质量差 | 弱模型生成的 Plan 可能不可行。设有降兜机制：Plan 生成失败或偏离后自动退回到标准 ReAct |
| 画像聚合消耗 token | 聚合使用轻量 LLM（如 glm-4-flash），每次 ~2000 token。10 轮触发一次，平均每轮仅 +200 token |

### 对比总结

| 能力 | Claude Code | WorkBuddy | 本方案 |
|------|------------|-----------|--------|
| 意图识别 | 隐式（全 ReAct） | **显式（Ask/Plan/Craft 用户选择）** | 显式（三模式选择器，学习 WorkBuddy） |
| 计划 enforcement | **硬约束（工具门控）** | 软约束（用户确认） | 硬约束（工具门控）+ 降兜 |
| 用户画像 | 无（依赖用户主动提供信息） | 无 | **对话驱动，自动聚合，画像驱动行为** |
