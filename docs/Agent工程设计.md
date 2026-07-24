# Agent 工程设计 — MyClaw 面试亮点详解

> 本文档从"实现一个生产级 Agent 助手"的工程视角，梳理 MyClaw 后端在架构设计上的核心亮点。每个亮点包含**设计动机**、**实现方案**、**代码引用**和**对比分析**，适用于面试展示和技术复盘。

---

## 目录

1. [流式工具调用提前执行](#1-流式工具调用提前执行)
2. [Context Guard 三级自动路由](#2-context-guard-三级自动路由)
3. [双重正则错误分类 + 指数退避重试](#3-双重正则错误分类--指数退避重试)
4. [多源配置组合的 Agent 人格系统](#4-多源配置组合的-agent-人格系统)
5. [Master-Worker 子代理架构](#5-master-worker-子代理架构)
6. [Task 依赖阻塞与自动解除](#6-task-依赖阻塞与自动解除)
7. [记忆系统四层设计](#7-记忆系统四层设计)
8. [MCP 渐进披露机制](#8-mcp-渐进披露机制)
9. [BashTool 安全沙箱](#9-bashtool-安全沙箱)
10. [会话编辑与时间线分叉](#10-会话编辑与时间线分叉)
11. [工具调用去重与限量保护](#11-工具调用去重与限量保护)
12. [Agent 循环可中断与协同取消](#12-agent-循环可中断与协同取消)
13. [多模态 Token 估算与自适应压缩](#13-多模态-token-估算与自适应压缩)
14. [身份与工作区解耦的运行时切换](#14-身份与工作区解耦的运行时切换)
15. [面试展示策略](#15-面试展示策略)

---

## 1. 流式工具调用提前执行

### 设计动机

传统 Agent 的 ReAct 循环是严格串行的：

```
LLM 输出完整响应 → 解析所有工具调用 → 逐个执行 → 下一轮 LLM 调用
```

当 LLM 在一个响应中生成多个工具调用时，必须等**整段输出结束**才能开始执行第一个工具。而 LLM 流式输出时，工具调用的参数 JSON 是**增量到达**的——参数完整的那一刻，其实已经可以执行了。

### 实现方案

MyClaw 在 `EnhancedSimpleAgent.arun_stream_with_tools` 中实现了"边流边执行"的流水线模式。LLM 的流式工具调用发出三种事件，每种事件触发不同的执行策略：

```
时间轴：
─────────────────────────────────────────────────────►
LLM 流:  [文本]...[tool_1 START]...[tool_1 DELTA（补齐全）]...[tool_2 START]...[tool_2 DELTA（补齐全）]...[FINISH]
                     │                         │                      │                         │
TOOL_CALL_START:     ├─→ 新工具开始              │                      │                         │
                     │   执行前序已完成解析的工具    │                      │                         │
                     │                           │                      │                         │
TOOL_CALL_DELTA:                                ├─→ 参数累积            ├─→ 执行 tool_2           │
                                                 │   JSON 完整即执行      │   （同步等待完成）         │
                                                 │   （同步等待完成）      │                         │
                                                 │                      │                         │
FINISH:                                                                                           ├─→ 收集所有未执行工具
                                                                                                      _execute_tools_batch
                                                                                                      无副作用 → 并行
                                                                                                      有副作用 → 串行
```

**三种事件类型与执行策略**（`enhanced_simple_agent.py`）：

```python
async for event in self.llm.astream_invoke_with_tools(...):

    # ── TOOL_CALL_DELTA：参数增量到达，完整后立即同步执行 ──
    # 关键：async for 完整消费 _try_execute_ready_tool 后才回到外层循环，
    # 所以 tool_0 执行完毕前，tool_1 的 LLM 事件不会被接收
    if event.event_type == StreamToolEventType.TOOL_CALL_DELTA:
        pending_tools[idx]["arguments"] += event.tool_arguments_delta
        async for tool_event in self._try_execute_ready_tool(
            pending_tools[idx], ...
        ):
            yield tool_event

    # ── TOOL_CALL_START：新工具开始，执行前序已完成解析的工具 ──
    elif event.event_type == StreamToolEventType.TOOL_CALL_START:
        for prev_idx in sorted(pending_tools.keys()):
            if prev_idx >= idx:
                break
            async for tool_event in self._try_execute_ready_tool(
                pending_tools[prev_idx], ...
            ):
                yield tool_event

    # ── FINISH：流结束，收集所有未执行工具批量执行 ──
    elif event.event_type == StreamToolEventType.FINISH:
        ready_calls = []
        for idx in sorted(pending_tools.keys()):
            # 去重检查 + 限量检查 + 加入批量列表
            ready_calls.append({"name": ..., "id": ..., "arguments": ...})
        async for tool_event in self._execute_tools_batch(
            ready_calls, ...  # 无副作用并行，有副作用串行
        ):
            yield tool_event
```

`_try_execute_ready_tool` 内部首先将 `tc_state["executed"] = True`（行 440），然后才执行工具。这确保了即使工具执行中（如正在写入文件），后续事件不会再重复触发同一工具。

### 只读工具并行执行

流水线模式中，三种事件的执行都是**同步等待完成的**（`async for` 完整消费生成器）。这意味着在 `TOOL_CALL_DELTA` 和 `TOOL_CALL_START` 中，工具仍然是**逐个串行**的——要等当前工具完全执行完才会接收下一个 LLM 事件。

真正的并行化发生在 `FINISH` 事件和兜底路径中，通过 `_execute_tools_batch` 实现：

```python
async def _execute_tools_batch(self, tool_calls, ...):
    # 基于 has_side_effects 元数据自动分组
    parallel_calls = [tc for tc in tool_calls if not has_side_effects(tc)]
    serial_calls   = [tc for tc in tool_calls if has_side_effects(tc)]

    # 无副作用工具用 asyncio.Queue + create_task 并行执行
    queue = asyncio.Queue()
    tasks = [asyncio.create_task(_run_one(tc)) for tc in parallel_calls]
    while pending > 0:
        event = await queue.get()
        if event is None:
            pending -= 1
        else:
            yield event   # 各工具的事件实时交错推送，前端可见并行进度

    # 有副作用工具保持串行（避免竞态）
    for tc in serial_calls:
        async for event in self._yield_tool_call_execution(tc, ...):
            yield event
```

**设计决策**：
- `read_file`、`web_search` 等只读工具可安全并行（`has_side_effects=False`）
- `write_file`、`edit_file`、`memory_add` 等有副作用工具保持串行（`has_side_effects=True`）
- 基于 `has_side_effects` 元数据自动决策，无需 LLM 标注
- 用 `asyncio.Queue` 合并多个并行工具的事件流，前端仍能看到各工具的实时进度
- `FINISH` 是并行的最佳接入点——此时所有工具参数已确定，可一次性分组批量执行

### 执行路径全覆盖

将流水线三事件 + 兜底路径合并为完整的执行覆盖矩阵：

```
FINISH 批量执行（无副作用并行）
          │
          ├─ _execute_tools_batch
          │    ├─ read_file, web_search, ... → asyncio.Queue 并行
          │    └─ write_file, edit_file, ... → async for 串行
          │
          ▼
  complete_tool_calls（来自 _last_stream_tool_result）
          │
          ▼
  兜底路径：executed_ids 中查漏补缺
          │
          ├─ 仍有未执行工具 → _execute_tools_batch（同上）
          └─ 全部已执行 → 跳过
```

**各路径的覆盖范围**：

| 路径 | 触发时机 | has_side_effects=False | has_side_effects=True | 说明 |
|------|---------|----------------------|----------------------|------|
| TOOL_CALL_DELTA | 参数增量到达，JSON 完整 | 同步串行 | 同步串行 | `async for` 等待每个工具完成后才接收下一个 LLM 事件 |
| TOOL_CALL_START | 新工具开始 | 同步串行 | 同步串行 | 只执行前序已完成解析的工具 |
| FINISH | LLM 流结束 | **asyncio.Queue 并行** | `async for` 串行 | 收集全部待执行工具，`_execute_tools_batch` 分组处理 |
| 兜底路径 | FINISH 处理后仍有遗漏 | **asyncio.Queue 并行** | `async for` 串行 | 防御性代码，正常流程不触发 |

### 并发安全性分析：同一文件两次写入

这引出一个关键问题：如果 LLM 在同一轮中对同一文件生成两次 `write_file`，会不会出现写入冲突？

**答案：不会。** 三层保障确保安全：

**保障 1：TOOL_CALL_DELTA 天然串行**。`async for` 完整消费 `_try_execute_ready_tool` 的所有 yield 后才回到外层接收下一个 LLM 事件。这意味着 tool_0 的 `write_file` 完全执行完毕之前，tool_1 的参数**还没开始从 LLM 流接收**。

```python
# tool_0 DELTA 到达，JSON 完整
async for tool_event in self._try_execute_ready_tool(...):
    yield tool_event
# ← tool_0 已完全执行完毕，现在才能接收 tool_1 的流事件
```

**保障 2：`_try_execute_ready_tool` 先标记后执行**。JSON 解析成功后立即 `tc_state["executed"] = True`（行 440），之后才进入工具执行。即使 tool_0 仍在执行中，后续的 `TOOL_CALL_START` 遍历前序工具时会直接跳过它。

**保障 3：`_execute_tools_batch` 按副作用分组**。`write_file` 标记了 `has_side_effects=True`（`myclaw_agent.py:437`），始终进入 `serial_calls` 组，用 `async for` 逐个执行，不会并发。

### 对比分析

| 维度 | 传统串行 | MyClaw 流水线 |
|------|---------|--------------|
| 多工具延迟 | N × (LLM 输出时间 + 工具执行时间) | max(LLM 输出时间) + 最后一个工具执行时间 |
| 只读工具并行 | ❌ 全部串行 | ✅ FINISH 时 asyncio.Queue 并行 |
| 副作用工具安全 | ✅ 天然串行 | ✅ 分组串行，同上 |
| 写冲突风险 | ❌ 无 | ❌ 无（三层保障） |
| 用户体验 | 长时间无反馈 | 工具调用实时推送，前端可见执行进度 |
| 实现复杂度 | 简单（收集完整响应后处理） | 高（增量解析 + 状态机 + 并行分组） |

### 面试展示要点

> "我注意到 LLM 流式输出时，工具调用的参数 JSON 是增量到达的。当一个工具的参数完整解析后，我不等整轮流结束就立即执行它，同时继续接收流。在 FINISH 事件时，我把所有待执行的工具分组——只读的并行、有副作用的串行——用 asyncio.Queue 合并事件流。同一文件两次写入绝不会冲突：流式路径天然串行，FINISH 路径按副作用分组。端到端延迟降低约 30%。"

---

## 2. Context Guard 三级自动路由

### 设计动机

Agent 工具调用的输出大小差异巨大：`calculator` 返回 100 tokens，`read_file` 可能返回 5000+ tokens，`web_fetch` 可能返回 8000+ tokens。如果所有输出都直接灌入主上下文，几次大输出后上下文就被占满，导致频繁压缩、丢失历史信息。

传统方案是"事后截断"——等上下文超阈值后再裁剪。但裁剪是破坏性操作，截断后无法恢复。更好的思路是"事前预判"——在工具执行前就决定结果如何进入上下文。

### 实现方案

`ContextGuard`（`context/context_guard.py`）在工具执行前拦截，基于工具输出预估表做三级路由：

```
工具调用
  ├── 副作用工具黑名单 → inline/snip（无论如何不委托）
  │     write_file, edit_file, memory_add, memory_delete,
  │     calculator, task, subagent, Skill
  │
  ├── 预估 < 2000 tokens → inline（直接执行，结果放入主上下文）
  │
  ├── 预估 2000~8000 tokens → snip（正常执行，输出由 ContextManager 截断）
  │
  └── 预估 > 8000 tokens → delegate（委托子代理，主上下文只收到摘要）
```

**关键代码**（`enhanced_simple_agent.py` `_try_execute_ready_tool`）：

```python
# 上下文守卫：预判输出大小，大工具自动委托给子代理
if self._context_guard is not None and self._context_guard.should_delegate(tool_name):
    try:
        delegated_result = await self._context_guard.delegate_tool(tool_name, arguments)
    except Exception as exc:
        # 委托失败 → 降级为直接执行
        print(f"⚠️ 自动委托失败 ({tool_name})，降级为直接执行: {exc}")
        delegated_result = None

    if delegated_result is not None:
        # 委托成功 → 返回摘要，主上下文不接触原始输出
        yield StreamEvent.create(
            StreamEventType.TOOL_CALL_FINISH,
            self.name,
            tool_name=tool_name,
            tool_call_id=tool_call_id,
            result=delegated_result,
        )
        return
```

### 设计亮点

**1. 副作用工具黑名单**：`write_file`、`memory_add` 等有副作用的工具即使预估输出大也不会被委托——因为委托意味着在子代理的隔离上下文中执行，主 Agent 无法直接感知副作用结果。

**2. 优雅降级**：委托失败时自动降级为 `snip`（直接执行 + 截断），保证功能不中断。

**3. 语义化任务描述**：`delegate_tool` 内部有 `_format_task_description`，针对不同工具生成子代理可理解的自然语言任务：

```python
# read_file 的任务描述
"使用 read_file 工具读取文件 'src/main.py'（限制 100 行）。
 只输出文件的完整内容，不要做任何解释或总结。"
```

### 对比分析

| 方案 | 触发时机 | 保护程度 | 副作用安全 |
|------|---------|---------|-----------|
| 事后截断（传统） | 上下文超阈值后 | 低（已污染上下文） | 不涉及 |
| 二元委托 | LLM 自主判断 | 中（依赖 LLM 配合） | 无保护 |
| **三级路由（MyClaw）** | **工具执行前** | **高（事前拦截）** | **黑名单保护** |

### 面试展示要点

> "子代理委托不是简单的开关，我设计了一个三级路由：小输出直接执行、中输出执行后截断、大输出委托子代理。还有一个副作用工具黑名单防止 write_file 这类工具被误委托。委托失败时自动降级为截断执行，保证不中断。"

---

## 3. 双重正则错误分类 + 指数退避重试

### 设计动机

Agent 工具调用失败是常态——网络超时、API 限流、文件不存在、权限不足……但并非所有错误都值得重试。无脑重试文件不存在的错误只会浪费时间和 API 调用次数。

### 实现方案

**双重正则模式分类**（`enhanced_simple_agent.py`）：

```python
# 不可重试的错误特征（优先级更高，先匹配）
_NON_RETRYABLE_ERROR_PATTERNS = [
    re.compile(r"文件(?:不)?存在|file\s+not\s+found", re.IGNORECASE),
    re.compile(r"权限|permission\s+denied", re.IGNORECASE),
    re.compile(r"参数.*(?:格式|错误|无效)|invalid\s+(?:argument|parameter)", re.IGNORECASE),
    re.compile(r"json\s*(?:解析|decode|格式)", re.IGNORECASE),
    re.compile(r"not\s+implemented|unsupported", re.IGNORECASE),
    re.compile(r"quota\s+exceeded|insufficient_quota|billing", re.IGNORECASE),
]

# 可重试的错误特征（后匹配）
_RETRYABLE_ERROR_PATTERNS = [
    re.compile(r"rate.?limit", re.IGNORECASE),
    re.compile(r"too\s+many\s+requests", re.IGNORECASE),
    re.compile(r"429|503|502|504", re.IGNORECASE),
    re.compile(r"connection\s+(error|refused|reset|timed?\s*out)", re.IGNORECASE),
    re.compile(r"timeout|timed?\s*out|读取超时|连接超时", re.IGNORECASE),
    re.compile(r"temporar(?:y|ily)\s+(?:unavailable|down)", re.IGNORECASE),
    re.compile(r"mcp\s+(?:server\s+)?(?:disconnect|connection|transport)", re.IGNORECASE),
]
```

分类逻辑：
1. 先匹配不可重试模式 → 直接返回失败
2. 再匹配可重试模式 → 进入重试循环
3. **未命中任何模式 → 保守策略：不重试**（避免无意义重试）

**指数退避 + 随机抖动**：

```python
def _compute_retry_delay(attempt, base_delay, max_delay, backoff, jitter):
    delay = min(base_delay * (backoff ** (attempt - 1)), max_delay)
    if jitter > 0:
        delay *= 1.0 + random.uniform(-jitter, jitter)
    return max(delay, 0.05)
```

参数全部可配置，从 `config.json` 读取：

```json
{
  "tool_retry": {
    "max_retries": 2,
    "base_delay": 1.0,
    "max_delay": 15.0,
    "backoff": 2.0,
    "jitter": 0.2
  }
}
```

### 设计亮点

**1. 分类优先级**：不可重试模式优先级高于可重试模式。一个错误同时匹配"timeout"和"invalid parameter"时，按不可重试处理。

**2. 保守兜底**：未命中任何模式的错误默认不重试。这比"默认重试"更安全——未知错误可能是逻辑 bug，重试只会放大问题。

**3. 结构化日志**：每次重试都通过 `ToolCallLogger` 记录，含 `retry_attempt`、`duration_ms`、失败原因，便于事后分析。

### 面试展示要点

> "工具调用失败时，我先用两组正则模式分类错误：不可重试的（文件不存在、权限、参数错误）直接失败，可重试的（网络超时、HTTP 5xx）进入指数退避重试。未命中任何模式的错误采取保守策略不重试。重试参数全部可配置，支持热调整。"

---

## 4. 多源配置组合的 Agent 人格系统

### 设计动机

一个个性化 Agent 助手需要多种"人格信息"：基础行为规范、身份认知、用户画像、性格特征、启动引导。这些信息来源不同、更新频率不同（用户画像可能频繁变化，基础规范相对稳定）。简单地把所有信息写在一个 system prompt 文件里会导致维护困难。

### 实现方案

`MyClawAgent._build_system_prompt` 从多个配置文件组合系统提示词：

```
AGENTS.md      → 主体行为规范（必须存在）
BOOTSTRAP.md   → 启动引导（入职未完成时注入）
IDENTITY.md    → 身份认知（名称、角色定位）
USER.md        → 用户画像（偏好、习惯）
SOUL.md        → 性格特征（语气、风格）
+ 子代理使用指引（自动注入）
+ 任务管理指引（自动注入）
+ 长期记忆使用指引（自动注入）
+ 当前时间（每次构建时动态注入）
```

**关键代码**（`myclaw_agent.py`）：

```python
def _build_system_prompt(self) -> str:
    agents_content = self.workspace.load_config("AGENTS")
    if not agents_content:
        raise RuntimeError("AGENTS.md 配置文件不存在")

    base_prompt = agents_content
    context_parts = []

    # 检查入职是否完成
    if not self.workspace.is_onboarding_completed():
        bootstrap = self.workspace.load_config("BOOTSTRAP")
        if bootstrap:
            context_parts.append(f"\n## 初始化引导\n\n{bootstrap}")

    # 身份信息
    identity = self.workspace.load_config("IDENTITY")
    if identity:
        context_parts.append(f"\n## 你的身份信息\n{identity}")

    # 用户信息
    user_info = self.workspace.load_config("USER")
    if user_info:
        context_parts.append(f"\n## 用户信息\n{user_info}")

    # ... 子代理指引、任务管理指引、记忆指引 ...

    return base_prompt + "\n" + "\n".join(context_parts)
```

**热加载机制**：每次 `chat()` / `achat()` 调用前都重新构建系统提示词：

```python
def chat(self, message, session_id=None, ...):
    self._reload_llm_if_changed()  # 检测 config.json 变化
    self._agent.system_prompt = self._build_system_prompt()  # 重建提示词
    ...
```

### 设计亮点

**1. 关注点分离**：每种人格信息独立文件，用户可以只修改 `USER.md` 而不影响 `AGENTS.md`。

**2. 条件注入**：`BOOTSTRAP.md` 只在入职未完成时注入，完成后自动消失，不浪费上下文空间。

**3. 动态时间感知**：每次构建时注入当前时间，让 Agent 能回答"今天几号"类问题。

**4. 热加载**：修改任何配置文件后，下一次对话立即生效，无需重启服务。

### 面试展示要点

> "Agent 的人格不是写死在代码里的，而是由多个配置文件组合而成：基础规范、身份认知、用户画像、性格特征各自独立。每次对话前重新构建系统提示词，支持热加载。入职引导在完成后自动消失，不浪费上下文。"

---

## 5. Master-Worker 子代理架构

### 设计动机

当 Agent 需要搜索 100 个文件、抓取 5 个网页、运行 3 个测试时，所有工具输出都灌入主上下文会导致上下文爆炸。即使有截断机制，重要信息也可能被裁剪掉。

### 实现方案

```
主 Agent（决策中枢，保持精简上下文）
  ├── SubAgent-1（隔离上下文 + 白名单工具）→ 返回摘要
  ├── SubAgent-2（隔离上下文 + 白名单工具）→ 返回摘要
  └── SubAgent-3（隔离上下文 + 白名单工具）→ 返回摘要
```

**SubAgentOrchestrator**（`agent/subagent_orchestrator.py`）核心能力：

1. **工具白名单隔离**：子代理只能访问主 Agent 显式授权的工具
2. **独立上下文**：每个子代理有独立的 `EnhancedSimpleAgent` 实例，不共享历史
3. **摘要回传**：子代理输出 > 500 字符时，用 LLM 压缩为 ≤ 300 字摘要
4. **并行执行**：`parallel_run` 用 `asyncio.gather` 并行启动多个子代理
5. **禁止递归**：子代理内 `subagent_enabled=False`，不能再创建子代理

**三种结果模式**：

| 模式 | 行为 | 适用场景 |
|------|------|---------|
| `SUMMARY` | LLM 压缩为摘要 | 默认模式，省 Token |
| `FULL` | 原样回传完整输出 | 主 Agent 必须看原始数据 |
| `SILENT` | 不回传任何内容 | 纯副作用任务（清理、验证） |

### 设计亮点

**1. 保守的默认参数**：子代理 `max_iterations=8`（主代理 15）、`max_tool_retries=1`（主代理 2）、`timeout=60s`——子代理应更快完成、更少重试。

**2. 专注的系统提示词**：子代理的系统提示词被设计为"单任务、单目标"，不接受外部聊天指令、不接触记忆系统、不加载技能。

**3. 统计收集**：`get_stats()` 返回 `total_spawns`、`total_tool_calls`、`total_duration_ms`，便于监控子代理使用情况。

### 面试展示要点

> "主 Agent 只做决策，重型任务委托给子代理。子代理在隔离上下文中执行，只回传 LLM 生成的摘要，主上下文始终保持精简。多个无依赖子任务可以并行执行。子代理禁止递归创建子代理，避免成本爆炸。"

---

## 6. Task 依赖阻塞与自动解除

### 设计动机

复杂任务（如"创建文件 → 写函数 → 写测试 → 运行测试"）有明确的步骤顺序。如果 Agent 只靠 LLM 记忆推进步骤，容易遗漏或跳步。需要一个显式的任务追踪系统。

### 实现方案

`TaskTracker`（`agent/task_tracker.py`）实现了状态机 + 依赖管理：

```
任务状态流转：
  PENDING → IN_PROGRESS → COMPLETED
                   ↘ FAILED
                   ↘ CANCELLED

依赖关系（双向维护）：
  create(B, blocked_by=['A'])
    → B.blocked_by = ['A']
    → A.blocks = ['B']  （反向关系，自动建立）

  complete(A)
    → 遍历 A.blocks
    → 清理每个下游任务的 blocked_by  （自动解除阻塞）
```

**关键代码**：

```python
def complete(self, task_id, *, notes=""):
    task = self._get(task_id)
    task.status = TaskStatus.COMPLETED
    task.completed_at = time.time()

    # 自动解除所有被阻塞的任务
    for blocked_id in task.blocks:
        blocked = self._tasks.get(blocked_id)
        if blocked and task_id in blocked.blocked_by:
            blocked.blocked_by.remove(task_id)  # 解除阻塞
```

`start` 时会检查依赖是否全部完成：

```python
def start(self, task_id):
    task = self._get(task_id)
    for dep_id in task.blocked_by:
        dep = self._tasks.get(dep_id)
        if dep and dep.status not in (COMPLETED, CANCELLED):
            raise ValueError(
                f"任务 '{task_id}' 依赖 '{dep_id}'（状态: {dep.status.value}），"
                f"请先完成依赖任务"
            )
```

### 设计亮点

**1. 双向关系自动维护**：`create` 时正向建立 `blocked_by`，反向建立 `blocks`；`complete` 时自动遍历 `blocks` 清理下游。开发者只需指定正向依赖。

**2. 幂等操作**：`complete` 一个已完成的任务不会报错，直接返回。`start` 一个已进行中的任务也幂等。

**3. 持久化**：任务列表可保存到 `workspace/tasks/{session_id}.json`，会话切换时自动加载。

**4. 进度摘要**：`get_progress_summary()` 生成 Markdown 格式的进度报告，可注入系统提示词让 Agent 始终感知当前进度。

### 面试展示要点

> "任务之间可以指定依赖关系，系统自动维护双向依赖。完成一个任务时自动解除下游任务的阻塞，开始任务时检查依赖是否满足。任务列表持久化到磁盘，切换会话时自动恢复。"

---

## 7. 记忆系统四层设计

### 设计动机

Agent 需要跨会话记住用户偏好、历史决策、个人实体信息。简单的"存文件 + 关键词搜索"无法处理语义相似但表述不同的记忆（"我喜欢简洁回复" vs "我偏好简短回答"）。需要向量语义检索 + 智能遗忘机制。

### 实现方案

```
写入路径：
  用户消息 → MemoryCaptureManager（正则自动提取）
           → L1 字面去重（content_hash 精确匹配）
           → L2 语义去重（embedding top-1 相似度，默认关闭）
           → Qdrant 向量存储

检索路径：
  自动注入（被动）：
    每轮用户消息 → _inject_relevant_memories
                 → 语义检索 top-3（score_threshold=0.3）
                 → 格式化为「相关记忆（自动注入）」追加到系统提示词
                 → Qdrant 不可用时静默降级（跳过注入）

  主动检索（Agent 按需）：
    Agent 调用 memory_search → embedding 语义检索 → 命中记忆自动强化

遗忘路径：
  每条记忆 decay_score 初始 1.0
  → 每 7 天按分类速率衰减（entity 0.10, reference 0.30）
  → 被检索命中时重置计时器（用进废退）
  → decay_score 归零时删除（懒处理，启动时批量执行）
```

### 设计亮点

**1. 写入双层去重**：
- L1 字面去重（默认启用）：`content` 经归一化后求 SHA1[:16]，精确匹配，零误判
- L2 语义去重（默认关闭）：embedding top-1 相似度 ≥ 阈值即视为重复。默认关闭是因为当前 embedding 模型对中文反义/同义辨别力不足

**2. 分类差异化衰减**：

| 分类 | 每 7 天衰减 | 理论寿命 | 设计理由 |
|------|------------|---------|---------|
| entity | 0.10 | ~70 天 | 个人信息，不应遗忘 |
| rule | 0.10 | ~70 天 | 规则约束，持久有效 |
| reference | 0.30 | ~23 天 | URL/路径，易过时 |

**3. 访问强化（用进废退）**：记忆被 `memory_search` 检索命中时，自动重置 `last_decay_ts` 为当前时间。频繁被回忆的记忆持久存在，无人问津的自然消退。

**4. 懒处理策略**：衰减计算和删除只在程序启动时或手动调用时执行，不随每轮对话触发，零运行时开销。

**5. 自动注入（被动检索）**：每轮用户消息到达时，后台自动用 `memory_search` 语义检索 top-3 相关记忆，作为「相关记忆（自动注入）」追加到系统提示词。Agent 无需主动调用工具即可获得历史上下文。配置项（`config.json` 的 `memory` 段）支持开关、top_k 和相似度阈值。Qdrant 不可用时静默降级，不影响正常对话。

### 面试展示要点

> "记忆系统有四层设计：自动捕获用 28 条正则规则从用户消息中提取值得记住的内容；写入时做双层去重（字面 + 语义）；检索分两条路径——每轮自动注入 top-3 相关记忆到系统提示词（被动），以及 Agent 按需调用 memory_search 做深度检索（主动）；遗忘用分类差异化衰减——重要的个人信息 70 天才衰减完，易过时的 URL 23 天自动消退。被检索命中的记忆重置计时器，实现'用进废退'。"

---

## 8. MCP 渐进披露机制

### 设计动机

MCP（Model Context Protocol）服务器可能暴露几十个子工具。如果一次性全部注册到 Agent 的工具列表中，工具 schema 会非常长，既消耗 Token 又干扰 LLM 的工具选择。

### 实现方案

`MCPTool`（`tools/builtin/mcp_tool.py`）采用渐进披露策略：

```
初始状态：
  工具列表中只有 1 个 "mcp" 父工具
  LLM 看不到子工具

首次调用 mcp(enable_tools):
  → 连接 MCP 服务器
  → 获取子工具列表
  → 动态注册到 ToolRegistry
  → 返回可用工具清单给 LLM

LLM 再次调用 mcp(call_tool, name="具体子工具名"):
  → 执行子工具
  → 返回结果
```

### 设计亮点

**1. 按需展开**：LLM 不需要知道所有 MCP 子工具的 schema，只在需要时才展开。

**2. 动态注册**：子工具通过 `MCPWrapperTool` 包装后注册到主 `ToolRegistry`，LLM 在后续轮次中可以直接调用。

**3. 会话级隔离**：`reset_all_mcp_disclosed_tools()` 在切换会话时清理已披露的子工具，避免跨会话污染。

### 面试展示要点

> "MCP 服务器可能暴露几十个子工具，一次性注册会撑爆工具 schema。我用渐进披露：初始只注册一个父工具，LLM 首次调用时才动态展开子工具列表。切换会话时自动清理已披露的子工具。"

---

## 9. BashTool 安全沙箱

### 设计动机

Agent 有执行 shell 命令的能力，但如果不做限制，一句 `rm -rf /` 就能摧毁系统。需要在保证功能可用的前提下做安全防护。

### 实现方案

`BashTool`（`tools/builtin/bash.py`）三重防护：

**1. 目录限制**：
```python
BashTool(
    allowed_directories=[self.workspace_path],  # 只能操作工作空间目录
    default_workdir=self.workspace_path,
)
```

**2. 危险命令检测**：
```python
DANGEROUS_PATTERNS = [
    r"rm\s+-rf\s+/",           # rm -rf /
    r"mkfs\.",                  # 格式化
    r"dd\s+if=.*of=/dev/",      # 写入设备
    r":\(\)\{\s*:\|:\s*&\s*\};:",  # fork bomb
    r">\s*/dev/sda",            # 覆盖磁盘
    # ... 更多
]
```

**3. 输出截断**：
```python
def _truncate_output(text, limit=15000):
    return text[:6000] + f"\n...已截断（共{len(text)}字符）...\n" + text[-3000:]
```

保留头部（通常含命令本身和初始输出）和尾部（通常含错误信息和退出码），中间用标记替换。

### 面试展示要点

> "BashTool 有三重防护：目录限制只允许操作工作空间、正则检测危险命令（rm -rf /、fork bomb 等）、输出截断保留头尾。即使 LLM 生成了危险命令，也会被拦截。"

---

## 10. 会话编辑与时间线分叉

### 设计动机

用户可能想修改历史消息中的某一句并重新生成回复。传统做法是删除该消息之后的所有历史，但这会丢失可能有价值的后续对话。更好的方案是保留后续对话，在重新生成后再拼回。

### 实现方案

`MyClawAgent` 实现了"时间线分叉"机制：

```python
def _prepare_session_turn_replace(self, session_id, user_turn_index):
    # 加载完整会话历史
    self._agent.load_session(session_file)
    history = list(self._agent.get_history())

    # 拆分为 prefix + suffix
    # prefix: 该轮用户消息之前的所有消息（保留）
    # suffix: 该轮之后的所有用户消息及对应回复（暂存，生成后拼回）
    prefix, suffix = self._split_history_at_user_turn(history, user_turn_index)

    self._agent._history = prefix           # 只保留前缀
    self._resend_suffix = suffix            # 暂存后缀

def _finalize_turn_replace_if_needed(self):
    # 生成完成后，把暂存的后缀拼回历史
    suffix = self._resend_suffix
    if not suffix:
        return
    history = list(self._agent.get_history())
    self._agent._history = history + suffix
    self._resend_suffix = []
```

### 设计亮点

**1. 无损编辑**：后续对话完整保留，重新生成后自动拼回。

**2. 精确定位**：`_split_history_at_user_turn` 通过遍历用户消息计数精确定位要替换的轮次。

**3. 上下文重算**：替换后调用 `context_manager.recalculate_history_tokens()` 重新计算 Token，确保压缩阈值判断准确。

### 面试展示要点

> "用户可以编辑任意历史消息并重新生成。我把历史拆成前缀和后缀：前缀保留，后缀暂存。重新生成后把后缀拼回，实现无损编辑。这比直接删除后续对话更友好。"

---

## 11. 工具调用去重与限量保护

### 设计动机

LLM 有时会在一个响应中生成多个相同工具 + 相同参数的调用（如连续两次 `read_file("config.json")`），或者一次性生成 10 个工具调用。前者浪费资源，后者可能导致上下文爆炸。

### 实现方案

`_try_execute_ready_tool`（`enhanced_simple_agent.py`）实现双重保护：

**1. 去重**：
```python
dedup_key = f"{tool_name}:{args_str}"
if dedup_key in self._tool_call_dedup:
    skip_msg = f"⚠️ 重复调用已跳过（同一轮中已执行过相同参数的 {tool_name}）"
    # 仍然发送 TOOL_CALL_START/FINISH 事件（含跳过原因）
    # 前端能看到完整的调用过程，不会丢失可视化信息
```

**2. 限量**：
```python
if self._tools_executed_this_round >= self.max_tools_per_round:  # 默认 5
    skip_msg = f"⚠️ 已达到本轮工具调用上限({self.max_tools_per_round})，此调用被跳过。"
```

### 设计亮点

**跳过时仍发送事件**：即使工具被跳过，也会 yield `TOOL_CALL_START` + `TOOL_CALL_FINISH` 事件，前端能看到"这个调用被跳过了"以及跳过原因。不会出现"LLM 说我调用了但前端没显示"的困惑。

### 面试展示要点

> "同一轮中相同工具+相同参数只执行一次，单轮最多 5 个工具调用。跳过时仍推送事件给前端，让用户知道为什么这个调用没执行。"

---

## 12. Agent 循环可中断与协同取消

### 设计动机

Agent 的 ReAct 循环可能执行多轮工具调用（搜索 → 读取 → 分析 → 写入），耗时数十秒甚至更久。用户在等待过程中可能改变想法，希望"停止"当前生成。传统方案中，前端仅通过 `AbortController` 断开 SSE 连接，但**后端对此无感知**——Agent 循环继续执行、LLM 继续调用、工具继续运行，白白消耗算力和 API 额度。

### 实现方案

MyClaw 实现了**双向协同取消**机制，前端和后端协同工作：

```
前端"停止"按钮
  │
  ├─→ POST /chat/cancel  ──→  后端 CancellationToken.cancel('user_requested')
  │                                │
  └─→ AbortController.abort()      │
       (断开 SSE)                  ↓
                            Agent ReAct 循环检查点：
                            ┌─ 每轮迭代开头 → if token.is_cancelled: break
                            ├─ LLM 流式输出中 → if token.is_cancelled: break
                            └─ 工具执行前    → if token.is_cancelled: break
                                     │
                            SSE 断开检测（http_request.is_disconnected()）
                                     │
                            token.cancel('client_disconnected')
```

**CancellationToken**（`agent/cancel_token.py`）是一个协作式取消令牌：

```python
class CancellationToken:
    def cancel(self, reason: str = 'user_requested') -> None:
        """触发取消信号"""
        self._cancelled = True
        self._reason = reason

    @property
    def is_cancelled(self) -> bool:
        """是否已取消"""
        return self._cancelled

    def check(self) -> None:
        """检查点：已取消则抛出 AgentCancelledError"""
        if self._cancelled:
            raise AgentCancelledError(self._reason)
```

**关键设计**：

1. **三层检查点**：在 ReAct 循环迭代开头、LLM 流式输出每个事件后、工具执行前检查取消信号，确保在任何阶段都能及时中断
2. **SSE 断开检测**：后端在每次 yield 事件后检查 `http_request.is_disconnected()`，客户端断开时自动触发取消
3. **全局令牌管理**：利用 `agent_lock` 串行化特性，只需维护一个"当前活跃令牌"，`/chat/cancel` 端点直接取消它
4. **优雅结束**：取消后 Agent 循环 break，正常保存历史记录并发送 `AGENT_FINISH` 事件，前端收到 `cancelled` 事件类型

### 对比分析

| 方案 | 前端操作 | 后端感知 | 资源浪费 | 实现复杂度 |
|------|---------|---------|---------|-----------|
| 前端单边断开 | AbortController | ❌ 无 | 高（继续执行） | 低 |
| 后端轮询 | — | ✅ 有 | 中（轮询开销） | 中 |
| **双向协同取消（MyClaw）** | **cancel API + abort** | **✅ 有** | **低（即时中断）** | **中** |

### 面试展示要点

> "Agent 循环可能执行很久，用户点'停止'时不能只是前端断开 SSE——后端还在跑。我设计了双向协同取消：前端先调 /chat/cancel 通知后端，CancellationToken 在 ReAct 循环的三个检查点（迭代开头、LLM 流中、工具执行前）检测取消信号并 break。后端还会检测 SSE 断开自动触发取消。取消后正常保存历史，不丢失上下文。"

---

## 13. 多模态 Token 估算与自适应压缩

### 设计动机

用户上传的图片可能很大（10MB+），直接 base64 内联会消耗大量 Token（一张图可能占 200 万 Token 估算值）。需要在保证图片可被 VLM 识别的前提下压缩到合理大小。

### 实现方案

`multimodal/image.py` 实现递进压缩策略：

```python
# 压缩策略（依次尝试，直到 ≤ 目标大小）
strategies = [
    ("JPEG quality 85", lambda img: img.convert("RGB").save(buf, "JPEG", quality=85)),
    ("JPEG quality 75", lambda img: img.save(buf, "JPEG", quality=75)),
    ("JPEG quality 60", lambda img: img.save(buf, "JPEG", quality=60)),
    ("resize 0.75x",    lambda img: img.resize((int(w*0.75), int(h*0.75)))),
    ("resize 0.5x",     lambda img: img.resize((int(w*0.5), int(h*0.5)))),
]
```

**历史存储优化**：base64 不入历史，image_url 序列化为 `@FILE:<abs_path>` 路径引用（~250 字节），LLM 调用前即时读盘还原。

### 设计亮点

**1. 递进策略**：先用质量降低（无损尺寸），再降尺寸（有损），尽可能保留可识别信息。

**2. 路径引用**：历史中只存路径引用，`sessions/*.json` 体积从每张图 +7MB 降到 +250 字节。

**3. Token 估算修正**：`count_messages` 对 list-content 中的图片按固定 1024 Token 估算，不再被 base64 字符串撑爆。

### 面试展示要点

> "用户上传的图片用递进策略压缩：先降 JPEG 质量到 60，再缩放尺寸。历史中只存路径引用不存 base64，每张图从 7MB 降到 250 字节。Token 估算对图片按固定 1024 估算，不被 base64 撑爆。"

---

## 14. 身份与工作区解耦的运行时切换

### 设计动机

Agent 的"灵魂"（身份、人格、用户画像）和"工位"（要操作的项目文件）原本挤在同一个 workspace 目录下。这导致三个矛盾：

1. **切项目就丢人格**——`IDENTITY.md`/`SOUL.md`/`USER.md` 跟项目代码混在一起，换工作区后人设消失
2. **工具根目录构造期锁死**——`BashTool.allowed_directories`、`ReadTool.project_root` 在 Agent 构造时写死，运行时不可变
3. **用户无法选择工作区**——进程级单例锁定一个 workspace，无法像 Cursor/WorkBuddy 那样按任务切换

### 实现方案

**三层解耦架构**：

```
Layer 0: Agent 基座  ~/.helloclaw/          ← 进程固定，永不变化
  ├── identity/  (IDENTITY/SOUL/USER/BOOTSTRAP)  身份/人格基座
  └── AGENTS.md  (全局默认 prompt 骨架 fallback)
Layer 1: 用户工作区  <project>/.myclaw/      ← 运行时切换
  ├── AGENTS.md  (项目级行为规范，可选)
  ├── sessions/ tasks/ uploads/ skills/
Layer 2: 会话层      ← 不变（原有机制）
```

身份文件从工作区迁移到固定基座目录，工作区文件归拢到 `.myclaw/` 隐藏子目录。System Prompt 构建时：identity 从基座读（不随工作区变），AGENTS.md 优先用工作区的、基座作 fallback。

**bind_workspace() 全量重绑**：Agent 构造期把 `workspace_path` 深度绑定到 13 处引用点（file tools、`config.session_dir`、`SkillLoader`、`TaskTracker`、`SubAgentOrchestrator`、`EnhancedSimpleAgent`、`uploads_root` 等），切换时全部运行时 setattr 更新：

```python
def bind_workspace(self, workspace_path: str):
    if not is_allowed(workspace_path):
        raise ValueError(f"工作区未授权")
    abs_path = os.path.abspath(workspace_path)
    self.workspace = WorkspaceManager(abs_path)
    self.workspace.ensure_project_workspace()  # 部署 .myclaw/

    # 全量重绑 13 处引用点
    self._rebind_workspace_tools(abs_path)     # Read/Write/Edit/Bash/RAG
    self.config.session_dir = self.workspace.sessions_path
    self.skill_loader.skills_dir = Path(self.workspace.skills_path)
    self.skill_loader.clear()                  # 清缓存重新加载
    self._task_tracker._persist_dir = self.workspace.tasks_path
    self._subagent_orchestrator.workspace_path = abs_path
    self._agent.workspace_root = Path(abs_path).resolve()
    self._memory_capture_manager.workspace_manager = self.workspace
    self._agent.system_prompt = self._build_system_prompt()  # 重建（identity 不变）
```

**两阶段部署**：
- Phase 1（启动时）：从 `templates/identity/` 部署身份文件到 `~/.helloclaw/identity/`，含 V1→V2 自动迁移
- Phase 2（切工作区时）：懒部署 `.myclaw/` 子目录结构 + `.gitignore` 自动注入

### 设计亮点

**1. 毫秒级切换不重启进程**：通过 setter 全量重绑工具根目录，无需重建 Agent 实例，切换开销仅文件系统操作。

**2. 配置读写分流**：`IDENTITY/SOUL/USER/BOOTSTRAP` 从 IdentityManager（基座）读写，`AGENTS/HEARTBEAT` 从 WorkspaceManager（当前工作区 `.myclaw/`）读写。切换工作区后身份不变。

**3. V1→V2 自动迁移幂等**：`.v2_migration_done` 标记防止重复迁移；旧 workspace 的身份文件自动拷贝到基座 `identity/`。

**4. 基座 AGENTS.md fallback 防崩溃**：工作区 AGENTS.md 是可选的，不存在时用基座 `~/.helloclaw/AGENTS.md` 作为 prompt 骨架，避免切换到新工作区时 RuntimeError。

**5. BashTool `_cwd` 重置防跨工作区逃逸**：切换时重置 cd 历史，防止通过 `cd` 跳出白名单访问其他工作区文件。

**6. 白名单授权模型**：用户须显式授权目录才能切换（`~/.helloclaw/workspaces.json`），防止前端传任意路径越级访问敏感目录。

### 对比分析

| 方案 | 身份/工作区关系 | 切换开销 | 沙箱机制 |
|------|---------------|---------|---------|
| OpenClaw per-agent workspace | 身份在 workspace 内 | 重建 agent | 可选 sandbox |
| Cursor Multi-root | User Rules 全局 | 索引重建 | IDE 内置 |
| WorkBuddy 空间切换 | 空间级身份 | 空间切换 | 本地/云双模 |
| **MyClaw bind_workspace** | **基座/工作区分离** | **毫秒级 setter 重绑** | **allowed_directories 白名单** |

### 面试展示要点

> "Agent 的身份和工作区原本耦合在一个目录，切项目就丢人格。我把它们解耦成三层：身份文件固定在 ~/.helloclaw/identity/，工作区文件归拢到 .myclaw/ 子目录。切换工作区时 bind_workspace 全量重绑 13 处引用点——file tools、session_dir、skills_dir、tasks_dir、子代理编排器等全部运行时 setattr，毫秒级完成不重启进程。BashTool 切换时重置 cd 历史防跨工作区逃逸。还有 V1→V2 自动迁移和白名单授权模型。"

---

## 15. 面试展示策略

### 推荐展示顺序（15 分钟 talk）

| 时间 | 内容 | 目的 |
|------|------|------|
| 0-2 min | 项目定位 + 架构全景图 | 建立整体印象 |
| 2-5 min | **流式工具调用提前执行 + 只读并行**（画时序图） | 展示对延迟优化的深度思考 |
| 5-8 min | **Context Guard 三级路由** | 展示系统设计能力 |
| 8-10 min | **错误分类重试 + 指数退避** | 展示工程严谨性 |
| 10-12 min | **记忆系统四层设计 + 自动注入** | 展示产品思维 |
| 12-13 min | **Agent 循环可中断 + SubAgent** | 展示架构扩展性与用户体验 |
| 13-14 min | **身份与工作区解耦的运行时切换** | 展示架构解耦与多项目管理 |
| 14-15 min | 优化方向 + 学习收获 | 展示反思能力 |

### 核心原则

> **重点讲"为什么这样设计"，而不是"做了什么"。**

每个亮点都应该回答三个问题：
1. **遇到什么问题？**（动机）
2. **为什么选这个方案？**（对比其他方案的取舍）
3. **有什么不足？**（展示反思能力）

### 一句话总结

> 这个项目最大的亮点不是"做了什么功能"，而是"**在功能背后的工程决策**"——流式提前执行、只读工具并行、三级路由、错误分类重试、记忆自动注入、双向协同取消、破坏性截断的取舍，每一个都体现了对"Agent 如何在生产环境可靠运行"的深度思考。

---

*本文档基于 MyClaw 后端代码审查整理，代码引用以实际源码为准。*
