# MyClaw

个性化 AI Agent 应用，实现了 **多工作区、意图识别（Ask/Plan/Craft）、DAG 计划执行、用户画像、三通道回忆（长期记忆 + 跨会话原文检索）、RAG、MCP、Skill 自进化、子代理（SubAgent）、定时任务、文件上传、WebSocket 外部接入、多模态输入** 等能力。

![](MyClaw.png)

## 核心架构

```
~/.helloclaw/                      # Agent 基座（全局、进程固定）
├── identity/                      # IDENTITY / USER / SOUL / BOOTSTRAP
├── AGENTS.md                      # 基座行为规范（工作区无 AGENTS 时的 fallback）
├── config.json                    # LLM / MCP / memory / curator 等全局配置
├── workspaces.json                # 工作区授权白名单
├── sessions/                      # 会话 JSON（全局共享）+ index.db（跨会话原文索引）
├── tasks/                         # 任务追踪（全局共享，跨工作区）
├── skills/                        # 全局 Skill
│   ├── .usage.json                # 使用量 / 归属 / 固定保护 / 生命周期遥测
│   ├── .archive/                  # 已归档技能（可恢复）
│   ├── .curator_state             # 生命周期维护器调度状态
│   └── .curator_backups/          # 变更前快照
└── logs/curator/                  # 可选归并审查审计报告

<workspace>/                       # 工作区（可运行时切换）
└── .myclaw/                       # Agent 工作区数据
    ├── AGENTS.md                  # 工作区级行为规范（优先于基座 AGENTS）
    ├── HEARTBEAT.md               # 项目上下文
    ├── uploads/                   # 文件上传
    ├── skills/                    # 工作区 Skill（含 .usage.json / .archive 等）
    └── automations/               # 定时任务存储
```

> **身份与工作区解耦**：人格与画像固定在 `~/.helloclaw/identity/`，项目文件归拢到 `<workspace>/.myclaw/`。**会话与任务为全局存储**，切换工作区时历史不丢失。基座 **system 提示在会话内冻结**（利于 prompt cache）；每轮相关记忆与 Plan 附加段挂在 user 侧临时上下文，不写入会话 JSON。

## 功能概览

| 能力 | 说明 |
|------|------|
| **Agent 模式** | Ask（只读问答）/ Plan（规划→确认→执行）/ Craft（全自动），前端下拉切换 |
| **Plan 执行** | 两阶段：READ_ONLY 生成 TODO → 用户确认 → FULL ReAct；进度摘要经本轮 `turn_context` 注入 |
| **系统提示词** | 会话边界冻结 AGENTS/身份/画像；同会话字节稳定；易变内容（记忆 top-K、Plan 指令）走 user 前缀 |
| 流式对话 | SSE 流式输出；支持编辑历史消息 / 重新生成（仅替换该轮回复，保留后续对话） |
| 多工作区 | 运行时切换工作区，隔离项目文件 / Skill / 定时任务；会话跨工作区共享 |
| **用户画像** | 对话驱动聚合偏好/技术栈等，写入 `USER.md` AUTO 区域，常驻于冻结 system |
| **三通道回忆** | Memory（Qdrant 事实）+ Profile（USER.md）+ Session Recall（`session_search` 跨会话原文） |
| 工具 | 文件读写（含 PDF/DOCX 等）、Shell、计算、网页搜索/抓取、RAG、Skill / skill_manage、MCP |
| **Skill 自进化** | 代理沉淀与局部修订；使用量遥测；生命周期维护器对自主维护技能做闲置→归档 |
| 子代理（SubAgent） | 上下文隔离的并行子任务，工具过滤与摘要返回 |
| 会话 | 多会话管理；全局 `sessions/` + SQLite 全文索引可重建 |
| 定时任务 | 基于 RRULE 的定时/循环任务 |
| Web UI | Vue 3：配置、记忆、会话、工作区、定时任务、工具日志、技能（含维护器状态） |
| 外部通道 | WebSocket Bridge（含飞书适配） |
| 文件上传 / 多模态 | 上传至工作区；聊天直传图片与文档 |
| 工具日志 | 可视化工具调用链路 |

## 设计要点（近期能力）

### 1. Skill 自进化

Skill 将「如何完成某类任务」固化为可版本管理的流程型知识（`SKILL.md` + 可选脚本/模板），与长期记忆（偏好/事实）、知识库（资料原文）分工明确。

在静态加载与人工导入之上增加三层自进化：

1. **技能管理工具 `skill_manage`**：新建、局部修订（优先）、改写、配套文件、查看；删除默认归档。写成功后刷新只读 `Skill` 工具描述。
2. **遥测 `.usage.json`**（工作区/全局各一份）：使用/查看/修订次数、归属、固定保护、生命周期状态。
3. **生命周期维护器**：仅处理已纳入自主维护的技能；活跃 → 闲置 → 已归档；固定保护豁免；变更前可备份。

对话中新建技能默认属用户资产；用户可主动移交维护权。详见 [MyClaw_SKILL实现文档.md](docs/MyClaw实现详解/MyClaw_SKILL实现文档.md)。

### 2. 系统提示词构造（缓存友好）

```
【稳定前缀 — system，会话内冻结】
AGENTS.md（工作区优先，基座 fallback）
+ BOOTSTRAP.md（仅入职未完成）
+ IDENTITY.md / USER.md / SOUL.md
+ 回忆通道静态指引（启用 session_search 时）
───────────────────────
【仅本轮 — user 前缀，不入会话历史】
相关记忆 top-K + Plan 规划指令/进度摘要（若有）
+ 用户本轮原文
```

- **重建时机**：初始化、切换会话、切换工作区、入职完成；同会话连续对话不重读磁盘。
- **目的**：供应商前缀缓存可命中稳定 system；记忆与 Plan 每轮可变但不打断前缀。

### 3. 记忆系统与跨会话检索

| 通道 | 存什么 | 如何进模型 |
|------|--------|------------|
| **Memory** | 短事实/偏好（Qdrant） | 每轮语义 top-K → `turn_context`；也可 `memory_search` |
| **Profile** | 稳定风格画像 | `USER.md` 写入冻结 system |
| **Session Recall** | 历史对话原文 | 全局 sessions + `index.db`；按需 `session_search`，不每轮灌全文 |

禁止把大段 transcript 整段 `memory_add`。详见 [MyClaw_Memory实现文档.md](docs/MyClaw实现详解/MyClaw_Memory实现文档.md)。

## 技术栈

Python · FastAPI · Hello-Agents · uv · Vue 3 · TypeScript · Ant Design Vue · Vite

## 项目结构

```
MyClaw/
├── backend/
│   ├── src/
│   │   ├── agent/                 # Agent 核心
│   │   │   ├── myclaw_agent.py     # 入口：Plan、画像、system 冻结、turn_context、session_search
│   │   │   ├── enhanced_simple_agent.py  # 流式 ReAct + turn_context
│   │   │   ├── enhanced_llm.py
│   │   │   ├── tool_mode_filter.py
│   │   │   ├── todo_scheduler.py
│   │   │   ├── profile_aggregator.py
│   │   │   ├── task_tracker.py
│   │   │   └── subagent/
│   │   ├── api/                   # FastAPI 路由（含 skills curator / pin / adopt 等）
│   │   ├── workspace/
│   │   ├── memory/                # Qdrant 长期记忆 + 衰减
│   │   ├── session_store/         # 跨会话原文索引与检索（FTS5）
│   │   ├── rag/
│   │   ├── tools/builtin/
│   │   │   ├── skill_tool.py           # 只读加载 Skill（正文进 tool_result）
│   │   │   ├── skill_manage_tool.py    # 自进化写入
│   │   │   ├── memory.py
│   │   │   └── ...
│   │   ├── skills/                # SkillLoader、usage、curator、provenance
│   │   ├── automation/
│   │   ├── channels/
│   │   └── context/
│   ├── pyproject.toml
│   └── README.md
├── frontend/
│   ├── src/
│   │   ├── views/                 # 含 SkillsView（用量/固定保护/维护器状态）等
│   │   ├── components/
│   │   ├── stores/
│   │   ├── api/
│   │   └── router/
│   └── package.json
├── bridge/
├── docs/
└── README.md
```

## 快速开始

**环境**：Python 3.10+ · Node.js 18+ · [uv](https://github.com/astral-sh/uv) · pnpm

### 1. 后端

```bash
cd backend
cp .env.example .env   # 配置 LLM_MODEL_ID / LLM_API_KEY / LLM_BASE_URL
uv sync
uv run uvicorn src.main:app --reload --port 8000
```

### 2. 前端

```bash
cd frontend
pnpm install
pnpm dev    # http://localhost:5173
```

### 3. Bridge（可选，外部实时对话）

```bash
cd bridge
cp .env.example .env
npm install && npm run build && npm start
```

在 `backend/.env` 中启用：

```env
EXTERNAL_BRIDGE_ENABLED=true
EXTERNAL_BRIDGE_URL=ws://127.0.0.1:3001
EXTERNAL_BRIDGE_ALLOW_FROM=*
```

详见 [bridge/README.md](bridge/README.md)、[docs/外部软件消息接入说明（External Bridge）.md](docs/外部软件消息接入说明（External Bridge）.md)。

## 配置要点

| 来源 | 内容 |
|------|------|
| `backend/.env` | `LLM_*`、`WORKSPACE_PATH`、Bridge、多模态、端口 |
| `~/.helloclaw/config.json` | 全局 LLM / MCP / `memory`（自动注入）/ `curator`（生命周期维护）/ `session_recall` |
| `~/.helloclaw/identity/` | IDENTITY / USER / SOUL / BOOTSTRAP |
| `~/.helloclaw/skills/` | 全局 Skill 与遥测、归档、维护器状态 |
| `~/.helloclaw/sessions/` | 会话 JSON + `index.db` 跨会话索引 |
| `~/.helloclaw/tasks/` | 任务与 Plan 暂存 |
| `<workspace>/.myclaw/` | uploads、skills、automations、AGENTS.md、HEARTBEAT.md |

**LLM 示例（智谱）**

```env
LLM_MODEL_ID=glm-4-flash
LLM_API_KEY=your-key
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
```

**工作区切换**：前端左上角下拉，或发送时附带 `workspace_path`。首次需在设置页授权目录。

**网页搜索（可选）**：配置 `BRAVE_API_KEY` / `TAVILY_API_KEY` / `SERPAPI_API_KEY` 之一。

配置优先级：`config.json` > `.env` > 代码默认值。

## 扩展能力

- **Agent 模式 / Plan / 用户画像** → [意图识别+用户画像](docs/MyClaw实现详解/MyClaw意图识别+用户画像.md)
- **系统提示词冻结与 turn_context** → [项目说明文档 · 系统提示词](docs/MyClaw项目说明文档.md) · [面试亮点 · 人格系统](docs/MyClaw面试亮点详解.md)
- **三通道回忆（Memory + Session Recall）** → [Memory 实现文档](docs/MyClaw实现详解/MyClaw_Memory实现文档.md)
- **Skill 加载与自进化** → [SKILL 实现文档](docs/MyClaw实现详解/MyClaw_SKILL实现文档.md)
- **多工作区** → [多工作区工程设计](docs/MyClaw实现详解/Agent多工作区设计与实现/多工作区工程设计.md)
- **RAG / MCP / 子代理 / 多模态 / Bridge** → 见下方文档索引

## 主要 API

| 端点 | 说明 |
|------|------|
| `GET /health` | 健康检查 |
| `POST /api/chat/send/stream` | 流式对话（SSE）；`mode` / `plan_confirmed` / `workspace_path` 等 |
| `POST /api/chat/send/sync` | 同步对话 |
| `GET/POST/DELETE /api/session/*` | 会话列表、历史、删除、上下文用量 |
| `GET/PUT /api/config/*` | Agent / LLM / MCP 配置 |
| `GET /api/memory/*` | 记忆查询、管理、清理、统计 |
| `GET /api/skills` | Skill 列表（含用量、固定保护、自主维护标记） |
| `GET/POST /api/skills/curator/*` | 生命周期维护器状态 / 立即运行 / 暂停 / 恢复 |
| `POST /api/skills/{name}/pin\|unpin\|adopt\|archive\|restore` | 固定保护、纳入自主维护、归档恢复 |
| `POST /api/upload/file` | 上传文件 |
| `POST /api/workspace/switch` | 切换工作区 |
| `GET/POST/PUT/DELETE /api/automation/*` | 定时任务 |
| `GET /api/agent/task-progress` | 当前会话任务进度 |
| `GET /api/logs/tool/*` | 工具调用日志 |

## 文档索引

> 实现详解位于 `docs/MyClaw实现详解/`；总览与面试材料见 `docs/` 根下文档。

| 文档 | 主题 |
|------|------|
| [docs/MyClaw项目说明文档.md](docs/MyClaw项目说明文档.md) | 产品总览：工作区、记忆、Skill 自进化、系统提示词等 |
| [docs/MyClaw面试亮点详解.md](docs/MyClaw面试亮点详解.md) | 工程亮点（含 Skill 自进化、三通道回忆、提示词冻结） |
| [docs/MyClaw_Agent能力评测框架.md](docs/MyClaw_Agent能力评测框架.md) | Agent 分层评测（门控/Plan/工具/SSE）；代码见 `backend/eval/` |
| [docs/MyClaw实现详解/MyClaw_Memory实现文档.md](docs/MyClaw实现详解/MyClaw_Memory实现文档.md) | 长期记忆 + 跨会话 Session Recall |
| [docs/MyClaw实现详解/MyClaw_SKILL实现文档.md](docs/MyClaw实现详解/MyClaw_SKILL实现文档.md) | Skill 加载、管理界面与自进化落地 |
| [docs/MyClaw实现详解/MyClaw意图识别+用户画像.md](docs/MyClaw实现详解/MyClaw意图识别+用户画像.md) | Ask/Plan/Craft、Plan 两阶段、画像聚合 |
| [docs/MyClaw实现详解/Agent多工作区设计与实现/多工作区工程设计.md](docs/MyClaw实现详解/Agent多工作区设计与实现/多工作区工程设计.md) | 多工作区与会话全局化 |
| [docs/MyClaw实现详解/MyClaw_RAG实现文档.md](docs/MyClaw实现详解/MyClaw_RAG实现文档.md) | RAG |
| [docs/MyClaw实现详解/MyClaw子代理及Task工具实现文档.md](docs/MyClaw实现详解/MyClaw子代理及Task工具实现文档.md) | 子代理与 Task |
| [docs/MyClaw实现详解/MyClaw_MCP实现文档.md](docs/MyClaw实现详解/MyClaw_MCP实现文档.md) | MCP |
| [docs/MyClaw实现详解/MyClaw多模态输入实现文档.md](docs/MyClaw实现详解/MyClaw多模态输入实现文档.md) | 多模态 |
| [docs/MyClaw实现详解/MyClaw外部通信实现文档.md](docs/MyClaw实现详解/MyClaw外部通信实现文档.md) | 外部通道 |
| [docs/MyClaw实现详解/MyClaw前端实现说明.md](docs/MyClaw实现详解/MyClaw前端实现说明.md) | 前端 |
| [backend/README.md](backend/README.md) | 后端补充说明 |

## 许可证

[MIT License](LICENSE)

## 致谢

[HelloClaw](https://github.com/tino-chen/helloclaw) · [Hello-Agents](https://github.com/hello-agents/hello-agents) · FastAPI · Vue.js · Ant Design Vue
