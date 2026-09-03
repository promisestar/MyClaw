# 长期记忆 Memory 通道

Memory 保存短事实、偏好与决策，供跨会话使用。

## 写入

- Agent 可通过 `memory_add` 写入（`source=agent`）
- Flush 机制会在接近压缩阈值时引导把对话中的事实沉淀进 Memory（`source=flush`）
- 也可经 HTTP `POST /api/memory/capture` 手动入库（`source=api`）
- 系统不会在对话结束后用正则自动捕获用户消息
- 写入路径含 L1 字面去重（默认开），可选 L2 语义去重（默认 `MEMORY_DEDUPE_THRESHOLD=1.0` 关闭）

## 检索与注入

- 每轮对话可用用户消息做语义检索并自动注入：**最多** `auto_inject_top_k` 条（默认 3），且相似度须 ≥ 阈值（约 0.3）
- 命中不足上限时只注入实际过阈值项；**0 命中则不注入**
- 需要更多结果时再调用 `memory_search`

## 衰减

长期记忆支持按分类差异化衰减，被检索命中会强化保留。
