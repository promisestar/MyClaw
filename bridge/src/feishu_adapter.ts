#!/usr/bin/env node
/**
 * Feishu adapter for generic bridge.
 *
 * Responsibilities:
 * 1) Receive Feishu events by WebSocket long connection (Lark SDK)
 * 2) Convert inbound Feishu text message to bridge "message"
 * 3) Subscribe bridge "send" and send reply back to Feishu
 */

import { existsSync, readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { WebSocket } from 'ws';
import * as Lark from '@larksuiteoapi/node-sdk';

type ReceiveIdType = 'chat_id' | 'open_id' | 'user_id' | 'union_id' | 'email';

interface BridgeInboundMessage {
  type: 'message';
  id: string;
  sender: string;
  pn: string;
  content: string;
  timestamp: number;
  isGroup: boolean;
  media: string[];
}

interface BridgeSendMessage {
  type: 'send';
  to?: string;
  text?: string;
}

class BridgeClient {
  private ws: WebSocket | null = null;
  private reconnectTimer: NodeJS.Timeout | null = null;
  private queue: string[] = [];

  constructor(
    private wsUrl: string,
    private token: string | undefined,
    private onSend: (msg: BridgeSendMessage) => Promise<void>,
  ) {}

  start(): void {
    this.connect();
  }

  private connect(): void {
    this.ws = new WebSocket(this.wsUrl);

    this.ws.on('open', () => {
      console.log(`🔌 Bridge 已连接: ${this.wsUrl}`);
      if (this.token) {
        this.ws?.send(JSON.stringify({ type: 'auth', token: this.token }));
      }
      this.flushQueue();
    });

    this.ws.on('message', async (data) => {
      try {
        const msg = JSON.parse(data.toString()) as BridgeSendMessage | { type: string };
        if (msg.type === 'send') {
          await this.onSend(msg as BridgeSendMessage);
        }
      } catch (e) {
        console.warn('⚠️ 解析 bridge 消息失败:', e);
      }
    });

    this.ws.on('close', () => {
      console.warn('⚠️ Bridge 连接关闭，准备重连...');
      this.scheduleReconnect();
    });

    this.ws.on('error', (err) => {
      console.warn('⚠️ Bridge 连接错误:', err);
    });
  }

  private scheduleReconnect(): void {
    if (this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, 2000);
  }

  sendInboundMessage(msg: BridgeInboundMessage): void {
    const payload = JSON.stringify(msg);
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(payload);
      return;
    }
    this.queue.push(payload);
  }

  private flushQueue(): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN || this.queue.length === 0) return;
    for (const payload of this.queue) {
      this.ws.send(payload);
    }
    this.queue = [];
  }
}

function parseTextContent(raw: string): string {
  try {
    const c = JSON.parse(raw) as { text?: string };
    return String(c.text || '').trim();
  } catch {
    return '';
  }
}

function loadEnvFile(envPath: string): void {
  if (!existsSync(envPath)) return;
  const content = readFileSync(envPath, 'utf-8');
  const lines = content.split(/\r?\n/);

  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith('#')) continue;
    const eqIndex = trimmed.indexOf('=');
    if (eqIndex <= 0) continue;

    const key = trimmed.slice(0, eqIndex).trim();
    let value = trimmed.slice(eqIndex + 1).trim();

    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }

    // 不覆盖已存在的系统环境变量
    if (process.env[key] === undefined) {
      process.env[key] = value;
    }
  }
}

function parseLoggerLevel(level: string): number {
  switch (level.toLowerCase()) {
    case 'debug':
      return Lark.LoggerLevel.debug;
    case 'info':
      return Lark.LoggerLevel.info;
    case 'warn':
      return Lark.LoggerLevel.warn;
    case 'error':
      return Lark.LoggerLevel.error;
    default:
      return Lark.LoggerLevel.info;
  }
}

function main(): void {
  // 自动加载 bridge/.env（可通过 FEISHU_ENV_FILE 覆盖路径）
  const envPath = process.env.FEISHU_ENV_FILE || resolve(process.cwd(), '.env');
  loadEnvFile(envPath);

  const BRIDGE_URL = process.env.BRIDGE_WS_URL || 'ws://127.0.0.1:3001';
  const BRIDGE_TOKEN = process.env.BRIDGE_TOKEN || undefined;

  const FEISHU_APP_ID = process.env.FEISHU_APP_ID || 'your_feishu_app_id';
  const FEISHU_APP_SECRET = process.env.FEISHU_APP_SECRET || 'your_feishu_app_secret';
  const FEISHU_RECEIVE_ID_TYPE = (process.env.FEISHU_RECEIVE_ID_TYPE || 'chat_id') as ReceiveIdType;
  const FEISHU_WS_LOGGER_LEVEL = process.env.FEISHU_WS_LOGGER_LEVEL || 'info';

  if (
    !FEISHU_APP_ID ||
    !FEISHU_APP_SECRET ||
    FEISHU_APP_ID === 'your_feishu_app_id' ||
    FEISHU_APP_SECRET === 'your_feishu_app_secret'
  ) {
    console.error('❌ 请在 bridge/.env 配置 FEISHU_APP_ID / FEISHU_APP_SECRET');
    process.exit(1);
  }

  const larkConfig = {
    appId: FEISHU_APP_ID,
    appSecret: FEISHU_APP_SECRET,
  };
  const larkClient = new Lark.Client(larkConfig);
  const handledEventIds = new Map<string, number>();

  const bridge = new BridgeClient(BRIDGE_URL, BRIDGE_TOKEN, async (msg) => {
    if (!msg.to || !msg.text) return;
    await larkClient.im.v1.message.create({
      params: { receive_id_type: FEISHU_RECEIVE_ID_TYPE },
      data: {
        receive_id: msg.to,
        msg_type: 'text',
        content: JSON.stringify({ text: msg.text }),
      },
    });
  });
  bridge.start();

  const wsClient = new Lark.WSClient({
    ...larkConfig,
    loggerLevel: parseLoggerLevel(FEISHU_WS_LOGGER_LEVEL),
  });

  wsClient.start({
    eventDispatcher: new Lark.EventDispatcher({}).register({
      'im.message.receive_v1': async (data: any) => {
        try {
          const messageObj = (data?.message || data?.event?.message || {}) as Record<string, unknown>;
          const senderObj = (data?.sender || data?.event?.sender || {}) as Record<string, unknown>;
          const senderIdObj = (senderObj.sender_id || {}) as Record<string, unknown>;
          const headerObj = (data?.header || {}) as Record<string, unknown>;

          const eventId = String(headerObj.event_id || messageObj.message_id || '');
          if (eventId) {
            const now = Date.now();
            if (handledEventIds.has(eventId)) return;
            handledEventIds.set(eventId, now);
            for (const [k, t] of handledEventIds) {
              if (now - t > 10 * 60 * 1000) handledEventIds.delete(k);
            }
          }

          const messageType = String(messageObj.message_type || '');
          if (messageType !== 'text') return;

          const text = parseTextContent(String(messageObj.content || ''));
          if (!text) return;

          const chatId = String(messageObj.chat_id || '');
          const openId = String(senderIdObj.open_id || '');
          if (!chatId) return;

          const createTimeMs = parseInt(String(messageObj.create_time || Date.now()), 10);
          const chatType = String(messageObj.chat_type || '');

          const bridgeMsg: BridgeInboundMessage = {
            type: 'message',
            id: String(messageObj.message_id || eventId || `feishu_${Date.now()}`),
            sender: chatId,
            pn: openId,
            content: text,
            timestamp: Number.isFinite(createTimeMs) ? createTimeMs : Date.now(),
            isGroup: chatType === 'group',
            media: [],
          };
          bridge.sendInboundMessage(bridgeMsg);
        } catch (e) {
          console.error('❌ 处理飞书 WS 事件失败:', e);
        }
      },
    }),
  });

  console.log('🐦 Feishu Adapter started (WS mode)');
  console.log(`- Bridge ws: ${BRIDGE_URL}`);
  console.log(`- Receive ID type: ${FEISHU_RECEIVE_ID_TYPE}`);
  console.log(`- Feishu WS logger: ${FEISHU_WS_LOGGER_LEVEL}`);
}

main();

