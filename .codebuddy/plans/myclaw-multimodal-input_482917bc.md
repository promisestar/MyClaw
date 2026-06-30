---
name: myclaw-multimodal-input
overview: 为 MyClaw 扩展多模态输入能力：聊天框支持图片（原生 VLM image_url，base64/URL 双模式）与文档（PDF/Word/Excel/Txt 抽文本注入消息），打通前端附件 UI → 上传 API → Agent 多模态消息 → LLM 视觉调用全链路。
todos:
  - id: explore-llm-layer
    content: 使用 [subagent:code-explorer] 探查 enhanced_llm 与 token_counter 对 list-content 的兼容性，产出改造点清单
    status: completed
  - id: backend-multimodal-core
    content: 新增 backend/src/multimodal/（extractor/image/content_builder）并在 pyproject.toml 加入 pypdf/python-docx/openpyxl/Pillow
    status: completed
    dependencies:
      - explore-llm-layer
  - id: backend-api-extend
    content: 扩展 upload.py 返回字段+新增 /extract 端点；chat.py 的 ChatRequest 增加 attachments 并透传
    status: completed
    dependencies:
      - backend-multimodal-core
  - id: agent-multimodal-pipeline
    content: 改造 myclaw_agent.chat/achat 接收 attachments、构造 list-content，并在 enhanced_llm/token_counter 中兼容透传与估算
    status: completed
    dependencies:
      - backend-api-extend
  - id: main-static-config
    content: 在 main.py 按 MULTIMODAL_IMAGE_MODE 挂载 /files 静态资源，补全 .env.example 与 README 配置说明
    status: completed
    dependencies:
      - agent-multimodal-pipeline
  - id: frontend-input-attachment
    content: 新增 ChatInput.vue + AttachmentChip.vue，实现 📎 按钮/拖拽/预览/先上传后发送，更新 api/upload.ts 与 api/chat.ts 类型
    status: completed
  - id: frontend-message-render
    content: 改造 ChatMessage.vue 渲染图片缩略图与文件卡，stores 消息结构增加 attachments，编辑/重发回填附件
    status: completed
    dependencies:
      - frontend-input-attachment
  - id: session-compat-and-docs
    content: 兼容会话历史 list-content 回放，新增 docs/多模态实现说明.md 并在 README 索引登记
    status: completed
    dependencies:
      - agent-multimodal-pipeline
      - frontend-message-render
---

## Product Overview

为 MyClaw 拓展多模态输入能力。在保留现有文本对话与文件上传通道的基础上，让用户可以在对话中直接附带图片与文档，由 Agent 把它们作为单次消息的一部分送入 LLM 理解，无需手动复制路径。

## Core Features

- **图片输入（VLM 原生多模态）**
- 在聊天输入框新增「📎 附件」按钮并支持拖拽，附件以缩略图/文件卡形式内联展示在用户气泡内
- 单条消息支持多张图片；图片以 OpenAI 兼容多模态格式 `content=[{type:"text"},{type:"image_url",...}]` 直接送入视觉模型（GLM-4V / GPT-4o / Qwen-VL 等）
- 图片传输两种模式可配置：默认 base64 内联；URL 模式下后端 mount 静态目录 `/files/*` 提供可访问 URL
- **文档输入（文本注入）**
- 支持 PDF / Word(.docx) / Excel(.xlsx) / 纯文本(.txt/.md) 四类
- **单文档大小硬上限 10MB**（由 `MULTIMODAL_DOC_MAX_BYTES` 控制，默认 `10*1024*1024`）；超出在上传环节由 `upload.py` 直接返回 `413 Payload Too Large`，前端在 📎 选择/拖拽阶段亦做同样上限的提前校验并提示用户更换更小文档
- 上传时由后端抽取文本，发送对话时把抽出的文本以 `<file name="...">...</file>` 片段拼接到当前用户消息文本部分（10MB 内的文档全文注入，不截断）
- 不自动入 RAG，用户仍可手动调用现有 `rag` 工具入库
- **会话与历史兼容**
- 消息气泡渲染多模态内容：图片缩略图、文件卡片（文件名 + 类型图标 + 大小）
- 历史会话回放兼容旧的「字符串 content」与新的「list content」两种格式
- 编辑 / 重新生成（user_turn_index + regenerate）保留原有附件
- **配置项**
- `MULTIMODAL_IMAGE_MODE`（base64 | url）、`MULTIMODAL_PUBLIC_BASE_URL`、`MULTIMODAL_MAX_IMAGE_MB`、`MULTIMODAL_DOC_MAX_BYTES`（默认 10MB，硬上限拒绝）
- **明确不做**
- 不做 TTS / 文生图 / 视频生成；不动 Bridge 协议；不做图片 OCR；不自动入 RAG

## 技术栈

- 后端：FastAPI、Hello-Agents（`HelloAgentsLLM` OpenAI 兼容协议）、Pydantic v2；新增 `pypdf`、`python-docx`、`openpyxl`、`Pillow`
- 前端：Vue 3 + TypeScript + Ant Design Vue + Vite；继续使用现有 `fetch` 流式 SSE 客户端
- LLM：保留现有 `EnhancedHelloAgentsLLM`，扩展为支持 `content: list[dict]` 多模态 parts 透传

## 实现策略（高层）

基于现有「上传落盘 → Agent 读路径」的链路，在前端、API、Agent、LLM 四层增加「**附件透传**」一条平行通路：

- 前端把附件先 `POST /api/upload/file` 落盘拿到 `stored_path`，发送消息时同时把 `attachments` 列表随 `ChatRequest` 一起发给后端
- 后端 `chat.py` 收到附件后，在调用 `agent.achat / chat` 时把 `attachments` 作为新增参数传入
- `MyClawAgent` 根据附件类型分流：图片 → 构造 `image_url` part；文档 → 调用 `DocumentExtractor` 抽文本拼到 text part；最终把 `content: list[part]` 而不是 `str` 送入 `EnhancedSimpleAgent.run / arun_stream_with_tools`
- LLM 层透传 list-content 给 OpenAI 兼容接口

**关键决策与权衡**：

1. **文档全文注入 + 10MB 字节硬上限**（用户已确认）：实现简单、效果最确定；通过 `MULTIMODAL_DOC_MAX_BYTES`（默认 `10*1024*1024`）在 `upload.py` 写入阶段流式累计 `total` 一旦超限即返回 `413`（复用现有 `_MAX_BYTES` 校验机制），不在 Agent 侧再做内容截断；超大文档由用户自行选择走 RAG
2. **图片 base64 默认**：本地部署最稳；URL 模式下挂载静态资源（`StaticFiles`，仅 `<workspace>/uploads` 目录，路径白名单）满足公网 VLM
3. **多模态 content 在历史中持久化为 list**：与 OpenAI 协议一致，回放零转换；`get_session_history` 增加「list → 给前端的结构化对象」适配，老会话（str content）保持原样兼容
4. **不引入新工具**：用户选择「消息内注入」路线，避免增加 Agent 决策负担，保留 `rag` 工具作为大文档兜底
5. **token 估算兼容**：`token_counter` 目前按字符估算，list-content 拍平为 text 再估算（图片按固定 1024 tokens 占位），避免 ContextManager 异常

## 实现要点（执行细节）

- **复用现有 upload 路径**：不新增上传接口，扩展 `upload.py` 返回字段（`mime_type`、`kind`：image|doc|other、`extracted_chars`）；**文档 kind 在写入阶段适用 `MULTIMODAL_DOC_MAX_BYTES`（10MB）上限**，超限直接 `413`；图片仍走 `UPLOAD_MAX_BYTES`/`MULTIMODAL_MAX_IMAGE_MB`；并新增 `GET /api/upload/extract?path=...` 在发送前按需抽取（避免大文件上传时阻塞）
- **前端预校验**：`ChatInput.vue` 在 📎 选择/拖拽时根据 `kind` 判断阈值：图片用 `MULTIMODAL_MAX_IMAGE_MB`，文档用 10MB；超限直接 toast 拒绝，不发起上传请求
- **DocumentExtractor 单一职责**：`backend/src/multimodal/extractor.py` 暴露 `extract_text(path) -> {text, kind, chars}`，按扩展名分发；解析异常返回安全降级文本（路径 + 错误提示）；不再做字符级截断（大小已在上传期约束）
- **图片处理**：`backend/src/multimodal/image.py` 提供 `to_image_url_part(path, mode, base_url, max_mb) -> dict`；base64 模式自动用 `Pillow` 压缩到 ≤max_mb 并保持纵横比
- **静态资源安全**：URL 模式下挂载 `/files`，仅允许 `<workspace>/uploads` 子路径；启动期校验 `MULTIMODAL_PUBLIC_BASE_URL` 非空才挂载；路径遍历防护（`Path.resolve().relative_to(uploads_root)`）
- **会话保存兼容**：`MyClawAgent.get_session_history` 中识别 `content: list` → 拆分为 `{text, attachments[]}` 返回给前端；保存仍透传原生格式
- **编辑/重新生成保留附件**：`ChatRequest` 在 `regenerate=true` 时，前端从历史消息里复原 `attachments` 一并提交（保证 user_turn_index 那条用户消息的附件不丢）
- **日志/性能**：超过 1MB 的 base64 不打全文，仅打 `len`/`mime`；抽取耗时 > 500ms 用现有 logger 记 `warning`；不在 hot path 重复 stat 文件

## 架构图

```mermaid
flowchart LR
    subgraph FE[Frontend Vue3]
        A[ChatInput 附件按钮 + 拖拽]
        B[ChatMessage 缩略图/文件卡]
        C[api/chat.ts attachments]
        D[api/upload.ts]
    end
    subgraph BE[Backend FastAPI]
        E[upload.py + extract]
        F[chat.py ChatRequest+attachments]
        G[multimodal/extractor.py]
        H[multimodal/image.py]
        I[MyClawAgent.achat]
        J[EnhancedHelloAgentsLLM]
        K[StaticFiles /files]
    end
    A-->D-->E
    A-->C-->F-->I
    E-->G
    I-->H
    I-->G
    I-->J
    K-.URL模式.->J
    B<--SSE--F
```

## 目录结构

```
MyClaw/
├── backend/
│   ├── pyproject.toml                              # [MODIFY] 新增 pypdf / python-docx / openpyxl / Pillow 依赖
│   ├── .env.example                                # [MODIFY] 新增 MULTIMODAL_* 配置示例
│   └── src/
│       ├── main.py                                 # [MODIFY] URL 模式下 mount StaticFiles("/files", workspace/uploads)；从 env 读 MULTIMODAL_*
│       ├── multimodal/                             # [NEW] 多模态处理子包（单一职责）
│       │   ├── __init__.py                         # [NEW] 导出 DocumentExtractor / build_image_part / build_multimodal_content
│       │   ├── extractor.py                        # [NEW] DocumentExtractor.extract_text(path)：按扩展名分发到 _extract_pdf/_extract_docx/_extract_xlsx/_extract_text；异常降级；返回 {text,kind,chars}
│       │   ├── image.py                            # [NEW] build_image_part(path,mode,base_url,max_mb)：base64 用 Pillow 压缩；url 校验白名单后拼接 base_url；返回 OpenAI image_url part
│       │   └── content_builder.py                  # [NEW] build_user_content(text, attachments, config) -> list[dict]：拼装 text part + 文档片段 + image_url parts（不做内容截断，大小已在上传期约束）
│       ├── api/
│       │   ├── upload.py                           # [MODIFY] UploadResponse 增加 mime_type/kind/extracted_chars；文档 kind 写入阶段套用 MULTIMODAL_DOC_MAX_BYTES（10MB），超限 413；新增 /upload/extract 端点（按 stored_path 抽取预览，不入历史）
│       │   └── chat.py                             # [MODIFY] ChatRequest 增加 attachments: List[Attachment]；透传给 agent.chat/achat
│       └── agent/
│           ├── myclaw_agent.py                     # [MODIFY] chat/achat 增加 attachments 形参；调用 build_user_content 构造 list-content；get_session_history 兼容 list content → 结构化输出
│           └── enhanced_llm.py                     # [MODIFY] 确认/补全 list-content 透传路径；token_counter 对 list 拍平估算
├── frontend/
│   └── src/
│       ├── api/
│       │   ├── chat.ts                             # [MODIFY] SendMessageOptions 增加 attachments；StreamEvent done 解析 attachments；body 中传 attachments
│       │   └── upload.ts                           # [MODIFY] UploadResponse 增加 mime_type/kind；新增 extractPreview(path)
│       ├── components/
│       │   ├── ChatInput.vue                       # [NEW or MODIFY] 输入框组件：📎 按钮 + 拖拽落区 + 附件预览条；发送时先上传再随消息提交
│       │   ├── AttachmentChip.vue                  # [NEW] 单个附件卡片（图标/缩略图/文件名/大小/删除）
│       │   └── ChatMessage.vue                     # [MODIFY] 渲染 message.attachments：图片缩略图（点击放大）、文档卡片
│       ├── views/                                  # [MODIFY] 接入 ChatInput 的发送回调（携带 attachments）；编辑/重发时回填附件
│       └── stores/                                 # [MODIFY] 会话消息类型增加 attachments 字段（与后端结构对齐）
└── docs/
    └── 多模态实现说明.md                            # [NEW] 模态范围/协议/配置/兼容性说明，挂到 README 文档索引
```

## 关键接口定义

```python
# backend/src/multimodal/extractor.py
class DocumentExtractor:
    def extract_text(self, path: str) -> dict: ...
    # 返回 {"text": str, "kind": "pdf|docx|xlsx|text|unknown", "chars": int}
    # 注：不做字符截断；文档体积已在 upload.py 通过 MULTIMODAL_DOC_MAX_BYTES (默认 10MB) 硬限

# backend/src/api/chat.py
class Attachment(BaseModel):
    stored_path: str        # 相对工作空间根
    filename: str
    mime_type: str
    kind: Literal["image", "doc", "other"]
    size: int

class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    user_turn_index: Optional[int] = None
    regenerate: bool = False
    skill: Optional[str] = None
    attachments: List[Attachment] = []   # 新增
```

## Agent Extensions

### SubAgent

- **code-explorer**
- Purpose: 深入分析 `EnhancedHelloAgentsLLM` / `HelloAgentsLLM` 对 list-content 的实际透传行为，以及 `ContextManager` / `token_counter` 在 list-content 下的兼容路径
- Expected outcome: 输出明确的 LLM 层改造点清单（需要拍平估算的位置、需要新增的多模态分支），避免 Phase 4 触发回归