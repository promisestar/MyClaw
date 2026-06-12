---
name: tool-call-log-viewer
overview: 在左侧菜单栏知识库与记忆之间新增「工具日志」页面，列表展示各日期日志文件，打开时弹出 Modal 显示 JSONL 内容，支持删除日志文件。
todos:
  - id: add-tool-logger-methods
    content: 在 ToolCallLogger 中新增 list_log_files() 和 read_log_file() 两个静态方法
    status: completed
  - id: create-backend-api
    content: 创建 backend/src/api/tool_logs.py，定义 GET list、GET read、DELETE delete 三个端点
    status: completed
    dependencies:
      - add-tool-logger-methods
  - id: register-backend-route
    content: 在 backend/src/main.py 中 import 并注册 tool_logs.router
    status: completed
    dependencies:
      - create-backend-api
  - id: create-frontend-api
    content: 创建 frontend/src/api/tool-logs.ts，封装 list()、read()、delete() 方法
    status: completed
  - id: create-tool-logs-view
    content: 创建 frontend/src/views/ToolLogsView.vue，实现日志文件列表和 Modal 弹窗查看功能
    status: completed
    dependencies:
      - create-frontend-api
  - id: update-sidebar-and-router
    content: 修改 App.vue 新增工具日志菜单项，修改 router/index.ts 新增 /tool-logs 路由
    status: completed
---

## 用户需求

在左侧菜单栏「知识库」和「记忆」之间新增「工具日志」菜单项。点击后右侧展示一个列表页面，列出按日期分组的工具调用日志文件（如 2026-06-12.jsonl）。每个日志文件项包含「打开」和「删除」按钮：

- **打开**：弹出 Modal 弹窗，展示该 JSONL 日志文件的完整内容，用户可以上下滚动逐行检查每条工具调用的详细信息（时间、工具名、入参、出参、耗时、状态等）
- **删除**：带二次确认，删除对应的日志文件，删除后刷新列表

## 产品概述

为 MyClaw 个人 Agent 助手新增工具调用日志的可视化管理能力。用户可在 Web UI 中按日期浏览历史工具调用记录，通过 Modal 弹窗检查每次调用的详情，并删除不再需要的日志文件。这是已实现的结构化日志（JSONL）的前端可视化补充。

## 核心功能

- 日志文件列表：展示 `~/.helloclaw/tool_logs/` 目录下所有 `.jsonl` 文件，按日期倒序排列，显示文件名和文件大小
- Modal 弹窗查看：点击「打开」弹出全屏 Modal，以等宽字体逐行展示 JSONL 内容（带语法高亮），支持上下滚动
- 删除日志文件：带 Popconfirm 二次确认，删除后自动刷新列表

## 技术栈

- 后端：Python + FastAPI + Pydantic v2（APIRouter 模式）
- 前端：Vue 3 + TypeScript + Ant Design Vue（Modal、Card、List、Popconfirm）
- 存储：文件系统（`~/.helloclaw/tool_logs/*.jsonl`）

## 实现方案

### 后端 API 设计

新建 `backend/src/api/tool_logs.py`，参照 `knowledge_base.py` 的风格（APIRouter + Pydantic 模型 + 辅助函数），提供 3 个端点：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/tool-logs/list` | 列出所有 JSONL 日志文件，返回文件名、日期、文件大小 |
| `GET` | `/api/tool-logs/read?date=2026-06-12` | 读取指定日期日志文件的完整 JSONL 内容（返回字符串数组，每行一个 JSON 对象） |
| `DELETE` | `/api/tool-logs/delete?date=2026-06-12` | 删除指定日期的日志文件 |


关键设计决策：

- 日志目录通过 `ToolCallLogger._ensure_log_dir()` 获取（复用已有逻辑，包括 `TOOL_LOG_DIR` 环境变量支持）
- 文件操作使用 `pathlib.Path.glob("*.jsonl")` 扫描目录
- 读取文件时直接 `read_text(encoding="utf-8")` 返回完整内容（JSONL 文件通常不大，无需分页）
- 删除前校验日期格式，防止路径穿越

### 前端设计

参照 `KnowledgeBaseView.vue` 和 `SessionsView.vue` 模式实现，核心差异在于：

1. **打开逻辑**：不跳转路由，改为打开 `Modal` 组件展示 JSONL 内容
2. **Modal 弹窗**：使用 `a-modal` 全屏宽度（width=80%），内容区域用 `<pre>` 标签 + 等宽字体展示原始 JSONL 文本，`max-height: 70vh` + `overflow-y: auto` 支持滚动

### 架构设计

```
App.vue (新增菜单项 FileTextOutlined)
  └─ RouterView
       └─ ToolLogsView.vue
            ├─ Card + List (日志文件列表)
            │    ├─ 打开按钮 → modalOpen = true, loadLogContent()
            │    └─ 删除按钮 → Popconfirm → deleteLogFile()
            └─ Modal (JSONL 内容查看)
                 └─ <pre> 标签展示原始文本
```

### 目录结构

```
backend/src/
├── api/
│   └── tool_logs.py              # [NEW] 工具日志 API 路由
├── logging/
│   └── tool_logger.py            # [MODIFY] 新增 list_log_files() 和 read_log_file() 静态方法
└── main.py                        # [MODIFY] import + include_router

frontend/src/
├── api/
│   └── tool-logs.ts              # [NEW] 前端 API 封装
├── views/
│   └── ToolLogsView.vue          # [NEW] 日志列表页 + Modal 弹窗
├── App.vue                        # [MODIFY] 新增菜单项（知识库和记忆之间）
└── router/
    └── index.ts                   # [MODIFY] 新增 /tool-logs 路由
```

### 实现细节

**ToolCallLogger 新增方法**（`logging/tool_logger.py`）：

- `list_log_files()` → 返回 `list[dict]`，每项含 `filename`、`date`、`size_bytes`
- `read_log_file(date_str)` → 返回文件完整文本内容

**安全检查**：

- 日期参数用正则 `^\d{4}-\d{2}-\d{2} 校验，防止路径穿越
- 文件路径限定在 `_ensure_log_dir()` 目录内

**性能考虑**：

- JSONL 文件通常每条约 500-2000 字符，一天最多几百条，总量可控，无需流式读取
- Modal 内容使用 `<pre>` 标签直接渲染文本，避免逐行 JSON.parse 带来的解析开销