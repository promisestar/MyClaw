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

