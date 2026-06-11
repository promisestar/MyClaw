---
name: knowledge-base-component
overview: 在左侧边栏新增"知识库"组件（会话和记忆之间），点击后在右侧展示知识库文档列表，每个文档项支持打开和删除操作。同时新增后端 API 路由对接 Qdrant 实现文档列举和删除。
todos:
  - id: add-qdrant-scroll
    content: 在 QdrantVectorStore 中新增 get_document_list() 方法，使用 scroll API 分页遍历 RAG chunk 并按 source_path 聚合文档信息
    status: completed
  - id: create-backend-api
    content: 创建 backend/src/api/knowledge_base.py，定义 DocumentInfo/KnowledgeBaseListResponse 模型和 GET list、DELETE document 两个端点
    status: completed
    dependencies:
      - add-qdrant-scroll
  - id: register-backend-route
    content: 在 backend/src/main.py 中注册 knowledge_base.router
    status: completed
    dependencies:
      - create-backend-api
  - id: create-frontend-api
    content: 创建 frontend/src/api/knowledge-base.ts，封装 list() 和 delete() 方法
    status: completed
  - id: create-knowledge-base-view
    content: 创建 frontend/src/views/KnowledgeBaseView.vue，以 SessionsView.vue 为模板实现文档列表展示
    status: completed
    dependencies:
      - create-frontend-api
  - id: update-sidebar-and-router
    content: 修改 frontend/src/App.vue 新增知识库菜单项，修改 frontend/src/router/index.ts 新增路由
    status: completed
    dependencies:
      - create-knowledge-base-view
---

## 用户需求

在 MyClaw 项目中新增"知识库"功能模块，包含前端界面和后端 API：

- 左侧边栏新增"知识库"菜单项，位于"会话"下方、"记忆"上方
- 右侧展示界面列出当前 Qdrant 知识库中的所有文档（按 source_path 聚合）
- 展示样式与会话列表一致，每项显示文档名称和 chunk 数量，并提供"打开"和"删除"按钮
- "打开"按钮跳转到聊天界面（传递文档名作为查询条件）
- "删除"按钮移除该文档的所有 chunk

## 核心功能

1. **知识库文档列表**：从 Qdrant 中按 source_path 聚合，展示每个文档的名称和 chunk 数量
2. **打开文档**：点击打开后跳转到聊天页面，携带文档名参数供后续检索使用
3. **删除文档**：点击删除后，从 Qdrant 中移除该文档所有 chunk，并刷新列表
4. **后端 API**：提供 `GET /knowledge-base/list` 和 `DELETE /knowledge-base/document` 两个端点

## Tech Stack

- 前端：Vue 3 + TypeScript + Ant Design Vue 4 + Vue Router 5 + Axios
- 后端：FastAPI + Pydantic v2 + qdrant-client
- 存储：Qdrant 向量数据库

## 实现策略

### 前端架构

完全复用现有的"会话"组件模式：

1. `App.vue` 在 Menu 中插入新项，使用 Ant Design 的 `FolderOutlined` 图标
2. `router/index.ts` 新增 `/knowledge-base` 懒加载路由
3. `KnowledgeBaseView.vue` 以 `SessionsView.vue` 为模板，复用相同的 Card+List 布局和按钮样式
4. `api/knowledge-base.ts` 参考 `api/session.ts`，封装 `list()` 和 `delete()` 方法

全部遵循现有约定：`.open-btn` / `.delete-btn` 样式、`onMounted` 加载数据、`message` 组件反馈。

### 后端架构

1. `api/knowledge_base.py`：新建 `APIRouter(prefix="/knowledge-base")`，参照 `session.py` 风格
2. `rag/qdrant_store.py`：新增 `get_document_list()` 方法，使用 Qdrant 原生 `scroll()` API 全量遍历 RAG chunk
3. `main.py`：注册新路由 `app.include_router(knowledge_base.router, prefix="/api")`

### 文档聚合策略

Qdrant 中每个 chunk 是一个独立的点。`get_document_list()` 使用 `scroll()` 方法遍历所有满足过滤条件（`memory_type=rag_chunk`、`is_rag_data=True`）的点，按 `source_path` 分组聚合，返回每组的 chunk 计数和元数据。

删除操作使用已存在的 `delete_by_filter({ "source_path": target })` 方法。

## 实现细节

### 前端文件变更

| 文件 | 类型 | 说明 |
| --- | --- | --- |
| `frontend/src/App.vue` | 修改 | 在"会话"和"记忆"之间新增 Menu.Item |
| `frontend/src/router/index.ts` | 修改 | 新增 `/knowledge-base` 路由 |
| `frontend/src/views/KnowledgeBaseView.vue` | 新增 | 知识库文档列表页面 |
| `frontend/src/api/knowledge-base.ts` | 新增 | 知识库 API 模块 |


### 后端文件变更

| 文件 | 类型 | 说明 |
| --- | --- | --- |
| `backend/src/api/knowledge_base.py` | 新增 | 知识库 API 路由 |
| `backend/src/main.py` | 修改 | 注册知识库路由 |
| `backend/src/rag/qdrant_store.py` | 修改 | 新增 `get_document_list()` 方法 |


### 性能考虑

`get_document_list()` 使用 Qdrant scroll API 分页遍历（每页 100 条），避免一次性加载大量点到内存。带 RAG 过滤条件只扫描 RAG 类型数据。时间复杂度 O(N)，N 为 RAG chunk 总数。对于大规模知识库，后续可考虑增加分页参数和缓存层。

### 向后兼容

- 所有新增文件和路由不修改现有接口
- `get_document_list()` 是纯新增方法，不改变现有 QdrantVectorStore 行为
- 前端新增路由不影响现有四个选项卡