# HelloClaw

**HelloClaw** 是一个基于 Hello-Agents 框架的个性化 AI Agent 应用，实现了 OpenClaw 中的核心功能。

![](helloclaw.png)

## 功能特性

- **智能对话** - 基于 ReActAgent 的智能对话能力
- **记忆系统** - 支持长期记忆和每日记忆的自动管理
- **工具调用** - 内置多种工具（文件操作、代码执行、网页搜索等）
- **会话管理** - 多会话支持，会话历史持久化
- **身份定制** - 可自定义 Agent 身份和个性
- **Web 界面** - 现代化的 Vue3 前端界面

## 技术栈

| 层级 | 技术 |
|------|------|
| 后端框架 | Python + FastAPI |
| Agent 框架 | Hello-Agents (ReActAgent) |
| 包管理 | uv |
| 前端框架 | Vue 3 + TypeScript |
| UI 组件 | Ant Design Vue |
| 构建工具 | Vite |

## 项目结构

```
helloclaw/
├── backend/                 # 后端服务
│   ├── src/
│   │   ├── agent/          # Agent 封装
│   │   ├── api/            # FastAPI 路由
│   │   ├── channels/       # Web/CLI 渠道
│   │   ├── cli/            # 命令行工具
│   │   ├── memory/         # 记忆管理
│   │   ├── tools/          # 内置工具
│   │   └── workspace/      # 工作空间管理
│   ├── .env.example        # 环境变量模板
│   └── pyproject.toml      # Python 依赖配置
├── frontend/               # 前端服务
│   ├── src/
│   │   ├── views/          # 页面组件
│   │   ├── components/     # 通用组件
│   │   ├── api/            # API 请求
│   │   └── assets/         # 静态资源
│   └── package.json        # 前端依赖配置
├── tests/                  # 测试脚本
└── CLAUDE.md              # Claude Code 维护文档
```

## 快速开始

### 环境要求

- Python 3.10+
- Node.js 18+
- uv（Python 包管理器）
- pnpm（前端包管理器）

### 安装 uv

```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# 或使用 pip
pip install uv
```

### 安装 pnpm

```bash
npm install -g pnpm
```

### 后端配置

1. 进入后端目录：
```bash
cd backend
```

2. 复制环境变量模板：
```bash
cp .env.example .env
```

3. 编辑 `.env` 文件，配置 LLM 服务：
```env
# LLM 配置（以智谱 AI 为例）
LLM_MODEL_ID=glm-5
LLM_API_KEY=your-api-key-here
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4/

# 服务配置
PORT=8000
CORS_ORIGINS=http://localhost:5173

# 工作空间配置
WORKSPACE_PATH=~/.helloclaw/workspace
```

4. 安装依赖并启动：
```bash
# 安装依赖
uv sync

# 启动服务
uv run python -m uvicorn src.main:app --reload --port 8000
```

### 前端配置

1. 进入前端目录：
```bash
cd frontend
```

2. 安装依赖：
```bash
pnpm install
```

3. 启动开发服务器：
```bash
pnpm dev
```

4. 访问 http://localhost:5173

## 配置说明

### LLM 配置

支持多种 LLM 提供商，修改 `.env` 文件中的配置：

**智谱 AI (GLM)**
```env
LLM_MODEL_ID=glm-5
LLM_API_KEY=your-zhipu-api-key
LLM_BASE_URL=https://open.bigmodel.cn/api/paas/v4/
```

### 配置优先级

1. `~/.helloclaw/config.json` - 全局配置（通过 Web 界面修改）
2. `.env` 环境变量
3. 代码默认值

### 工作空间

工作空间位于 `~/.helloclaw/`，包含：

```
~/.helloclaw/
├── config.json       # 全局 LLM 配置
└── workspace/        # Agent 工作空间
    ├── IDENTITY.md   # 身份配置
    ├── MEMORY.md     # 长期记忆
    ├── SOUL.md       # 灵魂/个性
    ├── USER.md       # 用户信息
    ├── memory/       # 每日记忆
    └── sessions/     # 会话历史
```

## API 接口

| 端点 | 方法 | 描述 |
|------|------|------|
| `/health` | GET | 健康检查 |
| `/api/chat` | POST | 发送消息（SSE 流式） |
| `/api/session/list` | GET | 获取会话列表 |
| `/api/session/create` | POST | 创建新会话 |
| `/api/session/delete` | DELETE | 删除会话 |
| `/api/config/agent/info` | GET | 获取 Agent 信息 |
| `/api/config/llm` | GET/PUT | LLM 配置管理 |
| `/api/memory/files` | GET | 获取记忆文件列表 |
| `/api/memory/content` | GET | 获取记忆内容 |


## 许可证

[MIT License](LICENSE)

## 致谢

- [Hello-Agents](https://github.com/hello-agents/hello-agents) - Agent 框架
- [FastAPI](https://fastapi.tiangolo.com/) - 后端框架
- [Vue.js](https://vuejs.org/) - 前端框架
- [Ant Design Vue](https://antdv.com/) - UI 组件库
