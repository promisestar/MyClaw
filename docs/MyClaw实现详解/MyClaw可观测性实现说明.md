# MyClaw 可观测性实现说明

> 本文档整理 MyClaw 当前**已具备**的可观测性能力（上下文窗口用量、工具调用日志、LLM token / 缓存用量、**轨迹 Span 树**、健康检查、链路关联），
> 并在末章给出**后续接入 LangFuse 等开源/托管可观测性后端的具体可实施计划**。
>
> 变更基线：
> - P0「补齐 token / 缓存用量埋点」（2026-09-07）
> - P1「本地轨迹 Span 树」（会话 → 轮次 → 模型调用 → 工具调用，事后 join 两类 JSONL）

## 目录

1. [概述](#1-概述)
2. [可观测性全景](#2-可观测性全景)
3. [能力一：上下文窗口用量](#3-能力一上下文窗口用量)
4. [能力二：工具调用日志](#4-能力二工具调用日志)
5. [能力三：LLM 用量（token / 缓存）](#5-能力三llm-用量token--缓存)
6. [能力四：轨迹 Span 树](#6-能力四轨迹-span-树)
7. [能力五：健康检查](#7-能力五健康检查)
8. [链路关联与埋点规范](#8-链路关联与埋点规范)
9. [现有盲区与局限](#9-现有盲区与局限)
10. [后续拓展：接入 LangFuse 等开源可观测性](#10-后续拓展接入-langfuse-等开源可观测性)
11. [附：涉及文件清单](#11-附涉及文件清单)

---

## 1. 概述

MyClaw 是一个本地优先的个人 Agent 系统。其可观测性设计遵循三条原则：

1. **零外部依赖、本地落盘**：所有遥测先写成 JSONL 或内存计数，不阻塞主流程、不依赖外网。
2. **贯穿请求链路的 trace_id**：用 `contextvars` 把一个请求/trace 的 `trace_id`、`session_id`、`agent_name` 贯穿到每次工具调用与每次 LLM 调用；主循环额外写入 **`iteration`（1-based）**，把「这一轮 LLM」与「这一轮触发的工具」精确挂在一起。
3. **遥测绝不反噬业务**：所有埋点都在 `try/except` 中静默失败（`log_response_safe`、Logger 内部吞异常）。

在 P0 之前，MyClaw 的流式主链路 **token 消耗完全不可见**（厂商流式响应默认不返回 `usage`）；P0 之后补齐了 `stream_options.include_usage`，并新增了独立的 **LLM 用量日志** 与前端 **用量统计** 页面。

在 P1 之前，工具日志与用量日志虽共享 `trace_id`，但仍是**两条扁平流水线**：无法一眼看出「某次用户提问 → 第几轮模型调用 → 触发了哪些工具」的因果关系。P1 在**不新增独立 span 存储**的前提下，于读取时把两类 JSONL **事后 join** 成四级 Span 树，并提供 `/api/trace/*` 与前端 **轨迹（TraceView）** 页面。

> 注意：hello_agents 自带的 `TraceLogger`（HTML 输出）与本系统**完全独立**。`MyClawAgent` 默认 `trace_enabled=False`，本文档所述「轨迹」一律指基于 tool / usage JSONL 的本地 Span 树。

---

## 2. 可观测性全景

```
┌────────────────────────────────────────────────────────────────────────────┐
│                            MyClaw 可观测性                                  │
├──────────────────┬──────────────────┬──────────────────┬───────────────────┤
│  运行状态(Health) │  上下文窗口用量   │   扁平日志链路     │   轨迹 Span 树     │
│  ──────────────  │  ──────────────  │  ──────────────  │  ───────────────  │
│  /health/detailed│  get_context_usage│  tool_logger     │  trace_tree 聚合   │
│  LLM/Qdrant/     │  内存计数 /       │  llm_usage_logger│  /api/trace/*      │
│  磁盘/内存       │  会话文件估算     │                  │  四级 + 瀑布条     │
│                  │                  │                  │                   │
│  前端: HealthView│  前端: ChatView   │  ToolLogs/Usage  │  前端: TraceView   │
│                  │  圆环            │                  │                   │
│  共同关联键: trace_id / session_id / agent_name / iteration（主循环）        │
└──────────────────┴──────────────────┴──────────────────┴───────────────────┘
```

| 维度 | 观察对象 | 后端实现 | 前端呈现 | 存储 |
|------|----------|----------|----------|------|
| 上下文窗口用量 | 每轮 system + history 占 context_window 百分比 | `agent.get_context_usage()` | ChatView 输入框右下角圆环 + tooltip | 内存 / 会话文件估算 |
| 工具调用 | 每次工具执行（名称、参数、结果、耗时、状态、重试、**iteration**） | `ToolCallLogger.log()` | ToolLogsView 列表 + JSON 弹窗 | `{log_dir}/YYYY-MM-DD.jsonl` |
| LLM 用量 | 每次 LLM 调用的 token / 缓存命中 / 调用点 / **request·response 元数据** | `LLMUsageLogger.log()` | UsageView 面板 + ChatView 本轮 chip | `{log_dir}/llm-usage-YYYY-MM-DD.jsonl` |
| **轨迹 Span 树** | 会话 → 轮次 → 模型调用 → 工具；旁路调用单独挂载 | `trace_tree` 事后 join + `/api/trace/*` | **TraceView**（左侧轮次列表 + 右侧瀑布条 / span 卡） | 无独立存储，读时聚合上述两类 JSONL |
| 系统健康 | LLM / 向量库 / 磁盘 / 内存 | `/api/health/detailed` | HealthView | 实时探测 |
| 上下文压缩 | 何时触发压缩、被压缩消息数 | `ContextManager` 日志/print | —（后端日志）；在轨迹页以 `side_spans` 可见 | 运行日志 + 用量 JSONL |

> **存储位置**：`TOOL_LOG_DIR` 环境变量可覆盖，默认 `~/.helloclaw/tool_logs/`。工具日志与 LLM 用量日志共用同一目录、不同文件名前缀，天然可按日期 + `trace_id` 关联同一轮对话。轨迹页不另写文件，只做读时聚合。

---

## 3. 能力一：上下文窗口用量

### 3.1 作用

让用户与开发者随时知道 **当前对话占用了多大比例的模型上下文窗口**，由 system 提示词 + 会话历史组成，用于判断是否需要新建会话或触发压缩。

### 3.2 后端实现

`backend/src/agent/myclaw_agent.py` 的 `get_context_usage(session_id)`：

- 若 `session_id` 与当前内存会话一致 → 用 `ContextManager.history_token_count`（增量计数，`O(1)`）读取历史 token 数。
- 否则读会话文件 `_estimate_session_file_tokens()` 估算。
- `system_tokens` 由 `_count_system_prompt_tokens()` 对当前冻结的 system 提示词计数。
- `used_tokens = system_tokens + history_tokens`；`used_percent = min(100, used/context_window*100)`。

返回字段：`session_id, system_tokens, history_tokens, used_tokens, context_window, used_percent`。

### 3.3 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/session/{session_id}/context-usage` | 查询某会话上下文用量 |

### 3.4 前端呈现（ChatView）

- 输入框右下角一个 **圆形进度环**（34px），按 `used_percent` 分段变色：`<70%` 绿 / `<90%` 橙 / `≥90%` 红。
- 悬停 tooltip 显示精确占比与 `used / context_window tokens`。
- 来源：进入/切换会话时拉取一次，对话结束由 SSE `done` 事件里的 `context_usage` 增量更新。

---

## 4. 能力二：工具调用日志

### 4.1 作用

审计 Agent 每轮调用了哪些工具、传参、返回、耗时、是否重试。是排查「Agent 为什么这么做/为何失败」的第一手记录；同时也是轨迹 Span 树中 **工具 span** 的唯一数据源。

### 4.2 后端实现

`backend/src/logging/tool_logger.py`：

- **`ToolCallLogger`（类方法，JSONL 单文件按天切分）**：`log()` 记录 `timestamp / trace_id / session_id / tool_name / tool_call_id / args / result / result_len / status / duration_ms`，附加字段 `agent_name / error_type / retry_attempt / retry_count`，以及 **`iteration`（1-based，与主循环 LLM 轮次对齐）**。
- **trace 上下文**：`set_trace_id()` / `get_trace_id()`（`contextvars`，8 位 hex）。API 层（`chat.py`）在流式请求开始 `set_trace_id(generate_trace_id())`。
- **参数与结果截断**：`_sanitize_args` 截断 >500 字符的参数；`result` 截断到 2000 字符，避免日志爆炸。
- **线程安全**：写入用 `threading.Lock`。
- 调用点：`retry_executor.py` 的 `execute_sync` / `execute_async`（把上层传入的 `iteration` 原样写入日志）。

### 4.3 重试语义

工具失败重试会在日志中产生**多条**记录：中间尝试 `status="retry"`（带 `retry_attempt=N`），最终结果 `status="done"/"error"`（带累计 `retry_count`）。结合 `agent_name` 字段可区分主代理 vs 子代理。

在轨迹页聚合时，同一 `tool_call_id` 的多条记录会被 **合并为单个工具 span**：最终记录为主 span，中间尝试折叠进 `attempts[]`（见 §6.4）。

### 4.4 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/tool-logs/list` | 工具日志文件列表（仅 `YYYY-MM-DD.jsonl`，不含 `llm-usage-*`） |
| `GET` | `/api/tool-logs/{date_str}` | 读取某日全部条目（`date_str` 须为 `YYYY-MM-DD`） |
| `DELETE` | `/api/tool-logs/{date_str}` | 删除某日文件 |

路径安全：`date_str` 正则 + `Path.resolve()` 前缀校验，防路径穿越。

### 4.5 前端（ToolLogsView）

文件列表卡片 → 点「打开」弹 Modal（`pre` 块 + 格式化 JSON），展示序号 / 状态图标 / 工具名 / 耗时；支持删除（`Popconfirm` 二次确认）。

> 需要看「某次提问里模型与工具的因果关系」时，优先使用 **轨迹页（§6）**，而不是在 ToolLogs 里手工对照时间戳。

---

## 5. 能力三：LLM 用量（token / 缓存）

### 5.1 背景与问题

此前 `enhanced_llm.py` 的流式请求 **未传 `stream_options`**，导致大多数厂商（含实测 MiniMax-M3）在流式响应中 **不返回 `usage`**，token 消耗与成本 100% 盲区。P0 修复了这一点。

### 5.2 关键修复：流式 usage 采集

`backend/src/agent/enhanced_llm.py`：

1. **请求参数**：`request_params["stream_options"] = {"include_usage": True}`。
2. **必须在 `if not chunk.choices: continue` 之前读取 usage**——因为 usage 只出现在流式**最后一个 chunk**，此时 `choices` 是空数组，若后读会被 `continue` 吞掉：

```python
chunk_usage = getattr(chunk, "usage", None)
if chunk_usage is not None:
    normalized = normalize_usage(chunk_usage)
    if normalized:
        result.usage = normalized
        result.has_usage = True
```

3. **降级保护**：不支持 `stream_options` 的厂商/网关会抛错。`_is_stream_options_unsupported()` 按「错误文案含 stream_options/include_usage」或 `status_code==400`（含链式 `__cause__`，因 hello_agents 适配器会包一层 `HelloAgentsException`）判断，命中则**去掉该参数重试一次并永久关闭**（`self._stream_usage_enabled=False`），不破坏整条链路。可用环境变量 `LLM_STREAM_INCLUDE_USAGE=0` 显式关闭。

### 5.3 用量归一化（`normalize_usage`）

兼容 OpenAI 与 MiniMax/智谱 的差异：

| 厂商形态 | 缓存命中所处位置 | 兼容 |
|----------|------------------|------|
| OpenAI 规范 | `prompt_tokens_details.cached_tokens` | ✅ 递归取 |
| MiniMax 等 | 顶层 `cached_tokens` | ✅ 顶层取 |
| 思考型模型 | `reasoning_tokens` / `completion_tokens_details.reasoning_tokens` | ✅ 递归取 |

归一化为固定五元组：`prompt_tokens / completion_tokens / total_tokens / cached_tokens / reasoning_tokens`。拿不到 usage 时返回 `None`（计入 `calls` 但不计 `calls_with_usage`，驱动「用量覆盖率」指标）。

### 5.4 记录器（`LLMUsageLogger`）

`backend/src/logging/llm_usage_logger.py`：

- 写 `{log_dir}/llm-usage-YYYY-MM-DD.jsonl`，独立文件名前缀，**复用** tool_logger 的 `trace_id` contextvar，另新增 `session_id` / `agent_name` contextvar。
- `log()` 记录基础字段：`timestamp / trace_id / session_id / agent_name / call_site / model / stream / iteration / prompt_tokens / completion_tokens / total_tokens / cached_tokens / reasoning_tokens / duration_ms / status`。
- **为轨迹 Span 树新增的附加字段**（主循环写入，旁路调用通常没有）：
  - `request`：`{system_count, message_count, tool_count}`——请求侧结构摘要
  - `response`：`{content_preview, content_len, tool_call_count, finish_reason, reasoning_tokens}`——响应侧摘要
  - `prompt_preview`：本轮用户提问前 120 字符，供轨迹页左侧轮次列表作标题
- 旁路安全封装 `log_response_safe()`：绝不抛异常。
- 聚合方法：`daily_summary(date)`、`range_summary(days)`（含按天趋势 `by_day`）、`recent()`、`list_files()`。聚合含 **`cache_hit_rate = cached_tokens / prompt_tokens`** 与 **`usage_coverage = calls_with_usage / calls`**，并按模型 / 按调用点分桶。

### 5.5 埋点覆盖（call_site 全景）

| 调用点 | call_site | 覆盖方式 | 在 Span 树中的位置 |
|--------|-----------|----------|-------------------|
| 主代理流式工具循环（每轮） | `main_loop` | `EnhancedHelloAgentsLLM` 返回后逐轮 `LLMUsageLogger.log` + 累计 `turn_usage`；带 `iteration` / `request` / `response` / `prompt_preview` | **`model_spans`**（主循环） |
| 主代理同步 `run()` 循环 | `main_loop` | 非流式响应直接取 `response.usage` | **`model_spans`** |
| 纯对话流式（无工具） | `chat_no_tools` | 复用 hello_agents `astream_invoke` + `stream_options`，读 `llm.last_call_stats.usage` | **`side_spans`** |
| 子代理输出摘要 | `subagent_summary` | `log_response_safe` | **`side_spans`** |
| 上下文压缩（消息压缩 + dict 摘要） | `context_compress` | `log_response_safe` | **`side_spans`** |
| 用户画像聚合 | `profile_aggregate` | `log_response_safe` | **`side_spans`** |
| RAG 生成/改写 | *未接* | 见 §9 盲区 | — |

### 5.6 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/usage/summary?days=N` | 最近 N 天汇总（按天趋势 + 按模型 + 按调用点） |
| `GET` | `/api/usage/day/{date}` | 单日汇总 |
| `GET` | `/api/usage/logs/{date}` | 读取某日用量原始 JSONL 条目（与 `/tool-logs/{date}` 对称） |
| `GET` | `/api/usage/recent?date=&limit=` | 最近原始记录（排障） |
| `GET` | `/api/usage/files` | 文件列表（仅 `llm-usage-*.jsonl`） |
| `DELETE` | `/api/usage/day/{date}` | 删除某日文件 |

> **与工具日志分流**：两类日志同目录、不同文件名前缀。`/api/tool-logs/list` 只返回 `YYYY-MM-DD.jsonl`；用量文件请用 `/api/usage/files` 与 `/api/usage/logs/{date}`。若误请求 `/api/tool-logs/llm-usage-YYYY-MM-DD`，后端会返回 400 并提示正确路径。

### 5.7 前端

**UsageView（用量统计页，侧边栏「用量统计」）**：
- 顶部 4 指标卡：总 tokens、**缓存命中率**、平均延迟、**用量覆盖率**。
- 用量构成条（Prompt / Completion / 缓存命中 / 推理）。
- 三张表：按模型 / 按调用点 / 按天趋势（含占比条）。
- 支持时间范围切换（今天 / 7 / 14 / 30 天）与清空今日记录。

**ChatView（对话页）**：输入框下方一行「本轮 X tokens · 缓存命中 Y% · N 次调用」，hover 显示细分。数据由 SSE `done` 事件的 `usage` / `llm_calls` 推送，新一轮开始时清零。

> **量化价值**：`cached_tokens` 被采集后，「system 提示词冻结」设计首次可被 `cache_hit_rate` **量化证伪**——若命中率高，说明冻结确实避免了重复计费；若始终为 0，则需排查前缀缓存是否生效。

---

## 6. 能力四：轨迹 Span 树

### 6.1 作用与设计取舍

轨迹页回答的是：**一次用户提问里，Agent 究竟走了怎样的「模型 → 工具 → 再模型」路径？**

与 ToolLogs / Usage 的扁平视角不同，Span 树提供：

| 维度 | ToolLogsView + UsageView | TraceView（Span 树） |
|------|--------------------------|----------------------|
| 数据视角 | 扁平列表 / 按模型聚合统计 | **层级树**：会话 → 轮次 → 模型调用 → 工具 |
| LLM ↔ 工具因果 | 需人工对照时间戳 | **`iteration` 精确挂载** + 时间窗兜底 |
| 重试 | 多条原始记录 | **合并为单 span** + `attempts[]` |
| 请求/响应上下文 | 无 | REQUEST / RESPONSE 元数据卡片 |
| 旁路调用 | 混在 usage 统计中 | 单独 `side_spans`，不计入主循环 `model_calls` |
| 归属质量 | 无 | `attribution` 字段 + UI 警告 |
| 时间可视化 | 无 | 每轮 Gantt 瀑布条 |

**关键设计**：不引入 OpenTelemetry / LangFuse，也**不另写 span 文件**。写入侧仍只写两类 JSONL；读取侧由 `trace_tree.py` 做事后 join。写入侧为精确聚合补了三个字段：工具与用量日志的 **`iteration`**，以及用量日志的 **`request` / `response` / `prompt_preview`**。

### 6.2 四级结构

```
会话 (session_id)
  └── 轮次 (trace_id，一次用户请求 / 一次流式 chat)
        ├── model_spans[]          # call_site ∈ {"main_loop"} 的模型调用
        │     └── tools[]          # 归属到该轮 iteration 的工具（重试已合并）
        ├── side_spans[]           # 旁路：context_compress / subagent_summary /
        │                          #        profile_aggregate / chat_no_tools 等
        └── unassigned_tools[]     # 无法归属到任何模型 span 的工具
```

常量（`trace_tree.py`）：

| 常量 | 值 | 含义 |
|------|----|------|
| `NO_TRACE` | `"__no_trace__"` | `trace_id` 为空时的分组键（如 `/chat/send/sync` 未设 trace） |
| `MAIN_LOOP_SITES` | `{"main_loop"}` | 主循环 vs 旁路的分界 |
| `DEFAULT_PREVIEW_LEN` | `120` | `prompt_preview` 截断长度 |

### 6.3 为什么必须有 `iteration`

流式主链路里，工具可能在 `TOOL_CALL_START` / `DELTA` / `FINISH` 三个早执行点就开始跑，而 **LLM 用量记录写在流式结束之后**。因此工具条目的 `timestamp` 经常**早于**其所属模型调用的 `timestamp`——不能简单按时间戳把工具塞进「最近一条 LLM」。

解决办法：主循环每轮 `current_iteration += 1`，同时写入：

1. `RetryExecutor.execute_*(..., iteration=step)` → `ToolCallLogger`
2. `_log_llm_usage(..., call_site="main_loop", iteration=current_iteration, ...)` → `LLMUsageLogger`

聚合时优先用 `tool.iteration == model_span.seq` 做精确归属。

### 6.4 聚合算法（`trace_tree.py`）

**入口函数**：

| 函数 | 用途 |
|------|------|
| `list_log_dates()` | 合并两类日志的可用日期 |
| `build_day_summaries(date, session_id?, prompt_resolver?)` | 某日全部轮次摘要（列表接口用，不含 span 明细） |
| `build_turn_tree(trace_id, date, prompt_resolver?)` | 单个轮次完整树；当天找不到时会**补读前一日**用量/工具日志（跨零点兜底） |

**工具重试合并（`_merge_tool_attempts`）**：

- 同一 `tool_call_id` 多条记录 → 取最后一条非 `retry` 记录作为主 span
- 中间尝试进入 `attempts[]`：`{status, duration_ms, retry_attempt, error_type, timestamp}`
- `start_ts = timestamp - duration_ms`（由结束时刻反推起点）

**工具归属（`_build_turn`）**：

```
对每个 merged_tool:
  1. 若 tool.iteration 是 int → 找 main_spans 中 seq == iteration 的 span  → attribution 贡献 "exact"
  2. 否则 → _pick_span_by_time：在「起点 ≤ 工具结束时刻」的 LLM span 中选起点最大者
     → attribution 贡献 "time_window"（兼容旧日志缺 iteration）
  3. 仍无 → unassigned_tools → attribution 贡献 "unassigned"
```

**`attribution` 枚举**（整轮汇总）：

| 值 | 含义 |
|----|------|
| `exact` | 全部工具通过 `iteration` 精确归属 |
| `time_window` | 旧日志缺 iteration，全部靠时间窗推断 |
| `unassigned` | 存在无法归属的工具 |
| `mixed` | 精确 + 时间窗，或精确 + 未归属 等混合情况 |

**轮次摘要字段**（`_SUMMARY_KEYS`，列表接口返回）：

`trace_id / session_id / index / prompt_preview / start_ts / end_ts / duration_ms / model_calls / tool_calls / usage / status / agents / attribution`

详情接口在摘要之上额外返回 `model_spans` / `side_spans` / `unassigned_tools`。

**模型 span 主要字段**：

`seq, iteration, call_site, model, agent_name, stream, status, error, duration_ms, start_ts, end_ts, usage, request, response, tools[]`

- `seq`：优先取日志里的 `iteration`；缺失则按时间顺序编为 1, 2, 3…
- `start_ts = timestamp - duration_ms`

**旧日志 `prompt_preview` 兜底**：API 层注入 `prompt_resolver`，从 `{workspace}/sessions/{session_id}.json` 的 `history` 取该会话内第 N 条 user 消息（兼容多模态 content 拍平）。`main.py` 启动时通过 `trace.set_sessions_dir_provider(...)` 注入会话目录。

### 6.5 写入生命周期（与 Agent 主循环的对齐）

```
chat.py 流式入口
  set_trace_id(generate_trace_id())   # 8 位 hex
  set_session_id(session_id)          # agent_start 后再确认一次
       │
       ▼
EnhancedSimpleAgent 主循环（每轮 current_iteration += 1）
  ├── LLM 流式调用
  │     └── 流过程中可能早执行工具
  │           RetryExecutor → ToolCallLogger.log(..., iteration=step)
  └── 流结束后
        _log_llm_usage(call_site="main_loop", iteration=...,
                       request_meta, response_meta, prompt_preview)
       │
       ▼
旁路 LLM（压缩 / 子代理摘要 / 画像 / 无工具对话）
  log_response_safe / _log_llm_usage → 继承同一 trace_id，无 iteration
       │
       ▼
读时：trace_tree.build_* → /api/trace/* → TraceView
```

> **`/chat/send/sync` 当前不设置 `trace_id`**，相关日志会归入 `__no_trace__` 桶；轨迹详情接口可用 `trace_id=__no_trace__` 查看。

### 6.6 API

路由模块：`backend/src/api/trace.py`，前缀 `/trace`，在 `main.py` 以 `app.include_router(trace.router, prefix="/api")` 注册。所有读盘与聚合均经 `run_in_threadpool`，避免阻塞 asyncio 事件循环。

| 方法 | 路径 | 参数 | 响应 |
|------|------|------|------|
| `GET` | `/api/trace/dates` | — | `{dates: [{date_str, tool_entries, usage_entries, modified_at}], total}` |
| `GET` | `/api/trace/turns` | `date`（必填）, `session_id?` | `{date_str, sessions: [{session_id, turn_count, turns}], total_turns}`；`turns` 为摘要，不含 span 明细 |
| `GET` | `/api/trace/turns/{trace_id}` | `date`（必填） | 完整轮次树；找不到返回 404 |

### 6.7 前端（TraceView）

| 资源 | 路径 |
|------|------|
| 路由 | `/trace`（侧边栏「轨迹」，`ApartmentOutlined`） |
| API 封装 | `frontend/src/api/trace.ts` |
| 主页面 | `frontend/src/views/TraceView.vue` |
| Span 卡片 | `frontend/src/components/TraceSpanCard.vue` |
| 时间瀑布条 | `frontend/src/components/TraceWaterfall.vue` |
| 工具结果块 | `frontend/src/components/TraceResultBlock.vue` |
| 格式化工具 | `frontend/src/utils/traceFormat.ts` |

**交互流程**：

1. 进入页面 → 拉日期列表 → 默认选最新日期
2. 拉该日轮次摘要 → 默认选中最后一轮
3. 点左侧某轮 → 拉完整 span 树到右侧

**左侧**：按 `session_id` 分组；展示 `prompt_preview`、时间、模型/工具次数、状态 Tag。

**右侧**：每个 `model_span` 一张卡——顶部 token 明细（含缓存命中）、`TraceWaterfall` Gantt 条、横向链路 `REQUEST → RESPONSE → 工具…`；其下另有旁路调用卡与未归属工具卡。

**旧日志降级展示**：

- 缺 `prompt_preview` → 「旧日志未采集提问」或会话文件兜底
- 缺 `request` / `response` → placeholder 文案
- `attribution !== 'exact'` → 黄色 Tag + 常驻说明条

### 6.8 测试覆盖

`backend/tests/test_trace_tree.py`（向临时目录写 JSONL，设置 `TOOL_LOG_DIR`，不依赖 FastAPI / 真实 LLM）：

| 测试 | 验证点 |
|------|--------|
| `test_exact_attribution` | 带 `iteration` → 精确挂到对应 `model_span`；token 汇总；`attribution=="exact"` |
| `test_time_window_fallback` | 缺 `iteration` → 时间窗归属；`attribution=="time_window"` |
| `test_retry_dedup` | 同 `tool_call_id` 重试合并；`retry_count` + `attempts[]` |
| `test_unassigned_and_no_trace` | 早于所有 LLM 的工具 → `unassigned_tools`；旁路 → `side_spans`；空 trace → `NO_TRACE` |
| `test_prompt_resolver_and_summary` | 缺 `prompt_preview` 时 resolver 兜底；摘要不含 `model_spans`；会话内轮次 `index` |
| `test_entry_fields_survive` | `request` / `response` / `prompt_preview` 完整透传到 span |

运行示例：`./.venv/Scripts/python.exe -m tests.test_trace_tree`（Windows 开发环境）。

---

## 7. 能力五：健康检查

`backend/src/api/health.py` 的 `GET /api/health/detailed` 返回 LLM 连接（`max_tokens=1` 轻量 ping）、Qdrant 向量库、磁盘空间、进程内存（含跨平台 fallback）的实时状态。前端 HealthView 呈现。属基础设施层可观测性，与对话级 trace 相对独立。

---

## 8. 链路关联与埋点规范

所有遥测通过 **三个 `contextvars` + 主循环 `iteration`** 关联到同一条对话链路：

| ContextVar / 字段 | 语义 | 由谁设置 | 消费方 |
|-------------------|------|----------|--------|
| `trace_id` | 一次对话请求（8 位 hex） | `api/chat.py` 流式请求开始 | tool_logger、llm_usage_logger、trace_tree |
| `session_id` | 会话标识 | `api/chat.py` 在 `agent_start` | llm_usage_logger、trace_tree |
| `agent_name` | 主代理 / 子代理 | agent 写入 | tool_logger、llm_usage_logger、trace_tree |
| **`iteration`（日志字段）** | 主循环第几轮（1-based） | `enhanced_simple_agent` → RetryExecutor / `_log_llm_usage` | **trace_tree 精确归属** |

**埋点铁律**（新调用点接入时遵守）：

1. 取不到 usage 也要写一条 `status="no_usage"` 记录，暴露盲区。
2. 遥测路径必须静默（`try/except` 或 `log_response_safe`）。
3. 写入加锁、结果截断，防止日志膨胀。
4. 复用 `trace_id` 关联，不另起炉灶。
5. **主循环工具与 LLM 必须同写 `iteration`**，否则轨迹页只能退化为时间窗归属（`attribution=time_window`）。
6. 主循环用量日志尽量带上 `request` / `response` / `prompt_preview`，否则轨迹页只能降级展示。

---

## 9. 现有盲区与局限

1. **RAG 链路的若干处 `llm.invoke`**（`rag/pipeline.py`、`rag_tool.py`、evals judge）**尚未接用量埋点**——属于同一体系应覆盖但尚未覆盖的调用面；也不会出现在 Span 树中。
2. **工具日志仍无独立聚合图表**：成功率 / 耗时分布 / 失败原因等仍依赖 Usage 侧或轨迹页人工浏览（轨迹页解决的是单轮因果，不是全局聚合）。
3. **本地文件限制**：JSONL 无全文检索、无告警、无趋势告警；长文件读取会阻塞事件循环（已用 `run_in_threadpool` 缓解）。
4. **成本无单价**：只统计 token 数，无模型单价映射（成本需接入后端统一换算，当前配置模型 MiniMax-M3 无内置单价表）。
5. **同步聊天无 trace**：`/chat/send/sync` 不设 `trace_id`，归入 `__no_trace__`。
6. **部分工具可能无 ToolCallLogger 记录**：如 `skipped_dedup` / `skipped_limit` 仅留在内存 `iteration_tool_records`，轨迹树看不到。
7. **跨日长请求**：详情接口有「补读前一日」兜底，列表接口只读单日。
8. **与 hello_agents TraceLogger 并存但未默认启用**：HTML trace 与本地 Span 树两套体系，避免混淆。

> 以上盲区中，**「无跨调用点统一时序 / 有向 span 树」已在 P1 由本地轨迹页解决**。仍欠缺的「聚合告警 / 成本核算 / 托管检索」是 §10 接入 LangFuse 等后端的主要动机。

---

## 10. 后续拓展：接入 LangFuse 等开源可观测性

### 10.0 目标与结论先行

- **目标**：在现有「本地 JSONL 遥测 + 本地 Span 树」之上，补齐当前仍不具备的能力——**聚合查询 / 成本核算 / 可视化检索 / 异常告警**，并形成「线上 trace → 抽样成回归数据集」的单向流转。
- **判断**：技术可行，MVP 1–2 人天；**不建议一上来就自托管** LangFuse（6 容器 / 4C16G，ClickHouse 2C8G 硬门槛；且 ClickHouse 已收购 LangFuse，中立性下降）。先用 **托管 Cloud Hobby（免费 50k units/月）** 或 **进程内 Phoenix** 跑通，再按需自托管。
- **总策略**：**埋点标准化与用量补齐（P0）+ 本地 Span 树（P1）与后端解耦**——同一套 `trace_id` / `call_site` / usage 字段可平移接入 LangFuse / Phoenix / 任意 OTEL 后端，后端选型可后置。本地轨迹页已经证明了「会话 → 轮次 → 模型 → 工具」语义，接外部后端时重点是导出与成本，而不是重新发明层级模型。

### 10.1 前置依赖（已完成 / 必须完成）

| # | 依赖 | 状态 |
|---|------|------|
| 1 | 流式 `stream_options.include_usage` 补齐（否则 token 恒 0） | ✅ P0 完成 |
| 2 | `cached_tokens` 采集 | ✅ P0 完成 |
| 3 | trace_id / session_id 贯穿 | ✅ 已有 |
| 4 | **本地 Span 树（iteration 归属 + TraceView）** | ✅ P1 完成 |
| 5 | RAG 链路埋点补齐（§9.1） | ⏳ 建议先行（约 1 小时） |

### 10.2 三套接入选型

| 方案 | 改动 | 覆盖 | 代价 | 适用 |
|------|------|------|------|------|
| **drop-in 换客户端**：`from langfuse.openai import AsyncOpenAI` | 1 行 | 仅主链路 | 第三方包（hello-agents）内部 8 处 `invoke` 漏掉 | 最小验证 |
| **OTEL instrumentor**（推荐）：`OpenAIInstrumentor().instrument()` | 1 处初始化 | 全部 OpenAI SDK 调用 | 需把业务 `trace_id` 对齐到 LangFuse trace | 全链路 |
| **手动 `@observe` / SDK `trace()`** | 每函数/每个调用点 | 最精确 | 侵入最大 | 关键路径精雕 |

> **P0/P1 价值兑现**：所有 LLM / 工具调用点已收敛到两条 Logger，且语义字段（`trace_id`、`session_id`、`call_site`、`iteration`、`model`、usage）已对齐 LangFuse 的 trace / observation 模型——接 instrumentor 时**几乎零改造**，只需把 Logger 的 `trace_id` 用 `langfuse` 的 `get_trace_id()` 覆盖即可实现双向关联。本地 `trace_tree` 的四级层级也可作为对照：外部后端应至少能表达同样的「轮次 → 模型 → 工具」关系。

### 10.3 实施路线（分阶段，每阶段可独立验收）

#### 阶段 P2 —— 打通 Cloud 托管（约 1 天）

**范围**：把已有本地 span 语义导出到 Cloud；token / 缓存 / 成本面板可见；采样 + masking 后数据出境。

1. **依赖与初始化**
   - `pip install langfuse`（v4 SDK，底层已是 OTEL）。
   - 启动处初始化 `Langfuse({public_key, secret_key, host="https://cloud.langfuse.com", ...})`，配置从 `.env`（`LANGFUSE_*`）读取，未配置时静默降级为「不初始化」，保持本地可用。
2. **全局 instrumentation**
   - `from langfuse.openai import OpenAIInstrumentor; OpenAIInstrumentor().instrument()` 覆盖 MyClaw 自建 `AsyncOpenAI` 与 hello-agents 内部所有 `openai` 调用。
3. **根 span 挂上下文**：在主代理 `arun_stream_with_tools` 入口创建根 span，设 `session_id`、`user_id`、workspace / mode / 模型 标签；用现有 `trace_id`（8 位 hex）作关联键或改由 LangFuse 生成并回写 Logger contextvar。主循环的 `iteration` 建议写成 observation 属性，便于与本地 TraceView 对照。
4. **数据出境防护**：开启 masking + 采样（建议 0.2–0.3）；敏感工作区在初始化处直接排除。
5. **验收**：Cloud 面板出现完整 span 树、每次 LLM 调用的 token / `cached_tokens`、缓存命中率与成本估算正确；业务 `trace_id` ↔ LangFuse trace 可互相检索；与本地 TraceView 对同一轮的层级大致一致。

#### 阶段 P3 —— 成本核算与自托管评估（按需）

1. **成本**：MiniMax-M3 等不在 LangFuse 内置单价表 → 在后台配 custom model price，或在前端把 token 按单价换算展示。
2. **自托管**：若数据敏感 / 需长期留存，评估 Docker 部署 LangFuse（4C/16G/100G 起，ClickHouse 2C8G 硬门槛，低配会在 merge 时 OOM）；**或切换进程内 Phoenix**——SQLite 存储、OTEL 原生、`pip install` 一条命令即可起，P2 埋点可平移，适合本地个人场景。
3. **与 evals 的边界**：`backend/evals`（100 场景 L0/L1/L2）已有回归能力。**不重复建 Dataset**，只做「线上 trace → 抽样转成回归数据集」的单向流转。

#### 阶段 P4 —— 补全盲区与告警

1. RAG / evals judge 调用点补埋（§9.1）——用与 P2 相同的 instrumentor，天然覆盖。
2. 工具成功率 / 失败原因分布做成聚合视图（可在现有 ToolLogs、本地轨迹页之上，或 LangFuse dataset 上）。
3. 基于 span 树加**异常告警**：首 token 超时率、工具失败率、缓存命中率突降等阈值告警（LangFuse metrics / 简单 Webhook）。

### 10.4 必须提前处理的坑

| # | 坑 | 对策 |
|---|----|------|
| 1 | SSE 流式是 async generator，客户端断开会让 span 不闭合 | 必须显式 `end()`，且 `flush()` 不能阻塞 SSE |
| 2 | 数据出境：prompt、工具参数、本地文件路径全量上传 | masking + 采样（0.2–0.3），敏感工作区排除 |
| 3 | 厂商不支持 `stream_options` 或异常中断 | 已用降级保护（§5.2）；instrumentor 同样受影响，需吞异常 |
| 4 | hello-agents 适配器包一层异常，`status_code` 丢失 | `_is_stream_options_unsupported` 查 `__cause__/__context__`（已处理） |
| 5 | ClickHouse 2026-01 收购 LangFuse，开源中立性下降 | 评估 Phoenix / 自建 OTEL collector 作为备选 |
| 6 | 本地 Span 树与外部后端双写不一致 | 以 Logger 字段为唯一事实源；外部只做导出，避免两套埋点语义漂移 |

### 10.5 里程碑与工作量

| 阶段 | 内容 | 工作量 | 验收 |
|------|------|--------|------|
| 已完成 | P0 用量埋点 + P1 本地 Span 树 | — | TraceView 可浏览会话→轮次→模型→工具 |
| 前置 | RAG 链路补埋 | 0.5h | call_site 覆盖到 100% |
| P2 | Cloud 托管 + instrumentor + masking/采样 | 1 天 | 面板 span/token/缓存/成本可见 |
| P3 | 成本表 + 自托管或 Phoenix | 0.5–1 天 | 本地长期留存可用 |
| P4 | 盲区聚合视图 + 告警 | 按需 | 异常可发现 |

> **一句话落地顺序**：先补 RAG 埋点 → 接 Cloud Hobby 跑通（masking + 采样）→ 验证外部 span 与成本面板，并与本地 TraceView 对照 → 数据敏感或要长期留存再自托管或切 Phoenix。**埋点与后端解耦，后端随时可换；本地轨迹页始终可用。**

---

## 11. 附：涉及文件清单

### P0 新增（用量）
| 文件 | 说明 |
|------|------|
| `backend/src/logging/llm_usage_logger.py` | LLM 用量 JSONL 记录器（normalize/merge/聚合/log_response_safe；后扩展 request/response/prompt_preview） |
| `backend/src/api/usage.py` | `/api/usage/*` 路由 |
| `backend/tests/test_p0_usage.py` | P0 联调验证脚本 |
| `frontend/src/api/usage.ts` | 前端 usage API 封装 + 类型 |
| `frontend/src/views/UsageView.vue` | 用量统计页面 |

### P1 新增（轨迹 Span 树）
| 文件 | 说明 |
|------|------|
| `backend/src/logging/trace_tree.py` | 事后 join：日期列表 / 日摘要 / 单轮完整 span 树 |
| `backend/src/api/trace.py` | `/api/trace/*` 路由；会话目录注入与 prompt 兜底 |
| `backend/tests/test_trace_tree.py` | 精确归属 / 时间窗 / 重试合并 / 未归属 / prompt 兜底 |
| `frontend/src/api/trace.ts` | 轨迹 API 封装 + TypeScript 类型 |
| `frontend/src/views/TraceView.vue` | 轨迹主页面（左列表 + 右详情） |
| `frontend/src/components/TraceSpanCard.vue` | REQUEST / RESPONSE / 工具 span 卡片 |
| `frontend/src/components/TraceWaterfall.vue` | 相对时间 Gantt 瀑布条 |
| `frontend/src/components/TraceResultBlock.vue` | 工具结果 JSON 着色与截断修复 |
| `frontend/src/utils/traceFormat.ts` | 时间 / token / attribution 格式化与 JSON 词法分析 |

### P0 / P1 修改
| 文件 | 说明 |
|------|------|
| `backend/src/agent/enhanced_llm.py` | 流式 `stream_options` + 末块 usage 采集 + 降级保护 |
| `backend/src/agent/enhanced_simple_agent.py` | 主循环累计 usage；写入 `iteration` / request·response meta / prompt_preview |
| `backend/src/agent/retry_executor.py` | 工具日志写入 `iteration` |
| `backend/src/agent/subagent_orchestrator.py` | 子代理摘要埋点 |
| `backend/src/context/context_manager.py` | 上下文压缩埋点（两处） |
| `backend/src/agent/profile_aggregator.py` | 画像聚合埋点 |
| `backend/src/logging/tool_logger.py` | 工具日志增加 `iteration` 字段 |
| `backend/src/logging/llm_usage_logger.py` | 用量日志扩展轨迹所需附加字段 |
| `backend/src/logging/__init__.py` | 导出 Logger |
| `backend/src/api/chat.py` | SSE `done` 带 usage、`agent_start` 设 session / trace |
| `backend/src/main.py` | 注册 usage / **trace** 路由；注入 `set_sessions_dir_provider` |
| `frontend/src/api/chat.ts` | `usage` / `llm_calls` 字段透传 |
| `frontend/src/views/ChatView.vue` | 本轮 token chip |
| `frontend/src/router/index.ts` | `/usage`、**`/trace`** 路由 |
| `frontend/src/App.vue` | 「用量统计」「**轨迹**」侧边栏项 |

### 既有（本文档梳理范围）
| 文件 | 说明 |
|------|------|
| `backend/src/logging/tool_logger.py` | 工具日志引擎 |
| `backend/src/api/tool_logs.py` | `/api/tool-logs/*` |
| `backend/src/api/health.py` | `/api/health/detailed` |
| `backend/src/api/session.py` | `/{id}/context-usage` |
| `backend/src/agent/myclaw_agent.py` | `get_context_usage` |
| `frontend/src/views/ToolLogsView.vue` / `HealthView.vue` / `ChatView.vue` | 前端呈现 |
