/**
 * WebSocket server for Python-Node.js bridge communication.
 * Security: binds to 127.0.0.1 only; optional BRIDGE_TOKEN auth.
 */

import { WebSocketServer, WebSocket } from 'ws';

interface SendCommand {
  type: 'send';
  to: string;
  text: string;
}

interface InboundCommand {
  type: 'message';
  id?: string;
  sender?: string;
  pn?: string;
  content?: string;
  timestamp?: number;
  isGroup?: boolean;
  media?: string[];
}

interface BridgeMessage {
  type: 'message' | 'status' | 'error' | 'send';
  [key: string]: unknown;
}

export class BridgeServer {
  private wss: WebSocketServer | null = null;
  private clients: Set<WebSocket> = new Set();

  constructor(private port: number, private token?: string) {}

  async start(): Promise<void> {
    // Bind to localhost only — never expose to external network
    this.wss = new WebSocketServer({ host: '127.0.0.1', port: this.port });
    console.log(`🌉 Bridge server listening on ws://127.0.0.1:${this.port}`);
    console.log('🔁 Relay mode enabled (no WhatsApp dependency)');
    if (this.token) console.log('🔒 Token authentication enabled');

    // Handle WebSocket connections
    this.wss.on('connection', (ws) => {
      if (this.token) {
        // Require auth handshake as first message
        const timeout = setTimeout(() => ws.close(4001, 'Auth timeout'), 5000);
        ws.once('message', (data) => {
          clearTimeout(timeout);
          try {
            const msg = JSON.parse(data.toString());
            if (msg.type === 'auth' && msg.token === this.token) {
              console.log('🔗 Python client authenticated');
              this.setupClient(ws);
            } else {
              ws.close(4003, 'Invalid token');
            }
          } catch {
            ws.close(4003, 'Invalid auth message');
          }
        });
      } else {
        console.log('🔗 Python client connected');
        this.setupClient(ws);
      }
    });
  }

  private setupClient(ws: WebSocket): void {
    this.clients.add(ws);
    ws.send(JSON.stringify({ type: 'status', status: 'connected' }));

    ws.on('message', async (data) => {
      try {
        const cmd = JSON.parse(data.toString()) as SendCommand | InboundCommand;
        await this.handleCommand(ws, cmd);
      } catch (error) {
        console.error('Error handling command:', error);
        ws.send(JSON.stringify({ type: 'error', error: String(error) }));
      }
    });

    ws.on('close', () => {
      console.log('🔌 Python client disconnected');
      this.clients.delete(ws);
    });

    ws.on('error', (error) => {
      console.error('WebSocket error:', error);
      this.clients.delete(ws);
    });
  }

  private async handleCommand(ws: WebSocket, cmd: SendCommand | InboundCommand): Promise<void> {
    if (cmd.type === 'message') {
      const normalized: BridgeMessage = {
        type: 'message',
        id: cmd.id ?? `msg_${Date.now()}`,
        sender: cmd.sender ?? '',
        pn: cmd.pn ?? '',
        content: cmd.content ?? '',
        timestamp: cmd.timestamp ?? Date.now(),
        isGroup: cmd.isGroup ?? false,
        media: cmd.media ?? [],
      };
      // Relay inbound message to all other clients (backend receivers subscribe this)
      this.broadcast(normalized, ws);
      return;
    }

    if (cmd.type === 'send') {
      // Relay outbound message to all other clients (external software subscribes this)
      this.broadcast({
        type: 'send',
        to: cmd.to,
        text: cmd.text,
      }, ws);
      ws.send(JSON.stringify({ type: 'sent', to: cmd.to }));
      return;
    }
  }

  private broadcast(msg: BridgeMessage, exclude?: WebSocket): void {
    const data = JSON.stringify(msg);
    for (const client of this.clients) {
      if (client.readyState === WebSocket.OPEN && client !== exclude) {
        client.send(data);
      }
    }
  }

  async stop(): Promise<void> {
    // Close all client connections
    for (const client of this.clients) {
      client.close();
    }
    this.clients.clear();

    // Close WebSocket server
    if (this.wss) {
      this.wss.close();
      this.wss = null;
    }
  }
}
