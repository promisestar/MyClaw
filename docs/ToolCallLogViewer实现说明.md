# 工具调用日志查看器（Tool Call Log Viewer）

## 概述

工具调用日志查看器用于浏览和检查 Agent 在运行过程中产生的工具调用日志。日志以 `.jsonl` 格式按日期存储在 `~/.helloclaw/tool_logs/` 目录下，通过 Web UI 可以方便地浏览、查看和删除历史日志。

## 功能特性

- **日志文件列表**：按日期倒序展示所有日志文件，显示条目数、文件大小和修改时间
- **日志内容查看**：点击「打开」按钮，弹出 Modal 弹窗展示 JSONL 日志内容，支持上下滚动
- **日志文件删除**：点击「删除」按钮，经 Popconfirm 二次确认后删除对应日期的日志文件
- **安全防护**：路径参数 `date_str` 经正则校验和路径前缀校验，防止路径穿越攻击

## 架构设计

```
┌──────────────────┐     HTTP API      ┌──────────────────┐
│   Frontend (Vue)  │ ◄──────────────► │  Backend (FastAPI)│
│                    │                   │                   │
│  App.vue (菜单)    │                   │  tool_logs.py      │
│  ToolLogsView.vue │                   │  (API Router)     │
│  tool-logs.ts     │                   │                   │
│                    │                   │  tool_logger.py    │
│                    │                   │  (日志引擎)        │
└──────────────────┘                   └────────┬──────────┘
                                                │
                                          ~/.helloclaw/
                                          └─ tool_logs/
                                             ├─ 2026-06-12.jsonl
                                             ├─ 2026-06-11.jsonl
                                             └─ ...
```

## 文件变更

| 文件 | 操作 | 说明 |
|------|------|------|
| `backend/src/logging/tool_logger.py` | 修改 | 新增 `get_log_dir()` 和 `list_files()` 类方法 |
| `backend/src/api/tool_logs.py` | 新建 | API 路由，提供 list/get/delete 端点 |
| `backend/src/main.py` | 修改 | 导入并注册 `tool_logs.router` |
| `frontend/src/api/tool-logs.ts` | 新建 | 前端 API 封装 |
| `frontend/src/views/ToolLogsView.vue` | 新建 | 日志列表页面 + Modal 弹窗组件 |
| `frontend/src/App.vue` | 修改 | 菜单栏新增「工具日志」项 |
| `frontend/src/router/index.ts` | 修改 | 新增 `/tool-logs` 路由 |

## 后端实现

### ToolCallLogger 新增方法

```python
@classmethod
def get_log_dir(cls) -> Path:
    """公开获取日志目录（供 API 使用）。"""

@classmethod
def list_files(cls) -> list[dict]:
    """扫描日志目录，返回所有 JSONL 文件元信息列表（按日期降序）。
    返回字段：date_str, file_name, entry_count, size_bytes, modified_at"""
```

### API 端点

**Base**: `/api/tool-logs`

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/list` | 获取所有日志文件列表 |
| `GET` | `/{date_str}` | 读取指定日期的日志内容（JSONL 原文） |
| `DELETE` | `/{date_str}` | 删除指定日期的日志文件 |

#### GET /api/tool-logs/list

**响应示例**：
```json
{
  "success": true,
  "files": [
    {
      "date_str": "2026-06-12",
      "file_name": "2026-06-12.jsonl",
      "entry_count": 42,
      "size_bytes": 15840,
      "modified_at": "2026-06-12T10:30:00"
    }
  ]
}
```

#### GET /api/tool-logs/{date_str}

**响应示例**：
```json
{
  "success": true,
  "content": "{\"timestamp\":\"...\"}\n{\"timestamp\":\"...\"}\n"
}
```

#### DELETE /api/tool-logs/{date_str}

**响应示例**：
```json
{
  "success": true,
  "message": "已删除日志文件: 2026-06-12.jsonl"
}
```

### 安全措施

```python
# 1. 正则校验 date_str 格式
import re
if not re.match(r'^\d{4}-\d{2}-\d{2}$', date_str):
    raise HTTPException(status_code=400, detail="无效的日期格式")

# 2. Path.resolve() + 前缀校验防路径穿越
file_path = log_dir / date_str).with_suffix(".jsonl").resolve()
if not str(file_path).startswith(str(log_dir.resolve())):
    raise HTTPException(status_code=403, detail="路径穿越被拒绝")
```

## 前端实现

### API 封装 (`tool-logs.ts`)

```typescript
export const toolLogsApi = {
  list: () => api.get('/tool-logs/list'),
  get: (dateStr: string) => api.get(`/tool-logs/${dateStr}`),
  delete: (dateStr: string) => api.delete(`/tool-logs/${dateStr}`),
}
```

### ToolLogsView.vue

- **列表区域**：`a-table` 展示日志文件，含日期、条目数、大小、修改时间四列，以及操作列（打开/删除按钮）
- **Modal 弹窗**：820px 宽，70vh 高度，内嵌 `pre` 块可上下滚动，每条日志展示序号、工具名、耗时、格式化 JSON
- **删除确认**：`a-popconfirm` 二次确认后执行删除

### 菜单与路由

**App.vue** 菜单项追加位置：
```
会话
知识库
工具日志     ← 新增，图标 FileTextOutlined
记忆
设置
```

**router/index.ts** 路由定义：
```typescript
{
  path: '/tool-logs',
  name: 'tool-logs',
  component: () => import('../views/ToolLogsView.vue'),
}
```

## 用户交互流程

1. 点击左侧菜单栏「工具日志」
2. 右侧展示所有日期日志文件的列表
3. 点击某条日志的「打开」→ 弹出 Modal，展示 JSONL 内容，可滚动浏览
4. 点击某条日志的「删除」→ Popconfirm 确认 → 删除文件，自动刷新列表

## 日志格式

单行 JSON 格式（`.jsonl`）：

```json
{
  "timestamp": "2026-06-12T10:30:00.123456",
  "tool_name": "read_file",
  "tool_input": {"filePath": "..."},
  "tool_output": "...",
  "duration_ms": 123.45,
  "status": "success"
}
```

## 技术栈

- **后端**：Python 3.10+, FastAPI, Pydantic
- **前端**：Vue 3, TypeScript, Ant Design Vue 4
- **存储**：本地文件系统 JSONL（`~/.helloclaw/tool_logs/YYYY-MM-DD.jsonl`）
