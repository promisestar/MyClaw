# RAG 模块重构说明

## 1. 重构范围

本次重构覆盖以下文件：

- `backend/src/tools/builtin/rag_tool.py`
- `backend/src/rag/pipeline.py`
- `backend/src/rag/qdrant_store.py`
- `backend/src/rag/embedding.py`
- `backend/src/rag/__init.py`

目标是提升 RAG 工具在生产使用中的安全性、可维护性、可观测性与失败降级能力，同时尽量保持原有对外 action 接口不变。

---

## 2. 主要重构内容

### 2.1 工具层 `rag_tool.py`

#### 2.1.1 统一日志体系

将原先分散的 `print()` 调试输出替换为 `logging.getLogger(__name__)`。

收益：

- 支持日志级别控制；
- 便于接入后端统一日志系统；
- 避免工具调用时向标准输出混入调试噪音。

#### 2.1.2 纯文本入库逻辑抽取

新增内部辅助方法：

- `_stable_text_document_id()`
- `_safe_filename_stem()`
- `_write_temp_text_document()`
- `_index_text_document()`

原先 `_add_text()`、`batch_add_texts()`、`add_texts_batch()` 中存在重复的临时文件写入、入库和清理逻辑，现在统一复用 `_index_text_document()`。

收益：

- 消除重复代码；
- 文本 document id 从 Python `hash()` 改为 `sha256`，避免进程重启后 hash 不稳定；
- 临时文件使用 `tempfile.NamedTemporaryFile(delete=False)`，降低文件名碰撞风险；
- 临时文件清理失败时有日志记录。

#### 2.1.3 搜索结果格式化优化

`_search()` 不再固定截断每条结果前 200 字，而是按 `max_chars` 作为总预算，在多条结果间动态分配展示长度。

收益：

- 更符合 `max_chars` 参数语义；
- 避免短结果也被强制追加省略号；
- 减少搜索结果格式化中的内联重复函数。

#### 2.1.4 问答上下文清洗优化

`_clean_content_for_context()` 不再使用 `" ".join(content.split())` 将内容压成单行，而是保留 Markdown 的基本结构，包括段落、列表、表格等。

收益：

- LLM 可获得更完整的结构信息；
- 对表格、列表、代码块类文档更友好；
- 降低因上下文结构丢失导致回答质量下降的风险。

#### 2.1.5 LLM 失败降级

`_ask()` 中 LLM 调用失败或返回空内容时，不再直接返回整体失败，而是降级返回已检索到的上下文片段。

新增方法：

- `_format_llm_fallback_answer()`

收益：

- 检索成功但 LLM 不可用时，仍然能给出可参考内容；
- 提升 RAG 工具在网络波动或模型服务异常下的可用性。

#### 2.1.6 性能信息改为 debug 控制

新增 `debug` 参数：

- 默认 `false`；
- 仅当 `debug=true` 时，最终回答中展示检索耗时、生成耗时、平均相似度。

收益：

- 默认输出更干净；
- 调试时仍可保留性能指标。

#### 2.1.7 命名空间清理安全性优化

原 `_clear_knowledge_base()` 调用 `store.clear_collection()`，实际会删除整个 Qdrant collection，和“清空指定 namespace”的语义不一致。

现在优先调用 `store.clear_namespace(namespace)`，仅删除指定 `rag_namespace` 的 RAG 数据。

同时 `clear_all_namespaces()` 增加 `confirm` 参数，未确认时不会执行。

收益：

- 避免误删其他 namespace 数据；
- 危险操作具备显式确认机制。

#### 2.1.8 shutdown 与连接缓存协同

`shutdown()` 改为通过 `QdrantConnectionManager.close_instances()` 关闭并移除匹配 collection 的连接缓存。

收益：

- 避免关闭 Qdrant client 后，连接管理器仍复用已关闭实例；
- 支持后续重新创建 pipeline。

---

### 2.2 Pipeline 层 `pipeline.py`

#### 2.2.1 统一使用 logging

将 `pipeline.py` 中的 `print()` 替换为 `logger.info()` / `logger.warning()` / `logger.error()` / `logger.debug()`。

收益：

- 文档解析、向量化、upsert、检索异常均进入统一日志；
- 进度类日志降级为 debug，减少默认输出噪音。

#### 2.2.2 复用 Qdrant 连接管理器

`create_rag_pipeline()` 原先直接构造 `QdrantVectorStore`，现在改为：

```python
QdrantConnectionManager.get_instance(...)
```

收益：

- 多个 namespace / pipeline 复用同一 collection 连接；
- 减少重复初始化 Qdrant 客户端与 collection；
- 与 `_create_default_vector_store()` 的行为保持一致。

#### 2.2.3 向量转换辅助函数

新增 `_maybe_to_list()`，用于统一处理 numpy / torch / 普通 list 的 `tolist()` 兼容逻辑。

收益：

- 减少重复判断；
- 兼容不同 embedding 后端返回类型；
- 降低静态类型检查噪音。

#### 2.2.4 类型标注与返回值修正

优化内容：

- `_create_default_vector_store(dimension: Optional[int])`；
- `index_chunks(chunks: Optional[List[Dict]])`；
- payload 过滤字典显式标注为 `Dict[str, Any]`；
- `tldr_summarize()` 返回值统一转为 `str`。

收益：

- 减少类型歧义；
- 避免 LLMResponse 等对象直接泄漏给字符串接口。

---

### 2.3 嵌入模块 `embedding.py`

#### 2.3.1 CrossEncoder 单例化

原先 `pipeline.py` 中每次调用 `rerank_with_cross_encoder()` 都会通过 `_try_load_cross_encoder()` 重新加载 CrossEncoder 模型，存在重复加载、无法统一管理生命周期的问题。

现在在 `embedding.py` 中新增 `get_cross_encoder()` 全局单例，架构与 `get_text_embedder()` 一致：

- `_build_cross_encoder()` — 从环境变量 `RERANK_MODEL_NAME`（默认 `cross-encoder/ms-marco-MiniLM-L-6-v2`）加载模型
- `get_cross_encoder()` — 线程安全单例访问
- `refresh_cross_encoder()` — 强制重建实例

新增环境变量：

| 变量 | 说明 | 默认值 |
|------|------|--------|
| `RERANK_MODEL_NAME` | CrossEncoder 模型名称 | `cross-encoder/ms-marco-MiniLM-L-6-v2` |
| `RERANK_ENABLED` | 设为 `0`/`false`/`no` 禁用重排序 | 默认启用 |

`pipeline.py` 中 `rerank_with_cross_encoder()` 改为调用 `get_cross_encoder()`，移除 `_try_load_cross_encoder()` 函数。`model_name` 参数保留为废弃参数兼容旧调用方。

收益：

- 避免重复加载 CrossEncoder 模型，减少内存占用
- 与 `get_text_embedder()` 统一的单例管理模式
- 支持通过环境变量灵活配置或禁用重排序
- 模型路径解析与嵌入模型共享 `_resolve_local_model()` 逻辑

---

### 2.4 Qdrant 存储层 `qdrant_store.py`

#### 2.3.1 连接缓存键增强

`QdrantConnectionManager` 的缓存 key 从：

```python
(url, collection_name)
```

扩展为：

```python
(url, collection_name, vector_size, distance)
```

收益：

- 避免同名 collection 在向量维度或距离度量变化时复用错误实例；
- 对动态切换 embedding 模型更安全。

#### 2.3.2 增加连接关闭能力

新增：

- `QdrantConnectionManager.close_instances()`

收益：

- 可关闭并移除匹配的缓存连接；
- 避免后续复用已关闭 client。

#### 2.3.3 支持按过滤条件删除

新增：

- `delete_by_filter(where)`
- `clear_namespace(namespace)`

`clear_namespace()` 使用以下过滤条件删除：

```python
{
    "memory_type": "rag_chunk",
    "is_rag_data": True,
    "data_source": "rag_pipeline",
    "rag_namespace": namespace,
}
```

收益：

- 支持 namespace 级安全清理；
- 避免清空整个 collection；
- 为后续按文档、按来源删除提供基础能力。

---

## 3. 行为变化说明

### 3.1 `ask` 默认不再展示性能指标

原行为：每次回答末尾追加：

```text
⚡ 检索: xxxms | 生成: xxxms | 平均相似度: x.xxx
```

新行为：默认隐藏；如需展示，调用时传：

```json
{
  "action": "ask",
  "question": "...",
  "debug": true
}
```

### 3.2 `clear` 现在按 namespace 清理

原行为：可能删除整个 Qdrant collection。

新行为：优先只删除指定 `rag_namespace` 下的 RAG chunk。

### 3.3 `add_text` 自动 document id 更稳定

原行为：使用 Python `hash(text) % 100000`。

新行为：使用 `sha256(text)[:16]`。

---

## 4. 验证结果

已执行语法编译检查：

```powershell
cd d:/Code/MyClaw/backend; python -m compileall src/rag src/tools/builtin/rag_tool.py
```

结果：通过，`pipeline.py`、`qdrant_store.py`、`embedding.py`、`rag_tool.py` 均可成功编译。

静态诊断结果：

- `rag_tool.py`：无 error，仅剩一个原有 unreachable hint；
- `pipeline.py`：无 error，仅剩未使用函数/参数类 hint；
- `qdrant_store.py`：无诊断问题。

---

## 5. 后续可继续优化方向

本次重构以安全、稳定、低风险为主，尚未大规模改变检索算法。后续可继续考虑：

1. 将内部方法从字符串结果逐步迁移为直接返回 `ToolResponse`，彻底移除 `_rag_response_from_text()`；
2. 将 `rerank_with_cross_encoder()` 接入高级检索链路；
3. 为 `ask/search` 增加可配置的 score threshold；
4. 增加按 `document_id/source_path` 删除单文档的能力；
5. 对 `MQE/HyDE` 查询扩展结果增加缓存，降低重复 LLM 调用成本；
6. 增加端到端单元测试或集成测试，覆盖入库、检索、清理 namespace、LLM 降级等核心场景。
