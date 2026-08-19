# Agent 工程设计 — MyClaw 面试亮点详解

> 本文档从"实现一个生产级 Agent 助手"的工程视角，梳理 MyClaw 在架构设计上的核心亮点（前端部分见第 18 章）。每个亮点包含**设计动机**、**实现方案**、**代码引用**和**对比分析**，适用于面试展示和技术复盘。

---

## 目录

1. [流式工具调用提前执行](#1-流式工具调用提前执行)
2. [Context Guard 三级自动路由](#2-context-guard-三级自动路由)
3. [双重正则错误分类 + 指数退避重试](#3-双重正则错误分类--指数退避重试)
4. [多源配置组合的 Agent 人格系统](#4-多源配置组合的-agent-人格系统)
5. [Master-Worker 子代理架构](#5-master-worker-子代理架构)
6. [Task 依赖阻塞与自动解除](#6-task-依赖阻塞与自动解除)
7. [记忆系统与三通道跨会话回忆](#7-记忆系统与三通道跨会话回忆)
8. [MCP 渐进披露机制](#8-mcp-渐进披露机制)
9. [BashTool 安全沙箱](#9-bashtool-安全沙箱)
10. [会话编辑与时间线分叉](#10-会话编辑与时间线分叉)
11. [工具调用去重与限量保护](#11-工具调用去重与限量保护)
12. [Agent 循环可中断与协同取消](#12-agent-循环可中断与协同取消)
13. [多模态 Token 估算与自适应压缩](#13-多模态-token-估算与自适应压缩)
14. [身份与工作区解耦的运行时切换](#14-身份与工作区解耦的运行时切换)
15. [意图识别：三模式 Agent 路由与 Plan 两阶段执行](#15-意图识别三模式-agent-路由与-plan-两阶段执行)
16. [用户画像：对话驱动的自动聚合系统](#16-用户画像对话驱动的自动聚合系统)
17. [最难的部分：三道硬关与攻克方案](#17-最难的部分三道硬关与攻克方案)
18. [前端 SSE 流式增量渲染与无损编辑](#18-前端-sse-流式增量渲染与无损编辑)
19. [Skill 自进化：流程型知识与生命周期维护](#19-skill-自进化流程型知识与生命周期维护)
20. [面试展示策略](#20-面试展示策略)

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

Agent 工具调用的输出大小差异巨大：`calculator` 返回约 100 tokens，`Read` 可能返回数千 tokens，`web_fetch` 可能返回近万 tokens。如果所有输出都直接灌入主上下文，几次大输出后上下文就被占满，导致频繁压缩、丢失历史信息。

传统方案是「事后截断」——等上下文超阈值后再裁剪。但裁剪是破坏性操作，截断后难以恢复。更好的思路是「事前预判」——在工具执行前就决定结果如何进入上下文。

同时，不同模型的上下文窗口从 32K 到 1M 不等：若把 inline/snip/delegate 的分界写死为 2000/8000，在大窗口上会把本可内联的结果错误委派给子代理。MyClaw 因此把**额度阈值**做成窗口比例，而把工具 `output_size_hint` 保留为输出规模的绝对值。

### 实现方案

`ContextGuard`（`context/context_guard.py`）在工具执行前拦截，基于工具输出预估值与**随窗口动态的阈值**做三级路由：

```
工具调用
  ├── 副作用工具黑名单 → inline/snip（无论如何不委托）
  │     Write, Edit, memory_add, memory_delete,
  │     calculator, task, subagent, Skill, browser, automation
  │
  ├── 预估 < small_threshold → inline（直接执行，结果放入主上下文）
  │
  ├── 预估 < large_threshold → snip（正常执行；压缩层按 tool_snip_chars 截断）
  │
  └── 预估 >= large_threshold → delegate（委托子代理，主上下文只收到摘要）
```

128K 基准下 `small_threshold=2000`、`large_threshold=8000`（与历史行为对齐）；窗口变大时阈值按比例升高。`MyClawAgent._sync_context_window` 同步更新 Guard 与 `ContextManager.tool_snip_chars`。

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

**1. 副作用工具黑名单**：`Write`、`memory_add` 等有副作用的工具即使预估输出大也不会被委托——因为委托意味着在子代理的隔离上下文中执行，主 Agent 无法直接感知副作用结果。

**2. 优雅降级**：委托失败时自动降级为本地直接执行，保证功能不中断。

**3. 语义化任务描述**：`delegate_tool` 内部有 `_format_task_description`，针对不同工具生成子代理可理解的自然语言任务（工具名与注册名一致，如 `Read`）：

```python
# Read 的任务描述
"使用 Read 工具读取文件 'src/main.py'（限制 100 行）。
 只输出文件的完整内容，不要做任何解释或总结。"
```

**4. 阈值与 hint 解耦**：hint 描述「工具大概吐多少」；阈值描述「当前窗口愿意花多少」。只缩放后者，大窗口才真正少委派、少误裁。

### 对比分析

| 方案 | 触发时机 | 保护程度 | 副作用安全 |
|------|---------|---------|-----------|
| 事后截断（传统） | 上下文超阈值后 | 低（已污染上下文） | 不涉及 |
| 二元委托 | LLM 自主判断 | 中（依赖 LLM 配合） | 无保护 |
| **三级路由（MyClaw）** | **工具执行前** | **高（事前拦截 + 窗口自适应）** | **黑名单保护** |

### 面试展示要点

> "子代理委托不是简单的开关，我设计了三级路由：小输出直接执行、中输出执行后由压缩层截断、大输出委托子代理。额度阈值按模型上下文窗口比例动态计算，工具 hint 保持绝对值，避免大窗口下误委派。还有副作用工具黑名单防止 Write 这类工具被误委托。委托失败时自动降级为直接执行，保证不中断。"

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

`MyClawAgent._build_system_prompt` 从多个配置文件组合**基座**系统提示词（**身份从基座 IdentityManager 读，AGENTS 从当前工作区读、基座 fallback**）。该结果经 `ensure_session_system_prompt` 在会话内冻结；每轮变化的记忆与 Plan 段走 `turn_context`（本轮 user 前缀，不入历史）。

```
AGENTS.md      → 主体行为规范（工作区 .myclaw/ 优先，否则 ~/.helloclaw/AGENTS.md）
BOOTSTRAP.md   → 启动引导（入职未完成时从 identity/ 注入）
IDENTITY.md    → 身份认知（~/.helloclaw/identity/）
USER.md        → 用户画像（~/.helloclaw/identity/）
SOUL.md        → 性格特征（~/.helloclaw/identity/）
+ 回忆通道指引（session_search 启用时）
```

**关键代码**（`myclaw_agent.py`）：

```python
def _build_system_prompt(self) -> str:
    agents_content = self.workspace.load_config("AGENTS")
    if not agents_content:
        # 基座 AGENTS.md fallback
        base_agents_path = os.path.join(self.home_path, "AGENTS.md")
        ...
    context_parts = []

    if not self.identity.is_onboarding_completed():
        bootstrap = self.identity.bootstrap
        if bootstrap:
            context_parts.append(f"\n## 初始化引导\n\n{bootstrap}")

    identity = self.identity.identity
    if identity:
        context_parts.append(f"\n## 你的身份信息\n{identity}")

    user_info = self.identity.user
    if user_info:
        context_parts.append(f"\n## 用户信息\n{user_info}")

    # ... SOUL、子代理指引、任务管理、记忆、session_search 指引 ...

    return base_prompt + "\n" + "\n".join(context_parts)
```

**会话内冻结与边界重建**：同会话连续对话不重写基座 system；在切换会话、切换工作区或入职完成时 `ensure_session_system_prompt(force=True)` 重建。LLM 配置仍每轮检测热加载。

```python
def chat(self, message, session_id=None, ...):
    self._reload_llm_if_changed()  # 检测 config.json 变化
    self.ensure_session_system_prompt()  # 会话内冻结；边界才重建
    turn_context = self._compose_turn_context(
        self._inject_relevant_memories(message),
    )
    ...
```

### 设计亮点

**1. 关注点分离**：每种人格信息独立文件，用户可以只修改 `USER.md` 而不影响 `AGENTS.md`。

**2. 条件注入**：`BOOTSTRAP.md` 只在入职未完成时注入，完成后自动消失，不浪费上下文空间。

**3. Prompt cache 友好**：基座 system 会话内稳定；相关记忆与 Plan 附加段挂在本轮 user 前缀（`turn_context`），不写入会话历史。

**4. 边界热加载**：修改身份/AGENTS 后，切换会话或工作区即可带上新内容，无需重启服务。

### 面试展示要点

> "Agent 的人格不是写死在代码里的，而是由多个配置文件组合而成：基础规范、身份认知、用户画像、性格特征各自独立。基座系统提示在会话内冻结以利于前缀缓存；每轮相关记忆走 user 侧临时上下文。入职引导在完成后从下一边界重建中消失，不浪费上下文。"

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

**4. 进度摘要**：`TaskTracker.get_progress_summary()` 生成 Markdown 进度报告，经任务工具返回值与相关 API 暴露给 Agent/前端；**不**自动写入 system 或 `turn_context`。Plan 执行期注入的是 `TodoScheduler.get_progress_summary()`（与 TaskTracker 不同模块）。

### 面试展示要点

> "任务之间可以指定依赖关系，系统自动维护双向依赖。完成一个任务时自动解除下游任务的阻塞，开始任务时检查依赖是否满足。任务列表持久化到磁盘，切换会话时自动恢复。"

---

## 7. 记忆系统与三通道跨会话回忆

### 设计动机

Agent 需要跨会话记住两类截然不同的东西：

1. **稳定事实 / 偏好**（「我喜欢简洁回复」）——表述可变，适合语义向量。  
2. **某次对话的原话 / 排障过程**（「上周 Redis 端口我们怎么定的？」）——必须可定位到消息级原文。

若把完整 transcript 灌进 Memory，会污染事实库、放大去重噪声，且每轮自动注入会撑爆 token。若只有会话 JSON 给前端、Agent 不可搜，则无法在新会话里回答过程性问题。因此拆成 **三通道**：Memory（事实）+ Profile（人设）+ Session Recall（原文）。

### 实现方案

#### A. Memory 四层（事实层）

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
                 → 格式化为「相关记忆（自动注入）」写入 turn_context
                 → 仅合并进本轮发给模型的 user 前缀（不入 system、不入会话历史）
                 → Qdrant 不可用时静默降级（跳过注入）

  主动检索（Agent 按需）：
    Agent 调用 memory_search → embedding 语义检索 → 命中记忆自动强化

遗忘路径：
  每条记忆 decay_score 初始 1.0
  → 每 7 天按分类速率衰减（entity 0.10, reference 0.30）
  → 被检索命中时重置计时器（用进废退）
  → decay_score 归零时删除（懒处理，启动时批量执行）
```

#### B. Session Recall（原文层）

```
权威原文：~/.helloclaw/sessions/<id>.json（全局，跨工作区）
检索索引：~/.helloclaw/sessions/index.db（SQLite FTS5，可重建）

写入挂钩：
  save_session 成功 → SessionIndexer.upsert_session（失败不阻断对话）
  delete_session   → 同步删索引行
  启动 lifespan    → probe FTS → 空库则后台全量 rebuild

召回工具 session_search（参数推断，无显式 mode）：
  query                         → discover（FTS + 锚定窗口 + bookends）
  session_id + around_message_id → scroll
  仅 session_id                 → read（head/tail）
  无参 / limit                   → browse 近期会话

进模型策略：仅按需工具，不每轮自动注入旧对话全文（控 token）
降级：无 FTS5 → LIKE-only；CJK 少命中 → LIKE fallback；零新依赖
```

#### C. Profile（人设层）

`ProfileAggregator` 把 preference/entity 等记忆沉淀到 `USER.md`，经 `_build_system_prompt` **常驻**注入——偏稳定风格，与每轮 top-K 自动注入互补。

### 设计亮点

**1. 写入双层去重（Memory）**：
- L1 字面去重（默认启用）：`content` 经归一化后求 SHA1[:16]，精确匹配，零误判
- L2 语义去重（默认关闭）：embedding top-1 相似度 ≥ 阈值即视为重复。默认关闭是因为当前 embedding 模型对中文反义/同义辨别力不足

**2. 分类差异化衰减**：

| 分类 | 每 7 天衰减 | 理论寿命 | 设计理由 |
|------|------------|---------|---------|
| entity | 0.10 | ~70 天 | 个人信息，不应遗忘 |
| rule | 0.10 | ~70 天 | 规则约束，持久有效 |
| reference | 0.30 | ~23 天 | URL/路径，易过时 |

**3. 访问强化（用进废退）**：记忆被 `memory_search` / 自动注入检索命中时，自动重置 `last_decay_ts`。频繁被回忆的记忆持久存在，无人问津的自然消退。

**4. 懒处理策略**：衰减计算和删除只在程序启动时或手动调用时执行，不随每轮对话触发，零运行时开销。

**5. 自动注入（被动检索）**：每轮用户消息到达时，后台语义检索 top-3 相关记忆，经 `turn_context` 挂到本轮 user 前缀（不改 system、不写历史）。配置项支持开关、top_k 和相似度阈值。Qdrant 不可用时静默降级。

**6. 事实 / 原文硬边界（Session Recall）**：
- JSON 为 source of truth，`index.db` 可丢可重建——与 Hermes「写入即索引」同构，但 MyClaw 不改 hello-agents 内核，用 save/delete 挂钩增量维护。  
- discover 返回锚定窗口 + 会话首尾 bookend，一次工具调用尽量给出「目标 → 命中 → 上下文」，避免模型连环 scroll。  
- 默认排除当前会话 live 命中，避免把已在上下文中的内容再塞一遍。  
- 索引优先 `metadata.original_content`（压缩 snip 后仍可找回原文）。  
- Ask 模式可读：`session_search` 不在 `SIDE_EFFECT_TOOLS`。

**7. 能力探测 + 零依赖降级**：启动 `probe_sqlite_fts_capabilities()`；无 FTS5 时自动 LIKE，不为此引入 Whoosh/jieba。

### 面试展示要点

> "回忆不是一个桶。我们拆成三通道：Memory 用 Qdrant 存短事实，每轮 top-3 挂到 user 侧临时上下文（不打断 system 前缀缓存），再加衰减和用进废退；Profile 把偏好沉淀进 USER.md 常驻于冻结 system；Session Recall 用全局会话 JSON + SQLite FTS，Agent 用 session_search 按需钻原文——锚定窗口加 bookend，不把旧对话每轮灌进 prompt。事实和原文分开，是为了既省 token，又答得出『上次我们具体怎么说的』。"

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
Layer 0: Agent 基座  ~/.helloclaw/          ← 进程固定，永不随切工作区变化
  ├── identity/  (IDENTITY/SOUL/USER/BOOTSTRAP)  身份/人格基座
  ├── sessions/ tasks/ config.json / skills/     全局共享
  ├── AGENTS.md  (全局默认 prompt 骨架 fallback)
  └── workspaces.json  (已授权工作区白名单)
Layer 1: 用户工作区  <project>/.myclaw/      ← 运行时切换
  ├── AGENTS.md  (项目级行为规范，可选)
  ├── uploads/ skills/ automations/
Layer 2: 会话层      ← 不变（原有机制；落点在 Layer 0 sessions/）
```

身份文件从工作区迁移到固定基座目录，工作区文件归拢到 `.myclaw/` 隐藏子目录。System Prompt 构建时：identity 从基座读（不随工作区变），AGENTS.md 优先用工作区的、基座作 fallback。

**bind_workspace() 全量重绑**：Agent 构造期把 `workspace_path` 深度绑定到约 12 处引用点（file tools、`SkillLoader`、`SubAgentOrchestrator`、`EnhancedSimpleAgent`、`uploads_root` 等），切换时全部运行时 setattr 更新（sessions 和 tasks 已于 v2 全局化，不再需要重绑）。**已是当前路径则幂等返回**，避免路径规范化差异误报未授权：

```python
def bind_workspace(self, workspace_path: str):
    abs_path = os.path.abspath(os.path.expanduser(workspace_path))
    if os.path.realpath(abs_path) == os.path.realpath(self._current_workspace):
        return  # 幂等
    if not is_allowed(workspace_path):
        raise ValueError(f"工作区未授权")
    self.workspace = WorkspaceManager(abs_path)
    self.workspace.ensure_project_workspace()  # 部署 .myclaw/

    # 全量重绑约 12 处引用点
    self._rebind_workspace_tools(abs_path)     # Read/Write/Edit/Bash/RAG
    self.skill_loader.skills_dir = Path(self.workspace.skills_path)
    self.skill_loader.clear()                  # 清缓存重新加载
    self._subagent_orchestrator.workspace_path = abs_path
    self._agent.workspace_root = Path(abs_path).resolve()
    self._memory_capture_manager.workspace_manager = self.workspace
    self.ensure_session_system_prompt(force=True)  # 会话边界重建（identity 不变）
    # sessions/tasks 已全局化（~/.helloclaw/），无需重绑
```

**两阶段部署**：
- Phase 1（启动时）：从 `templates/identity/` 部署身份文件到 `~/.helloclaw/identity/`，含 V1→V2 自动迁移；并对当前工作区 `ensure_authorized`
- Phase 2（切工作区时）：懒部署 `.myclaw/` 子目录结构 + `.gitignore` 自动注入

**身份工具路径别名（解耦后的关键修补）**：hello_agents 的相对路径默认落在工作区。若不处理，模型 `Write("IDENTITY.md")` 会写到项目根而非基座。`identity_paths.install_identity_path_resolver` 包装 Read/Write/Edit 的 `_resolve_path`（裸名 / `identity/` / 工作区根同名 → 基座），并对 Write/Edit 临时切换 `working_dir` 以免备份路径 `relative_to` 失败。AGENTS/BOOTSTRAP 模板同步禁止写到工作区根。

### 设计亮点

**1. 毫秒级切换不重启进程**：通过 setter 全量重绑工具根目录，无需重建 Agent 实例，切换开销仅文件系统操作。

**2. 配置读写分流**：`IDENTITY/SOUL/USER/BOOTSTRAP` 从 IdentityManager（基座）读写，`AGENTS/HEARTBEAT` 从 WorkspaceManager（当前工作区 `.myclaw/`）读写。切换工作区后身份不变。

**3. V1→V2 自动迁移幂等**：`.v2_migration_done` 标记防止重复迁移；旧 workspace 的身份文件自动拷贝到基座 `identity/`。

**4. 基座 AGENTS.md fallback 防崩溃**：工作区 AGENTS.md 是可选的，不存在时用基座 `~/.helloclaw/AGENTS.md` 作为 prompt 骨架，避免切换到新工作区时 RuntimeError。

**5. BashTool `_cwd` 重置防跨工作区逃逸**：切换时重置 cd 历史，防止通过 `cd` 跳出白名单访问其他工作区文件。

**6. 白名单 + 当前工作区自愈**：新路径须显式授权（`workspaces.json`）；启动与 `GET /api/workspace/list` 对**当前**工作区 `ensure_authorized`，避免「进程已绑定却报未授权」。默认 `WORKSPACE_PATH=~/.helloclaw/workspace`。

**7. 身份文件工具别名**：不新增核心工具，用 monkey-patch 把身份四件套接到基座，提示词与工具层双重约束，防止入职流程污染项目目录。

### 对比分析

| 方案 | 身份/工作区关系 | 切换开销 | 沙箱机制 |
|------|---------------|---------|---------|
| OpenClaw per-agent workspace | 身份在 workspace 内 | 重建 agent | 可选 sandbox |
| Cursor Multi-root | User Rules 全局 | 索引重建 | IDE 内置 |
| WorkBuddy 空间切换 | 空间级身份 | 空间切换 | 本地/云双模 |
| **MyClaw bind_workspace** | **基座/工作区分离 + 路径别名** | **毫秒级 setter 重绑** | **白名单 + 身份别名通道** |

### 面试展示要点

> "Agent 的身份和工作区原本耦合在一个目录，切项目就丢人格。我把它们解耦成三层：身份固定在 ~/.helloclaw/identity/，会话/任务/配置全局共享，项目文件归拢到 .myclaw/。切换时 bind_workspace 全量重绑约 12 处引用点，毫秒级、不重启；同路径幂等。白名单防任意路径越权，但对启动默认工作区做 ensure_authorized 自愈。解耦后还有第二道坑：模型仍按相对路径 Write IDENTITY.md，会写到工作区根——用 identity_paths 包装 Read/Write/Edit 重定向到基座，并临时切 working_dir 兼容工具备份逻辑。"

---

## 15. 意图识别：三模式 Agent 路由与 Plan 两阶段执行

### 设计动机

传统 Agent 只有"全自动"一种模式——LLM 自由调用所有工具，包括文件写入和命令执行。但很多场景不需要副作用工具：
- 用户只是想问"这个函数怎么用"，不希望 Agent 擅自修改代码
- 用户需要先看 Agent 打算怎么做，确认后再动手

正则自动分类会误判（"帮我写个脚本读文件"→ 到底是只读还是写？），而误判比不分类更糟——用户预期被违背。

### 实现方案

MyClaw 参考 WorkBuddy 的做法，让用户**显式选择模式**，后端根据模式过滤工具集：

| 模式 | 允许的工具 | 禁止的工具 | 执行流程 |
|------|-----------|-----------|---------|
| **Ask** | Read/Search/Web/List/RAG/Memory/session_search/Skill/MCP | Write/Edit/Execute/Automation | 单轮 ReAct，LLM 直接回复 |
| **Plan** | 规划期间 Ask；执行期全部 | 规划期禁止副作用工具 | 两阶段：规划→用户确认→执行 |
| **Craft** | 全部 | 无 | 自动 ReAct 循环，无需确认 |

#### Plan 两阶段执行流程

Plan 模式是整个系统中交互最复杂的模式，需要在无状态的 HTTP 请求之间传递状态：

```
[用户选择 Plan 模式发送消息]
  ↓
[后端] 设置 READ_ONLY 模式 → ReAct 循环
  ├── 注入规划指令："先分析问题，输出结构化 TODO JSON"
  ├── LLM 在 READ_ONLY 模式下收集信息
  └── 解析 LLM 输出中的 TODO JSON
  ↓
[后端] _store_pending_plan(session_id, ...) → {session_id}_plan.json
  ↓
[后端] yield _create_plan_event(todo_list) → SSE: plan_generated
  ↓ (SSE 流结束，等待用户操作)
[前端] 解析 PlanTodoItem[] → 渲染 Markdown TODO 确认卡片
  ├── 用户点击「确认」→ POST /api/chat/stream { mode: "craft", planConfirmed: true, skipUserMessage: true }
  └── 用户点击「取消」→ 清理状态，对话结束
  ↓
[后端] _load_pending_plan(session_id) 恢复计划
  ├── 进度摘要写入 turn_context（本轮 user 前缀）
  ├── 设置 FULL 模式 → ReAct 循环执行
  └── 完成后 _cleanup_plan_file(session_id)
```

#### 三个关键子问题

**① 如何在不修改外部库的前提下过滤工具？**

`hello_agents` 的 `ToolRegistry` 是外部包，不能直接加 mode 参数。MyClaw 设计了 `ToolModeFilter` 包装器：

```python
class ToolModeFilter:
    """包装外部 ToolRegistry，通过 wrapper 模式实现模式切换。
    
    不修改 ToolRegistry 源码，通过代理模式拦截 get_tools()/get_tool()。
    """
    _SIDE_EFFECT_TOOLS = frozenset({"write", "edit", "execute_command", "automation"})

    def set_mode(self, mode: ToolMode):
        self._mode = mode  # READ_ONLY 时屏蔽副作用工具

    def get_tools(self) -> List[Tool]:
        all_tools = self._registry.get_tools()
        if self._mode == ToolMode.FULL:
            return all_tools
        return [t for t in all_tools if t.name not in self._SIDE_EFFECT_TOOLS]
```

**② 如何在无状态 HTTP 间传递 Plan？**

Plan 生成和确认执行是**两次独立的 HTTP POST 请求**，中间 SSE 流已结束。MyClaw 通过**文件暂存 + 内存双通道**解决：

```python
# 规划阶段结束 → 写入文件
def _store_pending_plan(self, session_id, todo_list, plan_text):
    plan_file = os.path.join(self._task_tracker._persist_dir, f"{session_id}_plan.json")
    json.dump({"todo": todo_list, "plan_text": plan_text, "timestamp": time.time()}, fp)
    self._pending_plans[session_id] = {"todo": todo_list, "plan_text": plan_text}

# 确认后新请求到达 → 从文件恢复
def _load_pending_plan(self, session_id):
    # 优先内存，降级到文件
    if session_id in self._pending_plans:
        return self._pending_plans.pop(session_id)
    plan_file = os.path.join(..., f"{session_id}_plan.json")
    return json.load(fp) if os.path.exists(plan_file) else None
```

**③ 如何发送自定义 SSE 事件而不修改外部 StreamEventType 枚举？**

`hello_agents` 的 `StreamEventType` 枚举不支持 `plan_generated` 类型。MyClaw 创建了与 `StreamEvent` 接口兼容的自定义 dataclass：

```python
@dataclass
class _PlanEvent:
    type: StreamEventType  # 复用枚举字段名
    data: dict             # 复用 data 字段名

# 创建时：type 设置为 StreamEventType.PHRASE_FINISH 做类型占位，
# data 中携带 plan 列表。前端检查 data.plan 字段判断是否为 plan 事件。
```

### 设计亮点

1. **零误判**：用户显式选择模式，不需要 LLM/正则做意图分类
2. **双重工具门控**：`ToolModeFilter` 过滤 schema（LLM 看不到禁用的工具）+ `_execute_tools_batch` 运行时检查（LLM 即使幻觉调用了也会被拦截）
3. **Plan 两阶段**：规划期 READ_ONLY 确保 LLM 不会提前动手，确认后才解锁全部工具
4. **Plan 失败终止**：Plan 生成失败或用户拒绝时直接终止对话，不降兜到 Craft——避免用户预期违背
5. **资源清理**：Plan 执行完成后自动删除 `_plan.json`；`delete_session` 时间步清理

### DAG 任务调度器

Plan 生成的 TODO 列表有依赖关系（"写函数"依赖"创建文件"），`TodoScheduler` 负责：

```python
# 依赖解析 → 拓扑排序 → 按依赖顺序调度
def get_ready_tasks(self) -> List[TodoItem]:
    """返回所有依赖已满足的 pending/ready 任务"""
    ready = []
    for todo in self._todos.values():
        if todo.status not in ("pending", "ready"):
            continue
        deps_met = all(
            self._todos[dep_id].status == "completed"
            for dep_id in todo.dependencies
        )
        if deps_met:
            ready.append(todo)
    return ready

def fail_downstream(self, todo_id: str) -> None:
    """BFS 级联标记所有下游依赖为 failed（迭代式，避免递归栈溢出）"""
    queue = [todo_id]
    visited = set()
    while queue:
        current = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        for tid, todo in self._todos.items():
            if current in todo.dependencies and todo.status in ("pending", "ready"):
                todo.status = "failed"
                queue.append(tid)
```

### 面试展示要点

> "Agent 有三种运行模式：Ask 只读问答、Plan 先规划再执行、Craft 全自动。核心设计有两个：ToolModeFilter 包装器不修改外部库就能过滤工具；Plan 两阶段通过文件暂存解决无状态 HTTP 之间的状态传递——规划期 READ_ONLY 生成 TODO JSON，SSE 推送给前端确认，确认后新请求从文件恢复计划继续执行。任务之间有依赖就用 DAG 调度器按拓扑排序调度，失败自动 BFS 级联阻塞下游。"

### 为什么不是"最难部分"

意图识别和 Plan 执行的核心挑战是**集成复杂度**（状态在 HTTP 请求间传递、外部库接口绕过），而非算法或并发安全层面的根本性难题。文件暂存 + SSE 事件流的方案是成熟的工程模式，DAG 调度器也是经典的拓扑排序变体。它体现了良好的工程设计，但不需要像流式工具并发安全那样遍历所有状态组合来验证正确性。

---

## 16. 用户画像：对话驱动的自动聚合系统

### 设计动机

参考 Mercedes-Benz AG 和乌尔姆大学 2025 年的研究："画像不是一次性创建，而是对话的自然副产品"。传统 Agent 需要用户手动填写技术栈、代码风格等偏好，但用户通常不会主动维护。理想的方案是：Agent 在每次对话后自动从记忆中发现用户的偏好和特点，更新到画像中。

### 实现方案

```
对话结束 (achat() 末尾)
  ↓
ProfileAggregator.should_trigger() — 检查条件：
  ├── 每 10 轮对话触发一次
  └── preference 记忆超过 20 条时触发（_list_recent 精确统计）
  ↓
ProfileAggregator._collect_memories() — 从 Qdrant 检索 preference/entity/decision
  ↓
LLM 聚合 (_llm_aggregate / _llm_aggregate_sync)
  ├── 调用轻量 LLM 聚合为结构化摘要（~2000 token/次）
  └── 降兜：_text_aggregate() — 纯文本分组 + tech_keywords 区分技术栈/工作领域
  ↓
ProfileAggregator._update_user_md() — 原子写入 USER.md
  ├── HTML 注释标记区域边界（<!-- AUTO:tech_stack --> ... <!-- /AUTO:tech_stack -->）
  ├── _replace_region() 精确替换（不触碰手动编辑区域）
  ├── _atomic_save_user_md()：tempfile + os.replace 原子写入
  └── 注入防护：清理 LLM 输出中的 <!-- AUTO:xxx --> 标记
  ↓
下次对话 — _build_system_prompt() 自动注入 USER.md 到系统提示词
```

#### USER.md 的 AUTO 区域模板

```markdown
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
```

#### 三个关键子问题

**① 同步和异步两种调用场景如何统一？**

画像聚合有两个调用入口：`achat()` 末尾（异步）和 `memory_aggregate_profile` 工具（同步 `MemoryTool.run()`）。如果在同步上下文中创建新事件循环调用 `async aggregate()`，会触发 `RuntimeError`。

MyClaw 提供双接口：

```python
# 异步接口（achat() 中调用）
async def aggregate(self) -> dict:
    return await self._do_aggregate_async()

# 同步接口（MemoryTool 中直接调用，不创建新事件循环）
def aggregate_sync(self) -> dict:
    return self._do_aggregate(sync_llm=True)
```

两者共享 `_do_llm_aggregate` 核心方法（LLM 的 `invoke()` 本身是同步调用），只是外层接口签名不同。

**② LLM 不可用时的降兜方案？**

`_text_aggregate()` 不依赖 LLM，通过关键词分类完成聚合：

```python
tech_keywords = {"python", "java", "typescript", "vue", "react", "fastapi", "docker", ...}
tech_items = [e for e in entities if any(kw in e.lower() for kw in tech_keywords)]
domain_items = [e for e in entities if e not in tech_items]
# 用 decision 记忆补充 work_domain，避免永远为空
```

**③ 如何防止并发写入冲突？**

`USER.md` 可能同时被 Agent 对话和用户手动编辑。`_atomic_save_user_md` 写入临时文件后用 `os.replace` 原子替换，失败时降兜到 `identity.save_file()` 直接写入。

同时，写入前清理 LLM 输出中可能包含的 `<!-- AUTO:xxx -->` 标记，防止 LLM 注入破坏区域边界。

### 设计亮点

1. **画像跟随用户而非项目**：存于 `~/.helloclaw/identity/USER.md`，切换工作区不影响画像
2. **自动 + 手动共存**：AUTO 区域由系统维护，其他区域用户自由编辑，互不干扰
3. **触发条件精确计数**：用 `_list_recent(top_k=200)` 遍历所有记忆再按 `category == "preference"` 过滤，而非 `top_k=1 * 20` 的粗略估算
4. **平均成本极低**：聚合使用轻量 LLM，每触发一次 ~2000 token，10 轮触发一次，平均每轮仅 +200 token
5. **系统提示词天然感知**：USER.md 已被 `_build_system_prompt()` 注入，Agent 无需额外逻辑就能以用户画像为参考

### 面试展示要点

> "用户画像不是让用户填表，而是在对话中自动发现的。每次对话结束检查是否需要聚合——每 10 轮或偏好记忆超过 20 条就触发一次，用轻量 LLM 把 preference/entity/decision 记忆提炼成结构化文本，写入 USER.md 的 AUTO 区域。有同步和异步两个调用入口——achat 末尾和 memory_aggregate_profile 工具，共享同一个聚合核心。LLM 不可用时降兜到纯关键词分类。写入用 os.replace 原子替换防并发冲突，同时清理 LLM 输出中的区域标记防注入。"

### 为什么不是"最难部分"

画像聚合的每个子问题都有清晰的解决方向：LLM 聚合有降兜方案、并发写入有原子操作、同步异步分离有双接口模式。它们的组合体现了良好的工程设计，但没有一个问题是"遍历所有可能性才能验证正确性"或"阈值全靠经验调"的那种根本性认知挑战。

---

## 17. 最难的部分：三道硬关与攻克方案

> 如果面试官问"这个项目最难的是什么"，以下三道关是真正花时间啃下来的硬骨头。每个问题都记录了从**发现 → 误判 → 推翻 → 找到正确解法**的完整过程。
>
> **说明**：意图识别（§15）和用户画像（§16）虽然也有设计决策，但核心挑战是**集成复杂度**（状态在 HTTP 请求间传递、外部库接口绕过）而非**根本性认知挑战**（正确性证明、遍历全状态验证、无最优解的阈值权衡）。三道硬关的共同特征是：**没有正确解，只有更优解，需要反复验证才能确信不会出错**。

### 17.1 第一道关：流式工具调用的并发安全性

#### 问题描述

这是整个项目中**实现复杂度最高**的模块。LLM 流式输出工具调用时，参数 JSON 是增量到达的。如果等所有工具参数都到了再统一执行，就浪费了等待时间。但如果在参数完整的瞬间就提前执行，就面临一个棘手的并发问题：

**同一轮对话中，LLM 连续生成两个 `write_file("config.py", ...)` → `read_file("config.py", ...)`。如果 `read_file` 在 `write_file` 完成前就执行了，读到的是旧内容。**

#### 走过的弯路

| 尝试 | 方案 | 为什么失败 |
|------|------|-----------|
| v1 | 所有工具在 FINISH 后统一执行 | 延迟高，工具 0 明明已经参数完整了却要等工具 N 的参数都到场 |
| v2 | `TOOL_CALL_DELTA` 到达时 `create_task` 异步执行 | **数据竞争**：两个 write_file 可能被并发调度，`asyncio.create_task` 不保证执行顺序 |
| v3 | 在 `_execute_tools_batch` 中逐个 `await` | ✅ 串行安全，但**完全放弃了 DELTA 阶段的提前执行优势**，又退回到 v1 |

#### 正确解法：状态机 + 三种执行路径 + 元数据驱动的分组

最终方案不是"选一种执行模式"，而是**根据事件类型和执行阶段采用不同策略**——这是一个典型的状态机设计：

```
事件流              执行策略                     安全保证
───────────────────────────────────────────────────────────
TOOL_CALL_DELTA  →  async for 同步等待完成     ← 天然串行（async for 不消费完不会回到外层循环）
  (参数完整后)        工具执行完毕才接收下一个 LLM 事件

TOOL_CALL_START  →  遍历前序工具，同步逐个执行    ← 已被 executed=True 标记的跳过
  (新工具开始前)      只执行已完成参数解析但未执行的

FINISH            →  分组 + 并行/串行             ← has_side_effects 元数据驱动
  (流结束后)          read_file 等无副作用 → asyncio.Queue 并行
                     write_file 等有副作用 → async for 串行
```

**核心的三层安全保障**：

```
保障 1（TOOL_CALL_DELTA 天然串行）：
  async for tool_event in self._try_execute_ready_tool(...):
      yield tool_event
  # ← tool_0 的 write_file 已完全执行完毕，
  # 现在外层循环才接收 tool_1 的 LLM 事件
  # → 不存在 write_file(0) 和 read_file(1) 的执行顺序颠倒

保障 2（先标记后执行，防重复）：
  def _try_execute_ready_tool(self, tc_state, ...):
      tc_state["executed"] = True   # ← 先标记
      # 即使后续 TOOL_CALL_START 遍历前序工具时，
      # 也会直接跳过这个已标记为 executed 的工具
      task = asyncio.create_task(...)
      # 然后才执行

保障 3（副作用分组，FINISH 时也不并发）：
  _execute_tools_batch(tool_calls):
      parallel = [tc for tc in tool_calls if not has_side_effects(tc)]
      serial   = [tc for tc in tool_calls if     has_side_effects(tc)]
      # write_file 属于 has_side_effects=True → 永远在 serial 组
      # serial 组用 async for 逐个执行，绝对不并发
```

#### 这个为什么难

**难在"正确性证明"**。不是写不出并行执行，而是很难向自己证明"在所有可能的 LLM 输出模式下都不会出错"。LLM 的输出是不可预测的——工具调用数量、参数完整时机、工具类型组合，没有任何确定性保证。需要遍历所有状态组合来验证安全性：

- 单个工具、多个无副作用工具、多个有副作用工具、混合
- 流式提前执行 vs FINISH 批量执行的两条路径
- TOOL_CALL_START 事件是否在所有 DELTA 之后到达
- 同一个工具是否可能在两条路径中都被执行

最终用了**三层保障互为冗余**的设计：即使某一层因为未预见的流事件顺序而失效，另外两层仍能兜底。

#### 面试表述

> "最难的是流式工具调用的并发安全。LLM 增量输出工具参数，我想参数完整就立即执行以减少延迟，但两个有副作用的工具如果并发会导致数据竞争。我试了三种方案：统一等待（太慢）、create_task 异步执行（不安全）、逐个 await（退回到串行）。最终方案是状态机：在 DELTA 阶段提前执行但用 async for 保证串行、START 阶段只执行前序未执行工具、FINISH 阶段分组——无副作用工具并行、有副作用工具串行。三层保障互为冗余，确保在所有 LLM 输出模式下都不会出错。"

---

### 17.2 第二道关：跨 12 个引用点的运行时工作区切换

#### 问题描述

Agent 在构造期间将当前工作区路径**深度绑定**到了整个系统：文件工具（Read/Write/Edit/Bash）的根目录、Skill 加载目录、子代理编排器的 workspace_root、浏览器截图上传目录、自动化任务存储……多达 12 个独立的引用点。

最初的设计是"一个进程 = 一个工作区"，用户想换项目就得重启服务。这显然不可接受——Cursor 和 WorkBuddy 都能运行时切换项目。

#### 为什么不是"销毁 → 重建"

直觉方案是切换工作区时销毁当前 Agent 实例、用新工作区新建一个。但这条路行不通：

1. **LLM 连接池重建开销大**：`EnhancedSimpleAgent` 内部持有 aiohttp 连接池、tokenizer 实例、Qdrant 客户端等重量级对象
2. **进行中的对话会中断**：正在等待 LLM 响应的 SSE 连接失去 Agent 引用
3. **全局状态管理复杂**：`main.py` 中 `_agent` 是全局单例，多线程替换需要加锁、处理竞态

#### 正确解法：`bind_workspace()` 全量 setattr 重绑

不走销毁重建，而是在**保持 Agent 实例存活**的前提下，用运行时 setattr 更新所有绑定路径：

```python
def bind_workspace(self, workspace_path: str):
    """切换到指定工作区（运行时动态重绑 12 处引用点）
    
    注：sessions 和 tasks 已于 v2 迁移至 ~/.helloclaw/（全局共享），
    切换工作区时不再重绑，会话历史跨工作区保持可见。
    """
    # 1. 白名单鉴权
    if not is_allowed(workspace_path):
        raise ValueError("工作区未授权")

    # 2. 重建 WorkspaceManager（.myclaw/ 结构部署）
    self.workspace = WorkspaceManager(abs_path)
    self.workspace.ensure_project_workspace()

    # 3. 全量重绑（12 处 setattr）
    self._rebind_workspace_tools(abs_path)          # ① Read/Write/Edit/Bash/RAG/Search
    self.skill_loader.update_workspace_dir(...)       # ② Skill 加载器
    self.refresh_skill_tool()                        # ③ Skill 工具重新注册
    self._subagent_orchestrator.workspace_path = ...  # ④ 子代理编排
    self._agent.workspace_root = Path(abs_path)       # ⑤ Agent 工作区根
    self._browser_session._uploads_dir = Path(...)    # ⑥ 浏览器上传
    self._automation_store = AutomationStore(...)     # ⑦ 定时任务
    self._memory_capture_manager.workspace_manager = ...  # ⑧ 记忆捕获
    self.ensure_session_system_prompt(force=True)  # ⑨ 系统提示词（会话边界重建，identity 不变）
    # ⑩⑪⑫ ... 更多引用点

    print(f"🔄 已切换工作区: {abs_path}")
```

#### 关键子问题：为什么设计成三组目录

这个解耦方案催生了一个架构难题：身份文件（IDENTITY、SOUL、USER）原来在 workspace 目录下，现在要移到哪？如果放在工作区里，切项目就又丢人格了。如果全部搬到全局目录，那项目级的 AGENTS.md（行为规范）又失去了工作区隔离的能力。

最终采用**三组目录**的设计：

| 目录 | 内容 | 切换时行为 |
|------|------|----------|
| `~/.helloclaw/identity/` | IDENTITY、SOUL、USER、BOOTSTRAP | **不变**（人格跟随用户，不跟随项目） |
| `~/.helloclaw/` | sessions、tasks、全局 skills、AGENTS.md fallback、config.json | **不变**（跨工作区共享） |
| `<workspace>/.myclaw/` | AGENTS.md（项目级）、uploads、skills（项目级）、automations | **清空引用，指向新目录** |

System Prompt 构建时：`IDENTITY/SOUL/USER` 从 `~/.helloclaw/identity/` 读取（永远不变），`AGENTS.md` 优先用工作区的、基座那份作为 fallback（防止切换到空项目时 RuntimeError）。

工具层：`identity_paths` 把模型常用的裸文件名重定向到基座，避免「Prompt 读基座、Write 却落到工作区根」的半解耦状态。白名单对**当前**工作区启动/list 自愈，对新路径仍强制 authorize。

> **v2 变更**：sessions 和 tasks 从 `<workspace>/.myclaw/` 迁移到 `~/.helloclaw/`，解决了切换工作区后会话历史全部丢失的痛点。迁移后 `bind_workspace` 引用点从 14 减少到 12，不再需要重绑 session_dir 和 tasks_dir。

> **v1.2 变更**：默认工作区与 CLI 对齐为 `~/.helloclaw/workspace`；`ensure_authorized` + list 自愈；身份文件 Read/Write/Edit 别名。

#### v1→v2 演进：SessionStore 缓存问题已随全局化解决

在 v1 架构中（sessions 存于 `<workspace>/.myclaw/sessions/`），最隐蔽的 bug 是 `hello_agents` 库的 `SessionStore` 在 `__init__` 时缓存了 `session_dir`，更新 `Config` 后不自动同步。导致切换工作区后 session 仍保存到旧目录——切换工作区 ="丢失"对话历史。

**v2 解决方式**：将 sessions 迁移到 `~/.helloclaw/sessions/`（全局固定路径），`sessions_path` 属性返回 `os.path.expanduser("~/.helloclaw/sessions")` 并在 getter 中 `os.makedirs(exist_ok=True)` 自动创建。由于路径不随工作区变化，SessionStore 的缓存问题自然消失，`bind_workspace` 中不再需要重绑这一对引用点。

#### V1→V2 兼容迁移

上线时不能假设用户是全新安装。V1 的身份文件散落在 `~/.helloclaw/workspace/` 下（跟项目文件混在一起），V2 需要归类到 `~/.helloclaw/identity/`。迁移逻辑写在 `IdentityManager.ensure_exists()` 中：

```python
def ensure_exists(self, old_workspace=None):
    """部署 identity 基座，支持 V1→V2 迁移"""
    # 幂等标记：迁移一次后不再重复
    if os.path.exists(self.migration_done_flag):
        return

    # 从 V1 workspace 搬移身份文件
    if old_workspace and os.path.isdir(old_workspace):
        for file in ['IDENTITY.md', 'SOUL.md', 'USER.md', 'BOOTSTRAP.md']:
            src = os.path.join(old_workspace, file)
            if os.path.exists(src):
                shutil.copy2(src, self.identity_dir)
                os.remove(src)  # V2 后清理旧位置，避免混淆

    # 标记迁移完成
    with open(self.migration_done_flag, 'w') as f:
        f.write(datetime.now().isoformat())
```

#### 这个为什么难

**难在"不漏引用"**。Agent 系统中工作区路径被引用的位置远超预期——每个人写工具时都自然地把路径存为实例属性、类属性或闭包变量。初次实现 `bind_workspace` 时以为只有 5-6 个引用点，实际排查发现 12 个。每次漏一个就产生一个隐藏 bug：Read 工具读的是旧目录、浏览器截图画到了旧项目、定时任务写到错误的位置……这些 bug 不会立即报错，而是在用户操作到某个具体功能时才暴露。

> **v2 演进**：随着 sessions 和 tasks 全局化，引用点从最初的 14 减少到 12。每个引用点减少都意味着切换工作区后少一个"幽灵状态"的风险来源。

#### 面试表述

> "Agent 的工作区路径被深度绑定到约 12 个引用点。切换时不能销毁重建（开销大、中断对话），也不能只改 WorkspaceManager（工具路径仍是旧的）。用运行时 setattr 全量重绑，实例存活、毫秒级切换；同路径幂等。目录拆三组：基座身份、全局 sessions/tasks/config、工作区 .myclaw。解耦后还要修工具相对路径：identity_paths 把 IDENTITY 等重定向到基座。白名单防越权，当前工作区 ensure_authorized 自愈。"

---

### 17.3 第三道关：上下文窗口的精细化管控

#### 问题描述

Agent 最稀缺的资源不是 GPU、不是内存、不是磁盘——是**上下文窗口**。每次工具调用（搜索、读文件、抓网页）都在往窗口里灌内容，灌满就得压缩，压缩就丢信息。这个项目的核心矛盾是：**如何在有限窗口里挤进最多有用信息，同时不让垃圾撑爆它**。

这其实是一个**经济学问题**：有限资源（128K tokens）的多租户分配。每个功能都在抢：

- 系统提示词（人格 + 行为规范）→ ~5-15K
- 用户消息 + LLM 响应 → 动态增长
- 工具调用参数 → 动态
- 工具输出（read_file/web_fetch/rag）→ 每个 1K~50K，可爆炸
- 记忆自动注入 → ~1-3K/次
- Task 进度注入 → ~0.5-2K/次
- 子代理摘要回传 → 不定

#### 解法：三道防线 + 精细化分配

**事前防线（Context Guard）——"把问题消灭在发生前"**：

```python
# 工具执行前预判输出大小
if self._context_guard.should_delegate(tool_name):
    # 预判 > 8000 token → 委托子代理
    # 主上下文只收到 LLM 压缩的 ≤300 字摘要
    result = await self._context_guard.delegate_tool(tool_name, args)
```

但预判不总是准确。所以：

**事中防线（Context Manager 截断 + 压缩）——"挡不住就止损"**：

```python
# 工具输出 > 2000 token → snip 截断（保留头尾，中间省略）
# 上下文占用 > 128K * 0.8 ≈ 102K → 触发自动压缩
# 压缩策略：保留最近 10 轮完整对话 + 早期消息 LLM 摘要
```

**事后防线（副作用工具黑名单）——"有些工具绝对不能委托"**：

```python
# write_file、memory_add、calculator 等有副作用工具
# 即使预估输出大也不委托——委托意味着在子代理隔离上下文中执行
# 主 Agent 如果不知道文件已写入，后续逻辑会出错
SIDE_EFFECT_BLACKLIST = {
    "write_file", "multi_edit", "memory_add", "memory_delete",
    "calculator", "task", "Skill",
}
```

**更精细的窗口分配：多模态的代价**

用户上传一张 10MB 的截图，如果直接 base64 内联，估算 token 消耗约 200 万——远超整个上下文窗口。所以必须压缩。但压缩到什么程度？太狠了 VLM 识别不出内容，太松了占满窗口。

```
递进压缩策略（每一步后检查是否 ≤ 5MB）：
  JPEG quality 85  →  JPEG quality 75  →  JPEG quality 60
  → resize 0.75x   →  resize 0.5x

历史存储优化：
  不存 base64（每张图 +7MB）→ 存 @FILE:<abs_path>（每张图 +250 字节）
  调用 LLM 前即时从磁盘读回 base64
```

#### 设计哲学：窗口分配经济学

核心原则是 **"系统提示词的每条规则都是一种成本"**。注入子代理使用指引消耗 ~800 token，注入任务管理指引 ~600 token，注入记忆检索 top-3 ~500-2000 token。每加一条，都是在跟用户的对话内容抢空间。

所以我做了"条件注入"：BOOTSTRAP 入职引导只在入职完成前注入，完成后自动消失。记忆自动注入也有 Qdrant score_threshold 过滤，相关性不够的不注入。这些都是"按需付费"的思路——不用就不占窗口。

#### 这个为什么难

**难在"没有正确解"**。上下文窗口管理本质上是**有损压缩**——总有信息会丢失。传统工程问题（性能、安全、可靠性）有明确的正确/错误分界线，但这里没有。你只能选择"丢什么"：

- Context Guard 的 8000 token 阈值——太高了会灌爆窗口，太低了子代理调用太频繁
- 压缩保留 10 轮——太少了丢失关键决策历史，太多了压缩就没意义
- 图片 JPEG quality 60——太低了 VLM 可能误读，太高了窗口被占

**每个阈值都是经验和反复调试的结果**，而不是从哪个论文里搬来的最优值。

#### 面试表述

> "Agent 最稀缺的不是算力，是上下文窗口。它是一个有限资源（128K tokens）的多租户分配问题——系统提示词、对话历史、工具输出、记忆注入、子代理摘要全在抢。我用了三层防线：事前 Context Guard 预判大输出委托子代理，事中 Context Manager 截断 + 自动压缩，事后副作用黑名单防止误委托。图片用递进压缩先降质量再降尺寸，历史里不存 base64 只存路径引用。还有一个'条件注入'思路——入职引导完成后自动消失、低相关记忆不注入——不让不该占窗口的东西抢空间。每个阈值都是经验调出来的，没有正确解只有更优解。"

---
## 18. 前端 SSE 流式增量渲染与无损编辑

### 设计动机

AI 聊天前端通常只需要处理"用户发消息 → 流式接收文本"的简单流程。但 Agent 系统的对话远比这复杂——每一轮对话中包含多个 ReAct 步骤（思考 → 工具调用 → 再思考），SSE 事件流中文本片段（chunk）和工具调用卡片（tool_start / tool_finish）交替到达。如果每个事件都重新渲染整个消息体，性能很差；如果只追加到末尾，则无法表达"文本 → 工具卡片 → 更多文本"的交错布局。

此外，用户编辑历史消息并重新发送是一种高频操作。传统做法是删除该消息之后的所有历史再重发，导致后续有价值的对话丢失。

### 实现方案

#### 18.1 SSE 事件驱动的混合增量渲染

前端没有使用浏览器内置的 `EventSource`（它不支持 POST 请求和自定义请求体），而是基于 `fetch` + `ReadableStream` 手动实现了 SSE 解析器（`chatApi.sendMessageStream`，`chat.ts:84-197`）：

```typescript
// 手动 SSE 解析循环
const reader = response.body?.getReader()
const decoder = new TextDecoder()
let buffer = ''

while (true) {
  const { done, value } = await reader.read()
  if (done) break
  buffer += decoder.decode(value, { stream: true })

  // 按行分割，处理 event: / data: 配对
  const lines = buffer.split('\n')
  buffer = lines.pop() || ''  // 保留不完整的最后一行

  for (const line of lines) {
    if (line.startsWith('event:')) { currentEvent = line.substring(6).trim() }
    else if (line.startsWith('data:')) {
      const parsed = JSON.parse(data)
      // 根据 currentEvent 类型分发给回调
    }
  }
}
```

ChatView 收到事件后，不是简单地拼字符串，而是维护一个 **混合消息段列表**（`MessageSegment[]`），每段可以是 `TextSegment` 或 `ToolSegment`：

```typescript
interface TextSegment {
  type: 'text'
  id: number
  content: string          // 增量拼接
}

interface ToolSegment {
  type: 'tool'
  id: number
  tool: string
  args: Record<string, unknown>
  result?: string
  status: 'running' | 'done' | 'error'  // 实时切换
}
```

9 种 SSE 事件被映射为对段列表的操作：

| SSE 事件 | 前端操作 |
|----------|---------|
| `step_start` | 新建 TextSegment，插入段列表 |
| `chunk` | 找到当前 TextSegment，`content += event.content` |
| `tool_start` | 插入 ToolSegment（status: running），显示加载动画 |
| `tool_finish` | 找到对应 ToolSegment，更新 result 和 status |
| `step_finish` | 标记当前步骤完成 |
| `done` | 对话结束，保存会话 ID |
| `cancelled` | 显示取消提示 |
| `error` | 显示错误信息 |
| `session` | 绑定会话 ID（首次对话时由后端分配） |

工具调用卡片与文本段在同一列表中交替排列，Vue 模板中 `v-for="segment in msg.segments"` 根据 `segment.type` 渲染不同组件。整个过程只操作数组引用（`messages.value = [...messages.value]`），Vue 的响应式系统自动处理最小化 DOM 更新。

#### 18.2 前端消息编辑与无损回执

前端实现了与后端"时间线分叉"（§10）完美配合的前端编辑流程。`replaceUserTurnInUi` 是核心函数（`ChatView.vue:920-938`）：

```typescript
const replaceUserTurnInUi = (turn: number, newContent: string, attachments?) => {
  const split = splitMessagesAtUserTurn(turn)
  if (!split) return null

  const userMsg: Message = {
    id: Date.now(),
    role: 'user',
    content: newContent,
    userTurnIndex: turn,
    attachments,
  }

  // 无损替换：prefix + 新用户消息 + suffix（后续对话完整保留）
  messages.value = [...split.prefix, userMsg, ...split.suffix]
  return split.assistantInsertAt  // 返回插入点，供新回复使用
}
```

`splitMessagesAtUserTurn` 根据 `userTurnIndex` 精确定位要替换的轮次，将消息列表拆为三部分：

```
原始消息列表:
  [User-0] [Assistant-0] [User-1] [Assistant-1] [User-2] [Assistant-2]
                                    ↑ 编辑这一轮

splitMessagesAtUserTurn(1) 输出:
  prefix:  [User-0] [Assistant-0]            ← 保留
  suffix:  [User-2] [Assistant-2]            ← 保留
  ↓ 替换 User-1 内容，清空旧的 Assistant-1
```

编辑弹窗还处理了一个细节：用户消息的 `content` 可能包含 `<file name="..." kind="...">...</file>` 文档注入块（由后端的 `build_user_content` 生成）。编辑时用正则 `USER_FILE_BLOCK_RE` 剥离这些块，只让用户编辑自然语言部分，提交时再原样拼回——用户不会看到或误删文档内容。

### 与纯文本流式 UI 的对比

| 特性 | 纯文本流式 | MyClaw 前端 |
|------|----------|------------|
| SSE 解析 | `EventSource`（仅 GET） | `fetch` + `ReadableStream` 手动解析（支持 POST + 自定义 body） |
| 内容模型 | 单一字符串 | 混合段列表（文本 + 工具卡片交替） |
| 编辑历史 | 删除后续所有消息 | `replaceUserTurnInUi` 无损替换，后续对话完整保留 |
| 取消机制 | `AbortController` 断开 | `AbortController` + `POST /chat/cancel` 双向协同（§12） |

### 面试展示要点

> "前端的挑战在于 Agent 的对话不只是流式文本——每一轮 ReAct 中有多个工具调用卡片和文本块交替出现。我维护了一个混合段列表，9 种 SSE 事件映射为对段列表的增删改操作，Vue 响应式系统自动处理 DOM 更新。消息编辑也不是简单的删除重发——我用 splitMessagesAtUserTurn 精准拆分 prefix/suffix，替换单轮后无缝拼回，后续对话完整保留。编辑弹窗自动剥离 `<file>` 注入块，用户只看到自然语言。"

---

## 19. Skill 自进化：流程型知识与生命周期维护

### 设计动机

长期记忆回答「用户是谁、偏好与事实是什么」；知识库检索回答「资料里写了什么」；二者都不擅长「这类任务应按什么步骤完成」。Skill（技能）承担**流程型知识**：触发条件、执行步骤、常见陷阱与验收方式。

若技能只能由人工导入或整文件编辑，会出现三类问题：

1. **经验难以回流**：复杂排障成功后，流程只留在单次对话记录中，下次同类任务仍从零探索。  
2. **文档与环境脱节**：接口、操作系统或路径变更后文档未同步，代理仍按过时步骤执行并产生错误结果。  
3. **技能库无序膨胀**：只增不改、不归档，检索噪声增大、触发条件相互冲突。

自进化的目标不是微调模型权重，而是形成闭环：**一次成功经验 → 可版本管理的技能资产 → 再次执行 → 再修订**；同时用归属边界与生命周期策略，避免「自主维护」误伤用户自有技能。

### 实现方案

整体分三层（设计思路对齐 Hermes 的技能生命周期维护器，落地在 MyClaw 的工作区/全局双目录技能底座上）：

```
对话代理
  ├─ 只读工具 Skill（加载全文）── 累计使用次数 ──► .usage.json
  └─ 技能管理工具 skill_manage（新建 / 局部修订 / 改写 / …）
         │ 名称与文首元数据校验、模糊局部匹配、路径护栏
         ▼
   skills/ 磁盘目录（工作区或全局）
         │
         ├─ 记录创建归属 / 累计修订次数
         ├─ 重新扫描目录 + 刷新只读工具描述（写入后必须执行）
         └─ 生命周期维护器（系统空闲且到达巡检间隔时运行）
                ├─ 活跃 → 闲置 → 已归档（仅「已纳入自主维护」的技能）
                ├─ 固定保护：跳过自动迁移；首次巡检只写入基准时间、不立即清理
                └─ 开启归并审查时：写出 ~/.helloclaw/logs/curator/ 下的审计报告
```

**1. 写入工具层：技能管理工具（`tools/builtin/skill_manage_tool.py`）**

| 动作 | 语义 |
|------|------|
| 新建（create） | 创建技能目录与 `SKILL.md`；对话前台默认**不**标记为自主维护资产（视为用户资产） |
| 局部修订（patch） | 查找替换；先精确匹配，再按空白归一化做模糊匹配；优先用于局部纠错 |
| 改写 / 写配套文件 / 删配套文件 | 整篇重写，或仅允许在 scripts、references、examples、assets、templates 下操作 |
| 查看（view） | 先读后写的证据链；累计查看次数 |
| 删除（delete） | **归档**到 `.archive/`，而非直接删除目录；已固定保护则拒绝 |

写入成功后回调 `MyClawAgent.refresh_skill_tool()`，避免「磁盘已更新，但工具列表描述仍停留在旧版本」。

**2. 使用量与归属遥测（`skills/usage.py`）**

- 路径：`.myclaw/skills/.usage.json` 与 `~/.helloclaw/skills/.usage.json` 分别管理对应目录  
- 字段：加载次数、查看次数、修订次数、创建归属、是否固定保护、生命周期状态、时间戳  
- 文件锁与原子写入；计数失败不中断主工具执行路径  
- 归属字段取值为 `agent` 时，表示**用户已将该技能纳入自主维护范围**（显式加入），并非严格意义上的「作者证明」；用户可通过接口「纳入自主维护」完成移交

**3. 生命周期维护器（`skills/curator.py`、`curator_backup.py`）**

- 配置：`config.json` 的 `curator` 段（默认约 7 天巡检一次、闲置/归档阈值约 30/90 天、归并审查默认关闭）  
- 调度：`main.py` 每小时尝试一次；真正执行还需满足：已启用、未暂停、系统空闲、达到巡检间隔；聊天活动会刷新「最近活跃」时间  
- 确定性状态迁移：仅处理报告中的自主维护技能；从未使用的技能有宽限下限；重新活动可将闲置状态拉回活跃  
- 备份：状态变更前生成压缩快照，保留最近若干份  
- 归并审查：开启时写出候选清单审计报告（完整的大语言模型归并可在此基础上扩展）；后台写入路径通过写来源标记与「吸收目标」参数做缺省拒绝（参数不全则拒绝执行）

**4. 运维面**

提供使用量查询、固定保护开关、纳入自主维护、归档与恢复、维护器状态/立即运行/暂停/恢复等 HTTP 接口；前端技能页展示使用量、固定保护与自主维护标记、归档恢复入口及维护器状态条。

### 代码引用

| 模块 | 路径 |
|------|------|
| 遥测 | `backend/src/skills/usage.py` |
| 写入原语 | `backend/src/skills/loader.py`（新建、局部修订、归档、恢复） |
| 生命周期维护器 | `backend/src/skills/curator.py`、`curator_backup.py` |
| 写来源 | `backend/src/skills/provenance.py` |
| 代理工具 | `backend/src/tools/builtin/skill_manage_tool.py`、`skill_tool.py` |
| 接口 | `backend/src/api/skills.py` |
| 调度 | `backend/src/main.py`（维护器循环） |
| 引导文案 | `workspace/templates/workspace/AGENTS.md` 第 7.4 节 |

### 对比分析

| 方案 | 优点 | 问题 |
|------|------|------|
| 仅人工导入与编辑技能 | 可控性强 | 成功经验无法回流；代理直接写磁盘缺少校验与工具描述刷新 |
| 用通用文件写入工具改 skills 目录 | 无需新工具 | 缺少文首元数据校验、无使用量与归属记录、易发生路径穿越 |
| 直接永久删除过期技能 | 实现简单 | 不可恢复；易误删关键流程 |
| **自进化三层结构** | 可沉淀、可局部修订、可归档恢复，用户资产与自主维护资产分离 | 需维护附属遥测文件与后台调度；归并审查须谨慎开启 |

相对 Hermes：MyClaw 采用工作区与全局各一份附属遥测文件；对话中新建技能默认归用户资产；本期不移植「每轮对话结束后的后台审查」子流程；纳入自主维护主要依赖用户主动移交，再交由生命周期维护器处理。

### 面试展示要点

> 「Skill 是流程型知识，与长期记忆、知识库检索的分工不同。我们让代理用技能管理工具把一次做对的过程写成 `SKILL.md`，发现偏差再做局部修订；附属遥测文件记录使用量与归属；生命周期维护器只处理已纳入自主维护的技能，过期归档且可恢复，固定保护用于关键流程。这样能力可复现、可运维，又避免自主维护误伤用户自有技能。」

---

## 20. 面试展示策略

### 推荐展示顺序（15 分钟 talk）

| 时间 | 内容 | 目的 |
|------|------|------|
| 0-2 min | 项目定位 + 架构全景图（三组目录解耦 + 画一张中文示意图） | 建立整体印象 |
| 2-8 min | **三道硬关**（详见[第 17 章](#17-最难的部分三道硬关与攻克方案)）：<br/>① 流式工具并发安全 — 状态机 + 三层保障<br/>② 14 点运行时重绑 — setattr 全量切换<br/>③ 上下文窗口经济学 — 三道防线 + 条件注入 | 展示"解决真正难的问题"的能力 |
| 8-10 min | **三通道回忆** + 可选一句 **Skill 自进化**（流程型知识 vs 长期记忆） | 展示产品思维与事实/流程分工 |
| 10-12 min | **Agent 循环可中断 + 双向协同取消** | 展示对可靠性边界的理解 |
| 12-13 min | **意图识别 + 用户画像**（三模式路由、Plan 两阶段、对话驱动画像） | 展示产品设计 + 多系统联动 |
| 13-14 min | 踩过的坑 + 如果重新来过会怎么做不同 | 展示反思能力 |
| 14-15 min | 总结一句话 | 留下核心印象 |

> **补充**：如果面试官对全栈能力感兴趣，可以在 12-13 min 或 13-14 min 顺带提一下[前端 SSE 流式增量渲染与无损编辑](#18-前端-sse-流式增量渲染与无损编辑)（混合段列表模型 + 消息无损编辑），展示对前端也有工程化思考。可作为补充点，主推点仍是三道硬关 + 意图识别 + 用户画像。

### 短版（5 分钟）

如果时间紧，只讲**三道硬关**（第 17 章）+ 挑意图识别、用户画像或三通道回忆其一。这三道硬关是整个项目中真正"需要动脑子才能解决"的问题；意图识别、用户画像和跨会话原文召回体现了"如何设计一个让用户觉得好用的 Agent"的产品思维。

> "这个项目花了大量时间在三道硬关上：流式工具调用的并发安全——LLM 输出不可预测但必须保证没有数据竞争；12 个引用点的运行时工作区切换——不销毁不重建、毫秒级重定向，v2 把会话和任务全局化后引用点从 14 减到 12、彻底解决了切工作区丢历史的痛点；上下文窗口的精细化管控——这不是二进制对错问题，每个阈值都是经验调出来的有损压缩。"

### 核心原则

> **重点讲"为什么这样设计"，而不是"做了什么"。**

每个亮点都应该回答三个问题：
1. **遇到什么问题？**（动机）
2. **为什么选这个方案？**（对比其他方案的取舍）
3. **有什么不足？**（展示反思能力）

### 一句话总结

> 这个项目最大的亮点不是"做了什么功能"，而是"**在功能背后的工程决策**"——流式提前执行、只读工具并行、三级路由、错误分类重试、记忆自动注入与跨会话原文按需召回、Skill 流程型知识自进化与生命周期维护、双向协同取消、破坏性截断的取舍，每一个都体现了对"Agent 如何在生产环境可靠运行"的深度思考。

---

*本文档基于 MyClaw 后端代码审查整理，代码引用以实际源码为准。*
