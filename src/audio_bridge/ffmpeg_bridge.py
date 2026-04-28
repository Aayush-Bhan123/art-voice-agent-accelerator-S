from __future__ import annotations

import io
import shutil
import subprocess
import threading
import time
from collections import deque

from utils.ml_logging import get_logger

from src.audio_bridge.base import (
    AudioBridgeStats,
    PCM16_FRAME_BYTES_20MS,
    PCM16_SAMPLE_RATE,
    PCMU_FRAME_BYTES_20MS,
    PCMU_SAMPLE_RATE,
    pcm16_frame_bytes_20ms,
)

logger = get_logger(__name__)


class _FfmpegPipe:
    """Single direction FFmpeg transform pipe backed by stdin/stdout."""

    def __init__(
        self,
        *,
        input_args: list[str],
        output_args: list[str],
        frame_bytes: int,
        max_buffer_bytes: int,
        name: str,
    ) -> None:
        self._input_args = input_args
        self._output_args = output_args
        self._frame_bytes = frame_bytes
        self._max_buffer_bytes = max_buffer_bytes
        self._name = name
        self._process: subprocess.Popen[bytes] | None = None
        self._buffer = bytearray()
        self._data_ready = threading.Condition()
        self._write_lock = threading.Lock()
        self._closed = False
        self._dropped_frames = 0
        self._reader_thread: threading.Thread | None = None

    @property
    def pid(self) -> int | None:
        return self._process.pid if self._process else None

    @property
    def dropped_frames(self) -> int:
        return self._dropped_frames

    def start(self) -> None:
        if self._process is not None:
            return
        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            "-flags",
            "low_delay",
            "-probesize",
            "512",
            "-analyzeduration",
            "0",
            *self._input_args,
            "-flush_packets",
            "1",
            *self._output_args,
        ]
        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        # Wrap raw stdout in a small BufferedReader so read1() is available.
        # read1() returns whatever bytes are ready (up to frame_bytes) without
        # blocking for a full frame — this is the single biggest latency win.
        raw_stdout = self._process.stdout
        self._buffered_stdout = io.BufferedReader(raw_stdout, buffer_size=self._frame_bytes)
        self._reader_thread = threading.Thread(target=self._reader_loop, daemon=True)
        self._reader_thread.start()

    def push(self, payload: bytes) -> None:
        if not payload:
            return
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError(f"{self._name} ffmpeg process is not running")
        with self._write_lock:
            process.stdin.write(payload)
            process.stdin.flush()

    def pop(self) -> bytes:
        with self._data_ready:
            usable = len(self._buffer) - (len(self._buffer) % self._frame_bytes)
            if usable <= 0:
                return b""
            out = bytes(self._buffer[:usable])
            del self._buffer[:usable]
            return out

    def wait_and_pop(self, timeout_s: float) -> bytes:
        """Block until data is available or timeout expires, then pop."""
        with self._data_ready:
            usable = len(self._buffer) - (len(self._buffer) % self._frame_bytes)
            if usable <= 0:
                self._data_ready.wait(timeout=timeout_s)
                usable = len(self._buffer) - (len(self._buffer) % self._frame_bytes)
            if usable <= 0:
                return b""
            out = bytes(self._buffer[:usable])
            del self._buffer[:usable]
            return out

    def buffer_depth_ms(self, bytes_per_second: int) -> float:
        if bytes_per_second <= 0:
            return 0.0
        with self._data_ready:
            return (len(self._buffer) / float(bytes_per_second)) * 1000.0

    def close(self) -> None:
        self._closed = True
        process = self._process
        if not process:
            return
        try:
            if process.stdin:
                process.stdin.close()
        except Exception:
            pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
        self._process = None

    def _reader_loop(self) -> None:
        stdout = self._buffered_stdout
        if stdout is None:
            return
        try:
            while not self._closed:
                chunk = stdout.read1(self._frame_bytes)
                if not chunk:
                    break
                with self._data_ready:
                    total = len(self._buffer) + len(chunk)
                    if total > self._max_buffer_bytes:
                        overflow = total - self._max_buffer_bytes
                        drop_bytes = min(overflow, len(self._buffer))
                        drop_bytes -= drop_bytes % self._frame_bytes
                        if drop_bytes:
                            del self._buffer[:drop_bytes]
                            self._dropped_frames += drop_bytes // self._frame_bytes
                    self._buffer.extend(chunk)
                    self._data_ready.notify_all()
        except Exception as exc:
            logger.warning("Audio bridge reader %s stopped: %s", self._name, exc)


class FfmpegAudioBridge:
    """FFmpeg-backed bidirectional bridge for PCMU<->PCM16 conversion."""

    def __init__(self, *, buffer_limit_ms: int = 500, pcm16_sample_rate: int = PCM16_SAMPLE_RATE) -> None:
        if shutil.which("ffmpeg") is None:
            raise RuntimeError("ffmpeg executable not found in PATH")

        self._started_at = time.monotonic()
        self._pcm16_sample_rate = pcm16_sample_rate
        self._pcm16_frame_bytes = pcm16_frame_bytes_20ms(pcm16_sample_rate)
        pcm16_bps = pcm16_sample_rate * 2
        pcmu_bps = PCMU_SAMPLE_RATE
        max_u2l = max(int((buffer_limit_ms / 1000.0) * pcm16_bps), self._pcm16_frame_bytes)
        max_l2u = max(int((buffer_limit_ms / 1000.0) * pcmu_bps), PCMU_FRAME_BYTES_20MS)
        pcm16_rate_str = str(pcm16_sample_rate)
        self._u2l = _FfmpegPipe(
            input_args=["-f", "mulaw", "-ar", "8000", "-ac", "1", "-i", "pipe:0"],
            output_args=["-f", "s16le", "-ar", pcm16_rate_str, "-ac", "1", "pipe:1"],
            frame_bytes=self._pcm16_frame_bytes,
            max_buffer_bytes=max_u2l,
            name="u2l",
        )
        self._l2u = _FfmpegPipe(
            input_args=["-f", "s16le", "-ar", pcm16_rate_str, "-ac", "1", "-i", "pipe:0"],
            output_args=["-f", "mulaw", "-ar", "8000", "-ac", "1", "pipe:1"],
            frame_bytes=PCMU_FRAME_BYTES_20MS,
            max_buffer_bytes=max_l2u,
            name="l2u",
        )
        self._u2l.start()
        self._l2u.start()
        self._frames_in = 0
        self._frames_out = 0
        self._bytes_in = 0
        self._bytes_out = 0

    def push_pcmu(self, pcmu_bytes: bytes, *, frame_ms: int | None = None) -> None:
        del frame_ms
        if not pcmu_bytes:
            return
        self._u2l.push(pcmu_bytes)
        self._bytes_in += len(pcmu_bytes)
        self._frames_in += max(len(pcmu_bytes) // PCMU_FRAME_BYTES_20MS, 1)

    def pop_pcm16(self) -> bytes:
        out = self._u2l.pop()
        if out:
            self._bytes_out += len(out)
            self._frames_out += max(len(out) // self._pcm16_frame_bytes, 1)
        return out

    def push_pcm16(self, pcm16_bytes: bytes, *, frame_ms: int | None = None) -> None:
        del frame_ms
        if not pcm16_bytes:
            return
        self._l2u.push(pcm16_bytes)
        self._bytes_in += len(pcm16_bytes)
        self._frames_in += max(len(pcm16_bytes) // self._pcm16_frame_bytes, 1)

    def pop_pcmu(self) -> bytes:
        out = self._l2u.pop()
        if out:
            self._bytes_out += len(out)
            self._frames_out += max(len(out) // PCMU_FRAME_BYTES_20MS, 1)
        return out

    def transcode_pcmu_to_pcm16(self, pcmu_bytes: bytes, *, timeout_s: float = 0.25) -> bytes:
        if not pcmu_bytes:
            return b""
        out = deque()
        chunk_size = PCMU_FRAME_BYTES_20MS * 10
        for idx in range(0, len(pcmu_bytes), chunk_size):
            self.push_pcmu(pcmu_bytes[idx : idx + chunk_size])
            drained = self._drain_with_condition(self._u2l, timeout_s=min(timeout_s, 0.15))
            if drained:
                out.append(drained)
        tail = self._drain_with_condition(self._u2l, timeout_s=timeout_s)
        if tail:
            out.append(tail)
        return b"".join(out)

    def transcode_pcm16_to_pcmu(self, pcm16_bytes: bytes, *, timeout_s: float = 0.25) -> bytes:
        if not pcm16_bytes:
            return b""
        out = deque()
        chunk_size = self._pcm16_frame_bytes * 10
        for idx in range(0, len(pcm16_bytes), chunk_size):
            self.push_pcm16(pcm16_bytes[idx : idx + chunk_size])
            drained = self._drain_with_condition(self._l2u, timeout_s=min(timeout_s, 0.15))
            if drained:
                out.append(drained)
        tail = self._drain_with_condition(self._l2u, timeout_s=timeout_s)
        if tail:
            out.append(tail)
        return b"".join(out)

    def get_stats(self) -> AudioBridgeStats:
        dropped = self._u2l.dropped_frames + self._l2u.dropped_frames
        depth_ms = max(
            self._u2l.buffer_depth_ms(self._pcm16_sample_rate * 2),
            self._l2u.buffer_depth_ms(PCMU_SAMPLE_RATE),
        )
        return AudioBridgeStats(
            active_input_codec="pcmu",
            active_output_codec="pcm16",
            input_sample_rate=PCMU_SAMPLE_RATE,
            output_sample_rate=self._pcm16_sample_rate,
            frames_in=self._frames_in,
            frames_out=self._frames_out,
            bytes_in=self._bytes_in,
            bytes_out=self._bytes_out,
            buffer_depth_ms=depth_ms,
            dropped_frames=dropped,
            ffmpeg_pid_u2l=self._u2l.pid,
            ffmpeg_pid_l2u=self._l2u.pid,
            ffmpeg_restarts=0,
            bridge_uptime_s=max(0.0, time.monotonic() - self._started_at),
        )

    def close(self) -> None:
        self._u2l.close()
        self._l2u.close()

    @staticmethod
    def _drain_with_timeout(pop_fn, *, timeout_s: float) -> bytes:
        deadline = time.monotonic() + timeout_s
        chunks = deque()
        saw_data = False
        quiet_deadline = deadline
        while time.monotonic() < deadline:
            chunk = pop_fn()
            if chunk:
                chunks.append(chunk)
                saw_data = True
                # After first data arrives, keep draining until output goes quiet.
                quiet_deadline = min(deadline, time.monotonic() + 0.01)
                continue
            if saw_data and time.monotonic() >= quiet_deadline:
                break
            time.sleep(0.0005)
        return b"".join(chunks)

    @staticmethod
    def _drain_with_condition(pipe, *, timeout_s: float) -> bytes:
        """Drain using Condition-based wait — zero polling overhead."""
        deadline = time.monotonic() + timeout_s
        chunks = deque()
        saw_data = False
        quiet_deadline = deadline
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            # Use short waits: long enough to avoid busy-spin,
            # short enough to detect silence quickly.
            wait = min(remaining, 0.005) if not saw_data else min(remaining, 0.002)
            chunk = pipe.wait_and_pop(timeout_s=wait)
            if chunk:
                chunks.append(chunk)
                saw_data = True
                quiet_deadline = min(deadline, time.monotonic() + 0.005)
                continue
            if saw_data and time.monotonic() >= quiet_deadline:
                break
        return b"".join(chunks)
