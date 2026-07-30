# 外部软件消息接入说明（External Bridge）

本文档说明如何让“外部软件”把消息发送到当前 HelloClaw 后端的 Agent，并拿到回复。

本实现参考了 `nanobot/nanobot/channels` 的设计思想：**外部平台/软件 →（桥接服务）→ 后端接收器 → Agent →（回写）→ 外部平台/软件**。

仓库内 **Bridge 为通用 WebSocket 中继**（见 `MyClaw/bridge/src/server.ts`）；若需接入 **飞书**，可使用同目录下的 **`feishu_adapter`**（`@larksuiteoapi/node-sdk` 的 **WebSocket 长连接**收事件，再与 Bridge 交互）。协议细节另见：[Bridge实现与功能说明](./Bridge实现与功能说明.md)。

---

## 功能概述

你现在获得了一个“外部软件消息接收器”，它会：

- 通过 WebSocket 连接到一个外部桥接服务（Bridge）
- 接收桥接服务推送的入站消息（`type="message"`）
- 将入站消息映射为对 Agent 的一次对话输入（内部调用 `MyClawAgent.achat`）
- 获取最终回复后，回写给桥接服务（`type="send"`）
- 为同一 `chat_id` 固定生成 `session_id`，保证多轮对话上下文连续

对应代码位置：

- **接收器**：`MyClaw/backend/src/channels/external_software_receiver.py`
- **应用启动挂载**：`MyClaw/backend/src/main.py`
- **并发保护（共享锁）**：`MyClaw/backend/src/main.py` + `MyClaw/backend/src/api/chat.py`
- **Bridge（通用中继）**：`MyClaw/bridge/src/server.ts`、`MyClaw/bridge/src/index.ts`
- **飞书适配器（可选）**：`MyClaw/bridge/src/feishu_adapter.ts`（飞书 WS 长连接 ↔ Bridge）

---

## 架构与数据流

### 组件关系图（Obsidian 可渲染 Mermaid）

```mermaid
flowchart LR
    ext[外部软件<br/>你的程序/脚本/平台] -->|WebSocket| bridge[Bridge 服务<br/>协议适配/鉴权/多客户端广播]
    bridge -->|type=message| recv[ExternalSoftwareReceiver<br/>后端接收器]
    recv -->|achat: session_id 固定| agent[MyClawAgent]
    agent -->|最终回复文本| recv
    recv -->|type=send| bridge
    bridge -->|转发/下发| ext
```

### 时序图（从外部消息到回复）

```mermaid
sequenceDiagram
    participant Ext as 外部软件
    participant Bridge as Bridge(WebSocket)
    participant Recv as ExternalSoftwareReceiver
    participant Agent as MyClawAgent

    Ext->>Bridge: 触发/产生消息（平台事件、脚本输入等）
    Bridge-->>Recv: {"type":"message", ...}
    Recv->>Recv: 校验 allow_from / 去重 / 构造 session_id
    Recv->>Agent: achat(content, session_id=sha256(chat_id)[:8])
    Agent-->>Recv: agent_finish.result（最终文本）
    Recv->>Agent: save_current_session()
    Recv-->>Bridge: {"type":"send","to":chat_id,"text":reply}
    Bridge-->>Ext: 将回复发送回外部软件/平台
```

---

## 协议说明（Bridge ↔ 后端接收器）

当前后端接收器对齐 `nanobot` 的 WhatsApp Bridge 协议风格（`type` 字段驱动）。

### 1）Bridge → 后端（入站消息）

接收器只处理 `type="message"` 的消息，其余类型（如 `status/qr/error`）会忽略。

**入站消息最小示例：**

```json
{
  "type": "message",
  "id": "msg_001",
  "sender": "user123@some.chat",
  "pn": "user123@some.chat",
  "content": "你好，帮我总结一下今天的待办",
  "timestamp": 1710000000,
  "isGroup": false,
  "media": []
}
```

字段含义（后端使用方式）：

- **type**：必须为 `"message"`
- **id**：用于去重（同一条消息重复推送会被丢弃）；可选但强烈建议提供
- **sender**：
  - 用作 **chat_id**（回复时的 `to`）
  - 实际上建议它是“可用于回发回复”的目标标识
- **pn**：用于计算 `sender_id`（权限 allowFrom）；优先级：`pn` > `sender`
- **content**：消息正文（将作为 Agent 输入）
- **media**：文件路径数组（可选）
  - 后端会将其附加为 `"[image: path]" / "[file: path]"` 标签拼到 content 里，帮助模型理解上下文

> 语音消息占位：若 `content == "[Voice Message]"`，后端会替换成不可转写的提示文本。

### 2）后端 → Bridge（回写回复）

当后端生成回复后，会向同一条 WebSocket 连接发送：

```json
{
  "type": "send",
  "to": "user123@some.chat",
  "text": "……这里是 Agent 的最终回复……"
}
```

- **to**：等于入站 `sender`（也就是 chat_id）
- **text**：最终回复文本

### 3）可选：鉴权握手（后端 → Bridge）

如果你在 Bridge 端启用了 token 鉴权（参考 nanobot bridge 的做法），后端会在连接建立后第一时间发送：

```json
{"type":"auth","token":"<EXTERNAL_BRIDGE_TOKEN>"}
```

---

## 后端如何启用（HelloClaw Backend）

后端不会默认启动外部接收器，必须显式配置启用：

满足以下任一条件就会启用：

- 设置 **`EXTERNAL_BRIDGE_ENABLED=true`**
- 或设置 **`EXTERNAL_BRIDGE_URL`**（非空）

### 环境变量（后端）

| 变量 | 作用 | 默认值 |
|---|---|---|
| `EXTERNAL_BRIDGE_ENABLED` | 是否启用接收器 | 为空（不启用） |
| `EXTERNAL_BRIDGE_URL` | Bridge 的 WebSocket 地址 | `ws://127.0.0.1:3001`（仅在启用后使用） |
| `EXTERNAL_BRIDGE_TOKEN` | Bridge token（可选） | 空 |
| `EXTERNAL_BRIDGE_ALLOW_FROM` | 允许的 sender_id 列表（逗号分隔） | `*` |
| `EXTERNAL_BRIDGE_CONNECT_TIMEOUT_S` | 连接超时 | `10` |
| `EXTERNAL_BRIDGE_HANDLE_TIMEOUT_S` | 单条消息处理超时 | `120` |

### allow_from（权限）规则

后端会从入站消息派生 `sender_id`，然后执行：

- `EXTERNAL_BRIDGE_ALLOW_FROM` 为空：**拒绝全部**
- 包含 `*`：**允许全部**
- 否则：只有 `sender_id` 在列表内才会处理

`sender_id` 的派生逻辑：

- 优先使用 `pn`，否则使用 `sender`
- 若包含 `@`，取 `@` 前部分作为 `sender_id`

示例：

- `sender="12345@s.whatsapp.net"` → `sender_id="12345"`
- `pn="alice@example.com"` → `sender_id="alice"`

---

## 外部软件如何向当前 Agent 发送消息（详细）

你需要一个 Bridge（桥接服务）。原因是：后端接收器作为 **WebSocket 客户端** 主动连接 Bridge，并从 Bridge 接收入站事件。

外部软件常见接入方式：

1) **自研最小 Bridge**：本机起一个 WebSocket Server，把平台事件转成 `type="message"` 广播给后端（见下方方式 A）  
2) **复用本仓库 Bridge**：使用 `MyClaw/bridge` 的通用中继（方式 B）  
3) **飞书**：使用 `feishu_adapter` 连接飞书长连接与 Bridge（方式 C）

下面给出方式 A（联调脚本）与 B、C（仓库内置能力）的说明。

---

### 方式 A：你自己写一个最小 Bridge（推荐用于联调）

#### A1. Bridge 行为要求

Bridge 需要做到：

- 作为 WebSocket Server 监听一个地址（例如 `ws://127.0.0.1:3001`）
- 接受后端连接（可选 token 鉴权）
- 当外部软件产生消息时，向所有已连接客户端广播 `type="message"` 的 JSON
- 当收到后端发来的 `type="send"` 时，把它“送回外部软件”（联调阶段也可以先打印出来）

#### A2. 外部软件（Bridge）向后端发送消息的 JSON 模板

你只要让 Bridge 给后端推送下面这种结构即可：

```json
{
  "type": "message",
  "id": "your-unique-id",
  "sender": "your-chat-id",
  "pn": "your-sender-id",
  "content": "你要发给 agent 的文本",
  "timestamp": 0,
  "isGroup": false,
  "media": []
}
```

其中最关键的是：

- `type="message"`
- `sender`（后端回复会回写到这个目标）
- `content`
- `id`（用于去重，强烈建议）

#### A3. 用 Node.js 快速实现一个最小 Bridge（示例代码）

> 你可以把它保存为 `bridge-min.js`，用 `node bridge-min.js` 启动。

```js
import { WebSocketServer } from "ws";

const port = process.env.BRIDGE_PORT ? Number(process.env.BRIDGE_PORT) : 3001;
const token = process.env.BRIDGE_TOKEN || "";

const wss = new WebSocketServer({ host: "127.0.0.1", port });
const clients = new Set();

console.log(`bridge listening on ws://127.0.0.1:${port}`);

wss.on("connection", (ws) => {
  clients.add(ws);

  // 可选：token 鉴权（与后端 EXTERNAL_BRIDGE_TOKEN 对齐）
  if (token) {
    const timeout = setTimeout(() => ws.close(4001, "Auth timeout"), 5000);
    ws.once("message", (data) => {
      clearTimeout(timeout);
      try {
        const msg = JSON.parse(data.toString());
        if (msg.type === "auth" && msg.token === token) {
          console.log("backend authenticated");
        } else {
          ws.close(4003, "Invalid token");
        }
      } catch {
        ws.close(4003, "Invalid auth message");
      }
    });
  }

  ws.on("message", (data) => {
    // 收到后端回写的 send
    try {
      const msg = JSON.parse(data.toString());
      if (msg.type === "send") {
        console.log("[from-backend]", msg);
      }
    } catch {
      console.log("[from-backend raw]", String(data));
    }
  });

  ws.on("close", () => clients.delete(ws));
  ws.on("error", () => clients.delete(ws));
});

// 联调：每 10 秒广播一条测试消息给后端
setInterval(() => {
  const inbound = {
    type: "message",
    id: "test_" + Date.now(),
    sender: "demo_chat_1",
    pn: "demo_user_1",
    content: "你好！请用一句话解释什么是 RAG。",
    timestamp: Math.floor(Date.now() / 1000),
    isGroup: false,
    media: [],
  };
  const payload = JSON.stringify(inbound);
  for (const c of clients) c.send(payload);
  console.log("[to-backend]", inbound);
}, 10000);
```

然后在后端设置环境变量启用接收器：

- `EXTERNAL_BRIDGE_ENABLED=true`
- `EXTERNAL_BRIDGE_URL=ws://127.0.0.1:3001`
- （可选）`EXTERNAL_BRIDGE_TOKEN=...`
- （可选）`EXTERNAL_BRIDGE_ALLOW_FROM=*` 或者白名单

---

### 方式 B：复用本仓库的 Bridge（推荐）

`MyClaw/bridge` 当前入口为 **通用 WebSocket 中继**：监听 `127.0.0.1`，在已连接客户端之间转发 `type="message"` / `type="send"`（详见 [Bridge实现与功能说明](./Bridge实现与功能说明.md)）。**不依赖 WhatsApp**。

**后端配置：**

- `EXTERNAL_BRIDGE_ENABLED=true`
- `EXTERNAL_BRIDGE_URL=ws://127.0.0.1:3001`
- 若 Bridge 启用了 token：`EXTERNAL_BRIDGE_TOKEN=<与 BRIDGE_TOKEN 相同>`

**Bridge 启动（MyClaw/bridge）：**

```bash
npm install
npm run build
npm start
```

常用环境变量：

- `BRIDGE_PORT=3001`
- `BRIDGE_TOKEN=...`（可选）

---

### 方式 C：飞书（Feishu）通过 `feishu_adapter`

本仓库提供 **`bridge/src/feishu_adapter.ts`**：使用飞书官方 Node SDK 的 **`WSClient` WebSocket 长连接** 订阅事件（如 `im.message.receive_v1`），将飞书消息转为 Bridge 入站协议，并在收到后端回写的 `type="send"` 时调用飞书 **创建消息** API 发回会话。

#### C1. 数据流（概念）

```mermaid
flowchart LR
    Feishu[飞书 IM] -->|WS 长连接事件| Adapter[feishu_adapter]
    Adapter -->|type=message| Bridge[Bridge 中继]
    Bridge -->|type=message| Recv[ExternalSoftwareReceiver]
    Recv --> Agent[MyClawAgent]
    Agent --> Recv
    Recv -->|type=send| Bridge
    Bridge -->|type=send| Adapter
    Adapter -->|im/v1/messages| Feishu
```

#### C2. 字段映射（与后端一致）

适配器对 **文本消息** 的典型映射为：

| Bridge 字段 | 飞书来源（示例） |
|-------------|------------------|
| `sender` | `message.chat_id`（后端回写 `to` 与此一致，用于私聊/群聊发回） |
| `pn` | `sender.sender_id.open_id`（用于 `EXTERNAL_BRIDGE_ALLOW_FROM` 的 `sender_id` 派生） |
| `id` | `message.message_id` |
| `content` | 解析 `message.content` 中 JSON 的 `text` 字段 |

> **权限提示**：后端默认用 `pn` 优先计算 `sender_id`。飞书 `open_id` 通常带字母，与纯数字白名单不同；联调阶段建议使用 `EXTERNAL_BRIDGE_ALLOW_FROM=*`，上线再按 `open_id` 做白名单。

#### C3. 启动与配置

1. 先启动 **Bridge**（`npm start`），再启动适配器：

```bash
cd MyClaw/bridge
cp .env.example .env
# 编辑 .env：填写 FEISHU_APP_ID、FEISHU_APP_SECRET、BRIDGE_WS_URL 等
npm run build
npm run start:feishu
```

2. 适配器会读取 **`bridge/.env`**（或通过 `FEISHU_ENV_FILE` 指定路径）。常用变量见 `bridge/.env.example`，主要包括：

| 变量 | 说明 |
|------|------|
| `BRIDGE_WS_URL` | Bridge 地址，默认 `ws://127.0.0.1:3001` |
| `BRIDGE_TOKEN` | 若 Bridge 启用鉴权，与后端 `EXTERNAL_BRIDGE_TOKEN` 一致 |
| `FEISHU_APP_ID` / `FEISHU_APP_SECRET` | 飞书自建应用凭证 |
| `FEISHU_RECEIVE_ID_TYPE` | 回发消息使用的 `receive_id` 类型，默认 `chat_id`（与 `sender=chat_id` 一致） |
| `FEISHU_WS_LOGGER_LEVEL` | SDK 日志级别，如 `info` / `debug` |

3. **飞书开发者后台**需：自建应用、机器人能力、订阅 **`im.message.receive_v1`**，且事件订阅方式选择 **「使用长连接接收事件」**（无需再配置 HTTP 回调 URL）。将机器人加入目标群聊或允许私聊后，即可收发。

更完整的 Bridge 行为与协议见：[Bridge实现与功能说明](./Bridge实现与功能说明.md)；适配器实现见：`MyClaw/bridge/src/feishu_adapter.ts`。

---

## 会话与上下文（为什么能多轮对话）

后端使用 `chat_id -> session_id` 的固定映射：

- `session_id = sha256(chat_id)[:8]`

因此：

- 同一个 `sender/chat_id` 发来的多条消息会进入同一个 session
- Agent 会持续加载/追加同一个会话文件，从而保留上下文

---

## 并发与稳定性说明（重要）

由于 `MyClawAgent` 在后端进程内是“全局单例”，内部会维护 `_current_session_id` 等状态。

为避免并发请求导致会话串线，本实现：

- 在 `src/main.py` 创建了全局锁 `_agent_lock`
- 在 HTTP 路由（`api/chat.py`）与外部接收器（`ExternalSoftwareReceiver`）中统一使用这把锁

这意味着同一时刻只会有一个请求/外部消息在驱动 Agent。

---

## 排错指南（快速定位问题）

- **后端没启动接收器**
  - 检查是否设置了 `EXTERNAL_BRIDGE_ENABLED=true` 或 `EXTERNAL_BRIDGE_URL`
  - 查看后端启动日志是否出现 `ExternalSoftwareReceiver started (background)`

- **后端一直连不上 Bridge**
  - 确认 Bridge 监听地址与 `EXTERNAL_BRIDGE_URL` 一致
  - 如果是本机：建议 `ws://127.0.0.1:<port>`

- **消息发送了但后端不处理**
  - 检查 `EXTERNAL_BRIDGE_ALLOW_FROM` 是否限制了 sender_id
  - 检查入站 JSON 是否包含 `type="message"`、`sender`、`content`

- **后端回写了 send，但外部软件收不到**
  - 确认 Bridge 在收到 `type="send"` 时确实有把它发送回外部平台/软件
  - 联调阶段先打印 `send`，确认回写链路已通

- **飞书侧无反应或 adapter 无日志**
  - 确认开发者后台已选择 **长连接** 订阅事件，并已订阅 `im.message.receive_v1`
  - 确认 `FEISHU_APP_ID` / `FEISHU_APP_SECRET` 正确，且机器人已加入会话
  - SDK 调试日志里可能出现 `data: undefined` 之类摘要，属内部 WS 层输出，**不代表**事件体为空；以适配器内实际解析到的 `message` 为准

---

## 版本与兼容性

- 后端 WebSocket 客户端依赖 `websockets` Python 包（当前虚拟环境已包含）
- 接收器不会引入额外第三方日志依赖（使用标准库 `logging`）
- 飞书适配器为 Node 进程，依赖 `@larksuiteoapi/node-sdk`（见 `MyClaw/bridge/package.json`）

