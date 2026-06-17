# AGENTS.md — HelloClaw 工作指南

你是运行在本工作空间内的助手。路径均相对于工作空间根目录；可在根目录内自由 **Read / Write / Edit** 文件。

---

## 1. 工具选择（优先阅读）

按任务类型选工具，**不要**用错通道（例如用 `Read` 代替 RAG、用 `BashTool` 读文件）。

| 需求 | 使用工具 | 不要用 |
|------|----------|--------|
| 查看工作区文件/列目录 | **Read** | `BashTool` 的 cat/type/dir |
| 新建文件或整文件重写 | **Write** | `Edit`（局部替换）、`BashTool` 重定向写文件 |
| 改已有文件中的一处文本 | **Edit**（`old_string` 须唯一且与 Read 一致） | `Write` 覆盖全文 |
| 运行测试、git、安装依赖、构建 | **BashTool** | `Read`/`Write` |
| 精确数学计算 | **python_calculator** | 心算或 shell |
| 查历史对话/偏好（长期记忆） | **memory_search** / **memory_get** | 凭猜测回答 |
| 写入重要信息 | **memory_add** | 口头承诺 |
| 用户已入库文档（PDF 等） | **rag**（`ask` / `search`） | 仅凭记忆或 `Read` 工作区外的库 |
| 领域标准流程（PDF、专项规范） | **Skill**（先加载再动手） | 凭常识猜步骤 |
| 外部系统（GitHub、Slack 等） | **MCP 网关**（如 `github`）→ `enable_tools` 披露 → `mcp_*` 子工具 | 编造 API 或直接 `call_tool` 猜参数 |
| 查公开网络信息（新闻、文档入口） | **web_search** | `web_fetch` |
| 抓取已知 URL 全文 | **web_fetch** | `web_search` |

**文件三连击**：改代码前 **Read** → 小改用 **Edit** → 新建或全文重写用 **Write**。`Edit` 前必须从 **Read** 复制原文（含缩进与换行）。

**信息三连击**：工作区文件 → **Read**；用户知识库 → **rag**；长期记忆/偏好 → **memory_search**；公网 → **web_search** / **web_fetch**。

---

## 2. 标准执行流程

每轮用户请求建议按序执行（可跳过不适用的步骤）：

1. **理解**：明确目标、交付物、是否涉及外部系统或已入库资料。
2. **加载上下文**（见 §3）：记忆 / RAG / Skill / MCP 清单，按需调用，避免重复 Read 已注入文件。
3. **规划**：列出步骤；若多步依赖前一步结果，先完成再进入下一步。
4. **执行**：一次工具做好一件事；长文件用 Read 的 `offset`/`limit` 分段。
5. **验证**：改文件后必要时再 Read；命令看退出码与 stderr。
6. **收尾**：删除本轮临时文件（§8）；用自然语言回复用户（§9）。

**并行原则**：无依赖的工具调用可同轮发起；有依赖的必须等待结果（例如先 Read 再 Edit）。

---

## 3. 会话开始（有历史任务时）

下列内容**已注入系统提示词**，无需再 Read：`IDENTITY.md`、`USER.md`、`SOUL.md`。

仍需按需执行：

1. **memory_search** — 用户问题涉及过往偏好、人名、项目名时（语义检索长期记忆）
2. **rag** — 问题依赖「用户上传/入库的资料」时（`ask` 或 `search`）
3. **Skill** — 任务匹配某领域技能（如 PDF）时，**在写代码或改文件之前**加载
5. **MCP 网关**（如 `github`）— 读网关描述选远端工具 → `enable_tools` 披露 → 调用 `mcp_{网关}_*` 子工具

---

## 4. 工作区文件

| 文件 | 说明 |
|------|------|
| AGENTS.md | 本指南 |
| IDENTITY.md / USER.md / SOUL.md | 已注入，更新时用 Edit |
| HEARTBEAT.md | 心跳任务 |
| BOOTSTRAP.md | 首次初始化 |

---

## 5. 工具速查

### 5.1 文件与命令

- **Read** — 只读；`path` 为文件或目录；大文件用 `offset`/`limit`。
- **Write** — 整文件写入；`content` 必须是完整正文。
- **Edit** — 单次唯一替换；`old_string` / `new_string`。
- **BashTool** — 工作区内 shell；破坏性命令会被拦截；`cd` 在同会话内保持。

### 5.2 记忆（memory_*）

所有长期记忆存储在 Qdrant 向量数据库中，支持语义检索。Agent 按需调用，无需全量注入提示词。

| 工具 | 何时用 |
|------|--------|
| memory_search | 语义检索长期记忆（偏好、决策、实体等） |
| memory_get | 按 ID 查询具体记忆 |
| memory_add | 写入新的长期记忆 |
| memory_list | 列出最近的记忆 |
| memory_cleanup | 清理超过 7 天的过期记忆 |
| memory_delete | 删除指定记忆（按 ID）

### 5.3 知识库（rag）

`action`：`add_document` | `add_text` | `search` | `ask` | `stats` | `clear`（清空须用户确认 + `confirm=true`）。

- 问「资料里写了什么」→ 优先 **ask** 或 **search**
- 与工作区源码/配置无关的私有文档 → **rag**，不用 **Read** 代替

### 5.4 领域技能（Skill）

- 参数：`skill`（必填，如 `pdf`），`args`（可选，替换 `$ARGUMENTS`）
- **Skill** = 系统预置操作手册；**rag** = 用户已入库文档；可同时使用

### 5.5 MCP（渐进披露，类似 Skill）

MCP 采用 **两阶段** 模式（与 Skill 的「先加载再执行」类似，但披露的是可调用子工具）：

1. **选工具**：阅读 MCP 网关（如 `github`）描述中的远端工具目录
2. **披露**：`{"action":"enable_tools","tool_names":["远端工具名",...]}`
3. **调用**：下一轮直接使用披露后的名称（如 `mcp_github_search_repositories`）并传入完整参数

| action | 用途 |
|--------|------|
| **enable_tools** | **推荐** — 按需披露远端工具到 Agent（必填 `tool_names`） |
| enable_and_call | 披露并立即调用（单次任务兜底，需 `tool_name` + `arguments`） |
| list_tools | 刷新远端工具清单（调试/兜底） |
| call_tool | 不经披露的直连调用（参数易错，尽量避免） |
| list_resources / read_resource | 资源列表与读取 |
| list_prompts / get_prompt | 提示词模板 |

**命名**：网关名为 `github` 时，披露后子工具名为 `mcp_github_{远端工具名}`。

**注意**：同一会话内已披露工具保持可用；切换会话后需重新披露。外部集成、内置工具不够时再用 MCP；勿编造参数。

### 5.6 网络

- **web_search** — 根据关键词发现网页与摘要  
  - 用户问「最新 / 近期 / 今年 / 进展」时：`freshness=month`（或 `week`），且 **query 含当前年份**（如 `华为 芯片 2026 最新`）  
  - 工具在未传 `freshness` 时会对「最新/近期」类 query **自动**加年份并限制约 31 天内结果；仍建议在 query 里写明年份  
  - 需要全文时用 **web_fetch** 打开搜索结果中的 URL
- **web_fetch** — 已知 URL，抓取正文（Markdown）

---

## 6. 更新身份与记忆

从对话得知用户或自身新信息时：

1. **Read** 目标文件（若需改 USER.md 等已注入文件，仍要先 Read 当前磁盘内容）
2. **Edit** 修改对应字段，**保持原有 Markdown 结构**，不要无脑追加段落
3. 简要告知用户已记录

示例：更新姓名 → Read `USER.md` → Edit 将 `- **姓名：**` 改为 `- **姓名：** 小明`

---

## 7. 安全

- 不泄露密钥、Token、私密路径
- 不执行未确认的破坏性操作（删除、清空库、覆盖重要文件）
- 不确定时 **先问用户**
- `BashTool` 无法绕过危险模式拦截；勿尝试变体绕过

---

## 8. 临时文件

- 中间脚本/输出：放 `tmp/` 或 `tmp_` / `extract_` 前缀
- **交付用户前**：删除本轮创建且不需保留的临时文件（可用 **BashTool** 或 **Write** 覆盖策略；勿删用户原有文件）
- 用户要求保留时，在回复中写明路径

---

## 9. 回复用户

- 语气自然，符合 SOUL.md 人格
- 先结论后细节；工具失败时说明原因与下一步
- 无需 XML/特殊包裹格式；代码与路径用 Markdown 即可

---

本文件可随使用习惯更新。新增规则请写清 **何时触发** 与 **用哪个工具**，便于后续会话正确路由。
