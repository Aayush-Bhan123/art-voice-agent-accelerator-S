from __future__ import annotations

import audioop
import time

from src.audio_bridge.base import (
    AudioBridgeStats,
    PCMU_FRAME_BYTES_20MS,
    PCMU_SAMPLE_RATE,
    PCM16_SAMPLE_RATE,
    pcm16_frame_bytes_20ms,
)


class AudioopAudioBridge:
    """Zero-latency PCMU<->PCM16 bridge using CPython's audioop C extension.

    Eliminates FFmpeg subprocesses, pipes, threads, and polling entirely.
    Each conversion call completes in ~3µs per 20ms frame (vs milliseconds
    with FFmpeg).  Uses audioop.ratecv state to maintain sample continuity
    across streaming push/pop calls.
    """

    def __init__(self, *, buffer_limit_ms: int = 500, pcm16_sample_rate: int = PCM16_SAMPLE_RATE) -> None:
        self._started_at = time.monotonic()
        self._pcm16_sample_rate = pcm16_sample_rate
        self._pcm16_frame_bytes = pcm16_frame_bytes_20ms(pcm16_sample_rate)

        # Streaming output buffers
        self._pcm16_buf = bytearray()
        self._pcmu_buf = bytearray()

        # ratecv state for continuous resampling across frames
        self._u2l_state = None  # 8k -> target rate
        self._l2u_state = None  # target rate -> 8k

        # Buffer limits
        pcm16_bps = pcm16_sample_rate * 2
        pcmu_bps = PCMU_SAMPLE_RATE
        self._max_pcm16_bytes = max(int((buffer_limit_ms / 1000.0) * pcm16_bps), self._pcm16_frame_bytes)
        self._max_pcmu_bytes = max(int((buffer_limit_ms / 1000.0) * pcmu_bps), PCMU_FRAME_BYTES_20MS)

        # Stats
        self._frames_in = 0
        self._frames_out = 0
        self._bytes_in = 0
        self._bytes_out = 0
        self._dropped_frames = 0

    def push_pcmu(self, pcmu_bytes: bytes, *, frame_ms: int | None = None) -> None:
        del frame_ms
        if not pcmu_bytes:
            return
        self._bytes_in += len(pcmu_bytes)
        self._frames_in += max(len(pcmu_bytes) // PCMU_FRAME_BYTES_20MS, 1)

        # µ-law decode to PCM16 at 8kHz
        pcm16_8k = audioop.ulaw2lin(pcmu_bytes, 2)

        # Resample 8kHz -> target rate (16k/24k)
        if self._pcm16_sample_rate != PCMU_SAMPLE_RATE:
            pcm16_out, self._u2l_state = audioop.ratecv(
                pcm16_8k, 2, 1, PCMU_SAMPLE_RATE, self._pcm16_sample_rate, self._u2l_state,
            )
        else:
            pcm16_out = pcm16_8k

        # Overflow protection
        total = len(self._pcm16_buf) + len(pcm16_out)
        if total > self._max_pcm16_bytes:
            overflow = total - self._max_pcm16_bytes
            drop = min(overflow, len(self._pcm16_buf))
            drop -= drop % self._pcm16_frame_bytes
            if drop:
                del self._pcm16_buf[:drop]
                self._dropped_frames += drop // self._pcm16_frame_bytes
        self._pcm16_buf.extend(pcm16_out)

    def pop_pcm16(self) -> bytes:
        usable = len(self._pcm16_buf) - (len(self._pcm16_buf) % self._pcm16_frame_bytes)
        if usable <= 0:
            return b""
        out = bytes(self._pcm16_buf[:usable])
        del self._pcm16_buf[:usable]
        self._bytes_out += len(out)
        self._frames_out += len(out) // self._pcm16_frame_bytes
        return out

    def push_pcm16(self, pcm16_bytes: bytes, *, frame_ms: int | None = None) -> None:
        del frame_ms
        if not pcm16_bytes:
            return
        self._bytes_in += len(pcm16_bytes)
        self._frames_in += max(len(pcm16_bytes) // self._pcm16_frame_bytes, 1)

        # Resample target rate -> 8kHz
        if self._pcm16_sample_rate != PCMU_SAMPLE_RATE:
            pcm16_8k, self._l2u_state = audioop.ratecv(
                pcm16_bytes, 2, 1, self._pcm16_sample_rate, PCMU_SAMPLE_RATE, self._l2u_state,
            )
        else:
            pcm16_8k = pcm16_bytes

        # PCM16 encode to µ-law
        pcmu_out = audioop.lin2ulaw(pcm16_8k, 2)

        # Overflow protection
        total = len(self._pcmu_buf) + len(pcmu_out)
        if total > self._max_pcmu_bytes:
            overflow = total - self._max_pcmu_bytes
            drop = min(overflow, len(self._pcmu_buf))
            drop -= drop % PCMU_FRAME_BYTES_20MS
            if drop:
                del self._pcmu_buf[:drop]
                self._dropped_frames += drop // PCMU_FRAME_BYTES_20MS
        self._pcmu_buf.extend(pcmu_out)

    def pop_pcmu(self) -> bytes:
        usable = len(self._pcmu_buf) - (len(self._pcmu_buf) % PCMU_FRAME_BYTES_20MS)
        if usable <= 0:
            return b""
        out = bytes(self._pcmu_buf[:usable])
        del self._pcmu_buf[:usable]
        self._bytes_out += len(out)
        self._frames_out += len(out) // PCMU_FRAME_BYTES_20MS
        return out

    def transcode_pcmu_to_pcm16(self, pcmu_bytes: bytes, *, timeout_s: float = 0.25) -> bytes:
        del timeout_s
        if not pcmu_bytes:
            return b""
        pcm16_8k = audioop.ulaw2lin(pcmu_bytes, 2)
        if self._pcm16_sample_rate != PCMU_SAMPLE_RATE:
            pcm16_out, self._u2l_state = audioop.ratecv(
                pcm16_8k, 2, 1, PCMU_SAMPLE_RATE, self._pcm16_sample_rate, self._u2l_state,
            )
        else:
            pcm16_out = pcm16_8k
        # Trim to frame boundary
        usable = len(pcm16_out) - (len(pcm16_out) % self._pcm16_frame_bytes)
        if usable < len(pcm16_out):
            pcm16_out = pcm16_out[:usable]
        self._bytes_in += len(pcmu_bytes)
        self._bytes_out += len(pcm16_out)
        return pcm16_out

    def transcode_pcm16_to_pcmu(self, pcm16_bytes: bytes, *, timeout_s: float = 0.25) -> bytes:
        del timeout_s
        if not pcm16_bytes:
            return b""
        if self._pcm16_sample_rate != PCMU_SAMPLE_RATE:
            pcm16_8k, self._l2u_state = audioop.ratecv(
                pcm16_bytes, 2, 1, self._pcm16_sample_rate, PCMU_SAMPLE_RATE, self._l2u_state,
            )
        else:
            pcm16_8k = pcm16_bytes
        pcmu_out = audioop.lin2ulaw(pcm16_8k, 2)
        # Trim to frame boundary
        usable = len(pcmu_out) - (len(pcmu_out) % PCMU_FRAME_BYTES_20MS)
        if usable < len(pcmu_out):
            pcmu_out = pcmu_out[:usable]
        self._bytes_in += len(pcm16_bytes)
        self._bytes_out += len(pcmu_out)
        return pcmu_out

    def get_stats(self) -> AudioBridgeStats:
        pcm16_bps = self._pcm16_sample_rate * 2
        pcm16_depth_ms = (len(self._pcm16_buf) / pcm16_bps) * 1000.0 if pcm16_bps else 0.0
        pcmu_depth_ms = (len(self._pcmu_buf) / PCMU_SAMPLE_RATE) * 1000.0
        return AudioBridgeStats(
            active_input_codec="pcmu",
            active_output_codec="pcm16",
            input_sample_rate=PCMU_SAMPLE_RATE,
            output_sample_rate=self._pcm16_sample_rate,
            frames_in=self._frames_in,
            frames_out=self._frames_out,
            bytes_in=self._bytes_in,
            bytes_out=self._bytes_out,
            buffer_depth_ms=max(pcm16_depth_ms, pcmu_depth_ms),
            dropped_frames=self._dropped_frames,
            ffmpeg_pid_u2l=None,
            ffmpeg_pid_l2u=None,
            ffmpeg_restarts=0,
            bridge_uptime_s=max(0.0, time.monotonic() - self._started_at),
        )

    def close(self) -> None:
        self._pcm16_buf.clear()
        self._pcmu_buf.clear()
        self._u2l_state = None
        self._l2u_state = None
