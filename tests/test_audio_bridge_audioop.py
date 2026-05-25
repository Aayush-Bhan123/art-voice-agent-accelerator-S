from __future__ import annotations

import math
import struct
import time
from pathlib import Path

from src.audio_bridge import AudioopAudioBridge
from src.audio_bridge.base import PCM16_FRAME_BYTES_20MS, PCMU_FRAME_BYTES_20MS, pcm16_frame_bytes_20ms

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "audio_bridge"
PCM16_FIXTURE = FIXTURE_DIR / "recording_subrogation_16k_pcm16.wav"
PCMU_FIXTURE = FIXTURE_DIR / "recording_subrogation_8k_pcmu.wav"
TEST_BUFFER_LIMIT_MS = 20_000


def _read_wav_chunks(path: Path) -> tuple[int, int, int, bytes]:
    raw = path.read_bytes()
    if raw[0:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError(f"invalid WAV file: {path}")

    offset = 12
    audio_format = 0
    sample_rate = 0
    bits_per_sample = 0
    data = b""

    while offset + 8 <= len(raw):
        chunk_id = raw[offset : offset + 4]
        chunk_size = int.from_bytes(raw[offset + 4 : offset + 8], "little")
        chunk_start = offset + 8
        chunk_end = chunk_start + chunk_size

        if chunk_id == b"fmt ":
            fmt = raw[chunk_start:chunk_end]
            audio_format = int.from_bytes(fmt[0:2], "little")
            sample_rate = int.from_bytes(fmt[4:8], "little")
            bits_per_sample = int.from_bytes(fmt[14:16], "little")
        elif chunk_id == b"data":
            data = raw[chunk_start:chunk_end]
            break

        offset = chunk_end + (chunk_size % 2)

    if not data:
        raise ValueError(f"missing data chunk in {path}")
    return audio_format, sample_rate, bits_per_sample, data


def _pcm16_correlation(a: bytes, b: bytes) -> float:
    sample_count = min(len(a), len(b)) // 2
    if sample_count == 0:
        return 0.0

    a_samples = struct.unpack(f"<{sample_count}h", a[: sample_count * 2])
    b_samples = struct.unpack(f"<{sample_count}h", b[: sample_count * 2])

    dot = 0.0
    norm_a = 0.0
    norm_b = 0.0
    for i in range(sample_count):
        av = float(a_samples[i])
        bv = float(b_samples[i])
        dot += av * bv
        norm_a += av * av
        norm_b += bv * bv

    if norm_a <= 0.0 or norm_b <= 0.0:
        return 0.0
    return dot / math.sqrt(norm_a * norm_b)


def _stream_pcmu_to_pcm16(bridge: AudioopAudioBridge, pcmu_data: bytes) -> bytes:
    out = bytearray()
    for idx in range(0, len(pcmu_data), PCMU_FRAME_BYTES_20MS):
        bridge.push_pcmu(pcmu_data[idx : idx + PCMU_FRAME_BYTES_20MS])
        chunk = bridge.pop_pcm16()
        if chunk:
            out.extend(chunk)
    # No drain needed — audioop converts synchronously
    chunk = bridge.pop_pcm16()
    if chunk:
        out.extend(chunk)
    return bytes(out)


def _stream_pcm16_to_pcmu(bridge: AudioopAudioBridge, pcm16_data: bytes) -> bytes:
    out = bytearray()
    for idx in range(0, len(pcm16_data), PCM16_FRAME_BYTES_20MS):
        bridge.push_pcm16(pcm16_data[idx : idx + PCM16_FRAME_BYTES_20MS])
        chunk = bridge.pop_pcmu()
        if chunk:
            out.extend(chunk)
    chunk = bridge.pop_pcmu()
    if chunk:
        out.extend(chunk)
    return bytes(out)


def test_fixtures_present_and_expected_formats() -> None:
    assert PCM16_FIXTURE.exists(), f"missing fixture: {PCM16_FIXTURE}"
    assert PCMU_FIXTURE.exists(), f"missing fixture: {PCMU_FIXTURE}"

    pcm16_fmt, pcm16_rate, pcm16_bits, _ = _read_wav_chunks(PCM16_FIXTURE)
    pcmu_fmt, pcmu_rate, pcmu_bits, _ = _read_wav_chunks(PCMU_FIXTURE)

    assert pcm16_fmt == 1
    assert pcm16_rate == 16000
    assert pcm16_bits == 16

    assert pcmu_fmt == 7
    assert pcmu_rate == 8000
    assert pcmu_bits == 8


def test_pcmu_fixture_to_pcm16_matches_golden() -> None:
    _, _, _, pcmu_data = _read_wav_chunks(PCMU_FIXTURE)
    _, _, _, pcm16_golden = _read_wav_chunks(PCM16_FIXTURE)

    bridge = AudioopAudioBridge(buffer_limit_ms=TEST_BUFFER_LIMIT_MS)
    try:
        pcm16 = _stream_pcmu_to_pcm16(bridge, pcmu_data)
        assert pcm16
        assert len(pcm16) % PCM16_FRAME_BYTES_20MS == 0

        corr = _pcm16_correlation(pcm16, pcm16_golden)
        assert corr > 0.80
    finally:
        bridge.close()


def test_pcm16_fixture_roundtrip_preserves_signal() -> None:
    _, _, _, pcm16_golden = _read_wav_chunks(PCM16_FIXTURE)

    bridge = AudioopAudioBridge(buffer_limit_ms=TEST_BUFFER_LIMIT_MS)
    try:
        pcmu = _stream_pcm16_to_pcmu(bridge, pcm16_golden)
        assert pcmu
        assert len(pcmu) % PCMU_FRAME_BYTES_20MS == 0

        pcm16_roundtrip = _stream_pcmu_to_pcm16(bridge, pcmu)
        assert pcm16_roundtrip
        assert len(pcm16_roundtrip) % PCM16_FRAME_BYTES_20MS == 0

        corr = _pcm16_correlation(pcm16_roundtrip, pcm16_golden)
        assert corr > 0.65
    finally:
        bridge.close()


def test_pcmu_to_pcm16_24k_produces_output() -> None:
    """Verify the 24kHz bridge variant (for VoiceLive/Realtime) produces valid output."""
    _, _, _, pcmu_data = _read_wav_chunks(PCMU_FIXTURE)
    _, _, _, pcm16_16k_golden = _read_wav_chunks(PCM16_FIXTURE)

    frame_bytes_24k = pcm16_frame_bytes_20ms(24000)  # 960
    bridge = AudioopAudioBridge(buffer_limit_ms=TEST_BUFFER_LIMIT_MS, pcm16_sample_rate=24000)
    try:
        # Forward: PCMU 8kHz -> PCM16 24kHz
        pcm16_24k = bridge.transcode_pcmu_to_pcm16(pcmu_data)
        assert pcm16_24k
        assert len(pcm16_24k) % frame_bytes_24k == 0
        # 24kHz output should be ~50% larger than 16kHz output for same source
        assert len(pcm16_24k) > len(pcm16_16k_golden) * 1.3

        # Reverse: PCM16 24kHz -> PCMU 8kHz
        pcmu_rt = bridge.transcode_pcm16_to_pcmu(pcm16_24k)
        assert pcmu_rt
        assert len(pcmu_rt) % PCMU_FRAME_BYTES_20MS == 0
        # PCMU output should be roughly same size as original
        ratio = len(pcmu_rt) / len(pcmu_data)
        assert 0.85 < ratio < 1.15
    finally:
        bridge.close()
