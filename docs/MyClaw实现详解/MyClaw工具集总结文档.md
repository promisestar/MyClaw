# MyClaw Agent 工具集完整说明

> 本文档逐一介绍 MyClaw Agent 当前注册的所有工具，覆盖工具名称、参数、动作、实现要点、元数据（`output_size_hint` / `has_side_effects`）和典型使用场景。工具按注册顺序排列，共 19 个。

---

## 1. 代码检索三件套

### 1.1 Read（文档感知文件读取）

| 属性 | 值 |
|------|-----|
| 工具名 | `Read` |
| 实现 | `DocAwareReadTool`（继承 `hello_agents.ReadTool`） |
| 元数据 | `output_size_hint=5000`（预估输出 token **绝对值**，不随窗口缩放），`has_side_effects=False` |

**与标准 ReadTool 的差异**：继承自 hello_agents 的 `ReadTool`，但针对二进制文档格式（PDF/DOCX/XLSX/PPTX 等）做了增强——当 Agent 尝试读取这类文件时，自动委托 `DocumentExtractor`（底层调用 `markitdown`）提取纯文本，而非像原版那样 `open(encoding='utf-8')` 导致 `UnicodeDecodeError`。纯文本文件和目录列表行为与原版完全一致。

**返回值与 LLM 上下文（重要）**：

Agent 循环把工具结果写入 `messages` 时，只会使用 `ToolResponse.text`（见 `EnhancedSimpleAgent._execute_tool_call`），**不会**自动序列化整个 `data`。上游 `ReadTool` 曾把文件正文放在 `data.content`、`text` 仅有「读取 N 行…」摘要，导致模型看不到文件内容。

MyClaw 的修复：

1. **`DocAwareReadTool`**：纯文本路径在 `super().run()` 后把 `data.content` 合并进 `text`；文档提取路径直接以「摘要头 + 正文」作为 `text`（`data.content` 仍保留兼容）。
2. **执行层兜底**：`EnhancedSimpleAgent._execute_tool_call` 若发现成功响应的 `data.content` 未出现在 `text` 中，会再次合并后再返回给 LLM。

因此对 LLM 可见的典型形态为：

```text
读取 50 行（共 120 行，3456 字节）

<文件正文...>
```

目录列表本身已在父类 `text` 中，无需合并。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `path` | string | 是 | 文件或目录的相对/绝对路径 |
| `offset` | integer | 否 | 起始行号（0-based） |
| `limit` | integer | 否 | 最大行数 |

**适用场景**：读取源码文件、配置文件、Markdown、日志，以及 PDF/PPTX/DOCX/XLSX 等文档。

---

### 1.2 SearchContent（文件内容搜索）

| 属性 | 值 |
|------|-----|
| 工具名 | `search_content` |
| 实现 | `SearchContentTool` |
| 元数据 | `output_size_hint=2000`，`has_side_effects=False` |

基于 **ripgrep（优先）→ 纯 Python 正则（降级）** 的内容搜索。ripgrep 不可用时自动降级为 `re` + `os.walk` 实现，三端通用。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `pattern` | string | 是 | 搜索正则（ripgrep 语法） |
| `path` | string | 否 | 搜索目录（默认工作空间根） |
| `ignore_globs` | string | 否 | 忽略的 glob 模式，逗号分隔（如 `"*.pyc,.git"`） |
| `file_type` | string | 否 | 限定文件类型（如 `"py"`、`"ts"`） |
| `context_after` | integer | 否 | 匹配后显示 N 行 |
| `context_around` | integer | 否 | 匹配前后各显示 N 行 |
| `context_before` | integer | 否 | 匹配前显示 N 行 |
| `output_mode` | string | 否 | 输出模式（`content`/`files_with_matches`/`count`，默认 `content`） |
| `head_limit` | integer | 否 | 限制输出数量 |
| `case_sensitive` | boolean | 否 | 大小写敏感（默认不敏感） |
| `glob` | string | 否 | 文件过滤（rg `--glob`） |

**实现要点**：
- ripgrep 缓存：进程级探测一次 `rg` 路径，后续调用直接复用
- `.gitignore` 感知：自动读取并编译工作空间根目录的 `.gitignore` 规则
- 二进制跳过：检测 `\0` 字节自动跳过二进制文件
- 输出截断：`MAX_OUTPUT = 15000` 字符，防止上下文窗口爆炸

**适用场景**：在代码库中搜索函数定义、API 调用、TODO 标记等。

---

### 1.3 SearchFile（文件名搜索）

| 属性 | 值 |
|------|-----|
| 工具名 | `search_file` |
| 实现 | `SearchFileTool` |
| 元数据 | `output_size_hint=1000`，`has_side_effects=False` |

基于 `pathlib.Path.rglob` + `fnmatch` 的纯 Python 文件名搜索，递归扫描目录，自动跳过 `.git`/`node_modules`/`.venv` 等常见忽略目录。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `pattern` | string | 是 | 文件名 glob 模式（如 `"*.py"`、`"test_*.ts"`） |
| `target_directory` | string | 否 | 搜索目录（默认工作空间根） |
| `recursive` | boolean | 否 | 是否递归（默认 true） |
| `ignore_globs` | string | 否 | 忽略模式，逗号分隔 |
| `case_sensitive` | boolean | 否 | 大小写敏感（默认不敏感） |

**适用场景**：查找特定文件、定位配置文件、批量发现同类文件。

---

### 1.4 ListDir（目录列表）

| 属性 | 值 |
|------|-----|
| 工具名 | `list_dir` |
| 实现 | `ListDirTool` |
| 元数据 | `output_size_hint=1500`，`has_side_effects=False` |

基于 `os.scandir` 的单层目录扫描，返回条目名 + 类型标注（`[DIR]` / `[FILE]` / `[LINK]`），三端通用。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `target_directory` | string | 否 | 要列出的目录（默认工作空间根） |
| `ignore_globs` | string | 否 | 忽略的 glob 模式 |

**适用场景**：快速了解目录结构、发现项目子模块、确认文件存在。

---

## 2. 文件编辑工具

### 2.1 Write（写文件）

| 属性 | 值 |
|------|-----|
| 工具名 | `write` |
| 实现 | `hello_agents.WriteTool` |
| 元数据 | `output_size_hint=500`，`has_side_effects=True` |

直接写入内容到文件，自动创建父目录。支持覆盖写入和追加模式。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `path` | string | 是 | 目标文件相对/绝对路径 |
| `content` | string | 是 | 要写入的内容 |
| `mode` | string | 否 | `overwrite`（默认）或 `append` |

**安全约束**：路径限定在 `project_root` 内，禁止通过 `../` 突破边界。

**适用场景**：创建新文件、写入生成的代码、追加日志。

---

### 2.2 Edit（编辑文件）

| 属性 | 值 |
|------|-----|
| 工具名 | `edit` |
| 实现 | `hello_agents.EditTool` |
| 元数据 | `output_size_hint=800`，`has_side_effects=True` |

精确字符串替换（`old_str` → `new_str`），支持唯一性检测。需要提供足够上下文确保 `old_str` 在文件中唯一，否则会返回 `AMBIGUOUS_MATCH` 错误。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `path` | string | 是 | 目标文件路径 |
| `old_str` | string | 是 | 待替换的原始字符串（必须在文件中唯一） |
| `new_str` | string | 是 | 替换后的字符串 |
| `replace_all` | boolean | 否 | 是否替换所有匹配项（默认仅替换一次） |

**实现要点**：
- 乐观锁：修改前缓存文件 mtime，写入时检查是否被外部修改（`conflict` 错误）
- 唯一性校验：`old_str` 不唯一时报错并列出所有匹配位置，引导 Agent 提供更多上下文
- 元数据缓存：写入后更新 `ToolRegistry` 中的 mtime 缓存

**适用场景**：修改现有文件的特定位置、重构代码、修复 bug。

---

## 3. BashTool（命令执行）

| 属性 | 值 |
|------|-----|
| 工具名 | `execute_command` |
| 实现 | `BashTool` |
| 元数据 | `has_side_effects=False`（可委托子代理隔离执行） |

在工作空间内执行 shell 命令，返回 stdout / stderr / exit_code。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `command` | string | 是 | 要执行的 shell 命令 |
| `workdir` | string | 否 | 工作目录（默认上一次 `cd` 位置或工作空间根） |
| `timeout` | integer | 否 | 超时秒数（默认 120s） |

**安全机制**：
- **黑名单拦截**：18+ 条正则匹配 `rm -rf /`、`sudo`、`mkfs`、`dd`、fork bomb、`curl | sh` 等破坏性命令，击中后返回 `[BLOCKED]`
- **路径白名单**：`allowed_directories` 限制命令执行范围
- **`cd` 追踪**：解析 `cd` 命令并自动维护 `_cwd` 状态，支持链式工作
- **输出截断**：head 6000 + tail 3000，避免超长输出撑爆上下文

**适用场景**：运行脚本、安装依赖、git 操作、编译构建。

---

## 4. Calculator（数学计算）

| 属性 | 值 |
|------|-----|
| 工具名 | `calculator` |
| 实现 | `hello_agents.CalculatorTool` |
| 元数据 | `output_size_hint=100`，`has_side_effects=True`（输出太小，禁止委托） |

安全计算器，支持四则运算、乘方、模运算、括号嵌套。

**适用场景**：复杂算术运算、公式求值、金融数据计算。

---

## 5. WebSearch（网页搜索）

| 属性 | 值 |
|------|-----|
| 工具名 | `web_search` |
| 实现 | `WebSearchTool` |
| 元数据 | `output_size_hint=3000`，`has_side_effects=False` |

调用外部搜索 API（Brave / Tavily / SerpAPI），返回标题+URL+摘要列表。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `query` | string | 是 | 搜索关键词 |
| `max_results` | integer | 否 | 最大结果数（默认 10） |
| `language` | string | 否 | 搜索语言（如 `zh-CN`、`en-US`） |

**配置要求**：需在 `backend/.env` 中配置 `BRAVE_API_KEY` / `TAVILY_API_KEY` / `SERPAPI_API_KEY` 之一。

**适用场景**：搜索实时信息、获取最新文档、事实核查。

---

## 6. WebFetch（网页抓取）

| 属性 | 值 |
|------|-----|
| 工具名 | `web_fetch` |
| 实现 | `WebFetchTool` |
| 元数据 | `output_size_hint=8000`，`has_side_effects=False` |

抓取 HTTP/HTTPS URL 的内容，HTML 自动转为 Markdown，截断至 ~15000 字符。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | 是 | 目标 URL（必须 http/https） |
| `fetch_info` | string | 否 | 描述要提取什么信息（用于 LLM 后处理） |

**实现要点**：`requests` 发 HTTP GET → `html2text`（优先）或 `BeautifulSoup.get_text()` 转换 HTML → 截断 head 10000 + tail 5000。

**适用场景**：阅读网页文章、获取 API 文档、提取网页内容。

---

## 7. HttpRequest（结构化 HTTP 请求）

| 属性 | 值 |
|------|-----|
| 工具名 | `http_request` |
| 实现 | `HttpRequestTool` |
| 元数据 | `output_size_hint=4000`，`has_side_effects=False` |

基于 `httpx` 的结构化 HTTP 客户端，支持任意 HTTP 方法和认证。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `url` | string | 是 | 请求 URL（仅 http/https） |
| `method` | string | 否 | HTTP 方法（GET 默认），支持 POST/PUT/PATCH/DELETE/HEAD/OPTIONS |
| `headers` | string | 否 | JSON 格式请求头 |
| `body` | string | 否 | 请求体（dict 时自动 JSON 序列化 + Content-Type 注入） |
| `auth` | string | 否 | 认证：`{"type":"bearer","token":"xxx"}` 或 `{"type":"basic","username":"u","password":"p"}` |
| `timeout` | number | 否 | 超时秒数（默认 30s） |

**安全约束**：scheme 白名单（仅 http/https），拦截 file/ftp/javascript 等危险协议。响应截断 50KB。

**适用场景**：调用 REST API、Webhook 触发、外部服务集成。

---

## 8. Browser（浏览器自动化）

| 属性 | 值 |
|------|-----|
| 工具名 | `browser` |
| 实现 | `BrowserTool` |
| 元数据 | `output_size_hint=5000`，`has_side_effects=False` |

基于 Playwright 的浏览器自动化，通过 `action` 参数路由 11 种操作。

| 动作 | 关键参数 | 说明 |
|------|---------|------|
| `navigate` | `url` | 导航到 URL |
| `click` | `selector` | CSS 选择器点击 |
| `type` | `selector`, `text` | 在输入框输入文本 |
| `fill` | `selector`, `value` | 填充表单字段 |
| `screenshot` | `full_page` | 页面截图 |
| `evaluate` | `script` | 执行 JavaScript 代码 |
| `text` | — | 获取页面的 `textContent` |
| `press` | `key` | 按键（如 `Enter`、`Escape`） |
| `scroll` | `x`, `y` | 滚动到位置 |
| `wait` | `condition`, `timeout` | 等待条件（`load`/`selector`/`ms`/`response`） |
| `close` | — | 关闭浏览器 |

**实现要点**：
- **线程隔离**：通过 `ThreadPoolExecutor` 在专用线程运行 Playwright sync API，避免与 uvicorn 事件循环冲突
- **懒初始化**：首次 action 时才 `launch()`，减少空闲资源占用
- **Chrome headless**：默认 headless 模式，Linux 自动添加 `--no-sandbox`
- 截图支持 base64 内联返回或保存到 `uploads/`

**适用场景**：网页截图验证、表单自动填写、前端页面内容提取。

---

## 9. Memory（长期记忆）

| 属性 | 值 |
|------|-----|
| 工具名 | 展开为 `memory_search` / `memory_add` |
| 实现 | `MemoryTool`（`expandable=True`） |
| 元数据 | `memory_search`：`output_size_hint=1000`，`has_side_effects=False`；`memory_add`：`output_size_hint=200`，`has_side_effects=True` |

管理 Agent 长期记忆，底层 `MemoryVectorStore`（Qdrant）+ 衰减机制。为减少 LLM 工具选型噪声，**仅向 Agent 暴露检索与写入**；列表/删除/衰减/画像聚合由 lifespan、`/api/memory/*` 与 `ProfileAggregator` 自动路径承担。

| 动作 | 关键参数 | 说明 |
|------|---------|------|
| `memory_search` | `keyword`, `top_k`, `category` | 语义搜索最相关记忆（结果含全文与 ID） |
| `memory_add` | `content`, `category`, `session_id` | 添加记忆条目 |

**实现要点**：
- **自动捕获**：`MemoryCaptureManager` 在每轮对话结束后自动提取要点并存为记忆
- **双路检索**：语义检索（Qdrant）+ 关键词过滤，相关记忆自动注入本轮用户消息前缀
- **衰减机制**：长期未引用的记忆逐渐降低权重；启动时与 HTTP cleanup 触发 `process_decay`
- **去重**：文本 hash + embedding 余弦相似度双重去重

**适用场景**：记住用户偏好、保存项目背景、跨会话知识继承。

---

## 10. RAG（知识库检索引擎）

| 属性 | 值 |
|------|-----|
| 工具名 | `rag` |
| 实现 | `RAGTool` |
| 元数据 | `output_size_hint=3000`，`has_side_effects=True` |

管理用户私有文档知识库：向量化入库、语义检索、LLM 增强问答。

| 动作 | 关键参数 | 说明 |
|------|---------|------|
| `add_document` | `file_path` | 将文档添加到知识库 |
| `ask` | `question` | 基于知识库内容回答问题（检索+LLM增强） |
| `search` | `query`, `top_k` | 仅检索相关片段，不生成回答 |
| `delete_document` | `document_id` | 从知识库删除文档 |
| `list_documents` | — | 列出知识库文档 |
| `status` | — | 知识库状态 |

**实现要点**：
- 文档入库流程：`DocumentExtractor.parse()` → `SentenceTransformer.encode()` → `Qdrant.upsert()`
- 问答流程：`query.encode()` → `Qdrant.search()` → 构造增强 prompt → `LLM.chat()` → 返回答案
- 支持格式：PDF、DOCX、XLSX、TXT、Markdown、CSV、JSON
- 命名空间：支持 `namespace` 参数隔离不同知识库

**与 Memory 的区别**：Memory 记住"关於用户的偏好和背景"；RAG 管理"用户自己的文档和资料库"。

**适用场景**：查询技术文档、分析项目资料、知识库问答。

---

## 11. Task（任务管理）

| 属性 | 值 |
|------|-----|
| 工具名 | `task` |
| 实现 | `TaskTool` |
| 元数据 | `output_size_hint=300`，`has_side_effects=True` |

将复杂用户请求分解为结构化、可追踪的任务步骤，底层 `TaskTracker` 持久化到 `<workspace>/.myclaw/tasks/`。

| 动作 | 关键参数 | 说明 |
|------|---------|------|
| `task_create` | `subject`, `description`, `depends_on` | 创建新任务（支持依赖关系） |
| `task_start` | `task_id` | 标记任务为进行中 |
| `task_complete` | `task_id`, `notes` | 标记任务为已完成 |
| `task_fail` | `task_id`, `notes` | 标记任务失败 |
| `task_cancel` | `task_id` | 取消任务 |
| `task_list` | — | 列出所有任务及状态 |
| `task_progress` | — | 查看进度摘要 |

**子代理协作**：`SubAgentOrchestrator` 在派发子代理前从 `TaskTracker` 读取当前进度并注入子代理提示词。

**适用场景**：多步骤任务分解（如"分析 3 个模块并生成报告"）、进度追踪、依赖管理。

---

## 12. SubAgent（子代理编排）

| 属性 | 值 |
|------|-----|
| 工具名 | `subagent` |
| 实现 | `SubAgentTool` |
| 元数据 | `output_size_hint=2000`，`has_side_effects=False` |

派发上下文隔离的并行子代理执行任务，主 Agent 收摘要而非完整上下文。

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `description` | string | 是 | 任务描述（自然语言） |
| `task_ids` | string | 否 | 关联的 task ID，逗号分隔 |
| `tools` | string | 否 | 工具子集，逗号分隔（如 `"read,search_content,web_search"`） |
| `tool_skip` | string | 否 | 要禁用的工具，不传则继承 master 全部工具 |
| `max_iterations` | integer | 否 | 最大 LLM 迭代轮数（默认 8，主Agent 为 15） |
| `timeout_seconds` | integer | 否 | 超时秒数（动态计算：`30 + 15*tools + 10*iters`，范围 30-300s） |

**实现要点**：
- **上下文隔离**：子代理拥有独立的 64K 上下文窗口（主 Agent 128K）
- **只读并行 + 副作用串行**：无副作用工具（read/search/web_fetch）用 `asyncio.Queue` 并行执行，有副作用工具（write/edit/bash）串行执行
- **超时保护**：动态计算超时（最多 5 分钟），`asyncio.wait_for` + `future.result(timeout=120)` 双重保护
- **禁止递归**：子代理内部 `subagent_enabled=False`，防止子代理再创建子代理
- **摘要回传**：子代理返回 LLM 压缩摘要，而非完整对话历史

**适用场景**：大任务并行分解（如"同时搜索 A、B、C 三个主题并汇总"）、大文件分段处理。

---

## 13. Skill（技能加载）

| 属性 | 值 |
|------|-----|
| 工具名 | `skill` |
| 实现 | `SkillTool` |
| 元数据 | `has_side_effects=False` |

从 `<workspace>/.myclaw/skills/` 或 `~/.helloclaw/skills/` 加载领域专用流程文件（SKILL.md），将其指令注入当前对话上下文。Skill 内容在下一次 LLM 调用时生效（不修改系统提示词，只注入本消息）。

**实现要点**：
- **双层目录**：工作区 Skill（`<workspace>/.myclaw/skills/<name>/SKILL.md`）→ 全局 Skill（`~/.helloclaw/skills/<name>/SKILL.md`）
- **工作区切换时自动刷新**：`bind_workspace()` → `SkillLoader.update_workspace_dir()` → `refresh_skill_tool()`
- **上下文注入模式**：追加到工具输出/用户消息，不影响系统提示词完整性

**适用场景**：按流程处理特定格式文件、调用外部协议、执行标准化工作流。

---

## 14. MCP（外部工具服务）

| 属性 | 值 |
|------|-----|
| 工具名 | 动态（服务器名.工具名） |
| 实现 | `MCPTool` + `MCPWrappedTool` |

通过 [Model Context Protocol](https://spec.modelcontextprotocol.io/) 接入外部工具服务。在 `~/.helloclaw/config.json` 的 `mcp.servers` 中配置外部服务地址，自动发现并注册为 Agent 工具。

**实现要点**：
- **渐进披露**：配置 `auto_expand=true` 时，`ToolRegistry` 自动将 MCP Server 展开为子工具
- **双缓存策略**（计划中）：L1 进程级（memcache）+ L2 文件级（30min TTL）
- **防抖**：3s 内同一工具被连续调用 5 次时自动熔断

**适用场景**：接入第三方 MCP 服务、扩展 Agent 能力边界。

---

## 15. Automation（定时任务）

| 属性 | 值 |
|------|-----|
| 工具名 | `automation` |
| 实现 | `AutomationTool` |
| 元数据 | `has_side_effects=True` |

创建和管理自动化任务，支持三种调度类型。

| 动作 | 关键参数 | 说明 |
|------|---------|------|
| `create` | `name`, `prompt`, `schedule_type`, `schedule_config` | 创建自动化任务 |
| `list` | — | 列出所有任务 |
| `get` | `task_id` | 查看任务详情 + 最近 5 次运行 |
| `delete` | `task_id` | 删除任务 |
| `enable` | `task_id` | 启用任务 |
| `disable` | `task_id` | 禁用任务 |

**调度类型**：
- **once**：一次性执行，`schedule_config={"datetime":"2026-07-29T09:00:00+08:00"}`
- **interval**：固定间隔，`schedule_config={"interval_seconds":3600}`
- **rrule**：iCalendar RRULE 规则，`schedule_config={"rrule":"FREQ=DAILY;BYHOUR=9;BYMINUTE=0"}`

**实现要点**：
- `store_getter` 延迟绑定，支持工作区切换后自动指向新目录
- 运行历史保存在 `automations/` 下，支持前端可视化查询
- 后台 `AutomationScheduler` 轮询到期任务并调用 `AutomationExecutor` 执行

**适用场景**：定时报告生成、周期性数据采集、自动化检查。

---

## 总结速查表

| 工具 | 名称 | 副作用 | output_hint（绝对值 tokens） | 可委托 |
|------|------|--------|------------------------------|--------|
| Read | `Read` | 否 | 5000 | 视窗口动态阈值而定 |
| Write | `Write` | 是 | 500 | ❌ |
| Edit | `Edit` | 是 | 800 | ❌ |
| Calculator | `calculator` | 是 | 100 | ❌ |
| Bash | `execute_command` | 否¹ | 3000 | 视窗口动态阈值而定 |
| Memory | `memory_search` / `memory_add` | add 是 | 1000 / 200 | search ✅ / add ❌ |
| WebSearch | `web_search` | 否 | 4000 | 视窗口动态阈值而定 |
| WebFetch | `web_fetch` | 否 | 8000 | 视窗口动态阈值而定 |
| RAG | `rag` | 分动作 | 5000 | 视窗口动态阈值而定 |
| SearchContent | `search_content` | 否 | 3000 | 视窗口动态阈值而定 |
| SearchFile | `search_file` | 否 | 1000 | 视窗口动态阈值而定 |
| ListDir | `list_dir` | 否 | 1500 | 视窗口动态阈值而定 |
| HttpRequest | `http_request` | 否 | 4000 | 视窗口动态阈值而定 |
| Browser | `browser` | 是（有状态） | 4000 | ❌ |
| Automation | `automation` | 是 | 500 | ❌ |
| Skill | `Skill` | 否 | 2000 | ❌ |
| MCP | 动态 | 取决于外部服务 | 5000 | — |
| SubAgent | `subagent` | 是（元工具） | 500 | ❌ |
| Task | `task` | 是 | 300 | ❌ |
| SessionSearch | `session_search` | 否 | 4000 | 视窗口动态阈值而定 |

> ¹ BashTool 的 `has_side_effects` 以注册时元数据为准；是否委托还取决于 `ContextGuard` 当前 `large_threshold`（随模型上下文窗口缩放）。
>
> **`output_size_hint` 与动态阈值**：hint 表示工具「大概会吐出多少 token」的绝对值，不随窗口放大。`ContextGuard` 的 `small_threshold` / `large_threshold` 按窗口比例重算（128K 基准下为 2000 / 8000）。大窗口提高委派门槛，避免不必要的子代理委派；`ContextManager.tool_snip_chars` 同步按窗口放大，避免 tool 正文刚进上下文就被硬裁剪。

---

**相关文档**：
- [Agent工程设计.md](Agent工程设计.md) — 流式工具并发安全、Context Guard、身份解耦等系统级设计
- [SubAgent与Task系统实现说明.md](SubAgent与Task系统实现说明.md) — 子代理编排与任务管理详解
- [RAG_IMPLEMENTATION.md](RAG_IMPLEMENTATION.md) — RAG 知识库实现
- [Skill系统实现与升级说明.md](Skill系统实现与升级说明.md) — Skill 双层加载
- [MCP工具实现说明.md](MCP工具实现说明.md) — MCP 协议集成
