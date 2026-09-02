# MyClaw 运行时设计要点（夹具资料，供 RAG 场景入库）

## 工具门控

Ask 模式与 Plan 规划期使用 ToolMode.READ_ONLY，在 schema 层与执行层双重屏蔽
write / edit / execute_command / automation / memory_add。Craft 模式与 Plan
执行期放开全部工具。

## 上下文守卫

ContextGuard 按工具的输出体量提示（output_size_hint）与窗口占用比决定
inline / snip / delegate。write、edit、memory_add、task、subagent、Skill、
browser、automation 属于 NO_DELEGATE，永远不会被委托给子代理。

## 记忆与知识库

- 长期记忆走 Qdrant 向量库，Agent 侧只暴露 memory_search 与 memory_add。
- 文档问答走 RAG：add_document / add_text 入库，search 取原文片段，ask 生成答案。
- 跨会话原文回忆走 session_search，基于 SQLite FTS5，必要时回退 LIKE。

## 安全边界

Bash 工具先做危险命令正则匹配（COMMAND_BLOCKED），再校验工作目录白名单
（DIRECTORY_NOT_ALLOWED）。未授权工作区在请求入口即被拒绝。
