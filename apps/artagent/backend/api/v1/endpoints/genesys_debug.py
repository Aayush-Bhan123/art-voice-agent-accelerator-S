"""
Genesys AudioHook Debug Endpoint
=================================

Standalone WebSocket endpoint that speaks the Genesys AudioHook v2 protocol
correctly (strict seq numbering, proper message schema). Receives PCMU audio,
converts to PCM16 16kHz, and runs Azure Speech STT for transcription.

No multi-agent framework, no VoiceLive, no audio response — purely diagnostic.
Purpose: verify that the Genesys → ART WebSocket connection and protocol
handshake work correctly on Azure deployments.
"""

from __future__ import annotations

import json
import os
import uuid

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.websockets import WebSocketState
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode

from src.audio_bridge.ffmpeg_bridge import FfmpegAudioBridge
from utils.ml_logging import get_logger

logger = get_logger("api.v1.endpoints.genesys_debug")
tracer = trace.get_tracer(__name__)

router = APIRouter(tags=["Genesys Debug"])

# Genesys AudioHook v2 protocol message types
GENESYS_OPEN = "open"
GENESYS_OPENED = "opened"
GENESYS_PING = "ping"
GENESYS_PONG = "pong"
GENESYS_CLOSE = "close"
GENESYS_CLOSED = "closed"
GENESYS_UPDATE = "update"
GENESYS_UPDATED = "updated"

# Optional API key guard
GENESYS_WS_API_KEY = os.environ.get("GENESYS_WS_API_KEY", "").strip()


def _server_message(
    msg_type: str,
    session_id: str,
    seq: int,
    clientseq: int,
    parameters: dict | None = None,
) -> dict:
    """Build a Genesys AudioHook v2 server-side message with strict seq."""
    msg = {
        "version": "2",
        "type": msg_type,
        "seq": seq,
        "clientseq": clientseq,
        "id": session_id,
    }
    if parameters:
        msg["parameters"] = parameters
    else:
        msg["parameters"] = {}
    return msg


@router.websocket("/stream")
async def genesys_debug_stream(websocket: WebSocket) -> None:
    """
    Debug WebSocket endpoint implementing Genesys AudioHook v2 protocol.

    - Correct seq numbering on all server messages
    - Logs every protocol message received and sent
    - Converts PCMU audio → PCM16 and runs Azure Speech STT
    - No audio sent back (receive-only diagnostic)
    """
    query_params = dict(websocket.query_params)
    session_id = (
        websocket.headers.get("Audiohook-Session-Id")
        or websocket.headers.get("AudioHook-Session-Id")
        or query_params.get("session_id")
        or f"debug_{uuid.uuid4().hex[:12]}"
    )

    if GENESYS_WS_API_KEY:
        api_key = (
            websocket.headers.get("X-Api-Key")
            or query_params.get("api_key")
            or ""
        )
        if api_key != GENESYS_WS_API_KEY:
            await websocket.close(code=3000, reason="Invalid API Key")
            return

    await websocket.accept()
    logger.info("[%s] DEBUG endpoint: WebSocket accepted", session_id)

    server_seq = 0  # Server message counter (incremented before each send)
    client_seq = 0  # Last client seq seen
    audio_chunk_count = 0
    total_audio_bytes = 0
    recognizer = None
    bridge = None

    with tracer.start_as_current_span(
        "api.v1.genesys_debug.stream",
        kind=SpanKind.SERVER,
        attributes={"genesys.session_id": session_id, "debug": True},
    ) as span:
        try:
            # ── Phase 1: Wait for OPEN ──────────────────────────────────
            open_received = False
            while (
                websocket.client_state == WebSocketState.CONNECTED
                and websocket.application_state == WebSocketState.CONNECTED
            ):
                raw = await websocket.receive()
                if raw.get("type") == "websocket.disconnect":
                    logger.info("[%s] DEBUG: Client disconnected during handshake", session_id)
                    break
                if raw.get("type") != "websocket.receive":
                    continue

                text = raw.get("text")
                if text is not None:
                    try:
                        msg = json.loads(text)
                    except json.JSONDecodeError:
                        logger.warning("[%s] DEBUG: Invalid JSON: %s", session_id, text[:200])
                        continue

                    msg_type = msg.get("type")
                    client_seq = msg.get("seq", client_seq)
                    if msg.get("id"):
                        session_id = msg["id"]

                    logger.info(
                        "[%s] DEBUG RECV: type=%s seq=%s keys=%s",
                        session_id, msg_type, msg.get("seq"), list(msg.keys()),
                    )

                    if msg_type == GENESYS_OPEN:
                        # Log the full OPEN message for diagnostics
                        logger.info("[%s] DEBUG OPEN params: %s", session_id, json.dumps(msg.get("parameters", {}), indent=2))

                        server_seq += 1
                        opened_params = {
                            "startPaused": False,
                            "media": [
                                {
                                    "type": "audio",
                                    "format": "PCMU",
                                    "rate": 8000,
                                    "channels": ["external"],
                                }
                            ],
                        }
                        opened_msg = _server_message(
                            GENESYS_OPENED, session_id, server_seq, client_seq, opened_params
                        )
                        logger.info(
                            "[%s] DEBUG SEND: OPENED seq=%d clientseq=%d",
                            session_id, server_seq, client_seq,
                        )
                        await websocket.send_json(opened_msg)
                        open_received = True
                        break

                    if msg_type == GENESYS_PING:
                        # Do NOT respond to PING before OPENED.
                        # AudioHook v2 requires OPENED as the first server message (seq=1).
                        logger.info("[%s] DEBUG: Suppressing PING in pre-OPEN phase", session_id)
                        continue

                    if msg_type == GENESYS_CLOSE:
                        server_seq += 1
                        closed_msg = _server_message(GENESYS_CLOSED, session_id, server_seq, client_seq)
                        logger.info("[%s] DEBUG SEND: CLOSED seq=%d (pre-open close)", session_id, server_seq)
                        await websocket.send_json(closed_msg)
                        await websocket.close(1000)
                        span.set_status(Status(StatusCode.OK))
                        return

                    logger.info("[%s] DEBUG: Ignoring pre-OPEN message type=%s", session_id, msg_type)
                    continue

                binary = raw.get("bytes")
                if binary:
                    logger.debug("[%s] DEBUG: Binary before OPEN, ignoring %d bytes", session_id, len(binary))

            if not open_received:
                logger.info("[%s] DEBUG: Connection ended without OPEN", session_id)
                if (
                    websocket.client_state == WebSocketState.CONNECTED
                    and websocket.application_state == WebSocketState.CONNECTED
                ):
                    await websocket.close(1000)
                span.set_status(Status(StatusCode.OK))
                return

            # ── Phase 2: Initialize STT (optional — best-effort) ────────
            try:
                from src.speech.speech_recognizer import StreamingSpeechRecognizerFromBytes
                from apps.artagent.backend.config.settings import (
                    AZURE_SPEECH_REGION,
                    RECOGNIZED_LANGUAGE,
                    SILENCE_DURATION_MS,
                )

                recognizer = StreamingSpeechRecognizerFromBytes(
                    region=AZURE_SPEECH_REGION,
                    candidate_languages=RECOGNIZED_LANGUAGE,
                    vad_silence_timeout_ms=SILENCE_DURATION_MS,
                    audio_format="pcm",
                    enable_diarisation=False,
                    enable_neural_fe=False,
                    call_connection_id=f"debug_{session_id}",
                    enable_tracing=False,
                )

                def _on_partial(text: str, lang: str, speaker_id: str | None = None) -> None:
                    logger.info("[%s] STT PARTIAL (%s): %s", session_id, lang, text)

                def _on_final(text: str, lang: str, speaker_id: str | None = None) -> None:
                    logger.info("[%s] STT FINAL   (%s): %s", session_id, lang, text)

                recognizer.set_partial_result_callback(_on_partial)
                recognizer.set_final_result_callback(_on_final)
                recognizer.start()
                bridge = FfmpegAudioBridge(pcm16_sample_rate=16000)
                logger.info("[%s] DEBUG: STT recognizer started", session_id)
            except Exception as e:
                logger.warning(
                    "[%s] DEBUG: STT init failed (audio will still be logged): %s",
                    session_id, e,
                )
                recognizer = None
                bridge = None

            # ── Phase 3: Main protocol loop ─────────────────────────────
            logger.info("[%s] DEBUG: Entering main loop, ready for audio + protocol messages", session_id)

            while (
                websocket.client_state == WebSocketState.CONNECTED
                and websocket.application_state == WebSocketState.CONNECTED
            ):
                raw = await websocket.receive()
                if raw.get("type") == "websocket.disconnect":
                    logger.info("[%s] DEBUG: Client disconnected", session_id)
                    break
                if raw.get("type") != "websocket.receive":
                    continue

                text = raw.get("text")
                binary = raw.get("bytes")

                # ── JSON protocol messages ──────────────────────────────
                if text is not None:
                    try:
                        msg = json.loads(text)
                    except json.JSONDecodeError:
                        logger.warning("[%s] DEBUG: Invalid JSON: %s", session_id, text[:200])
                        continue

                    msg_type = msg.get("type")
                    client_seq = msg.get("seq", client_seq)
                    if msg.get("id"):
                        session_id = msg["id"]

                    logger.info(
                        "[%s] DEBUG RECV: type=%s seq=%s",
                        session_id, msg_type, msg.get("seq"),
                    )

                    if msg_type == GENESYS_PING:
                        server_seq += 1
                        pong_msg = _server_message(GENESYS_PONG, session_id, server_seq, client_seq)
                        logger.info("[%s] DEBUG SEND: PONG seq=%d clientseq=%d", session_id, server_seq, client_seq)
                        await websocket.send_json(pong_msg)
                        continue

                    if msg_type == GENESYS_UPDATE:
                        server_seq += 1
                        updated_msg = _server_message(GENESYS_UPDATED, session_id, server_seq, client_seq)
                        logger.info("[%s] DEBUG SEND: UPDATED seq=%d", session_id, server_seq)
                        await websocket.send_json(updated_msg)
                        continue

                    if msg_type == GENESYS_CLOSE:
                        server_seq += 1
                        closed_msg = _server_message(GENESYS_CLOSED, session_id, server_seq, client_seq)
                        logger.info("[%s] DEBUG SEND: CLOSED seq=%d", session_id, server_seq)
                        await websocket.send_json(closed_msg)
                        break

                    # Log any other message types for diagnostics
                    logger.info(
                        "[%s] DEBUG: Unhandled message type=%s full=%s",
                        session_id, msg_type, json.dumps(msg, default=str)[:500],
                    )
                    continue

                # ── Binary audio frames ─────────────────────────────────
                if binary:
                    audio_chunk_count += 1
                    total_audio_bytes += len(binary)

                    if audio_chunk_count <= 5 or audio_chunk_count % 100 == 0:
                        logger.info(
                            "[%s] DEBUG AUDIO: chunk #%d, %d bytes (total: %d bytes / %.1f sec)",
                            session_id,
                            audio_chunk_count,
                            len(binary),
                            total_audio_bytes,
                            total_audio_bytes / 8000,  # PCMU = 8000 bytes/sec
                        )

                    # Feed to STT if available
                    if recognizer and bridge:
                        try:
                            pcm16_data = bridge.transcode_pcmu_to_pcm16(binary)
                            recognizer.write_bytes(pcm16_data)
                        except Exception as e:
                            if audio_chunk_count <= 3:
                                logger.warning("[%s] DEBUG: STT write error: %s", session_id, e)

            # ── Session summary ─────────────────────────────────────────
            logger.info(
                "[%s] DEBUG SESSION SUMMARY: %d audio chunks, %d total bytes, %.1f seconds of audio, server_seq=%d, last_client_seq=%d",
                session_id,
                audio_chunk_count,
                total_audio_bytes,
                total_audio_bytes / 8000 if total_audio_bytes else 0,
                server_seq,
                client_seq,
            )
            span.set_status(Status(StatusCode.OK))

        except WebSocketDisconnect as e:
            if e.code in (1000, 1001):
                span.set_status(Status(StatusCode.OK))
            else:
                span.set_status(Status(StatusCode.ERROR, str(e)))
            logger.info("[%s] DEBUG: WebSocket disconnect code=%s", session_id, e.code)
        except Exception as e:
            span.set_status(Status(StatusCode.ERROR, str(e)))
            logger.exception("[%s] DEBUG: Unexpected error", session_id)
        finally:
            if recognizer:
                try:
                    recognizer.stop()
                    recognizer.close_stream()
                    logger.info("[%s] DEBUG: STT recognizer stopped", session_id)
                except Exception as e:
                    logger.warning("[%s] DEBUG: STT cleanup error: %s", session_id, e)
            if bridge:
                try:
                    bridge.close()
                except Exception as e:
                    logger.warning("[%s] DEBUG: Bridge cleanup error: %s", session_id, e)
            if (
                websocket.client_state == WebSocketState.CONNECTED
                and websocket.application_state == WebSocketState.CONNECTED
            ):
                try:
                    await websocket.close()
                except Exception:
                    pass
            logger.info("[%s] DEBUG: Session ended", session_id)
