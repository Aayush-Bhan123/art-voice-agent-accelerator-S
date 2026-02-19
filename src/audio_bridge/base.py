from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


PCMU_SAMPLE_RATE = 8000
PCM16_SAMPLE_RATE = 16000
PCMU_FRAME_BYTES_20MS = 160
PCM16_FRAME_BYTES_20MS = 640


def pcm16_frame_bytes_20ms(sample_rate: int = PCM16_SAMPLE_RATE) -> int:
    """Compute PCM16 frame size in bytes for a 20ms window at the given rate."""
    return sample_rate * 2 // 50  # 2 bytes per sample, 50 frames per second


@dataclass(slots=True)
class AudioBridgeStats:
    """Runtime bridge statistics for session telemetry."""

    active_input_codec: str = "pcmu"
    active_output_codec: str = "pcm16"
    input_sample_rate: int = PCMU_SAMPLE_RATE
    output_sample_rate: int = PCM16_SAMPLE_RATE
    frames_in: int = 0
    frames_out: int = 0
    bytes_in: int = 0
    bytes_out: int = 0
    buffer_depth_ms: float = 0.0
    dropped_frames: int = 0
    ffmpeg_pid_u2l: int | None = None
    ffmpeg_pid_l2u: int | None = None
    ffmpeg_restarts: int = 0
    bridge_uptime_s: float = 0.0


class AudioBridge(Protocol):
    """Bidirectional PCMU <-> PCM16 bridge interface."""

    def push_pcmu(self, pcmu_bytes: bytes, *, frame_ms: int | None = None) -> None: ...

    def pop_pcm16(self) -> bytes: ...

    def push_pcm16(self, pcm16_bytes: bytes, *, frame_ms: int | None = None) -> None: ...

    def pop_pcmu(self) -> bytes: ...

    def transcode_pcmu_to_pcm16(self, pcmu_bytes: bytes, *, timeout_s: float = 0.25) -> bytes: ...

    def transcode_pcm16_to_pcmu(self, pcm16_bytes: bytes, *, timeout_s: float = 0.25) -> bytes: ...

    def get_stats(self) -> AudioBridgeStats: ...

    def close(self) -> None: ...
