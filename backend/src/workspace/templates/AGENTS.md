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
| 查历史对话/偏好（memory/ 下） | **memory_get** / **memory_search** | `Read` 猜路径 |
| 写入今日见闻 | **memory_add** | 直接改 MEMORY.md（长期用 **memory_update_longterm**） |
| 用户已入库文档（PDF 等） | **rag**（`ask` / `search`） | 仅凭记忆或 `Read` 工作区外的库 |
| 领域标准流程（PDF、专项规范） | **Skill**（先加载再动手） | 凭常识猜步骤 |
| 外部系统（GitHub、Slack 等） | **mcp**（先 `list_tools`） | 编造 API |
| 查公开网络信息（新闻、文档入口） | **web_search** | `web_fetch` |
| 抓取已知 URL 全文 | **web_fetch** | `web_search` |

**文件三连击**：改代码前 **Read** → 小改用 **Edit** → 新建或全文重写用 **Write**。`Edit` 前必须从 **Read** 复制原文（含缩进与换行）。

**信息三连击**：工作区文件 → **Read**；用户知识库 → **rag**；历史偏好 → **memory_***；公网 → **web_search** / **web_fetch**。

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

下列内容**已注入系统提示词**，无需再 Read：`IDENTITY.md`、`USER.md`、`SOUL.md`、`MEMORY.md`。

仍需按需执行：

1. **memory_get** — 今日与昨日 `memory/YYYY-MM-DD.md`
2. **memory_search** — 用户问题涉及过往偏好、人名、项目名时
3. **rag** — 问题依赖「用户上传/入库的资料」时（`ask` 或 `search`）
4. **Skill** — 任务匹配某领域技能（如 PDF）时，**在写代码或改文件之前**加载
5. **mcp** `list_tools` — 需要外部集成且不确定工具名时

---

## 4. 工作区文件

| 文件 | 说明 |
|------|------|
| AGENTS.md | 本指南 |
| IDENTITY.md / USER.md / SOUL.md / MEMORY.md | 已注入，更新时用 Edit |
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

| 工具 | 何时用 |
|------|--------|
| memory_get | 读指定记忆文件或行范围 |
| memory_search | 关键词搜索全部记忆 |
| memory_add | 写入今日日记 |
| memory_update_longterm | 更新长期要点（慎重） |
| memory_list | 查看有哪些记忆文件 |
| memory_cleanup | 清理过期每日记忆（需明确意图） |

长期结构化信息在 **MEMORY.md**（已注入）；流水账在 **memory/日期.md**。

### 5.3 知识库（rag）

`action`：`add_document` | `add_text` | `search` | `ask` | `stats` | `clear`（清空须用户确认 + `confirm=true`）。

- 问「资料里写了什么」→ 优先 **ask** 或 **search**
- 与工作区源码/配置无关的私有文档 → **rag**，不用 **Read** 代替

### 5.4 领域技能（Skill）

- 参数：`skill`（必填，如 `pdf`），`args`（可选，替换 `$ARGUMENTS`）
- **Skill** = 系统预置操作手册；**rag** = 用户已入库文档；可同时使用

### 5.5 MCP（mcp / mcp_*）

| action | 用途 |
|--------|------|
| list_tools | 首次或不确定名称时 **必须先执行** |
| call_tool | 调用远端工具 |
| list_resources / read_resource | 资源列表与读取 |
| list_prompts / get_prompt | 提示词模板 |

外部集成、内置工具不够时再用 MCP；勿编造参数。

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
