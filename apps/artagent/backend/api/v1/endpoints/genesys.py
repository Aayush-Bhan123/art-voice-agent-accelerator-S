"""
Genesys AudioHook WebSocket endpoint – connector inside ART
==========================================================

Exposes a WebSocket that speaks the Genesys AudioHook protocol so Genesys Cloud
can connect directly to ART (no separate AudioHook server). Respects Genesys
events (OPEN, OPENED, PING, PONG, CLOSE, CLOSED) and streams PCMU audio
bidirectionally through the existing VoiceLive + FFmpeg bridge.

Protocol reference: https://developer.genesys.cloud/devapps/audiohook/protocol-reference
"""

import asyncio
import base64
import json
import os
import time
import uuid
from collections import deque
from pathlib import Path

from fastapi.responses import HTMLResponse
from src.stateful.state_managment import MemoManager
from apps.artagent.backend.voice import VoiceLiveSDKHandler
from src.pools.session_manager import SessionContext
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.websockets import WebSocketState
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from utils.ml_logging import get_logger

logger = get_logger("api.v1.endpoints.genesys")
tracer = trace.get_tracer(__name__)

router = APIRouter(tags=["Genesys"])

# Genesys protocol message types
GENESYS_OPEN = "open"
GENESYS_OPENED = "opened"
GENESYS_PING = "ping"
GENESYS_PONG = "pong"
GENESYS_CLOSE = "close"
GENESYS_CLOSED = "closed"
GENESYS_UPDATE = "update"
GENESYS_UPDATED = "updated"

# Optional API key (if GENESYS_WS_API_KEY is set, X-Api-Key header must match)
GENESYS_WS_API_KEY = os.environ.get("GENESYS_WS_API_KEY", "").strip()

# Timeout for receiving a message from Genesys before considering the connection dead (seconds).
# Genesys sends audio at ~50fps (20ms) plus PINGs, so 30s of silence is abnormal.
_RECEIVE_TIMEOUT_S = float(os.environ.get("GENESYS_RECEIVE_TIMEOUT", "30"))

# ──────────────────────────────────────────────────────────────────────────────
# AUDIOHOOK-0012 (server message rate limiting) – instrumentation thresholds
# ──────────────────────────────────────────────────────────────────────────────
# Genesys closes the WebSocket and raises AUDIOHOOK-0012 when the server sends
# too many messages in a short window. Published limits per AudioHook session:
#
#   ┌──────────────────────┬──────────────┬─────────────┐
#   │ Message type         │ Sustained /s │ Burst /s    │
#   ├──────────────────────┼──────────────┼─────────────┤
#   │ Text messages        │      5       │     25      │
#   │ Binary messages      │      5       │     25      │  (when applicable)
#   │ Pause messages       │      2       │      4      │
#   │ Resume messages      │      2       │      4      │
#   └──────────────────────┴──────────────┴─────────────┘
#
# We log a WARNING when our outbound rate within a rolling 1-second window
# approaches the sustained limit, so the breach is visible in App Insights
# *before* Genesys terminates the call.
# Ref: https://developer.genesys.cloud/platform/operational-event-catalog/audiohook/audiohook-0012
_AUDIOHOOK_RATE_WINDOW_S = 1.0
_AUDIOHOOK_TEXT_WARN_PER_S = 4   # warn when ≥ 4 text frames/s (limit is 5)
_AUDIOHOOK_TEXT_BURST_WARN = 20  # warn when ≥ 20 text frames in window (burst limit 25)
_AUDIOHOOK_BIN_WARN_PER_S = 60   # binary audio normally ~50 fps; >60 indicates a burst
_AUDIOHOOK_PING_WARN_PER_S = 3   # Genesys normally PINGs every ~5-10s; >3/s = storm

# Possible locations for tests/genesys_test.html (genesys.py is in .../api/v1/endpoints/)
_THIS_DIR = Path(__file__).resolve().parent
_CANDIDATE_ROOTS = [
    _THIS_DIR.parents[6],   # project root (endpoints -> v1 -> api -> backend -> artagent -> apps -> root)
    _THIS_DIR.parents[5],   # apps
    Path.cwd(),             # current working directory when server runs
]
_GENESYS_TEST_HTML_PATH = None
for _root in _CANDIDATE_ROOTS:
    _p = _root / "tests" / "genesys_test.html"
    if _p.exists():
        _GENESYS_TEST_HTML_PATH = _p
        break
if _GENESYS_TEST_HTML_PATH is None:
    _GENESYS_TEST_HTML_PATH = Path.cwd() / "tests" / "genesys_test.html"


def _get_genesys_test_html() -> str:
    """Load test page HTML from file if present."""
    if _GENESYS_TEST_HTML_PATH.exists():
        return _GENESYS_TEST_HTML_PATH.read_text(encoding="utf-8")
    # Fallback: minimal page that tells user the URL and links to open file
    return """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Genesys Test</title></head>
<body style="font-family:sans-serif; padding:2rem; background:#0f172a; color:#e2e8f0;">
<h1>Genesys AudioHook Test</h1>
<p>Test page file not found. Use either:</p>
<ul>
<li>Open <strong>tests/genesys_test.html</strong> in your browser (from project root), then set WebSocket URL to <code>ws://localhost:8010/api/v1/genesys/stream</code></li>
<li>Or ensure <code>tests/genesys_test.html</code> exists and restart the backend.</li>
</ul>
<p>WebSocket endpoint: <code>ws://localhost:8010/api/v1/genesys/stream</code></p>
</body></html>"""


@router.get("/test", response_class=HTMLResponse, include_in_schema=False)
async def genesys_test_page() -> HTMLResponse:
    """Serve the Genesys AudioHook test page for local testing."""
    return HTMLResponse(content=_get_genesys_test_html())


class _GenesysWebSocketWrapper:
    """
    WebSocket-like object passed to VoiceLiveSDKHandler. Intercepts send_json:
    - For kind "AudioData": decode base64 and send raw PCMU bytes to Genesys.
    - Other messages (e.g. type "audio_data" for UI): no-op so we don't send JSON to Genesys.
    Proxies state, client_state, application_state, app to the real WebSocket.

    Uses a shared asyncio.Lock to serialize all writes to the underlying WebSocket,
    preventing interleaved frames from concurrent tasks (audio out + PONG responses).
    """

    def __init__(self, real_ws: WebSocket, write_lock: asyncio.Lock):
        self._real = real_ws
        self._write_lock = write_lock
        self._send_count = 0
        # AUDIOHOOK-0012 instrumentation: rolling 1s window of binary frames
        # sent to Genesys, paired with each frame's byte size. Used to detect
        # bursts that may trip the binary message rate limit (5/s sustained,
        # 25/s burst, Genesys 429 → AUDIOHOOK-0012).
        # Stored as (monotonic_timestamp, byte_size) tuples.
        self._binary_send_window: "deque[tuple[float, int]]" = deque()
        self._binary_rate_warn_logged_at: float = 0.0
        # Throttles the per-second INFO summary (frames/s, bytes/s, chunk
        # min/avg/max) so the log shows one rollup per second of active audio
        # rather than one line per frame.
        self._binary_summary_logged_at: float = 0.0

    @property
    def state(self):
        return self._real.state

    @property
    def app(self):
        return self._real.app

    @property
    def client_state(self):
        return self._real.client_state

    @property
    def application_state(self):
        return self._real.application_state

    async def send_json(self, message: dict) -> None:
        kind = message.get("kind") or message.get("Kind")
        if kind == "AudioData":
            audio_section = message.get("AudioData") or message.get("audioData") or {}
            data_b64 = audio_section.get("data")
            if data_b64:
                try:
                    pcmu_bytes = base64.b64decode(data_b64)
                    self._send_count += 1
                    if self._send_count <= 5 or self._send_count % 50 == 0:
                        logger.info(
                            "[GenesysWrapper] Sending %d PCMU bytes to browser (frame #%d) ws_state=%s",
                            len(pcmu_bytes), self._send_count, self._real.client_state,
                        )
                    if self._real.client_state != WebSocketState.CONNECTED:
                        logger.warning("[GenesysWrapper] WebSocket not connected, dropping audio frame #%d", self._send_count)
                        return
                    async with self._write_lock:
                        await self._real.send_bytes(pcmu_bytes)
                    # ── AUDIOHOOK-0012 binary-frame instrumentation ──────
                    # Mirror of the text-frame tracker in _control_task. We
                    # need byte-level visibility here because the rate limit
                    # we are tripping is on outbound binary frames (Genesys
                    # 429 "Rate limit exceeded"). Every frame is tracked in a
                    # rolling 1s window so we can report:
                    #   - frame rate (frames/s)
                    #   - throughput (bytes/s) ≈ realtime if pacing is OK
                    #     (PCMU 8kHz µ-law ⇒ 8000 bytes/s == 1.0x realtime)
                    #   - chunk size distribution (min/avg/max bytes)
                    # Limit refresher:
                    #   binary messages → 5/s sustained, 25/s burst
                    now = time.monotonic()
                    frame_bytes = len(pcmu_bytes)
                    self._binary_send_window.append((now, frame_bytes))
                    cutoff = now - _AUDIOHOOK_RATE_WINDOW_S
                    while self._binary_send_window and self._binary_send_window[0][0] < cutoff:
                        self._binary_send_window.popleft()
                    rate = len(self._binary_send_window)
                    total_bytes = sum(sz for _, sz in self._binary_send_window)
                    sizes = [sz for _, sz in self._binary_send_window]
                    min_sz = min(sizes)
                    max_sz = max(sizes)
                    avg_sz = total_bytes // rate
                    realtime_x = total_bytes / 8000.0  # 8000 B/s == 1.0x realtime for PCMU 8kHz

                    # Per-second INFO summary while audio is active.
                    if (now - self._binary_summary_logged_at) >= 1.0:
                        self._binary_summary_logged_at = now
                        logger.info(
                            "[GenesysWrapper] AUDIOHOOK-0012 binary egress 1s: "
                            "frames=%d bytes=%d (%.2fx realtime) chunk min/avg/max=%d/%d/%d "
                            "total_frames_sent=%d",
                            rate, total_bytes, realtime_x,
                            min_sz, avg_sz, max_sz, self._send_count,
                        )

                    # WARNING when we approach/exceed the published Genesys
                    # binary rate limits. Throttled to one warning per second.
                    if rate >= _AUDIOHOOK_BIN_WARN_PER_S and (now - self._binary_rate_warn_logged_at) >= 1.0:
                        self._binary_rate_warn_logged_at = now
                        logger.warning(
                            "[GenesysWrapper] AUDIOHOOK-0012 watch: outbound binary frame burst "
                            "%d/s bytes=%d (%.2fx realtime) chunk min/avg/max=%d/%d/%d "
                            "(threshold=%d, hard limit=25 burst / 5 sustained)",
                            rate, total_bytes, realtime_x,
                            min_sz, avg_sz, max_sz, _AUDIOHOOK_BIN_WARN_PER_S,
                        )
                except Exception as e:
                    logger.warning("Genesys send_bytes failed: %r (%s)", e, type(e).__name__)
            return
        logger.debug("Genesys transport ignoring non-AudioData send_json: kind=%s", kind)


def _genesys_server_message(
    msg_type: str,
    session_id: str,
    seq: int,
    clientseq: int,
    parameters: dict | None = None,
) -> dict:
    """Build a Genesys server message (OPENED, PONG, CLOSED)."""
    if parameters is None:
        parameters = {}
    return {
        "version": "2",
        "type": msg_type,
        "seq": seq,
        "clientseq": clientseq,
        "id": session_id,
        "parameters": parameters,
    }


@router.websocket("/stream")
async def genesys_audiohook_stream(websocket: WebSocket) -> None:
    """
    WebSocket endpoint that speaks the Genesys AudioHook protocol.

    Architecture: After the OPEN handshake, the endpoint splits into concurrent tasks:
    - **Reader task**: Reads all WebSocket frames and dispatches text messages to a
      control queue and binary audio to an audio queue.
    - **Control task**: Processes PING/PONG, UPDATE, CLOSE from the control queue with
      immediate responses — never blocked by audio processing.
    - **Audio task**: Processes binary audio chunks from the audio queue, forwarding
      to VoiceLive via the handler.
    - **Keepalive task**: Sends periodic server-side PINGs to detect dead connections.

    This prevents PING/PONG starvation (AUDIOHOOK-0006) caused by slow audio processing
    blocking the main receive loop.
    """
    query_params = dict(websocket.query_params)
    session_id = (
        websocket.headers.get("Audiohook-Session-Id")
        or websocket.headers.get("AudioHook-Session-Id")
        or query_params.get("session_id")
        or f"genesys_{uuid.uuid4().hex[:12]}"
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
    handler = None
    server_seq = 0
    client_seq = 0
    # Shared lock serializes all writes to the Genesys WebSocket (PONG, CLOSED, audio out)
    ws_write_lock = asyncio.Lock()

    with tracer.start_as_current_span(
        "api.v1.genesys.stream",
        kind=SpanKind.SERVER,
        attributes={"genesys.session_id": session_id},
    ) as span:
        try:
            # ── Phase 1: Wait for OPEN before starting VoiceLive ──────────
            open_received = False
            while (
                websocket.client_state == WebSocketState.CONNECTED
                and websocket.application_state == WebSocketState.CONNECTED
            ):
                raw = await websocket.receive()
                if raw.get("type") == "websocket.disconnect":
                    break
                if raw.get("type") != "websocket.receive":
                    continue

                text = raw.get("text")
                binary = raw.get("bytes")

                if text is not None:
                    try:
                        msg = json.loads(text)
                    except json.JSONDecodeError:
                        logger.warning("[%s] Invalid JSON from Genesys", session_id)
                        continue
                    msg_type = msg.get("type")
                    client_seq = msg.get("seq", client_seq)
                    if msg.get("id"):
                        session_id = msg["id"]

                    if msg_type == GENESYS_OPEN:
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
                        opened_msg = _genesys_server_message(
                            GENESYS_OPENED, session_id, server_seq, client_seq, opened_params
                        )
                        await websocket.send_json(opened_msg)
                        open_received = True
                        logger.info("[%s] Genesys OPEN → OPENED", session_id)
                        break
                    if msg_type == GENESYS_PING:
                        # Do NOT respond to PING before OPENED is sent.
                        # AudioHook v2 protocol requires that the first server message is OPENED (seq=1).
                        logger.debug("[%s] Suppressing PING response in pre-OPEN phase", session_id)
                        continue
                    if msg_type == GENESYS_CLOSE:
                        server_seq += 1
                        await websocket.send_json(
                            _genesys_server_message(GENESYS_CLOSED, session_id, server_seq, client_seq)
                        )
                        await websocket.close(1000)
                        span.set_status(Status(StatusCode.OK))
                        return
                    if msg_type in ("playback_started", "playback_completed", "playback_stopped"):
                        continue
                    logger.debug("[%s] Genesys text message type=%s", session_id, msg_type)
                    continue

                if binary and open_received and handler:
                    b64 = base64.b64encode(binary).decode("utf-8")
                    acs_msg = json.dumps({
                        "kind": "AudioData",
                        "audioData": {"data": b64, "silent": False},
                    })
                    await handler.handle_audio_data(acs_msg)
                    continue

                if binary and not open_received:
                    logger.debug("[%s] Binary before OPEN, ignoring", session_id)

            if not open_received:
                await websocket.close(1000)
                span.set_status(Status(StatusCode.OK))
                return

            # ── Phase 2: Create VoiceLive handler ─────────────────────────
            call_connection_id = f"genesys_{session_id}"
            redis_mgr = getattr(websocket.app.state, "redis", None)
            memory_manager = (
                MemoManager.from_redis(session_id, redis_mgr)
                if redis_mgr
                else MemoManager(session_id=session_id)
            )
            websocket.state.cm = memory_manager
            websocket.state.session_context = SessionContext(
                session_id=session_id,
                memory_manager=memory_manager,
                websocket=websocket,
            )
            websocket.state.session_id = session_id

            wrapper = _GenesysWebSocketWrapper(websocket, ws_write_lock)
            handler = VoiceLiveSDKHandler(
                websocket=wrapper,
                session_id=session_id,
                call_connection_id=call_connection_id,
            )
            try:
                await handler.start()
            except Exception as e:
                logger.exception(
                    "[%s] VoiceLive handler.start() failed (check AZURE_VOICELIVE_* credentials): %s",
                    session_id,
                    e,
                )
                await websocket.close(1011, reason="VoiceLive start failed")
                span.set_status(Status(StatusCode.ERROR, str(e)))
                return

            # Inject AudioMetadata so handler knows 8kHz PCMU
            await handler.handle_audio_data(
                json.dumps({
                    "kind": "AudioMetadata",
                    "payload": {"rate": 8000, "channels": 1},
                })
            )

            # ── Phase 3: Concurrent reader/control/audio/keepalive tasks ──
            # Queues decouple the single WebSocket reader from processing.
            control_queue: asyncio.Queue[dict] = asyncio.Queue()
            audio_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=500)
            shutdown_event = asyncio.Event()

            # Mutable state shared across tasks via a list (to allow nonlocal mutation)
            seq_state = {"server": server_seq, "client": client_seq, "session_id": session_id}

            async def _reader_task() -> None:
                """Read all WebSocket frames, dispatch to control or audio queue."""
                try:
                    while not shutdown_event.is_set():
                        try:
                            raw = await asyncio.wait_for(
                                websocket.receive(), timeout=_RECEIVE_TIMEOUT_S
                            )
                        except asyncio.TimeoutError:
                            logger.warning(
                                "[%s] No data from Genesys for %.0fs — closing as dead connection",
                                seq_state["session_id"],
                                _RECEIVE_TIMEOUT_S,
                            )
                            shutdown_event.set()
                            return

                        if raw.get("type") == "websocket.disconnect":
                            logger.info(
                                "[%s] Genesys peer closed WebSocket (websocket.disconnect code=%s)",
                                seq_state["session_id"],
                                raw.get("code"),
                            )
                            shutdown_event.set()
                            return
                        if raw.get("type") != "websocket.receive":
                            continue

                        text = raw.get("text")
                        binary = raw.get("bytes")

                        if text is not None:
                            try:
                                msg = json.loads(text)
                            except json.JSONDecodeError:
                                continue
                            await control_queue.put(msg)
                        elif binary:
                            try:
                                audio_queue.put_nowait(binary)
                            except asyncio.QueueFull:
                                # Drop oldest audio chunk to prevent unbounded memory growth
                                try:
                                    audio_queue.get_nowait()
                                except asyncio.QueueEmpty:
                                    pass
                                audio_queue.put_nowait(binary)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "[%s] Reader task error — closing Genesys connection",
                        seq_state["session_id"],
                        exc_info=True,
                    )
                finally:
                    shutdown_event.set()

            async def _control_task() -> None:
                """Process control messages (PING, UPDATE, CLOSE) with immediate responses."""
                # AUDIOHOOK-0012 instrumentation: rolling 1s windows for
                #   - outbound text frames (PONG/UPDATED/CLOSED) → text rate limit
                #     (5/s sustained, 25/s burst).
                #   - inbound PINGs → a PING storm forces a matching PONG burst,
                #     which is the most likely path to trip the text rate limit.
                # See https://developer.genesys.cloud/platform/operational-event-catalog/audiohook/audiohook-0012
                text_send_times: "deque[float]" = deque()
                ping_recv_times: "deque[float]" = deque()
                text_warn_logged_at: float = 0.0
                ping_warn_logged_at: float = 0.0

                def _record_text_send(sid: str, kind: str) -> None:
                    """AUDIOHOOK-0012: log a warning if outbound text rate gets close to the limit."""
                    nonlocal text_warn_logged_at
                    now = time.monotonic()
                    text_send_times.append(now)
                    cutoff = now - _AUDIOHOOK_RATE_WINDOW_S
                    while text_send_times and text_send_times[0] < cutoff:
                        text_send_times.popleft()
                    rate = len(text_send_times)
                    if (
                        (rate >= _AUDIOHOOK_TEXT_WARN_PER_S or rate >= _AUDIOHOOK_TEXT_BURST_WARN)
                        and (now - text_warn_logged_at) >= 1.0
                    ):
                        text_warn_logged_at = now
                        logger.warning(
                            "[%s] AUDIOHOOK-0012 watch: outbound text frame rate %d/s "
                            "(last=%s, sustained_limit=5, burst_limit=25)",
                            sid, rate, kind,
                        )

                try:
                    while not shutdown_event.is_set():
                        try:
                            msg = await asyncio.wait_for(control_queue.get(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue

                        msg_type = msg.get("type")
                        seq_state["client"] = msg.get("seq", seq_state["client"])
                        if msg.get("id"):
                            seq_state["session_id"] = msg["id"]

                        sid = seq_state["session_id"]

                        # AUDIOHOOK-0012: capture Genesys 'error' messages at WARNING
                        # so the message that arrives *just before* a forced close is
                        # visible in App Insights. This is the single most important
                        # diagnostic for AUDIOHOOK-0012 — it identifies which message
                        # class and rate tripped the limit.
                        if msg_type == "error":
                            try:
                                raw_err = json.dumps(msg)[:2000]
                            except (TypeError, ValueError):
                                raw_err = repr(msg)[:2000]
                            logger.warning(
                                "[%s] Genesys ERROR frame received (possible AUDIOHOOK-0012): %s",
                                sid, raw_err,
                            )
                            continue

                        if msg_type == GENESYS_PING:
                            # AUDIOHOOK-0012: track inbound PING rate. A PING storm
                            # forces a 1:1 PONG burst on our side, which is the most
                            # plausible way to breach the text-message rate limit.
                            now = time.monotonic()
                            ping_recv_times.append(now)
                            cutoff = now - _AUDIOHOOK_RATE_WINDOW_S
                            while ping_recv_times and ping_recv_times[0] < cutoff:
                                ping_recv_times.popleft()
                            ping_rate = len(ping_recv_times)
                            if (
                                ping_rate >= _AUDIOHOOK_PING_WARN_PER_S
                                and (now - ping_warn_logged_at) >= 1.0
                            ):
                                ping_warn_logged_at = now
                                logger.warning(
                                    "[%s] AUDIOHOOK-0012 watch: inbound Genesys PING rate %d/s "
                                    "— each PING triggers a PONG, risking text rate limit (5/s)",
                                    sid, ping_rate,
                                )

                            seq_state["server"] += 1
                            pong = _genesys_server_message(
                                GENESYS_PONG, sid, seq_state["server"], seq_state["client"]
                            )
                            async with ws_write_lock:
                                await websocket.send_json(pong)
                            _record_text_send(sid, "pong")
                            continue

                        if msg_type == GENESYS_UPDATE:
                            seq_state["server"] += 1
                            updated = _genesys_server_message(
                                GENESYS_UPDATED, sid, seq_state["server"], seq_state["client"]
                            )
                            async with ws_write_lock:
                                await websocket.send_json(updated)
                            _record_text_send(sid, "updated")
                            continue

                        if msg_type == GENESYS_CLOSE:
                            logger.info(
                                "[%s] Genesys CLOSE received (reason=%r) — sending CLOSED",
                                sid,
                                (msg.get("parameters") or {}).get("reason"),
                            )
                            seq_state["server"] += 1
                            closed = _genesys_server_message(
                                GENESYS_CLOSED, sid, seq_state["server"], seq_state["client"]
                            )
                            async with ws_write_lock:
                                await websocket.send_json(closed)
                            _record_text_send(sid, "closed")
                            shutdown_event.set()
                            return

                        if msg_type in ("playback_started", "playback_completed", "playback_stopped"):
                            continue

                        # AUDIOHOOK-0012: log any unknown control message at INFO
                        # (was debug) so future protocol surprises are not silent.
                        logger.info(
                            "[%s] Genesys control message type=%s payload=%s",
                            sid, msg_type, json.dumps(msg)[:500],
                        )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "[%s] Control task error — closing Genesys connection",
                        seq_state["session_id"],
                        exc_info=True,
                    )
                finally:
                    shutdown_event.set()

            async def _audio_task() -> None:
                """Process audio chunks from the queue, forwarding to VoiceLive handler."""
                audio_chunk_count = 0
                try:
                    while not shutdown_event.is_set():
                        try:
                            binary = await asyncio.wait_for(audio_queue.get(), timeout=1.0)
                        except asyncio.TimeoutError:
                            continue

                        audio_chunk_count += 1
                        if audio_chunk_count <= 3 or audio_chunk_count % 200 == 0:
                            logger.info(
                                "[%s] Forwarding mic audio to handler: %d bytes (chunk #%d)",
                                seq_state["session_id"],
                                len(binary),
                                audio_chunk_count,
                            )
                        b64 = base64.b64encode(binary).decode("utf-8")
                        acs_msg = json.dumps({
                            "kind": "AudioData",
                            "audioData": {"data": b64, "silent": False},
                        })
                        await handler.handle_audio_data(acs_msg)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.warning(
                        "[%s] Audio task error — closing Genesys connection",
                        seq_state["session_id"],
                        exc_info=True,
                    )
                finally:
                    shutdown_event.set()

            # Launch concurrent tasks
            # Note: No server-initiated PINGs — the AudioHook v2 protocol only allows
            # the CLIENT (Genesys) to send PINGs. The server responds with PONGs.
            # Dead connection detection relies on the reader task's receive timeout.
            tasks = [
                asyncio.create_task(_reader_task(), name=f"genesys-reader-{session_id}"),
                asyncio.create_task(_control_task(), name=f"genesys-control-{session_id}"),
                asyncio.create_task(_audio_task(), name=f"genesys-audio-{session_id}"),
            ]
            try:
                # Wait until any task signals shutdown
                await shutdown_event.wait()
            finally:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

            span.set_status(Status(StatusCode.OK))
        except WebSocketDisconnect as e:
            if e.code in (1000, 1001):
                span.set_status(Status(StatusCode.OK))
            else:
                span.set_status(Status(StatusCode.ERROR, str(e)))
                logger.warning("[%s] Genesys WebSocket disconnect code=%s", session_id, e.code)
        except Exception as e:
            span.set_status(Status(StatusCode.ERROR, str(e)))
            logger.exception("[%s] Genesys stream error", session_id)
        finally:
            if handler:
                try:
                    await handler.stop()
                except Exception as e:
                    logger.error("Error stopping Genesys handler: %s", e)
            if (
                websocket.client_state == WebSocketState.CONNECTED
                and websocket.application_state == WebSocketState.CONNECTED
            ):
                try:
                    await websocket.close()
                except Exception:
                    pass
