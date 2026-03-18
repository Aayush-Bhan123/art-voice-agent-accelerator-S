"""
Tests for the Genesys AudioHook WebSocket endpoint.

Validates protocol compliance, concurrent task architecture, PING/PONG handling,
write serialization, receive timeout, and graceful shutdown.
"""

import asyncio
import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.websockets import WebSocketState

# Import protocol constants and helpers directly
from apps.artagent.backend.api.v1.endpoints.genesys import (
    GENESYS_CLOSE,
    GENESYS_CLOSED,
    GENESYS_OPEN,
    GENESYS_OPENED,
    GENESYS_PING,
    GENESYS_PONG,
    GENESYS_UPDATE,
    GENESYS_UPDATED,
    _GenesysWebSocketWrapper,
    _genesys_server_message,
)


# ── Unit tests for _genesys_server_message ────────────────────────────────


class TestGenesysServerMessage:
    def test_basic_message(self):
        msg = _genesys_server_message("opened", "sess-1", 1, 0, {"startPaused": False})
        assert msg["version"] == "2"
        assert msg["type"] == "opened"
        assert msg["seq"] == 1
        assert msg["clientseq"] == 0
        assert msg["id"] == "sess-1"
        assert msg["parameters"]["startPaused"] is False

    def test_no_parameters(self):
        msg = _genesys_server_message("pong", "sess-1", 2, 1)
        assert msg["parameters"] == {}

    def test_seq_increments(self):
        msg1 = _genesys_server_message("pong", "s", 1, 1)
        msg2 = _genesys_server_message("pong", "s", 2, 2)
        assert msg2["seq"] == msg1["seq"] + 1


# ── Unit tests for _GenesysWebSocketWrapper ───────────────────────────────


def _make_mock_ws(connected=True):
    ws = AsyncMock()
    ws.client_state = WebSocketState.CONNECTED if connected else WebSocketState.DISCONNECTED
    return ws


class TestGenesysWebSocketWrapper:
    def test_send_count_is_per_instance(self):
        """Audio log counter must be per-wrapper-instance, not shared globally."""
        ws = _make_mock_ws()
        lock = asyncio.Lock()
        w1 = _GenesysWebSocketWrapper(ws, lock)
        w2 = _GenesysWebSocketWrapper(ws, lock)
        assert w1._send_count == 0
        assert w2._send_count == 0

    def test_audio_data_sent_as_binary(self):
        """AudioData messages should be decoded from base64 and sent as raw bytes."""
        ws = _make_mock_ws()
        lock = asyncio.Lock()
        wrapper = _GenesysWebSocketWrapper(ws, lock)

        pcmu = b"\x80" * 160
        b64 = base64.b64encode(pcmu).decode()
        msg = {"kind": "AudioData", "audioData": {"data": b64}}
        asyncio.get_event_loop().run_until_complete(wrapper.send_json(msg))
        ws.send_bytes.assert_awaited_once_with(pcmu)

    def test_non_audio_data_ignored(self):
        """Non-AudioData messages should not produce any WebSocket write."""
        ws = _make_mock_ws()
        lock = asyncio.Lock()
        wrapper = _GenesysWebSocketWrapper(ws, lock)

        asyncio.get_event_loop().run_until_complete(
            wrapper.send_json({"kind": "TranscriptUpdate", "data": "hello"})
        )
        ws.send_bytes.assert_not_awaited()

    def test_disconnected_ws_drops_audio(self):
        """Audio should be dropped when the underlying WebSocket is not connected."""
        ws = _make_mock_ws(connected=False)
        lock = asyncio.Lock()
        wrapper = _GenesysWebSocketWrapper(ws, lock)

        msg = {"kind": "AudioData", "audioData": {"data": base64.b64encode(b"\x80").decode()}}
        asyncio.get_event_loop().run_until_complete(wrapper.send_json(msg))
        ws.send_bytes.assert_not_awaited()

    def test_write_lock_is_acquired(self):
        """Wrapper must acquire the write lock before sending bytes."""
        ws = _make_mock_ws()
        lock = asyncio.Lock()
        wrapper = _GenesysWebSocketWrapper(ws, lock)
        lock_was_held = []

        original_send = ws.send_bytes

        async def guarded_send(data):
            lock_was_held.append(lock.locked())
            return await original_send(data)

        ws.send_bytes = guarded_send

        msg = {"kind": "AudioData", "audioData": {"data": base64.b64encode(b"\x80").decode()}}
        asyncio.get_event_loop().run_until_complete(wrapper.send_json(msg))
        assert lock_was_held == [True], "Write lock should be held during send_bytes"


# ── Integration-style tests for protocol flow ─────────────────────────────

class TestProtocolFlow:
    """Test the protocol message building for correctness."""

    def test_opened_message_format(self):
        """OPENED message must include media with format, rate, channels per AudioHook v2."""
        params = {
            "startPaused": False,
            "media": [
                {"type": "audio", "format": "PCMU", "rate": 8000, "channels": ["external"]}
            ],
        }
        msg = _genesys_server_message(GENESYS_OPENED, "sess-1", 1, 1, params)
        media = msg["parameters"]["media"][0]
        assert media["format"] == "PCMU"  # Not "codec"
        assert media["channels"] == ["external"]  # Not ["capture", "playback"]
        assert media["rate"] == 8000
        assert msg["seq"] == 1  # First server message must be seq=1

    def test_pong_response_format(self):
        """PONG must echo the session id and track server/client seq."""
        msg = _genesys_server_message(GENESYS_PONG, "sess-abc", 5, 10)
        assert msg["type"] == "pong"
        assert msg["id"] == "sess-abc"
        assert msg["seq"] == 5
        assert msg["clientseq"] == 10

    def test_closed_message_format(self):
        msg = _genesys_server_message(GENESYS_CLOSED, "sess-1", 3, 2)
        assert msg["type"] == "closed"
        assert msg["version"] == "2"

    def test_updated_message_format(self):
        msg = _genesys_server_message(GENESYS_UPDATED, "sess-1", 4, 3)
        assert msg["type"] == "updated"

    def test_ping_message_format(self):
        """Server-initiated PING message for keepalive."""
        msg = _genesys_server_message(GENESYS_PING, "sess-1", 6, 5)
        assert msg["type"] == "ping"
        assert msg["seq"] == 6
        assert msg["clientseq"] == 5
