# MyClaw

基于 [Hello-Agents](https://github.com/hello-agents/hello-agents) 的个性化 AI Agent 应用，在 [HelloClaw](https://github.com/tino-chen/helloclaw) 能力之上扩展 **RAG、MCP、Skill、文件上传、WebSocket 外部接入** 等能力。

![](MyClaw.png)

## 功能概览

| 能力 | 说明 |
|------|------|
| 流式对话 | SSE 流式输出；支持**编辑历史消息 / 重新生成**（仅替换该轮回复，保留后续对话） |
| 记忆 | 长期记忆 + 每日记忆，自动捕获与检索 |
| 工具 | 文件读写、Shell、计算器、网页搜索/抓取、RAG、Skill、MCP |
| 会话 | 多会话、历史持久化（`sessions/`） |
| Web UI | Vue 3 配置、记忆、会话管理 |
| 外部通道 | WebSocket Bridge（含飞书等适配）；HTTP 与 Bridge 共用 Agent 锁串行处理 |
| 文件上传 | 保存至工作空间 `uploads/`，可在对话中引用路径或入库 RAG |
| 多模态输入 | 聊天直传图片（VLM `image_url`，base64/URL 双模式）与文档（PDF/DOCX/XLSX/TXT 抽文本注入，单文档 ≤10MB） |

## 技术栈

Python · FastAPI · Hello-Agents · uv · Vue 3 · TypeScript · Ant Design Vue · Vite

## 项目结构

```
MyClaw/
├── backend/          # FastAPI + Agent（src/agent, api, rag, memory, tools, workspace）
├── frontend/         # Vue 3 前端
├── bridge/           # WebSocket 中继（外部软件 / 飞书等 → 后端）
├── docs/             # 实现与设计文档（RAG、Memory、Bridge、前端等）
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
cp .env.example .env    # 按需配置
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
| `backend/.env` | LLM、端口、工作空间、Bridge、上传大小、**多模态（`MULTIMODAL_*`）** 等 |
| `~/.helloclaw/config.json` | 全局 LLM / **MCP**（Web 配置页可改） |
| `~/.helloclaw/workspace/` | Agent 工作区：`IDENTITY.md`、`MEMORY.md`、`memory/`、`sessions/` 等 |

**LLM 示例（智谱）**

```env
LLM_MODEL_ID=glm-4-flash
LLM_API_KEY=your-key
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
```

**网页搜索（可选）**：在 `.env` 中配置 `BRAVE_API_KEY` / `TAVILY_API_KEY` / `SERPAPI_API_KEY` 之一。

配置优先级：`config.json` > `.env` > 代码默认值。

## 扩展能力（简要）

- **RAG**：对用户已入库文档检索/问答（工具 `rag`，action：`add_document` / `search` / `ask` 等）→ [docs/RAG_IMPLEMENTATION.md](docs/RAG_IMPLEMENTATION.md)
- **Skill**：加载 `backend/skills/<name>/SKILL.md` 领域流程 → 工作区 `AGENTS.md`
- **MCP**：在 `config.json` 的 `mcp.servers` 配置外部服务；支持 `auto_expand` → [docs/MCP工具实现说明.md](docs/MCP工具实现说明.md)
- **上传**：`POST /api/upload/file`（`multipart/form-data`，字段 `file`、可选 `session_id`）

## 主要 API

| 端点 | 说明 |
|------|------|
| `GET /health` | 健康检查 |
| `POST /api/chat/send/stream` | 流式对话（SSE）；可选 `user_turn_index` + `regenerate` 编辑/重答 |
| `POST /api/chat/send/sync` | 同步对话 |
| `GET/POST /api/session/*` | 会话列表、创建、历史、删除 |
| `GET/PUT /api/config/*` | Agent / LLM 配置 |
| `GET /api/memory/*` | 记忆文件与内容 |
| `POST /api/upload/file` | 上传文件到工作空间 |

## 文档索引

| 文档 | 主题 |
|------|------|
| [docs/HelloClaw-Backend-请求执行流程.md](docs/HelloClaw-Backend-请求执行流程.md) | 请求与 Agent 执行链路 |
| [docs/前端实现说明.md](docs/前端实现说明.md) | 前端架构与页面 |
| [docs/Memory实现与功能说明.md](docs/Memory实现与功能说明.md) | 记忆系统 |
| [docs/Bridge实现与功能说明.md](docs/Bridge实现与功能说明.md) | Bridge 架构 |
| [docs/多模态实现说明.md](docs/多模态实现说明.md) | 多模态输入（图片 + 文档）协议与实现 |
| [backend/README.md](backend/README.md) | 后端补充说明 |

## 许可证

[MIT License](LICENSE)

## 致谢

[HelloClaw](https://github.com/tino-chen/helloclaw) · [Hello-Agents](https://github.com/hello-agents/hello-agents) · FastAPI · Vue.js · Ant Design Vue
