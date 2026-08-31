# Qdrant 向量库运维要点

Qdrant 是本项目的向量数据库，用于 Memory 与 RAG 的语义检索。

## 部署

- 本地开发常用地址：`http://localhost:6333`
- 集合创建时需指定向量维度，须与 embedding 模型输出维度一致
- 推荐使用余弦相似度（cosine）作为距离度量

## 过滤检索

检索时可通过 payload 字段过滤，例如：

- `memory_type=longterm` 仅查长期记忆
- `rag_namespace` 隔离不同知识库
- `is_rag_data=true` 限定 RAG chunk

## 注意

维度不匹配会导致写入或检索失败。更换 embedding 模型后通常需要重建集合。
