# MyClaw RAG 实现说明

## 1. 概览

本项目的 RAG 由两层组成：

- **核心管道层**：`src/rag/`（文档解析、切块、向量化、Qdrant 存储与检索）
- **工具编排层**：`src/tools/builtin/rag_tool.py`（对 Agent 暴露 `add_text / add_document / search / ask / stats / clear`）

当前实现是“**Qdrant 向量检索 + 可选查询扩展（MQE / HyDE）+ LLM 答案生成**”的架构。

---

## 2. 目录与职责

### 2.1 `src/rag/embedding.py`

统一嵌入提供器，核心职责：

- 提供 `EmbeddingModel` 抽象
- 本地实现：`LocalTransformerEmbedding`
  - 优先 `sentence-transformers`
  - 失败回退 `transformers + torch`
- 兜底实现：`TFIDFEmbedding`
- 全局单例接口：
  - `get_text_embedder()`
  - `get_dimension()`
  - `refresh_embedder()`

关键点：

- 向量维度动态来自 `embedder.dimension`，默认回退 `384`
- 通过环境变量控制：`EMBED_MODEL_TYPE / EMBED_MODEL_NAME / EMBED_API_KEY / EMBED_BASE_URL`

### 2.2 `src/rag/qdrant_store.py`

Qdrant 适配层，核心职责：

- 初始化客户端与集合（云端或本地）
- 建立 payload 索引（`memory_type`、`rag_namespace`、`is_rag_data` 等）
- 向量写入：`add_vectors`
- 向量检索：`search_similar`
- 清理、删除、统计、健康检查

关键点：

- 新旧 API 兼容：优先 `query_points()`，回退 `search()`
- 检索时 `where` 转换为 Qdrant `Filter(must=[FieldCondition...])`
- 维度不匹配或连接异常时会记录日志并返回空结果（而不是抛出致命异常）

### 2.3 `src/rag/pipeline.py`

RAG 核心流程实现，分三段：

1. **Ingestion（入库）**
   - `load_and_chunk_texts`：读取文档 -> Markdown 化 -> 段落/标题感知切块
   - `index_chunks`：文本预处理 -> 批量 embedding -> upsert Qdrant
2. **Retrieval（检索）**
   - `search_vectors`：单查询向量检索
   - `search_vectors_expanded`：多查询扩展检索（MQE + HyDE）
3. **High-level API**
   - `create_rag_pipeline` 返回统一接口字典：
     - `add_documents`
     - `search`
     - `search_advanced`
     - `get_stats`

补充能力（目前部分未接入主链路）：

- `rerank_with_cross_encoder`（交叉编码器重排，当前未直接接入主检索返回）
- `rank / merge_snippets / compress_ranked_items / tldr_summarize` 等后处理函数

### 2.4 `src/tools/builtin/rag_tool.py`

面向 Agent 的工具层，核心职责：

- 管理多命名空间 pipeline 缓存（`self._pipelines`）
- 对外动作：
  - `add_document`：文件入库
  - `add_text`：文本入库（先落临时 `.md` 再复用入库流程）
  - `search`：直接检索并格式化输出
  - `ask`：检索 -> 组上下文 -> 调用 LLM 生成答案
  - `stats`：读 Qdrant 统计
  - `clear`：清空并重建该命名空间
- `run()` 已按 hello_agents 协议返回 `ToolResponse`（适配 `run_with_timing`）

---

## 3. 端到端流程

## 3.1 文档入库流程（add_document / add_text）

```mermaid
flowchart TD
    A["Agent 调用 rag: add_document 或 add_text"] --> B["RAGTool.run"]
    B --> C{"action 分发"}
    C -->|add_document| D["_add_document"]
    C -->|add_text| E["_add_text（先写临时 md）"]
    D --> F["_get_pipeline namespace"]
    E --> F
    F --> G["create_rag_pipeline（缓存不存在时）"]
    G --> H["pipeline.add_documents"]
    H --> I["load_and_chunk_texts"]
    I --> I1["MarkItDown 转 markdown"]
    I1 --> I2["按标题与段落切块"]
    I2 --> I3["生成 chunk metadata 并去重"]
    I3 --> J["index_chunks"]
    J --> J1["embedder.encode 批量向量化"]
    J1 --> J2["构建 payload: rag_chunk + rag_namespace"]
    J2 --> K["QdrantVectorStore.add_vectors upsert"]
    K --> L["返回 chunks_added"]
    L --> M["RAGTool 文本结果 -> ToolResponse"]
```

## 3.2 检索流程（search）

```mermaid
flowchart TD
    A[Agent 调用 rag action=search] --> B[RAGTool._search]
    B --> C{enable_advanced_search}
    C -->|false| D[pipeline.search]
    C -->|true| E[pipeline.search_advanced]
    D --> F[search_vectors]
    E --> G[search_vectors_expanded]
    F --> H[embed_query]
    G --> G1[生成 expansions: 原query + MQE + HyDE]
    G1 --> H
    H --> I[store.search_similar]
    I --> I1[构建 Qdrant Filter: memory_type/is_rag_data/data_source/rag_namespace]
    I1 --> I2[query_points 或 search]
    I2 --> J[命中结果 id/score/metadata]
    J --> K[RAGTool 格式化文本结果]
```

## 3.3 问答流程（ask）

```mermaid
flowchart TD
    A[Agent 调用 rag action=ask] --> B[RAGTool._ask]
    B --> C[pipeline.search 或 search_advanced]
    C --> D[拿到 chunks 列表]
    D --> E[整理上下文: 清洗/截断/max_chars]
    E --> F[构建 system+user prompt]
    F --> G[self.llm.invoke 生成答案]
    G --> H[拼接引用和耗时]
    H --> I[返回 answer 文本 -> ToolResponse]
```

---

## 4. 数据模型（在 Qdrant 的 payload）

入库时每个 chunk 会写入以下关键字段（来自 `index_chunks`）：

- 检索过滤标记：
  - `memory_type = "rag_chunk"`
  - `is_rag_data = True`
  - `data_source = "rag_pipeline"`
  - `rag_namespace = <namespace>`
- 文档结构信息：
  - `source_path`
  - `doc_id`
  - `start` / `end`
  - `heading_path`
  - `lang` / `file_ext`
- 内容相关：
  - `content`（原 chunk 内容）
  - `memory_id`（chunk id）

---

## 5. 检索策略细节

### 5.1 基础检索

- 查询文本 -> `embed_query` 向量化
- 过滤条件固定包含 RAG 标记，避免检索到非 RAG 记忆
- 结果按 Qdrant 分数返回

### 5.2 高级检索（当前默认开启）

- `MQE`：通过 LLM 生成多个改写查询
- `HyDE`：通过 LLM 生成一段“假设答案文档”作为检索查询
- 合并策略：同一 `memory_id` 取最高分

### 5.3 异常与退化

- embedding 失败 -> 零向量回退（可继续执行但召回质量下降）
- Qdrant 搜索异常 -> 返回空结果并打印日志
- 因此上层常见表现是“未找到结果”，而非服务直接崩溃

---

## 6. 工具接口约定（`rag_tool.py`）

`run(parameters)` 的 `action`：

- `add_document`
- `add_text`
- `search`
- `ask`
- `stats`
- `clear`

返回：

- 统一为 `ToolResponse`
- 其中 `.text` 是给模型/用户的可读信息
- `run_with_timing` 会额外注入 `stats.time_ms`

---

## 7. 当前实现的已知特点与注意项

1. **`rerank_with_cross_encoder` 目前未接入主检索链路**  
   函数存在且可用，但 `search_vectors/search_vectors_expanded` 未调用。

2. **`ask` 中 `self.llm.invoke` 返回类型依赖 hello_agents 版本**  
   若返回 `LLMResponse`，需要取 `.content`；否则要确保为字符串。

3. **Qdrant 云连接稳定性影响检索可用性**  
   如出现 `WinError 10054`，属于网络或远端连接中断，不是算法逻辑错误。

4. **维度一致性由 `get_dimension()` 驱动**  
   向量库集合维度和嵌入模型维度必须一致，否则会降级/丢弃部分向量。

---

## 8. 快速定位入口（调试时）

- 工具入口：`src/tools/builtin/rag_tool.py` -> `RAGTool.run`
- 管道创建：`src/rag/pipeline.py` -> `create_rag_pipeline`
- 入库链路：`add_documents` -> `load_and_chunk_texts` -> `index_chunks`
- 检索链路：`search/search_advanced` -> `search_vectors/search_vectors_expanded`
- 向量存储：`src/rag/qdrant_store.py` -> `add_vectors/search_similar`

---

## 9. Chunk 切分、向量化、入库与关联（详细）

这一节聚焦你关心的 5 件事：**切分、向量化、入库、关联、实际查询**。

### 9.1 Chunk 切分策略

入口：`load_and_chunk_texts(paths, chunk_size, chunk_overlap, namespace, source_label)`

执行顺序：

1. 文档统一转换为 markdown（`_convert_to_markdown`）
2. 按标题与段落做结构化拆分（`_split_paragraphs_with_headings`）
3. 基于近似 token 长度合并段落生成 chunk（`_chunk_paragraphs`）
4. 为每个 chunk 记录位置信息（`start/end`）与结构信息（`heading_path`）
5. 使用 `content_hash` 去重，生成 `chunk_id`

说明：

- `chunk_size` / `chunk_overlap` 控制 chunk 粒度与上下文重叠
- 如果单段过长，仍可能形成超大 chunk，后续 embedding 端可能发生截断

```mermaid
flowchart TD
    A["输入文件 paths"] --> B["convert_to_markdown"]
    B --> C["split_paragraphs_with_headings"]
    C --> D["chunk_paragraphs chunk_size/chunk_overlap"]
    D --> E["遍历 chunk"]
    E --> F["计算 content_hash 去重"]
    F --> G["生成 chunk_id 与 metadata"]
    G --> H["返回 chunks 列表"]
```

### 9.2 向量化与入库

入口：`index_chunks(store, chunks, rag_namespace)`

执行顺序：

1. 对 chunk 文本做 markdown 预处理（去格式噪音）
2. 调用统一 embedder 批量编码（`embedder.encode`）
3. 向量归一化与维度对齐（异常时填零向量）
4. 组装 payload（包含 RAG 标记 + 文档定位信息）
5. 调用 `QdrantVectorStore.add_vectors` 批量 upsert

关键 payload 字段（用于检索与关联）：

- `memory_id`
- `memory_type = rag_chunk`
- `rag_namespace`
- `is_rag_data = True`
- `data_source = rag_pipeline`
- `doc_id`, `source_path`, `start`, `end`, `heading_path`, `content`

```mermaid
flowchart TD
    A["chunks"] --> B["preprocess_markdown_for_embedding"]
    B --> C["embedder.encode 批量向量化"]
    C --> D["向量归一化与维度校验"]
    D --> E["构建 payload 与 ids"]
    E --> F["Qdrant add_vectors upsert"]
    F --> G["写入完成"]
```

### 9.3 多个 chunk 之间如何保持关联

关联不是靠“向量之间互相链接”，而是靠 payload 元数据：

- **同文档关联**：`doc_id`
- **原文顺序关联**：`start/end`
- **来源关联**：`source_path`
- **语义结构关联**：`heading_path`
- **唯一定位**：`memory_id`

在检索后，系统可按 `doc_id` 聚合、按 `start` 排序，必要时补邻居 chunk（`expand_neighbors_from_pool`）恢复上下文连续性。

```mermaid
flowchart LR
    A["chunk A"] --> A1["doc_id=D1 start=0 end=300"]
    B["chunk B"] --> B1["doc_id=D1 start=260 end=560"]
    C["chunk C"] --> C1["doc_id=D1 start=520 end=820"]
    A1 --> D["同 doc_id 可聚合"]
    B1 --> D
    C1 --> D
    D --> E["按 start 排序恢复上下文"]
```

### 9.4 实际查询流程（向量库检索）

基础检索入口：`search_vectors`

1. query -> `embed_query` 得到查询向量
2. 构建 where 过滤（`rag_chunk`、`is_rag_data`、`data_source`、`rag_namespace`）
3. 调 `store.search_similar`
4. Qdrant 返回 topK chunk（`id/score/metadata`）

高级检索入口：`search_vectors_expanded`

1. 在原 query 基础上扩展 `MQE + HyDE`
2. 每个扩展查询各自向量检索
3. 按 `memory_id` 合并去重并保留最高分

```mermaid
flowchart TD
    A["用户 query"] --> B{"普通 or 高级检索"}
    B -->|普通| C["embed_query"]
    B -->|高级| D["生成扩展查询 MQE/HyDE"]
    D --> E["多查询分别 embed_query"]
    C --> F["Qdrant search_similar"]
    E --> F
    F --> G["返回命中 chunk 列表"]
    G --> H["按 memory_id 合并与排序"]
    H --> I["交给 search/ask 组装输出"]
```

### 9.5 与 `rag_tool.py` 的对接关系

- `add_document` / `add_text` -> `pipeline.add_documents`（触发切分+向量化+入库）
- `search` -> `pipeline.search` 或 `pipeline.search_advanced`
- `ask` -> 先检索 chunk，再拼接上下文给 LLM 生成回答

