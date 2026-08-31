# Session Recall 跨会话原文

Session Recall 负责「某次对话原话说了什么」，不走向量库。

## 存储

- 会话消息保存在全局 sessions JSON
- SQLite FTS（或 LIKE fallback）建立消息级索引

## 使用方式

- Agent 通过 `session_search` 按需检索
- 默认不把历史全文自动注入 prompt，以控制 token

## 与 Memory 的边界

事实偏好写 Memory；过程细节与原话查 Session Recall。禁止把大段 transcript 塞进长期记忆。
