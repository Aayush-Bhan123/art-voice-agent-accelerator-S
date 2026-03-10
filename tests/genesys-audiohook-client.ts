/**
 * TypeScript test client for ART Genesys AudioHook WebSocket endpoint.
 * Run: npx ts-node tests/genesys-audiohook-client.ts
 * Or:  WSS_URL=wss://artagent-backend-xxx.azurecontainerapps.io/api/v1/genesys/stream npx ts-node tests/genesys-audiohook-client.ts
 *
 * Endpoint (confirm):
 *   Local:   ws://localhost:8010/api/v1/genesys/stream
 *   Deployed: wss://artagent-backend-lr91text.yellowcoast-8f98b9f8.eastus2.azurecontainerapps.io/api/v1/genesys/stream
 */

import WebSocket from "ws";

const DEFAULT_URL =
  process.env.WSS_URL ||
  "ws://localhost:8010/api/v1/genesys/stream";

const sessionId = `ts-${Date.now().toString(36)}`;
let clientSeq = 0;

function sendOpen(ws: WebSocket) {
  const msg = {
    version: "2",
    type: "open",
    id: sessionId,
    seq: ++clientSeq,
    parameters: {
      organizationId: "test-org",
      conversationId: sessionId,
      participant: { id: "test-user", ani: "+1234567890", dnis: "+0987654321" },
      media: [
        { type: "audio", codec: "PCMU", rate: 8000, channels: ["capture", "playback"] },
      ],
    },
  };
  ws.send(JSON.stringify(msg));
  console.log("[TX] open", sessionId);
}

function main() {
  const url = DEFAULT_URL;
  console.log("Connecting to", url, "\n");

  const ws = new WebSocket(url);

  ws.on("open", () => {
    console.log("[WS] Connected");
    sendOpen(ws);
  });

  ws.on("message", (data: WebSocket.Data) => {
    if (Buffer.isBuffer(data) || data instanceof ArrayBuffer) {
      const len = Buffer.isBuffer(data) ? data.length : (data as ArrayBuffer).byteLength;
      console.log("[RX] binary", len, "bytes (PCMU from bot)");
      return;
    }
    const msg = JSON.parse(data.toString());
    console.log("[RX]", msg.type, msg.seq != null ? `seq=${msg.seq}` : "");
    if (msg.type === "opened") {
      console.log("Session OPENED — ready for audio. Send binary PCMU 8kHz to stream mic.");
    }
    if (msg.type === "closed" || msg.type === "pong") {
      // pong is normal; closed = server closed
    }
  });

  ws.on("error", (err) => {
    console.error("[WS] Error:", err.message);
  });

  ws.on("close", (code, reason) => {
    console.log("[WS] Closed", code, reason?.toString() || "");
    process.exit(code === 1000 ? 0 : 1);
  });
}

main();
