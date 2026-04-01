#!/usr/bin/env node
/**
 * HelloClaw / MyClaw Generic Bridge
 * 
 * This bridge relays messages between external software and
 * HelloClaw/MyClaw backend via WebSocket.
 * 
 * Usage:
 *   npm run build && npm start
 *   
 * Or with custom settings:
 *   BRIDGE_PORT=3001 npm start
 */

import { BridgeServer } from './server.js';

const PORT = parseInt(process.env.BRIDGE_PORT || '3001', 10);
const TOKEN = process.env.BRIDGE_TOKEN || undefined;

console.log('🐈 HelloClaw / MyClaw Generic Bridge');
console.log('========================\n');

const server = new BridgeServer(PORT, TOKEN);

// Handle graceful shutdown
process.on('SIGINT', async () => {
  console.log('\n\nShutting down...');
  await server.stop();
  process.exit(0);
});

process.on('SIGTERM', async () => {
  await server.stop();
  process.exit(0);
});

// Start the server
server.start().catch((error) => {
  console.error('Failed to start bridge:', error);
  process.exit(1);
});
