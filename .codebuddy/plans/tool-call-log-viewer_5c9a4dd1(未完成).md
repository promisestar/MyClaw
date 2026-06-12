---
name: tool-call-log-viewer
overview: 在左侧菜单知识库与记忆之间新增「工具日志」页面，列表展示各日期日志文件（如2026-06-12.jsonl），打开弹出Modal展示JSONL内容，支持删除。
todos:
  - id: add-logger-methods
    content: 在 ToolCallLogger 中新增 get_log_dir() 和 list_files() 类方法，供 API 调用
    status: completed
  - id: create-backend-api
    content: 创建 backend/src/api/tool_logs.py，实现 GET list、GET date_str、DELETE date_str 三个端点
    status: completed
    dependencies:
      - add-logger-methods
  - id: register-backend-route
    content: 在 backend/src/main.py 中导入并注册 tool_logs.router
    status: completed
    dependencies:
      - create-backend-api
  - id: create-frontend-api
    content: 创建 frontend/src/api/tool-logs.ts，封装 list/get/delete 方法
    status: completed
  - id: create-log-viewer-view
    content: 创建 frontend/src/views/ToolLogsView.vue，实现日志文件列表、Modal 查看弹窗、删除功能
    status: completed
    dependencies:
      - create-frontend-api
  - id: update-sidebar-and-router
    content: 修改 App.vue 新增菜单项、修改 router/index.ts 新增 /tool-logs 路由
    status: in_progress
    dependencies:
      - create-log-viewer-view
---

## 产品概述

在前端左侧菜单栏「知识库」和「记忆」之间新增「工具日志」页面，提供工具调用日志文件的浏览、查看和删除功能。日志文件按日期存储在 `~/.helloclaw/tool_logs/YYYY-MM-DD.jsonl`，每条日志为一行 JSON 记录（JSONL 格式）。

## 核心功能

- **日志文件列表**：展示所有日期日志文件，每项显示文件名（如 2026-06-12.jsonl）、记录条数、文件大小、最后修改时间
- **打开查看**：点击「打开」弹出 Modal 弹窗，展示该日志文件的完整 JSONL 内容（每行 JSON 格式化输出），用户可上下滚动逐条检查工具调用详情
- **删除日志**：点击「删除」触发 Popconfirm 二次确认，确认后删除对应日志文件并刷新列表
- **空状态**：无日志文件时显示占位提示

## 技术栈

- 后端：Python + FastAPI + Pydantic v2 + `ToolCallLogger`（已有类）
- 前端：Vue 3 + TypeScript + Ant Design Vue + Axios + Vite

## 实现方案

### 后端新增 API (`backend/src/api/tool_logs.py`)

参考 `knowledge_base.py` 的模式，新增 FastAPI APIRouter，提供三个端点：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/api/tool-logs/list` | 扫描日志目录，返回所有 `*.jsonl` 文件元信息列表 |
| `GET` | `/api/tool-logs/{date_str}` | 读取指定日期 JSONL 文件，返回解析后的 JSON 对象数组 |
| `DELETE` | `/api/tool-logs/{date_str}` | 删除指定日期的日志文件 |


**列表端点设计**：

- 复用 `ToolCallLogger._ensure_log_dir()` 获取日志目录路径
- 遍历 `*.jsonl` 文件，读取每个文件的记录数（统计非空 JSON 行）、文件大小、最后修改时间
- 按文件名降序排列（新日志在前）
- `date_str` 从文件名提取（去掉 `.jsonl` 后缀）

**读取端点设计**：

- 读取完整 JSONL 文件，逐行 `json.loads` 解析
- 返回 `List[dict]` 格式，前端直接展示
- 文件不存在时返回 404

**删除端点设计**：

- 校验 `date_str` 防止路径穿越（使用 `ToolCallLogger._ensure_log_dir()` 拼接 + `resolve()` 校验）
- 删除文件，文件不存在时返回 404

### 存储层扩展 (`backend/src/logging/tool_logger.py`)

新增两个类方法供 API 使用：

- `get_log_dir()`：返回日志目录路径（复用 `_ensure_log_dir()`）
- `list_files()`：扫描日志目录，返回文件信息列表

### 前端实现

**API 层** (`frontend/src/api/tool-logs.ts`)：

- `toolLogsApi.list()` - GET `/tool-logs/list`
- `toolLogsApi.get(dateStr)` - GET `/tool-logs/{date_str}`
- `toolLogsApi.delete(dateStr)` - DELETE `/tool-logs/{date_str}`

**视图组件** (`frontend/src/views/ToolLogsView.vue`)：

- 复用 `KnowledgeBaseView.vue` 的布局和样式模式（Card + List + 打开/删除按钮 + Popconfirm + 空状态）
- 列表项展示：文件名（等宽字体）、记录条数、文件大小、最后修改时间
- **打开按钮**：调用 `toolLogsApi.get(dateStr)` 获取内容 → 将每行 JSON 格式化 → 打开 Modal 弹窗
- **Modal 弹窗**：使用 `Ant Design Vue` 的 `Modal` 组件，内容区域设置 `max-height: 70vh` + `overflow-y: auto` 可滚动，每行条目用 `<pre>` 标签 + `JSON.stringify(entry, null, 2)` 格式化展示
- **删除按钮**：`Popconfirm` 确认 → `toolLogsApi.delete(dateStr)` → 刷新列表

**菜单栏** (`frontend/src/App.vue`)：

- 在知识库 `<Menu.Item key="knowledge-base">` 和记忆 `<Menu.Item key="memory">` 之间插入 `<Menu.Item key="tool-logs">`
- 图标使用 `FileTextOutlined`（从 `@ant-design/icons-vue` 导入）

**路由** (`frontend/src/router/index.ts`)：

- 在 `/knowledge-base` 路由之后、`/memory` 路由之前插入 `/tool-logs` 路由
- 组件懒加载 `() => import('../views/ToolLogsView.vue')`

### 关键设计

- 路径安全：删除时确保 `date_str` 只包含安全字符（`YYYY-MM-DD` 格式），使用 `Path.resolve()` 校验最终路径在日志目录内
- Modal 性能：JSONL 文件可能较大，展示时不做分页但限制 Modal 最大高度 70vh + 滚动，每条 JSON 默认折叠（也可全部展开）
- 样式一致性：复用 `.open-btn` / `.delete-btn` / `.empty-card` 等全局样式模式

## 目录结构

```
backend/
├── src/
│   ├── api/
│   │   └── tool_logs.py          # [NEW] 日志文件列表、读取、删除 API
│   ├── logging/
│   │   └── tool_logger.py        # [MODIFY] 新增 get_log_dir() 和 list_files() 类方法
│   └── main.py                   # [MODIFY] 导入并注册 tool_logs.router

frontend/
├── src/
│   ├── api/
│   │   └── tool-logs.ts          # [NEW] 工具日志 API 封装
│   ├── views/
│   │   └── ToolLogsView.vue      # [NEW] 工具日志列表页 + Modal 查看弹窗
│   ├── App.vue                   # [MODIFY] 新增菜单项
│   └── router/
│       └── index.ts              # [MODIFY] 新增路由
```