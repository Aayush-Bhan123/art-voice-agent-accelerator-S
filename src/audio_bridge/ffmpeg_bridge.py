from __future__ import annotations

import math
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

_MAX_SAMPLES = 5000  # ~100s of 20ms frames

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
        self._lock = threading.Lock()
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
            *self._input_args,
            *self._output_args,
        ]
        self._process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
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
        with self._lock:
            usable = len(self._buffer) - (len(self._buffer) % self._frame_bytes)
            if usable <= 0:
                return b""
            out = bytes(self._buffer[:usable])
            del self._buffer[:usable]
            return out

    def buffer_depth_ms(self, bytes_per_second: int) -> float:
        if bytes_per_second <= 0:
            return 0.0
        with self._lock:
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
        process = self._process
        if process is None or process.stdout is None:
            return
        try:
            while not self._closed:
                chunk = process.stdout.read(self._frame_bytes)
                if not chunk:
                    break
                with self._lock:
                    total = len(self._buffer) + len(chunk)
                    if total > self._max_buffer_bytes:
                        overflow = total - self._max_buffer_bytes
                        drop_bytes = min(overflow, len(self._buffer))
                        drop_bytes -= drop_bytes % self._frame_bytes
                        if drop_bytes:
                            del self._buffer[:drop_bytes]
                            self._dropped_frames += drop_bytes // self._frame_bytes
                    self._buffer.extend(chunk)
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
        # Transcode timing / RTF tracking
        self._ingress_count = 0
        self._ingress_total_ms = 0.0
        self._ingress_rtf_max = 0.0
        self._egress_count = 0
        self._egress_total_ms = 0.0
        self._egress_rtf_max = 0.0
        # Per-call sample deques for percentile analytics
        self._ingress_rtf_samples: deque[float] = deque(maxlen=_MAX_SAMPLES)
        self._ingress_latency_samples: deque[float] = deque(maxlen=_MAX_SAMPLES)
        self._egress_rtf_samples: deque[float] = deque(maxlen=_MAX_SAMPLES)
        self._egress_latency_samples: deque[float] = deque(maxlen=_MAX_SAMPLES)

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
        t0 = time.perf_counter()
        out = deque()
        chunk_size = PCMU_FRAME_BYTES_20MS * 10
        for idx in range(0, len(pcmu_bytes), chunk_size):
            self.push_pcmu(pcmu_bytes[idx : idx + chunk_size])
            drained = self._drain_with_timeout(self.pop_pcm16, timeout_s=min(timeout_s, 0.15))
            if drained:
                out.append(drained)
        tail = self._drain_with_timeout(self.pop_pcm16, timeout_s=timeout_s)
        if tail:
            out.append(tail)
        result = b"".join(out)
        self._record_transcode("ingress", t0, len(pcmu_bytes), PCMU_SAMPLE_RATE, 1)
        return result

    def transcode_pcm16_to_pcmu(self, pcm16_bytes: bytes, *, timeout_s: float = 0.25) -> bytes:
        if not pcm16_bytes:
            return b""
        t0 = time.perf_counter()
        out = deque()
        chunk_size = self._pcm16_frame_bytes * 10
        for idx in range(0, len(pcm16_bytes), chunk_size):
            self.push_pcm16(pcm16_bytes[idx : idx + chunk_size])
            drained = self._drain_with_timeout(self.pop_pcmu, timeout_s=min(timeout_s, 0.15))
            if drained:
                out.append(drained)
        tail = self._drain_with_timeout(self.pop_pcmu, timeout_s=timeout_s)
        if tail:
            out.append(tail)
        result = b"".join(out)
        self._record_transcode("egress", t0, len(pcm16_bytes), self._pcm16_sample_rate, 2)
        return result

    def _record_transcode(self, direction: str, t0: float, input_bytes: int, sample_rate: int, bytes_per_sample: int) -> None:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        audio_duration_ms = (input_bytes / (sample_rate * bytes_per_sample)) * 1000.0 if input_bytes else 0.0
        rtf = (elapsed_ms / audio_duration_ms) if audio_duration_ms > 0 else 0.0

        if direction == "ingress":
            self._ingress_count += 1
            self._ingress_total_ms += elapsed_ms
            self._ingress_rtf_max = max(self._ingress_rtf_max, rtf)
            self._ingress_rtf_samples.append(rtf)
            self._ingress_latency_samples.append(elapsed_ms)
        else:
            self._egress_count += 1
            self._egress_total_ms += elapsed_ms
            self._egress_rtf_max = max(self._egress_rtf_max, rtf)
            self._egress_rtf_samples.append(rtf)
            self._egress_latency_samples.append(elapsed_ms)

        logger.info(
            "FFmpeg %s transcode | bytes=%d duration_ms=%.1f transcode_ms=%.2f RTF=%.4f",
            direction, input_bytes, audio_duration_ms, elapsed_ms, rtf,
        )

    def get_stats(self) -> AudioBridgeStats:
        dropped = self._u2l.dropped_frames + self._l2u.dropped_frames
        depth_ms = max(
            self._u2l.buffer_depth_ms(self._pcm16_sample_rate * 2),
            self._l2u.buffer_depth_ms(PCMU_SAMPLE_RATE),
        )
        ingress_avg_ms = (self._ingress_total_ms / self._ingress_count) if self._ingress_count else 0.0
        ingress_avg_rtf = 0.0
        if self._ingress_count and self._ingress_total_ms > 0:
            # Approximate average RTF from aggregate totals
            total_audio_ms = (self._bytes_in / max(PCMU_SAMPLE_RATE, 1)) * 1000.0
            ingress_avg_rtf = (self._ingress_total_ms / total_audio_ms) if total_audio_ms > 0 else 0.0
        egress_avg_ms = (self._egress_total_ms / self._egress_count) if self._egress_count else 0.0
        egress_avg_rtf = 0.0
        if self._egress_count and self._egress_total_ms > 0:
            total_audio_out_ms = (self._bytes_out / max(PCMU_SAMPLE_RATE, 1)) * 1000.0
            egress_avg_rtf = (self._egress_total_ms / total_audio_out_ms) if total_audio_out_ms > 0 else 0.0
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
            ingress_transcode_count=self._ingress_count,
            ingress_transcode_total_ms=self._ingress_total_ms,
            ingress_transcode_avg_ms=ingress_avg_ms,
            ingress_rtf_avg=ingress_avg_rtf,
            ingress_rtf_max=self._ingress_rtf_max,
            egress_transcode_count=self._egress_count,
            egress_transcode_total_ms=self._egress_total_ms,
            egress_transcode_avg_ms=egress_avg_ms,
            egress_rtf_avg=egress_avg_rtf,
            egress_rtf_max=self._egress_rtf_max,
        )

    def get_percentile_stats(self) -> dict[str, float]:
        """Compute percentile analytics from stored per-call samples.

        Returns a flat dict with P50/P90/P95/P99/max for both directions'
        RTF and latency, plus current uptime and sample counts.
        """
        result: dict[str, float] = {
            "uptime_s": max(0.0, time.monotonic() - self._started_at),
        }
        for prefix, rtf_samples, lat_samples in (
            ("ingress", self._ingress_rtf_samples, self._ingress_latency_samples),
            ("egress", self._egress_rtf_samples, self._egress_latency_samples),
        ):
            result[f"{prefix}_n"] = float(len(rtf_samples))
            for metric_name, samples in (("rtf", rtf_samples), ("latency_ms", lat_samples)):
                for label, q in (("p50", 50), ("p90", 90), ("p95", 95), ("p99", 99)):
                    result[f"{prefix}_{metric_name}_{label}"] = _percentile(samples, q)
                result[f"{prefix}_{metric_name}_max"] = max(samples) if samples else 0.0
        return result

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
                quiet_deadline = min(deadline, time.monotonic() + 0.03)
                continue
            if saw_data and time.monotonic() >= quiet_deadline:
                break
            time.sleep(0.002)
        return b"".join(chunks)


# ---- Percentile helper & periodic analytics reporter ----

def _percentile(samples: deque[float], q: int) -> float:
    """Compute the *q*-th percentile from a deque of floats (nearest-rank)."""
    n = len(samples)
    if n == 0:
        return 0.0
    sorted_s = sorted(samples)
    idx = int(math.ceil(q / 100.0 * n)) - 1
    return sorted_s[max(idx, 0)]


async def start_bridge_periodic_analytics(
    bridge: FfmpegAudioBridge,
    session_id: str,
    *,
    interval_s: float = 30.0,
) -> "asyncio.Task[None]":
    """Launch a background asyncio task that logs percentile analytics every *interval_s*.

    Returns the task so the caller can cancel it during bridge teardown.
    """
    import asyncio

    async def _loop() -> None:
        short_id = session_id[-8:] if session_id else "unknown"
        while True:
            await asyncio.sleep(interval_s)
            try:
                p = bridge.get_percentile_stats()
            except Exception:
                break  # bridge likely closed
            logger.info(
                "FFmpeg bridge periodic analytics | session=%s uptime_s=%.1f "
                "| INGRESS (n=%.0f) rtf=[p50=%.4f p90=%.4f p95=%.4f p99=%.4f max=%.4f] "
                "latency_ms=[p50=%.2f p90=%.2f p95=%.2f p99=%.2f max=%.2f] "
                "| EGRESS (n=%.0f) rtf=[p50=%.4f p90=%.4f p95=%.4f p99=%.4f max=%.4f] "
                "latency_ms=[p50=%.2f p90=%.2f p95=%.2f p99=%.2f max=%.2f]",
                short_id,
                p["uptime_s"],
                p["ingress_n"],
                p["ingress_rtf_p50"], p["ingress_rtf_p90"],
                p["ingress_rtf_p95"], p["ingress_rtf_p99"], p["ingress_rtf_max"],
                p["ingress_latency_ms_p50"], p["ingress_latency_ms_p90"],
                p["ingress_latency_ms_p95"], p["ingress_latency_ms_p99"], p["ingress_latency_ms_max"],
                p["egress_n"],
                p["egress_rtf_p50"], p["egress_rtf_p90"],
                p["egress_rtf_p95"], p["egress_rtf_p99"], p["egress_rtf_max"],
                p["egress_latency_ms_p50"], p["egress_latency_ms_p90"],
                p["egress_latency_ms_p95"], p["egress_latency_ms_p99"], p["egress_latency_ms_max"],
            )

    task = asyncio.create_task(_loop(), name=f"bridge-analytics-{session_id[-8:]}")
    return task
