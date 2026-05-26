## 1. 前置条件

- Node.js **>= 20**
- npm（或 pnpm/yarn）

## 2. 安装与启动

在 `MyClaw/bridge` 目录执行：

```bash
npm install
npm run build
npm start
```

如需固定环境变量，建议先复制一份：

```bash
cp .env.example .env
```

## 3. 环境变量

| 变量 | 说明 | 默认值 |
|---|---|---|
| `BRIDGE_PORT` | WebSocket 监听端口（只绑定 `127.0.0.1`） | `3001` |
| `AUTH_DIR` | WhatsApp 登录态/密钥目录 | `~/.helloclaw/whatsapp-auth` |
| `BRIDGE_TOKEN` | 可选 token；启用后后端需先发 `{"type":"auth","token":...}` | 空 |

## 4. 后端对接（MyClaw/backend）

后端需要启用外部接收器并连接此 Bridge：

```env
EXTERNAL_BRIDGE_ENABLED=true
EXTERNAL_BRIDGE_URL=ws://127.0.0.1:3001
EXTERNAL_BRIDGE_TOKEN=   # 若 Bridge 设置了 BRIDGE_TOKEN，则这里填同一个
EXTERNAL_BRIDGE_ALLOW_FROM=*
```

启动后端后，后端会作为 WebSocket 客户端连接 Bridge。

## 5. 协议（摘要）

- Bridge → 后端（入站）：
  - `{"type":"message", "id", "sender", "pn", "content", "timestamp", "isGroup", "media?"}`
- 后端 → Bridge（回写）：
  - `{"type":"send", "to", "text"}`

## 6. Feishu Adapter（飞书接入）

本目录提供了 `src/feishu_adapter.ts`，使用飞书官方 Node SDK 的 **WebSocket 长连接**（`WSClient`）接收事件，将消息转换为 Bridge 协议，并将后端回复发回飞书。

### 6.1 启动方式

```bash
npm run build
npm run start:feishu
```

`feishu_adapter` 会自动读取当前目录下 `.env`（可通过 `FEISHU_ENV_FILE` 指定其他路径），因此通常只需配置一次，不必每次启动时 `set` 环境变量。

### 6.2 环境变量（Feishu Adapter）

| 变量 | 说明 | 默认值 |
|---|---|---|
| `BRIDGE_WS_URL` | Bridge WebSocket 地址 | `ws://127.0.0.1:3001` |
| `BRIDGE_TOKEN` | Bridge token（若启用） | 空 |
| `FEISHU_APP_ID` | 飞书应用 App ID（必填） | 无 |
| `FEISHU_APP_SECRET` | 飞书应用 App Secret（必填） | 无 |
| `FEISHU_RECEIVE_ID_TYPE` | 回复消息使用的 receive_id 类型 | `chat_id` |
| `FEISHU_WS_LOGGER_LEVEL` | 飞书 WS SDK 日志级别（`debug/info/warn/error`） | `info` |

### 6.3 消息流向

1. Adapter 与飞书建立 WS 长连接并订阅 `im.message.receive_v1`（文本）  
2. Adapter 转为 Bridge 入站消息：`type="message"`，其中 `sender=chat_id`、`pn=open_id`  
3. 后端处理后回写 Bridge：`type="send"`  
4. Adapter 收到 `send` 后调用飞书 `im/v1/messages` 发回用户/群聊

