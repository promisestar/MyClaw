# Embedding 模型说明

文本向量化由统一的 embedding 模块提供。

## 默认模型

- 默认本地模型：`all-MiniLM-L6-v2`
- 输出维度通常为 384
- Memory 与 RAG 共用同一 embedder，保证向量空间一致

## 中文场景

MiniLM 对中文语义区分有限。若检索以中文为主，可评估切换到如 `BAAI/bge-small-zh-v1.5` 等中文友好模型。

## 环境变量

可通过 `EMBED_MODEL_TYPE`、`EMBED_MODEL_NAME` 等控制加载方式。更换模型后需重建向量索引。
