# RAG 知识库检索

RAG 面向外部文档问答，与 Memory 事实库分离。

## 入库

- 支持 `add_document` / `add_text`
- 文档经 Markdown 化后按段落/标题切块
- chunk 写入 Qdrant，并带 `rag_namespace`

## 检索

- 基线：`search_vectors` 单查询向量检索
- 进阶：可启用 MQE / HyDE 查询扩展，并可 CrossEncoder 重排
- 工具侧暴露 `rag_search` 与 `rag_ask`

## 隔离

不同知识库用 `rag_namespace` 隔离，避免互相污染。
