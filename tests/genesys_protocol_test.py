#!/usr/bin/env python3
"""
Minimal Genesys protocol test - runs without Redis/Azure.
Serves OPEN→OPENED, PING→PONG, binary PCMU passthrough.
Use: python tests/genesys_protocol_test.py
Then open http://localhost:8011/api/v1/genesys/test in browser.
"""
import asyncio
import base64
import json
import uuid
from pathlib import Path

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
import uvicorn

app = FastAPI()
GENESYS_OPEN = "open"
GENESYS_OPENED = "opened"
GENESYS_PING = "ping"
GENESYS_PONG = "pong"
GENESYS_CLOSE = "close"
GENESYS_CLOSED = "closed"


def _genesys_msg(msg_type: str, session_id: str, seq: int, clientseq: int, params: dict | None = None):
    return {"version": "2", "type": msg_type, "seq": seq, "clientseq": clientseq, "id": session_id, "parameters": params or {}}


@app.get("/api/v1/genesys/test", response_class=HTMLResponse)
async def test_page():
    html_path = Path(__file__).parent / "genesys_test.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>Test page not found</h1>", status_code=404)


@app.websocket("/api/v1/genesys/stream")
async def genesys_stream(websocket: WebSocket):
    await websocket.accept()
    session_id = f"test_{uuid.uuid4().hex[:8]}"
    server_seq = 0
    client_seq = 0
    open_received = False

    try:
        while True:
            raw = await websocket.receive()
            if raw.get("type") == "websocket.disconnect":
                break
            if raw.get("type") != "websocket.receive":
                continue

            text = raw.get("text")
            binary = raw.get("bytes")

            if text:
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError:
                    continue
                msg_type = msg.get("type")
                client_seq = msg.get("seq", client_seq)
                if msg.get("id"):
                    session_id = msg["id"]

                if msg_type == GENESYS_OPEN:
                    server_seq += 1
                    await websocket.send_json(_genesys_msg(
                        GENESYS_OPENED, session_id, server_seq, client_seq,
                        {"startPaused": False, "media": [{"type": "audio", "codec": "PCMU", "rate": 8000, "channels": ["capture", "playback"]}]}
                    ))
                    open_received = True
                    continue
                if msg_type == GENESYS_PING:
                    server_seq += 1
                    await websocket.send_json(_genesys_msg(GENESYS_PONG, session_id, server_seq, client_seq))
                    continue
                if msg_type == GENESYS_CLOSE:
                    server_seq += 1
                    await websocket.send_json(_genesys_msg(GENESYS_CLOSED, session_id, server_seq, client_seq))
                    break
                continue

            if binary and open_received:
                # Echo PCMU back (minimal test - no VoiceLive)
                await websocket.send_bytes(binary)
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


if __name__ == "__main__":
    # Use port 8011 to avoid conflict with main backend
    uvicorn.run(app, host="0.0.0.0", port=8011)
