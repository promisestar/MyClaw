# Memory 实现与功能说明

本文档基于当前代码，说明 MyClaw 中 **Memory（记忆）** 的存储位置、内置工具接口、`backend/src/memory` 子模块职责，以及 **Memory 对 Agent 的意义** 与 **Memory 和 RAG 的关系**。文中的 Mermaid 图可在 Obsidian 中渲染。

---

## 1. 功能总览

记忆系统已重构为 **统一的长期记忆**，基于 **Qdrant 向量数据库** 存储，使用与 RAGTool 相同的 embedding 基础设施做语义检索。

| 特性 | 说明 |
|------|------|
| **存储** | Qdrant 向量数据库（collection: `helloclaw_memory`） |
| **写入方式** | 自动捕获（MemoryCaptureManager，正则匹配）、Agent 工具 `memory_add`、Memory Flush 静默回合 |
| **检索方式** | 语义检索（embedding → Qdrant search_similar），由 Agent 按需调用，不自动注入系统提示词 |
| **遗忘机制** | 衰减式遗忘：`decay_score` 初始 1.0，每 7 天按分类速率衰减，归零则删除；被检索命中的记忆重置计时器（用进废退）；懒处理（启动时批量执行） |
| **分类体系** | preference / decision / entity / fact / plan / relationship / reference / rule（8 种） |

### 与旧架构的区别

| 维度 | 旧架构 | 新架构 |
|------|--------|--------|
| 长期记忆 | `MEMORY.md` 文件 | Qdrant 向量 |
| 每日记忆 | `memory/YYYY-MM-DD.md` | 统一为长期记忆 |
| 会话摘要 | `memory/YYYY-MM-DD-slug.md` | 已移除 |
| 检索方式 | 文件子串匹配 | embedding 语义检索 |
| 进入模型 | 全量注入系统提示词 | Agent 按需调用 `memory_search` |

---

## 2. 存储层

### 2.1 MemoryVectorStore（`memory/vector_store.py`）

记忆专用 Qdrant 封装层，提供统一的 CRUD 接口：

```
MemoryVectorStore
├── qdrant_store: QdrantVectorStore (collection="helloclaw_memory")
├── embedder: EmbeddingModel (从 embedding.py 单例获取)
├── add_memory(content, category, session_id, source) → memory_id
├── search_memories(query, top_k, score_threshold, category) → List[dict]
├── delete_memories(memory_ids) → bool
├── process_decay() → {deleted, updated, total}   # 衰减式遗忘（懒处理）
├── cleanup_expired(days) → int                     # 向后兼容包装器 → process_decay
├── _reinforce_memories(memory_ids)                 # 访问强化（内部方法）
└── get_stats() → dict
```

**Qdrant Payload 结构**：

| 字段 | 类型 | 说明 |
|------|------|------|
| `content` | string | 记忆文本内容 |
| `category` | keyword | 分类标签 |
| `memory_type` | keyword | 固定 "longterm" |
| `memory_id` | keyword | UUID |
| `timestamp` | integer | Unix 时间戳（创建时间） |
| `added_at` | integer | Unix 时间戳（与 timestamp 相同，冗余保留） |
| `session_id` | keyword | 关联会话 ID（可选） |
| `source` | keyword | capture / agent / flush / api |
| `decay_score` | float | 衰减分数，初始 1.0，随时间递减，归零则删除 |
| `last_decay_ts` | integer | 上次衰减计算时间戳（用于懒策略 + 访问强化计时） |

### 2.2 遗忘机制（衰减式）

采用 **衰减分数 + 分类差异化速率 + 访问强化 + 懒处理** 的设计，替代旧的固定时间阈值删除。

#### 核心机制

- 每条记忆写入时 `decay_score = 1.0`，`last_decay_ts = 当前时间戳`
- 每 **7 天**（`DECAY_INTERVAL_DAYS`）为一个衰减周期，每周期按分类对应的速率扣减 `decay_score`
- 当 `decay_score` 归零（≤ 0.0）时，该记忆被删除
- 衰减计算和删除 **只在程序启动时** 或 **手动调用 `memory_cleanup`** 时执行（懒策略），不随每轮对话触发

#### 分类差异化衰减速率

不同分类的记忆重要性不同，衰减速率也不同。值越小衰减越慢、记忆保留越久：

| 分类 | 每7天衰减量 | 理论寿命 | 设计理由 |
|------|------------|---------|---------|
| `entity` | 0.10 | ~70 天 | 个人信息、账号 — 不应遗忘 |
| `rule` | 0.10 | ~70 天 | 规则、约束 — 持久有效 |
| `preference` | 0.15 | ~47 天 | 用户偏好 — 较重要 |
| `relationship` | 0.15 | ~47 天 | 人际关系 — 较重要 |
| `decision` | 0.20 | ~35 天 | 决策 — 中等重要 |
| `plan` | 0.25 | ~28 天 | 计划 — 标准衰减 |
| `fact` | 0.25 | ~28 天 | 事实 — 标准衰减 |
| `reference` | 0.30 | ~23 天 | URL/路径 — 易过时 |

#### 访问强化（用进废退）

当记忆被 `search_memories` 检索命中时，自动重置该记忆的 `last_decay_ts` 为当前时间。这相当于给予一个新的完整衰减周期——**频繁被回忆的记忆将持久存在，无人问津的记忆自然消退**。每次检索最多涉及 `top_k` 条记忆（通常 5 条），每条仅一次轻量级 `set_payload` 调用。

#### `process_decay()` 处理流程

```mermaid
flowchart TD
    A["启动 / 手动触发"] --> B["scroll 遍历全部记忆"]
    B --> C{"elapsed = now - last_decay_ts<br/>≥ 7天?"}
    C -->|否| D["跳过，不衰减"]
    C -->|是| E["计算周期数 periods<br/>new_score = max(0, score - rate × periods)"]
    E --> F{"new_score ≤ 0?"}
    F -->|是| G["标记删除"]
    F -->|否| H["标记更新<br/>decay_score = new_score<br/>last_decay_ts += periods × 7天"]
    G --> I["批量删除归零记忆"]
    H --> J["逐条 set_payload 更新分数"]
    I --> K["返回 {deleted, updated, total}"]
    J --> K
    D --> K
```

#### 触发时机

1. **Agent 启动时**（`main.py` lifespan startup）
2. **Agent 调用 `memory_cleanup` 工具时**
3. **调用 `/api/memory/cleanup` HTTP 接口时**

#### 向后兼容

- `cleanup_expired(days)` 保留为薄包装器，内部调用 `process_decay()` 并返回删除数量
- 对没有 `decay_score` / `last_decay_ts` 字段的旧记忆，`process_decay` 会自动回退到 `timestamp` 作为基准补算衰减

### 2.3 Embedding 共享

MemoryVectorStore 使用与 RAGTool 相同的 embedding 基础设施（`rag/embedding.py` 中的 `get_text_embedder()` 单例），确保向量空间一致。

```mermaid
flowchart TB
    subgraph Store["MemoryVectorStore"]
        EMB["embedding.py<br/>get_text_embedder()"]
        QD["QdrantVectorStore<br/>collection: helloclaw_memory"]
        FG["process_decay()<br/>衰减式遗忘（懒处理）"]
        RF["_reinforce_memories()<br/>访问强化（用进废退）"]
    end

    subgraph Write["写入路径"]
        CAP["MemoryCaptureManager<br/>正则自动捕获"]
        AGT["Agent<br/>memory_add"]
        FLUSH["MemoryFlush<br/>静默回合"]
    end

    subgraph Read["检索路径"]
        TOOL["MemoryTool<br/>memory_search"]
    end

    CAP -->|"add_memory()<br/>decay_score=1.0"| EMB
    AGT -->|"add_memory()<br/>decay_score=1.0"| EMB
    FLUSH --> AGT
    EMB --> QD
    TOOL -->|"搜索 → embedding"| EMB
    TOOL --> QD
    TOOL -->|"命中记忆 → 重置计时器"| RF
    RF --> QD
    QD -.->|"启动时批量衰减"| FG
    FG -.->|"删除归零记忆<br/>更新存活记忆分数"| QD
```

---

## 3. 内置工具：`MemoryTool`

实现文件：`backend/src/tools/builtin/memory.py`。

| 子动作 | 说明 | 检索方式 |
|--------|------|----------|
| **`memory_search`** | 语义检索长期记忆 | embedding → Qdrant search_similar，支持 `category` 过滤 |
| **`memory_get`** | 按 memory_id 查询具体内容 | 通过 ID 精确查找 |
| **`memory_add`** | 写入新的长期记忆（合并旧 `memory_update_longterm`） | 向量化 → Qdrant upsert |
| **`memory_list`** | 列出最近记忆 | 按 timestamp 降序返回 |
| **`memory_cleanup`** | 处理记忆衰减，删除归零记忆 | 调用 `process_decay()`（懒策略：批量计算衰减分数 + 删除） |
| **`memory_delete`** | 删除指定记忆（按 ID） | `delete_memories(ids)` |

### 检索流程

```mermaid
flowchart TD
    A["memory_search(query, top_k, category)"] --> B{"query 是否为空?"}
    B -->|是| C["返回最近 top_k 条记忆"]
    B -->|否| D["embedding(query) → 向量化"]
    D --> E["Qdrant.search_similar<br/>score_threshold=0.3<br/>memory_type=longterm"]
    E --> F{"有结果?"}
    F -->|否| G["返回空结果"]
    F -->|是| H["格式化结果<br/>含 content / category / timestamp / decay_score / id"]
    H --> I["访问强化：重置命中记忆的 last_decay_ts"]
    I --> J["返回结果"]
```

### Agent 记忆操作对齐

Agent **不自动注入**记忆内容到系统提示词，而是在需要时主动调用工具：

- **检索**：`memory_search` 语义搜索（与 RAGTool 使用方式一致），命中记忆自动获得访问强化
- **写入**：`memory_add` 写入向量数据库（初始 `decay_score=1.0`）
- **删除**：`memory_delete` 按 ID 删除
- **清理**：`memory_cleanup` 触发衰减处理，删除归零记忆

系统提示词中仅注入使用指引，提醒 Agent 在需要上下文时调用 `memory_search`。

---

## 4. `backend/src/memory` 子模块

### 4.1 `MemoryCaptureManager`（`capture.py`）

在 **每轮用户消息处理结束后**（`MyClawAgent.achat` 流程末尾），对用户消息异步执行 `acapture_and_store`：

- 按 **句子** 切分（支持中文逗号/分号 + 转折连词断句）。
- 用 28 条 `MEMORY_TRIGGERS` 正则规则匹配 8 种分类（preference / decision / entity / fact / plan / relationship / reference / rule）。
- 一条句子可命中多个分类（`_match_trigger` 返回 `List[str]`）。
- 写入 **Qdrant**（`MemoryVectorStore.add_memory`）。

```mermaid
sequenceDiagram
    participant User as 用户消息
    participant Agent as MyClawAgent
    participant Cap as MemoryCaptureManager
    participant Store as MemoryVectorStore
    participant QD as Qdrant

    Agent->>Cap: acapture_and_store(用户消息)
    Cap->>Cap: 分句 + 28条规则匹配 + 多分类收集
    Cap->>Store: add_memory(content, category)
    Store->>Store: embedding → 向量化
    Store->>QD: upsert (向量 + payload)
```

### 4.2 `MemoryFlushManager`（`memory_flush.py`）

在 **上下文接近压缩阈值** 时触发 **一次静默回合**：

- 向底层 Agent 注入 prompt，要求使用 `memory_add` 将关键信息写入 Qdrant。
- 若模型只回复 `[SILENT]` 则视为无需保存。
- **每会话仅触发一次**（`_flush_triggered`），新会话加载时 `reset()`。

```mermaid
flowchart TD
    A[对话结束] --> B{估算 token 达触发点?}
    B -->|否| C[结束]
    B -->|是且未触发过| D[注入 flush_prompt<br/>引导使用 memory_add]
    D --> E[Agent.run 静默回合]
    E --> F{响应为 SILENT?}
    F -->|是| G[不写入]
    F -->|否| H[Agent 调用 memory_add<br/>写入 Qdrant]
```

### 4.3 `MemoryVectorStore`（`vector_store.py`）

见第 2.1 节。

### 4.4 包导出（`memory/__init__.py`）

导出 `MemoryCaptureManager`、`MemoryFlushManager`、`MemoryVectorStore`。

---

## 5. Memory 对 Agent 的意义

1. **向量语义检索**：不再依赖文件子串匹配，基于 embedding 找到真正语义相关的历史记忆。
2. **按需而非全量注入**：Agent 根据当前对话内容主动调用 `memory_search`，避免每次将全量记忆塞入上下文消耗 token。
3. **衰减式遗忘**：记忆按分类差异化速率衰减，重要的个人信息（entity/rule）保留 ~70 天，易过时的 URL（reference）~23 天自动消退；被检索命中的记忆重置计时器（用进废退）；衰减处理只在启动时批量执行，零运行时开销。
4. **统一存储**：消除长期记忆/每日记忆/会话摘要的界限，所有记忆以统一格式存储在 Qdrant 中。

---

## 6. Memory 与 RAG 的关系

二者均基于 **Qdrant + embedding 语义检索**，但数据来源与用途不同：

| 维度 | Memory | RAG（`RAGTool` / `backend/src/rag`） |
|------|--------|--------------------------------------|
| **内容性质** | 用户偏好、对话中沉淀的事实、决策、实体信息 | 用户主动入库的知识文档（PDF、笔记等） |
| **存储** | Qdrant collection `helloclaw_memory` | Qdrant collection（同一实例，通过 `memory_type` / `is_rag_data` 区分） |
| **检索方式** | 语义检索（`memory_search`） | 语义检索 + ask 管道（`rag.search` / `rag.ask`） |
| **进入模型的路径** | Agent 按需调用 `memory_search` | Agent 按需调用 `rag.search` / `rag.ask` |
| **生命周期** | 衰减式遗忘（分类差异化速率 + 访问强化 + 懒处理） | 由用户管理，无自动过期 |
| **典型用途** | 「记得我喜欢简短回复」「上周决定用方案A」 | 「根据手册第 3 章回答」「知识库规范」 |

```mermaid
flowchart LR
    subgraph mem["Memory"]
        M1["Qdrant: helloclaw_memory<br/>(memory_type=longterm)"]
        M2["memory_search 语义检索<br/>+ 访问强化"]
        M3["衰减式遗忘<br/>分类差异化 + 懒处理"]
    end
    subgraph rag["RAG"]
        R1["Qdrant: rag_chunks<br/>(is_rag_data=true)"]
        R2["rag.search / rag.ask"]
    end
    User["用户任务"] --> Agent["MyClawAgent"]
    Agent --> M2
    Agent --> R2
    M2 --> M1
    R2 --> R1
    M3 -.-> M1
```

**协同建议**：个人化、对话衍生、需长期跟随用户的信息优先 **Memory**；大体积资料、规范文档、多文档推理优先 **RAG**。两者共享同一 embedding 基础设施和 Qdrant 连接，由模型按任务选择工具。

---

## 7. 记忆捕获规则参考

当前 `MEMORY_TRIGGERS` 共 28 条规则，覆盖 8 个分类：

| 分类 | 示例触发词 | 条数 |
|------|------------|------|
| **fact** | 记住、记下、remember、keep in mind、版本、带数字的事实 | 5 |
| **preference** | 喜欢、偏好、prefer、习惯、经常、rather | 3 |
| **decision** | 决定、选定、不对、错了、改成、纠正、切换 | 3 |
| **plan** | 我想、计划、明天、下周、deadline、日程、待办、要做 | 4 |
| **entity** | 电话、邮箱、密码、密钥、我叫、GitHub、QQ | 7 |
| **relationship** | 同事、老板、团队、朋友、家人、领导 | 2 |
| **reference** | https://、文件路径 | 2 |
| **rule** | 禁止、不允许、always、务必、格式要求 | 2 |

---

## 8. 相关代码与 API 索引

| 位置 | 作用 |
|------|------|
| `backend/src/memory/vector_store.py` | MemoryVectorStore：Qdrant 封装，CRUD + 遗忘机制 |
| `backend/src/memory/capture.py` | MemoryCaptureManager：28 条规则自动捕获，对接 Qdrant |
| `backend/src/memory/memory_flush.py` | MemoryFlushManager：压缩前静默回合 |
| `backend/src/memory/__init__.py` | 包导出 |
| `backend/src/tools/builtin/memory.py` | MemoryTool：6 个子动作，全部基于 Qdrant 向量操作 |
| `backend/src/rag/qdrant_store.py` | QdrantVectorStore：底层 Qdrant 操作 |
| `backend/src/rag/embedding.py` | 统一 embedding 基础设施 |
| `backend/src/workspace/manager.py` | WorkspaceManager（已移除记忆文件操作） |
| `backend/src/agent/myclaw_agent.py` | 注册工具、系统提示词注入使用指引、Capture/Flush 调度 |
| `backend/src/main.py` | 启动时初始化 MemoryVectorStore + 过期清理 |
| `backend/src/api/memory.py` | 记忆列表、捕获、统计、清理等 HTTP 接口（基于 Qdrant） |

---

## 9. 配置与运维提示

- **Qdrant 连接**：MemoryVectorStore 自动复用 `QdrantConnectionManager` 单例，与 RAG 共享同一 Qdrant 实例的不同 collection。
- **遗忘机制**：衰减式遗忘，不再使用固定天数阈值。衰减周期为 7 天，各分类的衰减速率见 `CATEGORY_DECAY_RATES`（entity/rule 最慢 0.10，reference 最快 0.30）。衰减处理在启动时自动执行，也可通过 `memory_cleanup` 工具或 `/api/memory/cleanup` 手动触发。
- **Embedding 模型**：由环境变量 `EMBED_MODEL_TYPE` / `EMBED_MODEL_NAME` 控制，与 RAGTool 一致。
- **回退兼容**：MemoryTool 和 MemoryCaptureManager 均保留 `workspace_manager` 参数，在 `memory_store` 为空时回退到旧的文件操作模式。
- **自动捕获的局限**：基于规则与正则，可能漏检或误检；重要信息仍建议用户确认或让 Agent 显式调用 `memory_add`。

---

以上为当前 Memory 子系统的实现与功能说明；若后续调整 `MEMORY_TRIGGERS` 或工具参数，请以对应源码为准。
