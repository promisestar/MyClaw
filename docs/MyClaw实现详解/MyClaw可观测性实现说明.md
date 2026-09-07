# MyClaw 可观测性实现说明

> 本文档整理 MyClaw 当前**已具备**的可观测性能力（上下文窗口用量、工具调用日志、LLM token / 缓存用量、健康检查、链路关联），
> 并在末章给出**后续接入 LangFuse 等开源/托管可观测性后端的具体可实施计划**。
>
> 变更基线：P0「补齐 token / 缓存用量埋点」（2026-09-07）。

## 目录

1. [概述](#1-概述)
2. [可观测性全景](#2-可观测性全景)
3. [能力一：上下文窗口用量](#3-能力一上下文窗口用量)
4. [能力二：工具调用日志](#4-能力二工具调用日志)
5. [能力三：LLM 用量（token / 缓存）](#5-能力三llm-用量token--缓存)
6. [能力四：健康检查](#6-能力四健康检查)
7. [链路关联与埋点规范](#7-链路关联与埋点规范)
8. [现有盲区与局限](#8-现有盲区与局限)
9. [后续拓展：接入 LangFuse 等开源可观测性](#9-后续拓展接入-langfuse-等开源可观测性)
10. [附：涉及文件清单](#10-附涉及文件清单)

---

## 1. 概述

MyClaw 是一个本地优先的个人 Agent 系统。其可观测性设计遵循三条原则：

1. **零外部依赖、本地落盘**：所有遥测先写成 JSONL 或内存计数，不阻塞主流程、不依赖外网。
2. **贯穿请求链路的 trace_id**：用 `contextvars` 把一个请求/trace 的 `trace_id`、`session_id`、`agent_name` 贯穿到每次工具调用与每次 LLM 调用。
3. **遥测绝不反噬业务**：所有埋点都在 `try/except` 中静默失败（`log_response_safe`、Logger 内部吞异常）。

在 P0 之前，MyClaw 的流式主链路 **token 消耗完全不可见**（厂商流式响应默认不返回 `usage`）；P0 之后补齐了 `stream_options.include_usage`，并新增了独立的 **LLM 用量日志** 与前端 **用量统计** 页面。

---

## 2. 可观测性全景

```
┌──────────────────────────────────────────────────────────────────────┐
│                          MyClaw 可观测性                              │
├──────────────────────┬──────────────────────┬─────────────────────────┤
│   运行状态(Health)    │    上下文窗口用量      │       日志链路           │
│  ────────────────    │  ────────────────    │  ────────────────       │
│  /health/detailed    │  get_context_usage    │  tool_logger（工具）      │
│  LLM/Qdrant/磁盘/内存 │  内存计数 / 会话文件估算 │  llm_usage_logger(LLM)  │
│                      │                      │                         │
│  前端: HealthView    │  前端: ChatView 圆环  │  前端: ToolLogs / Usage  │
│                      │                      │                         │
│  共同关联键: trace_id / session_id / agent_name                        │
└──────────────────────┴──────────────────────┴─────────────────────────┘
```

| 维度 | 观察对象 | 后端实现 | 前端呈现 | 存储 |
|------|----------|----------|----------|------|
| 上下文窗口用量 | 每轮 system + history 占 context_window 百分比 | `agent.get_context_usage()` | ChatView 输入框右下角圆环 + tooltip | 内存 / 会话文件估算 |
| 工具调用 | 每次工具执行（名称、参数、结果、耗时、状态、重试） | `ToolCallLogger.log()` | ToolLogsView 列表 + JSON 弹窗 | `{log_dir}/YYYY-MM-DD.jsonl` |
| LLM 用量 | 每次 LLM 调用的 token / 缓存命中 / 调用点 | `LLMUsageLogger.log()` | UsageView 面板 + ChatView 本轮 chip | `{log_dir}/llm-usage-YYYY-MM-DD.jsonl` |
| 系统健康 | LLM / 向量库 / 磁盘 / 内存 | `/api/health/detailed` | HealthView | 实时探测 |
| 上下文压缩 | 何时触发压缩、被压缩消息数 | `ContextManager` 日志/print | —（后端日志） | 运行日志 |

> **存储位置**：`TOOL_LOG_DIR` 环境变量可覆盖，默认 `~/.helloclaw/tool_logs/`。工具日志与 LLM 用量日志共用同一目录、不同文件名前缀，天然可按日期关联同一 trace。

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

审计 Agent 每轮调用了哪些工具、传参、返回、耗时、是否重试。是排查「Agent 为什么这么做/为何失败」的第一手记录。

### 4.2 后端实现

`backend/src/logging/tool_logger.py`：

- **`ToolCallLogger`（类方法，JSONL 单文件按天切分）**：`log()` 记录 `timestamp / trace_id / session_id / tool_name / tool_call_id / args / result / result_len / status / duration_ms`，附加字段 `agent_name / error_type / retry_attempt / retry_count`。
- **trace 上下文**：`set_trace_id()` / `get_trace_id()`（`contextvars`，8 位 hex）。API 层（`chat.py`）在请求开始 `set_trace_id(generate_trace_id())`。
- **参数与结果截断**：`_sanitize_args` 截断 >500 字符的参数；`result` 截断到 2000 字符，避免日志爆炸。
- **线程安全**：写入用 `threading.Lock`。
- 调用点：`retry_executor.py`（重试路径），被 Agent 主循环内各工具执行包装调用。

### 4.3 重试语义

工具失败重试会在日志中产生**多条**记录：中间尝试 `status="retry"`（带 `retry_attempt=N`），最终结果 `status="done"/"error"`（带累计 `retry_count`）。结合 `agent_name` 字段可区分主代理 vs 子代理。

### 4.4 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/tool-logs/list` | 日志文件列表（日期、条数、大小、修改时间） |
| `GET` | `/api/tool-logs/{date_str}` | 读取某日全部条目 |
| `DELETE` | `/api/tool-logs/{date_str}` | 删除某日文件 |

路径安全：`date_str` 正则 + `Path.resolve()` 前缀校验，防路径穿越。

### 4.5 前端（ToolLogsView）

文件列表卡片 → 点「打开」弹 Modal（`pre` 块 + 格式化 JSON），展示序号 / 状态图标 / 工具名 / 耗时；支持删除（`Popconfirm` 二次确认）。

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
- `log()` 记录：`timestamp / trace_id / session_id / agent_name / call_site / model / stream / iteration / prompt_tokens / completion_tokens / total_tokens / cached_tokens / reasoning_tokens / duration_ms / status`。
- 旁路安全封装 `log_response_safe()`：绝不抛异常。
- 聚合方法：`daily_summary(date)`、`range_summary(days)`（含按天趋势 `by_day`）、`recent()`、`list_files()`。聚合含 **`cache_hit_rate = cached_tokens / prompt_tokens`** 与 **`usage_coverage = calls_with_usage / calls`**，并按模型 / 按调用点分桶。

### 5.5 埋点覆盖（call_site 全景）

| 调用点 | call_site | 覆盖方式 |
|--------|-----------|----------|
| 主代理流式工具循环（每轮） | `main_loop` | `EnhancedHelloAgentsLLM` 返回后逐轮 `LLMUsageLogger.log` + 累计 `turn_usage` |
| 主代理同步 `run()` 循环 | `main_loop` | 非流式响应直接取 `response.usage` |
| 纯对话流式（无工具） | `chat_no_tools` | 复用 hello_agents `astream_invoke` + `stream_options`，读 `llm.last_call_stats.usage` |
| 子代理输出摘要 | `subagent_summary` | `log_response_safe`（**此前盲区，P0 补齐**） |
| 上下文压缩（消息压缩 + dict 摘要） | `context_compress` | `log_response_safe`（**此前盲区，P0 补齐**） |
| 用户画像聚合 | `profile_aggregate` | `log_response_safe`（**此前盲区，P0 补齐**） |
| RAG 生成/改写 | *未接* | 见 §8 盲区 |

### 5.6 API

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/usage/summary?days=N` | 最近 N 天汇总（按天趋势 + 按模型 + 按调用点） |
| `GET` | `/api/usage/day/{date}` | 单日汇总 |
| `GET` | `/api/usage/recent?date=&limit=` | 最近原始记录（排障） |
| `GET` | `/api/usage/files` | 文件列表 |
| `DELETE` | `/api/usage/day/{date}` | 删除某日文件 |

### 5.7 前端

**UsageView（用量统计页，新侧边栏入口）**：
- 顶部 4 指标卡：总 tokens、**缓存命中率**、平均延迟、**用量覆盖率**。
- 用量构成条（Prompt / Completion / 缓存命中 / 推理）。
- 三张表：按模型 / 按调用点 / 按天趋势（含占比条）。
- 支持时间范围切换（今天 / 7 / 14 / 30 天）与清空今日记录。

**ChatView（对话页）**：输入框下方一行「本轮 X tokens · 缓存命中 Y% · N 次调用」，hover 显示细分。数据由 SSE `done` 事件的 `usage` / `llm_calls` 推送，新一轮开始时清零。

> **量化价值**：`cached_tokens` 被采集后，「system 提示词冻结」设计首次可被 `cache_hit_rate` **量化证伪**——若命中率高，说明冻结确实避免了重复计费；若始终为 0，则需排查前缀缓存是否生效。

---

## 6. 能力四：健康检查

`backend/src/api/health.py` 的 `GET /api/health/detailed` 返回 LLM 连接（`max_tokens=1` 轻量 ping）、Qdrant 向量库、磁盘空间、进程内存（含跨平台 fallback）的实时状态。前端 HealthView 呈现。属基础设施层可观测性，与对话级 trace 相对独立。

---

## 7. 链路关联与埋点规范

所有遥测通过 **三个 `contextvars`** 关联到同一条对话链路：

| ContextVar | 语义 | 由谁设置 | 消费方 |
|------------|------|----------|--------|
| `trace_id` | 一次对话请求（8 位 hex） | `api/chat.py` 请求开始 | tool_logger、llm_usage_logger |
| `session_id` | 会话标识 | `api/chat.py` 在 `agent_start` | llm_usage_logger |
| `agent_name` | 主代理 / 子代理 | agent 写入 | tool_logger、llm_usage_logger |

**埋点铁律**（新调用点接入时遵守）：
1. 取不到 usage 也要写一条 `status="no_usage"` 记录，暴露盲区。
2. 遥测路径必须静默（`try/except` 或 `log_response_safe`）。
3. 写入加锁、结果截断，防止日志膨胀。
4. 复用 `trace_id` 关联，不另起炉灶。

---

## 8. 现有盲区与局限

1. **RAG 链路的 3 处 `llm.invoke`**（`rag/pipeline.py:833/850/1237`、`rag_tool.py:761`、evals judge）**尚未接用量埋点**——属于同一体系应覆盖但 P0 未覆盖的调用面。
2. **无聚合视图**：工具日志只有「按文件浏览」，没有成功率 / 耗时 / 失败原因分布等聚合图表。
3. **无跨调用点的统一时序视图**：trace 只落 JSONL，缺少「一次对话从 LLM 调工具→再调 LLM」的**有向 span 树**。
4. **本地文件**：JSONL 无检索、无告警、无趋势告警；长文件读取会阻塞事件循环（已用 `run_in_threadpool` 缓解）。
5. **成本无单价**：只统计 token 数，无模型单价映射（成本需接入后端统一换算，当前配置模型 MiniMax-M3 无内置单价表）。

> 以上盲区恰好是 §9 接入 LangFuse 等后端的主要动机。

---

## 9. 后续拓展：接入 LangFuse 等开源可观测性

### 9.0 目标与结论先行

- **目标**：在现有「本地 JSONL 遥测」之上，补齐三个当前不具备的能力——**span 树 / 聚合查询 / 成本核算 / 可视化检索**，形成「线上 trace → 抽样成回归数据集」的单向流转。
- **判断**：技术可行，MVP 1–2 人天；**不建议一上来就自托管** LangFuse（6 容器 / 4C16G，ClickHouse 2C8G 硬门槛；且 ClickHouse 已收购 LangFuse，中立性下降）。先用 **托管 Cloud Hobby（免费 50k units/月）** 或 **进程内 Phoenix** 跑通，再按需自托管。
- **总策略**：**埋点标准化与用量补齐（P0，已完成）与后端解耦**——同一套埋点可平移接入 LangFuse / Phoenix / 任意 OTEL 后端，后端选型可后置。

### 9.1 前置依赖（已完成 / 必须完成）

| # | 依赖 | 状态 |
|---|------|------|
| 1 | 流式 `stream_options.include_usage` 补齐（否则 token 恒 0） | ✅ P0 完成 |
| 2 | `cached_tokens` 采集 | ✅ P0 完成 |
| 3 | trace_id / session_id 贯穿 | ✅ 已有 |
| 4 | RAG 链路埋点补齐（§8.1） | ⏳ 建议先行（1 小时内） |

### 9.2 三套接入选型

| 方案 | 改动 | 覆盖 | 代价 | 适用 |
|------|------|------|------|------|
| **drop-in 换客户端**：`from langfuse.openai import AsyncOpenAI` | 1 行 | 仅主链路 | 第三方包（hello-agents）内部 8 处 `invoke` 漏掉 | 最小验证 |
| **OTEL instrumentor**（推荐）：`OpenAIInstrumentor().instrument()` | 1 处初始化 | 全部 OpenAI SDK 调用 | 需把业务 `trace_id` 对齐到 LangFuse trace | 全链路 |
| **手动 `@observe` / SDK `trace()`** | 每函数/每个调用点 | 最精确 | 侵入最大 | 关键路径精雕 |

> **P0 价值兑现**：P0 把所有 LLM 调用点收敛到 **两条 Logger**（tool_logger / llm_usage_logger），且语义字段（trace_id、session_id、call_site、model、usage）已对齐 LangFuse 的 trace / observation 模型——接 instrumentor 时**几乎零改造**，只需把 Logger 的 `trace_id` 用 `langfuse` 的 `get_trace_id()` 覆盖即可实现双向关联。

### 9.3 实施路线（分阶段，每阶段可独立验收）

#### 阶段 P1 —— 打通 Cloud 托管（约 1 天）

**范围**：主链路 span 树 + token / 缓存 / 成本面板可见，采样 + masking 后数据出境。

1. **依赖与初始化**
   - `pip install langfuse`（v4 SDK，底层已是 OTEL）。
   - 启动处初始化 `Langfuse({public_key, secret_key, host="https://cloud.langfuse.com", ...})`，配置从 `.env`（`LANGFUSE_*`）读取，未配置时静默降级为「不初始化」，保持本地可用。
2. **全局 instrumentation**
   - `from langfuse.openai import OpenAIInstrumentor; OpenAIInstrumentor().instrument()` 覆盖 MyClaw 自建 `AsyncOpenAI` 与 hello-agents 内部所有 `openai` 调用。
3. **根 span 挂上下文**：在主代理 `arun_stream_with_tools` 入口创建根 span，设 `session_id`、`user_id`、workspace / mode / 模型 标签；用现有 `trace_id`（8 位 hex）作关联键或改由 LangFuse 生成并回写 Logger contextvar。
4. **数据出境防护**：开启 masking + 采样（建议 0.2–0.3）；敏感工作区在初始化处直接排除。
5. **验收**：Cloud 面板出现完整 span 树、每次 LLM 调用的 token / `cached_tokens`、缓存命中率与成本估算正确；业务 `trace_id` ↔ LangFuse trace 可互相检索。

#### 阶段 P2 —— 成本核算与自托管评估（按需）

1. **成本**：MiniMax-M3 等不在 LangFuse 内置单价表 → 在后台配 custom model price，或在前端把 token 按单价换算展示。
2. **自托管**：若数据敏感 / 需长期留存，评估 Docker 部署 LangFuse（4C/16G/100G 起，ClickHouse 2C8G 硬门槛，低配会在 merge 时 OOM）；**或切换进程内 Phoenix**——SQLite 存储、OTEL 原生、`pip install` 一条命令即可起，P1 埋点可平移，适合本地个人场景。
3. **与 evals 的边界**：`backend/evals`（100 场景 L0/L1/L2）已有回归能力。**不重复建 Dataset**，只做「线上 trace → 抽样转成回归数据集」的单向流转。

#### 阶段 P3 —— 补全盲区与告警

1. RAG / evals judge 调用点补埋（§8.1）——用与 P1 相同的 instrumentor，天然覆盖。
2. 工具成功率 / 失败原因分布做成聚合视图（可在现有 ToolLogs 或 LangFuse dataset 上）。
3. 基于 span 树加**异常告警**：首 token 超时率、工具失败率、缓存命中率突降等阈值告警（LangFuse metrics / 简单 Webhook）。

### 9.4 必须提前处理的坑

| # | 坑 | 对策 |
|---|----|------|
| 1 | SSE 流式是 async generator，客户端断开会让 span 不闭合 | 必须显式 `end()`，且 `flush()` 不能阻塞 SSE |
| 2 | 数据出境：prompt、工具参数、本地文件路径全量上传 | masking + 采样（0.2–0.3），敏感工作区排除 |
| 3 | 厂商不支持 `stream_options` 或异常中断 | 已用降级保护（§5.2）；instrumentor 同样受影响，需吞异常 |
| 4 | hello-agents 适配器包一层异常，`status_code` 丢失 | `_is_stream_options_unsupported` 查 `__cause__/__context__`（已处理） |
| 5 | ClickHouse 2026-01 收购 LangFuse，开源中立性下降 | 评估 Phoenix / 自建 OTEL collector 作为备选 |

### 9.5 里程碑与工作量

| 阶段 | 内容 | 工作量 | 验收 |
|------|------|--------|------|
| 前置 | RAG 链路补埋 | 0.5h | call_site 覆盖到 100% |
| P1 | Cloud 托管 + instrumentor + masking/采样 | 1 天 | 面板 span/token/缓存/成本可见 |
| P2 | 成本表 + 自托管或 Phoenix | 0.5–1 天 | 本地长期留存可用 |
| P3 | 盲区聚合视图 + 告警 | 按需 | 异常可发现 |

> **一句话落地顺序**：先补 RAG 埋点 → 接 Cloud Hobby 跑通（masking + 采样）→ 验证 span 树与成本面板 → 数据敏感或要长期留存再自托管或切 Phoenix。**埋点与后端解耦，后端随时可换。**

---

## 10. 附：涉及文件清单

### P0 新增
| 文件 | 说明 |
|------|------|
| `backend/src/logging/llm_usage_logger.py` | LLM 用量 JSONL 记录器（normalize/merge/聚合/log_response_safe） |
| `backend/src/api/usage.py` | `/api/usage/*` 路由 |
| `backend/tests/test_p0_usage.py` | P0 联调验证脚本 |
| `frontend/src/api/usage.ts` | 前端 usage API 封装 + 类型 |
| `frontend/src/views/UsageView.vue` | 用量统计页面 |

### P0 修改
| 文件 | 说明 |
|------|------|
| `backend/src/agent/enhanced_llm.py` | 流式 `stream_options` + 末块 usage 采集 + 降级保护 |
| `backend/src/agent/enhanced_simple_agent.py` | 主循环/同步/纯对话累计并上报 usage |
| `backend/src/agent/subagent_orchestrator.py` | 子代理摘要埋点 |
| `backend/src/context/context_manager.py` | 上下文压缩埋点（两处） |
| `backend/src/agent/profile_aggregator.py` | 画像聚合埋点 |
| `backend/src/logging/__init__.py` | 导出新 Logger |
| `backend/src/api/chat.py` | SSE `done` 带 usage、`agent_start` 设 session |
| `backend/src/main.py` | 注册 usage 路由 |
| `frontend/src/api/chat.ts` | `usage`/`llm_calls` 字段透传 |
| `frontend/src/views/ChatView.vue` | 本轮 token chip |
| `frontend/src/router/index.ts` | `/usage` 路由 |
| `frontend/src/App.vue` | 「用量统计」侧边栏项 |

### 既有（本文档梳理范围）
| 文件 | 说明 |
|------|------|
| `backend/src/logging/tool_logger.py` | 工具日志引擎 |
| `backend/src/api/tool_logs.py` | `/api/tool-logs/*` |
| `backend/src/api/health.py` | `/api/health/detailed` |
| `backend/src/api/session.py` | `/{id}/context-usage` |
| `backend/src/agent/myclaw_agent.py` | `get_context_usage` |
| `frontend/src/views/ToolLogsView.vue` / `HealthView.vue` / `ChatView.vue` | 前端呈现 |
