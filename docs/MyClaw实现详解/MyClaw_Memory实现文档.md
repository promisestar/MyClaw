# Memory 与跨会话回忆 — 实现与功能说明

本文档基于当前代码，说明 MyClaw 中 **长期记忆（Memory）**、**用户画像（Profile）** 与 **跨会话原文召回（Session Recall）** 三通道如何分工协作：存储位置、进入模型的方式、内置工具、`backend/src/memory` / `session_store` 职责，以及与 RAG 的边界。文中的 Mermaid 图可在 Obsidian 中渲染。

设计草案详见 [`docs/MyClaw_跨会话回忆设计方案.md`](../MyClaw_跨会话回忆设计方案.md)。

---

## 1. 功能总览

### 1.1 三通道回忆模型

```mermaid
flowchart TB
    Q["用户问题 / 当前任务"] --> R{"回忆类型?"}
    R -->|"偏好、事实、决策摘要"| M["Memory 通道<br/>auto_inject + memory_search"]
    R -->|"稳定人设 / 沟通风格"| P["Profile 通道<br/>USER.md"]
    R -->|"某次对话原话 / 过程细节"| S["Session Recall<br/>session_search 工具"]
    M --> A[Agent 作答]
    P --> A
    S --> A
```

| 通道 | 存什么 | 进模型方式 | 典型问法 |
|------|--------|------------|----------|
| **Memory** | 短条目事实 / 偏好 | 每轮 top-3 自动注入 + `memory_*` 工具 | 「我喜欢什么风格？」 |
| **Profile** | 聚合后的用户画像 | 常驻 system prompt（`USER.md`） | 「按我的习惯来」 |
| **Session Recall** | 消息级原文索引 | **仅工具按需**（不自动注入全文） | 「上次部署失败我们怎么排查的？原话是什么？」 |

**原则**：事实偏好走 Memory；「那次对话说了什么」走 Session Recall；禁止把大段 transcript 用 `memory_add` 塞进长期记忆。

### 1.2 Memory 通道（事实层）

记忆系统为 **统一的长期记忆**：基于 **Qdrant 向量数据库** 存储，使用与 RAGTool 相同的 embedding 基础设施做语义检索。进入模型的路径是 **双通道**：

1. **每轮自动注入（默认开启）**：用户消息到达时，按语义检索 top-K（默认 **3**）条相关记忆，经 `turn_context` 合并进本轮发给模型的 **user 前缀**（不写入 `system_prompt`、不入会话历史），以利于 prompt cache。
2. **工具按需深挖**：Agent 可再调 `memory_search` 等子动作获取更多或按分类过滤的结果。

| 特性 | 说明 |
|------|------|
| **存储** | Qdrant 向量数据库（collection: `helloclaw_memory`，可由 `QDRANT_COLLECTION` 覆盖） |
| **写入方式** | 自动捕获（`MemoryCaptureManager`，正则匹配）、Agent 工具 `memory_add`、Memory Flush 静默回合、HTTP `/api/memory/capture` |
| **写入去重** | **L1 字面去重（默认启用）**：`content_hash + category`；**L2 语义去重（默认关闭）**：embedding top-1 + 阈值。命中均不新建，复用旧 `memory_id` 并强化 |
| **检索 / 进入模型** | ① 每轮 `auto_inject` 语义检索并写入 ephemeral `turn_context`；② Agent 工具 `memory_search` 按需深挖；③ `USER.md` 画像区域由聚合器从记忆沉淀后，经 `_build_system_prompt` 常驻于**冻结** system |
| **遗忘机制** | 衰减式遗忘：`decay_score` 初始 1.0，每 7 天按分类速率衰减，归零则删除；检索命中重置计时器（用进废退）；懒处理（启动时 / `/api/memory/cleanup`） |
| **分类体系** | preference / decision / entity / fact / plan / relationship / reference / rule（8 种） |
| **用户画像联动** | `ProfileAggregator` 从记忆聚合写入 `~/.helloclaw/identity/USER.md` 自动区域（每 10 轮或 preference 过多时触发） |

### 1.3 Session Recall 通道（原文层）— 本期已落地

| 特性 | 说明 |
|------|------|
| **权威原文** | `~/.helloclaw/sessions/<id>.json`（全局，跨工作区保留） |
| **检索索引** | `~/.helloclaw/sessions/index.db`（SQLite + FTS5，可全量重建；损坏可丢） |
| **Agent 工具** | `session_search`：discover / scroll / read / browse（参数推断，无显式 mode） |
| **进模型** | **仅按需工具**；默认不把旧对话全文自动注入（控 token） |
| **中文 / 降级** | FTS 主路径；CJK 少命中时 LIKE fallback；无 FTS5 时整库 LIKE-only |
| **Ask 模式** | 只读，允许调用 |

### 与旧架构的区别

| 维度 | 旧架构 | 当前架构 |
|------|--------|----------|
| 长期记忆 | `MEMORY.md` 文件 | Qdrant 向量 |
| 每日记忆 / 会话摘要 | 独立 Markdown 文件 | 已移除，统一为长期记忆 |
| 检索方式 | 文件子串匹配 | embedding 语义检索（Memory）+ SQLite FTS（Session Recall） |
| 进入模型 | 早期曾「仅工具按需、不注入」 | **Memory**：每轮 top-K + 工具 + `USER.md`；**Session**：按需 `session_search` |

---

## 2. 每轮对话中的记忆生命周期

```mermaid
sequenceDiagram
    participant U as 用户消息
    participant A as MyClawAgent
    participant VS as MemoryVectorStore
    participant QD as Qdrant
    participant Cap as MemoryCaptureManager
    participant PA as ProfileAggregator
    participant Flush as MemoryFlushManager

    U->>A: chat / achat
    A->>A: _build_system_prompt()<br/>（含 USER.md 画像等）
    A->>VS: _inject_relevant_memories(query)
    VS->>QD: search_similar(top_k=3, threshold=0.3)
    QD-->>VS: 相关记忆
    VS-->>A: 格式化「相关记忆（自动注入）」
    A->>A: system_prompt += memory_context
    A->>A: Agent ReAct / 流式回复
    Note over A: 以下仅 achat 主路径在对话结束后执行
    A->>Cap: acapture_and_store(用户消息)
    Cap->>VS: add_memory（经 L1/L2 去重）
    A->>PA: _maybe_aggregate_profile（条件满足时）
    A->>Flush: _check_and_run_memory_flush（近压缩阈值时）
```

### 2.1 自动注入（`_inject_relevant_memories`）

实现位置：`backend/src/agent/myclaw_agent.py`。在 `chat()` / `achat()` 中，于会话绑定与 `ensure_session_system_prompt()` **之后**、Agent 运行 **之前** 组装进 `turn_context`。

行为要点：

- 用当前用户消息（多模态时先拍平为纯文本）做 query，调用 `MemoryVectorStore.search_memories`。
- 命中则写入本轮 `turn_context`，标题为 `## 相关记忆（自动注入）`，并提示不足时可再调 `memory_search`。
- **基座 system 在会话内冻结**；记忆块每轮可变，但只出现在发给模型的 user 前缀，**不会**追加进 `system_prompt`，也**不会**写入会话历史。
- 记忆不可用、配置关闭、query 为空或无命中时返回空串，不影响对话。

配置（工作区 / 全局 `config.json` 的 `memory` 段，模板见 `workspace/templates/config.json`）：

| 键 | 默认 | 说明 |
|----|------|------|
| `auto_inject` | `true` | 是否启用每轮自动注入 |
| `auto_inject_top_k` | `3` | 每轮注入条数 |
| `auto_inject_threshold` | `0.3` | 相似度下限（与工具检索默认阈值一致） |

```json
"memory": {
  "auto_inject": true,
  "auto_inject_top_k": 3,
  "auto_inject_threshold": 0.3
}
```

工作区 `AGENTS.md` 中的 Memory / Session Recall 说明已与此对齐：系统会自动注入相关记忆；不够时再用 `memory_search`；问某次对话原话用 `session_search`。

### 2.2 对话结束后的写入与维护（`achat`）

| 步骤 | 方法 | 说明 |
|------|------|------|
| 自动捕获 | `_capture_memories` → `MemoryCaptureManager.acapture_and_store` | 正则分句匹配后写入 Qdrant |
| 画像聚合 | `_maybe_aggregate_profile` | 每 10 轮，或 preference 记忆偏多时，聚合到 `USER.md` |
| Memory Flush | `_check_and_run_memory_flush` | 接近上下文压缩阈值时，静默回合引导 `memory_add`（每会话最多一次） |
| 会话索引 | `save_current_session` → `_index_session_safe` | JSON 保存成功后增量 upsert 到 `index.db`（失败不阻断对话） |

> **说明**：同步 `chat()` 同样做自动注入与会话保存（含索引）；自动捕获 / 画像 / Flush 挂在异步 `achat` 结束路径上（前端流式对话走 `achat`）。

---

## 3. 存储层

### 3.1 MemoryVectorStore（`memory/vector_store.py`）

记忆专用 Qdrant 封装层：

```
MemoryVectorStore
├── qdrant_store: QdrantVectorStore (collection="helloclaw_memory")
├── embedder: EmbeddingModel (从 embedding.py 单例获取)
├── add_memory(content, category, session_id, source) → memory_id
├── search_memories(query, top_k, score_threshold, category) → List[dict]
├── delete_memories(memory_ids) → bool
├── process_decay() → {deleted, updated, total}
├── cleanup_expired(days) → int          # 兼容包装 → process_decay
├── _reinforce_memories(memory_ids)      # 检索命中强化
└── get_stats() → dict
```

**Qdrant Payload 结构**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `content` | string | 记忆文本 |
| `content_hash` | keyword | 归一化内容 sha1[:16]，L1 去重 |
| `category` | keyword | 分类标签 |
| `memory_type` | keyword | 固定 `longterm` |
| `memory_id` | keyword | UUID |
| `timestamp` / `added_at` | integer | 创建时间 |
| `session_id` | keyword | 关联会话（可选） |
| `source` | keyword | capture / agent / flush / api |
| `decay_score` | float | 衰减分数，初始 1.0 |
| `last_decay_ts` | integer | 上次衰减基准 / 访问强化计时 |
| `access_count` | integer | 去重命中或检索强化次数 |

初始化时自动确保 `category`、`content_hash` 的 payload index（云端 Qdrant 对 filter 字段强制要求）。

### 3.2 遗忘机制（衰减式）

- 写入时 `decay_score = 1.0`，`last_decay_ts = now`
- 每 **7 天**（`DECAY_INTERVAL_DAYS`）按分类速率扣减；`≤ 0` 则删除
- **懒处理**：启动时（`main.py` lifespan）或 HTTP `/api/memory/cleanup` 时批量执行
- **访问强化**：`search_memories` 命中（含自动注入检索）会重置 `last_decay_ts`

| 分类 | 每 7 天衰减量 | 理论寿命 | 设计理由 |
|------|---------------|----------|----------|
| `entity` / `rule` | 0.10 | ~70 天 | 个人与规则宜久留 |
| `preference` / `relationship` | 0.15 | ~47 天 | 偏好与关系较重要 |
| `decision` | 0.20 | ~35 天 | 决策中等 |
| `plan` / `fact` | 0.25 | ~28 天 | 标准 |
| `reference` | 0.30 | ~23 天 | URL/路径易过时 |

```mermaid
flowchart TD
    A["启动 / API cleanup"] --> B["scroll 全部记忆"]
    B --> C{"elapsed ≥ 7天?"}
    C -->|否| D["跳过"]
    C -->|是| E["new_score = max(0, score - rate × periods)"]
    E --> F{"new_score ≤ 0?"}
    F -->|是| G["删除"]
    F -->|否| H["更新 decay_score / last_decay_ts"]
```

旧记忆若缺少 `decay_score` / `last_decay_ts`，会回退用 `timestamp` 补算。

### 3.3 写入路径去重（L1 / L2）

`add_memory` 在真正写入前：

1. **L1（默认开）**：归一化文本 → `content_hash`，同 `category` 精确匹配 → 强化旧条、不新建  
2. **L2（默认关）**：仅当 `MEMORY_DEDUPE_THRESHOLD < 1.0`；同分类 embedding top-1 ≥ 阈值 → 同上  

失败均静默回退为正常写入。`MemoryCaptureManager.capture()` 内另有**单次调用内** `seen_contents` 去重；跨次 / 跨会话去重统一由 VectorStore 负责。

| 场景 | L1 | L2（默认关） | 结果 |
|------|----|--------------|------|
| 同内容同分类反复写 | 命中 | — | 复用 ID，`access_count++` |
| 大小写/空白扰动 | 命中 | — | 同上 |
| 同内容不同分类 | 未命中 | 未命中 | 新建两条 |
| 同义改写（L2 关） | 未命中 | 未触发 | 新建 |
| 同义改写（L2 开且命中） | 未命中 | 命中 | 复用 ID |

默认 embedding（如 `all-MiniLM-L6-v2`）对中文反义/同义区分不足，故 L2 默认关闭；换中文友好模型后可设 `MEMORY_DEDUPE_THRESHOLD=0.92` 启用。

### 3.4 Embedding 共享

与 RAG 共用 `rag/embedding.py` 的 `get_text_embedder()`，保证向量空间一致。

```mermaid
flowchart TB
    subgraph Store["MemoryVectorStore"]
        EMB["embedding 单例"]
        QD["Qdrant helloclaw_memory"]
        DEDUPE["L1/L2 去重"]
        DECAY["process_decay"]
    end

    subgraph Write["写入"]
        CAP["Capture"]
        AGT["memory_add"]
        FLUSH["Flush → memory_add"]
        API["HTTP capture"]
    end

    subgraph Read["读入模型"]
        AUTO["_inject_relevant_memories"]
        TOOL["memory_search"]
        USER["USER.md 经 system_prompt"]
    end

    CAP --> DEDUPE
    AGT --> DEDUPE
    FLUSH --> AGT
    API --> DEDUPE
    DEDUPE --> EMB --> QD
    AUTO --> EMB
    TOOL --> EMB
    AUTO --> QD
    TOOL --> QD
    QD -.-> DECAY
```

---

## 4. 内置工具：`MemoryTool`

实现：`backend/src/tools/builtin/memory.py`（`expandable=True`，注册时展开为独立工具）。

为降低 LLM 工具选型噪声，**Agent 仅可见两个子工具**；列表/按 ID 查询/衰减/删除/手动画像聚合仍由系统 lifespan、HTTP API、`ProfileAggregator` 自动路径提供。

| 子工具 | 说明 | 元数据 |
|--------|------|--------|
| `memory_search` | 语义检索（默认 top_k=5，可按 category 过滤）；结果含全文与 ID | `has_side_effects=False`，`output_size_hint=1000` |
| `memory_add` | 写入长期记忆（`source=agent`） | `has_side_effects=True`，`output_size_hint=200`（Ask / Plan 规划期屏蔽） |

与自动注入的关系：

- 自动注入覆盖「本轮最相关的几条」，降低漏调工具的概率。  
- 需要更多结果或按分类过滤时，使用 `memory_search`。  
- 工具检索命中同样触发访问强化。

`memory_store` 不可用时，search/add 可回退到旧的 `workspace_manager` 文件 API（过渡兼容）。

---

## 5. `backend/src/memory` 与画像联动

### 5.1 `MemoryCaptureManager`（`capture.py`）

在 `achat` 结束后对用户消息 `acapture_and_store`：

- 按句子切分（中文标点 / 转折连词）  
- `MEMORY_TRIGGERS`（约 28 条）匹配 8 类；一句可多分类，主分类取首个  
- 写入 Qdrant（经 L1/L2）

### 5.2 `MemoryFlushManager`（`memory_flush.py`）

上下文接近压缩阈值时触发**一次**静默回合（`soft_threshold_tokens` 提前量）：

- Prompt 要求用 `memory_add` 写入 Qdrant，勿写文件  
- 仅回复 `[SILENT]` 则不写  
- 新会话 `activate_session` / 清空时 `reset()`

### 5.3 `ProfileAggregator`（`agent/profile_aggregator.py`）

不属于 `memory/` 包，但是记忆链路的下游：

- 从 preference / entity 等记忆聚合到 `USER.md` 的自动区域（`tech_stack` / `work_domain` / `communication` / `code_style`）  
- 保留手动区域（HTML 注释边界）  
- 触发：对话每 10 轮，或 preference 条数超过阈值（`achat` 末尾自动）；**不再**向 Agent 暴露 `memory_aggregate_profile` 工具 
- 聚合结果经 `_build_system_prompt` 的「用户信息」块**常驻**进 system prompt（与每轮 top-K 自动注入互补：画像偏稳定偏好，自动注入偏本轮相关事实）

### 5.4 包导出

`memory/__init__.py` 导出 `MemoryCaptureManager`、`MemoryFlushManager`、`MemoryVectorStore`。

---

## 6. Memory 对 Agent 的意义

1. **低摩擦回忆**：每轮自动注入 top-K，无需模型先猜要不要调工具。  
2. **可深挖**：自动块不够时用 `memory_search`；重要信息仍可用 `memory_add` 显式写入。  
3. **语义而非字面**：向量检索覆盖改写表述。  
4. **衰减 + 用进废退**：冷记忆自然消退，常被检索的更持久。  
5. **去重**：避免捕获规则把同一句话写成多条。  
6. **画像沉淀**：零散 preference 记忆收敛为 `USER.md`，跨会话稳定影响人设与风格。  
7. **与原文通道互补**：Memory 答「记住了什么」；需要原话时用 `session_search`，避免把 transcript 污染事实库。

---

## 7. Memory / RAG / Session Recall 三通道关系

| 维度 | Memory | RAG | Session Recall |
|------|--------|-----|----------------|
| **内容** | 对话沉淀的偏好、事实、决策、实体等 | 用户入库的文档知识 | 历史会话消息原文 |
| **存储** | Qdrant `helloclaw_memory` | Qdrant RAG collection | JSON 权威 + SQLite `index.db` |
| **进模型** | 每轮 auto_inject + 工具 + USER.md | `rag.search` / `rag.ask` | **仅** `session_search` 按需 |
| **生命周期** | 衰减遗忘 | 用户管理 | 随会话 JSON 增删；索引可重建 |

```mermaid
flowchart LR
    subgraph mem["Memory"]
        M1["Qdrant longterm"]
        M2["每轮 auto_inject top-K"]
        M3["memory_search"]
        M4["USER.md 画像"]
    end
    subgraph rag["RAG"]
        R1["文档 chunks"]
        R2["rag.search / ask"]
    end
    subgraph sess["Session Recall"]
        S1["sessions/*.json"]
        S2["index.db FTS"]
        S3["session_search"]
    end
    User --> Agent
    Agent --> M2
    Agent --> M3
    Agent --> M4
    Agent --> R2
    Agent --> S3
    M2 --> M1
    M3 --> M1
    R2 --> R1
    S3 --> S2
    S2 -.-> S1
```

**协同建议**：个人化、对话衍生 → Memory；大文档 / 规范 → RAG；「某次对话原话 / 排障过程」→ `session_search`（先 discover，必要时再 `memory_add` 固化短事实）。**【禁止】** 整段 transcript 进 Memory。

---

## 7.1 Session Recall 实现详解

### 架构与数据流

```mermaid
sequenceDiagram
    participant User
    participant Agent as MyClawAgent
    participant JSON as sessions/*.json
    participant Idx as SessionIndexer
    participant DB as index.db
    participant Tool as session_search

    Note over Agent,JSON: 写入路径
    Agent->>JSON: save_session
    Agent->>Idx: upsert_session
    Idx->>JSON: 读 history，拍平文本
    Idx->>DB: REPLACE messages + FTS

    Note over User,Tool: 召回路径（按需）
    User->>Agent: 「上次怎么配的 Redis」
    Agent->>Tool: session_search(query=...)
    Tool->>DB: FTS MATCH / LIKE
    DB-->>Tool: hits + 锚定窗口 + bookends
    Tool-->>Agent: JSON 结果
```

| 路径 | 角色 |
|------|------|
| `~/.helloclaw/sessions/*.json` | **权威原文**（UI `/session/{id}/history`、load_session） |
| `~/.helloclaw/sessions/index.db` | **检索索引**（可 `POST /api/session-search/rebuild` 重建） |

核心包：`backend/src/session_store/`（`probe` / `db` / `indexer` / `search` / `models` / `config`）。

### 工具：`session_search`

实现：`backend/src/tools/builtin/session_search_tool.py`。参数推断模式（对齐 Hermes）：

| 模式 | 触发参数 | 行为 |
|------|----------|------|
| **discover** | `query` | FTS/LIKE 检索 → 按 session 去重 → snippet + ±window + bookends |
| **scroll** | `session_id` + `around_message_id` | 以该消息为中心的窗口 |
| **read** | 仅 `session_id` | 会话摘要 + head/tail（不全量倾倒） |
| **browse** | 无参或仅 `limit` | 按 `updated_at` 列近期会话 |

约束与默认：

- 默认排除当前会话 live 命中（`exclude_current_session`）  
- 默认只搜 `user,assistant`；tool 消息默认不入索引（`index_tool_messages: false`）  
- 索引文本优先 `metadata.original_content`（ContextManager snip 场景），否则 `content`；摘要行可标 `is_compacted`  
- `has_side_effects=False`，Ask / Plan 规划期可用；`output_size_hint≈4000`

### 索引生命周期

| 事件 | 动作 |
|------|------|
| `save_current_session` / `chat` 保存成功 | `_index_session_safe` → `upsert_session`（失败只打日志） |
| `delete_session` | `indexer.delete_session` |
| 启动 lifespan | probe FTS → `ensure_schema` → 空库或 schema 落后则后台 `rebuild_if_needed` |
| HTTP | `POST /api/session-search/rebuild` 全量重建 |

启动时探测本机 SQLite 是否编入 FTS5；不可用则 `index_mode=like`，**不引入新 pip 依赖**。

### 配置（`config.json` → `session_recall`）

```json
"session_recall": {
  "enabled": true,
  "db_path": null,
  "discover_limit": 3,
  "fts_scan_limit": 100,
  "default_window": 5,
  "index_tool_messages": false,
  "exclude_current_session": true,
  "cjk_like_fallback": true,
  "auto_hint_in_prompt": false
}
```

`enabled: false` 时不注册工具、不挂钩索引。`auto_hint_in_prompt` 预留（本期默认关、不实现每轮自动塞旧对话）。

### HTTP 调试接口

| 方法 | 路径 | 作用 |
|------|------|------|
| GET | `/api/session-search?q=&limit=` | discover |
| GET | `/api/session-search/{session_id}/around/{msg_id}` | scroll |
| POST | `/api/session-search/rebuild` | 全量重建 |

同步 SQLite 经 `run_in_threadpool`。

### 测试

`backend/tests/session_store/test_session_store.py`：probe、LIKE 降级、双会话隔离、排除当前会话、删改一致、rebuild、`original_content` 优先、Ask 模式不在副作用黑名单等。

```bash
cd backend && python -m unittest tests.session_store.test_session_store -v
```

---

## 8. 记忆捕获规则参考

`MEMORY_TRIGGERS` 覆盖 8 类（条数以 `capture.py` 为准，约 28 条），示例：

| 分类 | 示例触发 | 条数约 |
|------|----------|--------|
| fact | 记住、remember、带数字事实、版本 | 若干 |
| preference | 喜欢、偏好、习惯、rather | 若干 |
| decision | 决定、改成、纠正、切换 | 若干 |
| plan | 计划、明天、deadline、待办 | 若干 |
| entity | 电话、邮箱、密钥、我叫、GitHub | 若干 |
| relationship | 同事、老板、团队、家人 | 若干 |
| reference | https://、文件路径 | 若干 |
| rule | 禁止、务必、格式要求 | 若干 |

自动捕获可能漏检/误检；关键信息仍建议显式 `memory_add` 或 Flush。

---

## 9. 相关代码与 API 索引

| 位置 | 作用 |
|------|------|
| `backend/src/memory/vector_store.py` | CRUD、衰减、L1/L2、payload index |
| `backend/src/memory/capture.py` | 正则自动捕获 |
| `backend/src/memory/memory_flush.py` | 压缩前静默 Flush |
| `backend/src/tools/builtin/memory.py` | MemoryTool 子动作 |
| `backend/src/session_store/` | Session Recall：probe / schema / indexer / search |
| `backend/src/tools/builtin/session_search_tool.py` | `session_search` 工具 |
| `backend/src/api/session_search.py` | HTTP discover / scroll / rebuild |
| `backend/src/agent/myclaw_agent.py` | 自动注入、捕获/Flush/画像、session 索引挂钩、system prompt |
| `backend/src/agent/profile_aggregator.py` | 记忆 → USER.md 聚合 |
| `backend/src/workspace/templates/config.json` | `memory.*` / `session_recall.*` 默认 |
| `backend/src/workspace/templates/workspace/AGENTS.md` | 面向模型的三通道使用说明 |
| `backend/src/main.py` | 启动初始化 + `process_decay` + session 索引后台 rebuild |
| `backend/src/api/memory.py` | HTTP 列表/统计/捕获/清理/删除（Qdrant 调用走 `run_in_threadpool`） |
| `backend/tests/session_store/` | Session Recall 单测 |

### HTTP 接口（Memory）

| 方法 | 路径 | 作用 |
|------|------|------|
| GET | `/api/memory/list` | 最近列表 / 关键词检索 |
| GET | `/api/memory/stats` | 分类统计 |
| POST | `/api/memory/capture` | 手动添加 |
| POST | `/api/memory/cleanup` | 衰减处理 |
| DELETE | `/api/memory/{memory_id}` | 按 ID 删除 |

### HTTP 接口（Session Recall）

| 方法 | 路径 | 作用 |
|------|------|------|
| GET | `/api/session-search?q=&limit=` | discover |
| GET | `/api/session-search/{session_id}/around/{msg_id}` | scroll |
| POST | `/api/session-search/rebuild` | 全量重建索引 |

---

## 10. 配置与运维提示

- **自动注入**：`config.json` → `memory.auto_inject` / `auto_inject_top_k` / `auto_inject_threshold`；关掉 `auto_inject` 则退回「仅工具检索」。  
- **Session Recall**：`session_recall.enabled`；`force_like` 可强制 LIKE 路径（测试/排障）；索引默认 `~/.helloclaw/sessions/index.db`。  
- **Qdrant**：与 RAG 共享连接，不同 collection；`QDRANT_COLLECTION` 可覆盖记忆 collection 名。  
- **Payload index**：初始化自动建 `category`、`content_hash`。  
- **去重**：`MEMORY_DEDUPE_THRESHOLD`（默认 `1.0` = 关 L2）。  
- **Embedding**：`EMBED_MODEL_TYPE` / `EMBED_MODEL_NAME`；换模后旧向量空间不一致，宜重建或接受效果下降。  
- **HTTP**：同步 Qdrant / SQLite 操作经 `run_in_threadpool`，避免堵事件循环。  
- **兼容**：`MemoryTool` / Capture 仍保留 `workspace_manager` 回退参数。  
- **局限**：Memory 自动注入只带 top-K，不是全库；正则捕获非完备。跨会话**原文**由 `session_search` 补齐；语义改写召回弱于向量（中文依赖 LIKE fallback，二期可加 trigram）。前端「搜索历史」UI 尚未做。

---

以上为当前 **Memory + Session Recall** 说明。若调整 `auto_inject*`、`session_recall.*`、`MEMORY_TRIGGERS`、`CATEGORY_DECAY_RATES`、`MEMORY_DEDUPE_THRESHOLD` 或工具参数，以对应源码为准。
