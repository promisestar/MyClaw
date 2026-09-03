# MyClaw 核心能力说明

项目链接：https://github.com/promisestar/MyClaw
---

## 1. 项目背景

MyClaw 是一个**个性化 AI Agent 应用**。目标不是「单次问答」，而是长期伴随用户：能切换项目、记住偏好、按计划做事，并通过 Web UI、定时任务与外部通道（如飞书 Bridge）接入日常工作流。

技术栈：Python · FastAPI · Vue 3 · Qdrant · WebSocket Bridge。

---

## 2. 遇到的核心问题

| 问题 | 表现 |
|------|------|
| **上下文失控** | 工具结果、大文件、网页抓取塞满主对话；system 每轮变化导致缓存失效、成本高 |
| **回忆混乱** | 偏好事实、某次对话原文、用户画像混在一起；整段 transcript 进向量库又贵又不准 |
| **工具选型噪声** | 同类工具过多（如 Memory 十余个子动作）干扰模型选型；读写结果未正确进入 LLM |
| **多项目串线** | 身份与项目文件纠缠；切换工作区后会话/任务丢失或路径锁死 |
| **复杂任务失控** | 全自动易越权改文件；又缺「先规划再确认」的可审计路径 |
| **外部/多模态接入** | 外部 IM 与 Web 聊天协议不同；图片 base64 撑爆会话与 token 估算 |

---

## 3. 解决方案（核心能力）

### 3.1 身份与工作区解耦 + 会话冻结 system

- 人格 / 画像固定在 `~/.helloclaw/identity/`；项目数据在 `<workspace>/.myclaw/`。
- **会话内冻结** AGENTS + 身份进 system（利于 prompt cache）；相关记忆、Plan 指令走本轮 **user `turn_context`**，不污染历史 JSON。

### 3.2 三通道回忆

| 通道 | 存什么 | 如何进模型 |
|------|--------|------------|
| Memory | 短事实 / 偏好（Qdrant） | 每轮 top-K → turn_context；工具仅保留 `memory_search` / `memory_add` |
| Profile | 稳定风格 | `USER.md` AUTO 区写入冻结 system；对话驱动聚合 |
| Session Recall | 历史原文 | `session_search` 按需检索，禁止大段 transcript 灌 Memory |

### 3.3 Ask / Plan / Craft 与上下文守卫

三种模式由用户显式选择（前端 Segmented），后端在 `MyClawAgent.achat` 入口设置 `ToolMode`，并采用 **schema 过滤 + 执行硬拦 + turn_context 提示词** 三层约束，避免「只靠 prompt 劝阻」或「只拦 FINISH 批量路径」导致的漏网。

| 模式 | `ToolMode` | 行为要点 |
|------|------------|----------|
| **Ask** | `READ_ONLY` | 只读工具；注入 `_ask_mode_instruction()`；写工具调用会被拒绝 |
| **Plan（未确认）** | `READ_ONLY` | 注入强化后的 `_plan_planning_instruction()` → 只读调研 → 解析 TODO → `plan_generated` → **结束** |
| **Plan（已确认）** | `FULL` | 恢复 plan → 进度摘要进 turn_context → 全工具执行 |
| **Craft** | `FULL` | 全自动 ReAct，不注入只读禁令 |

**执行硬拦覆盖路径**（`EnhancedSimpleAgent`）：

1. 流式早执行 `_try_execute_ready_tool`（边收边跑，历史上曾绕过门控）
2. FINISH 批量 `_execute_tools_batch`
3. 底层 `_execute_tool_call`（纵深防御）

`SIDE_EFFECT_TOOLS` 含：`Write`/`Edit`/`execute_command`/`bash`/`automation`/`memory_add`。拒绝文案统一含「只读模式」「被禁用」，供评测 scorer 识别。模式指令走 ephemeral **`turn_context`**（与相关记忆拼接），**不写入**冻结 system、不入会话历史。

- **ContextGuard**：按模型窗口动态调整委派阈值；大输出可摘要委托子代理；工具 `output_size_hint` 用绝对值；副作用工具永不委托。
- **Read 正文回传**：修正「只写 data、LLM 只读 text」的断层，保证文件内容进入上下文。

### 3.4 子代理与工具面精简

- SubAgent：隔离上下文 + 线程池并行；主对话只收摘要。
- 工具按场景收敛（Memory 只留检索/写入等），降低选型错误。

### 3.5 多模态与外部 Bridge

- 聊天：图 → `image_url`；文档 → 抽取后 `<file>` 注入 text；历史存 `@FILE:` / `__MM_V1__`，发 LLM 前再物化。
- Bridge：WebSocket 中继统一 `message` / `send`，后端 `ExternalSoftwareReceiver` 调 Agent；飞书等走适配器，不绑死平台。

---

## 4. 最终效果

1. **更懂用户**：偏好进画像、事实进 Memory、原文可检索，跨会话仍连续。
2. **可控执行**：Ask 不改库；Plan 可确认再干；Craft 适合熟练全自动。
3. **上下文更稳**：system 可缓存；大工具输出可裁剪 / 委托；窗口变大时减少误委派。
4. **多项目可用**：切换工作区隔离文件与 Skill，会话与任务全局保留。
5. **可接入日常**：Web UI + 定时任务 + Bridge（含飞书）+ 图文文档直聊。

---
