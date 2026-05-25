from __future__ import annotations

import asyncio
import math
import shutil
import struct
import time
from pathlib import Path

import pytest

from src.audio_bridge import FfmpegAudioBridge, start_bridge_periodic_analytics
from src.audio_bridge.base import PCM16_FRAME_BYTES_20MS, PCMU_FRAME_BYTES_20MS, pcm16_frame_bytes_20ms


pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")

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


def _stream_pcmu_to_pcm16(bridge: FfmpegAudioBridge, pcmu_data: bytes) -> bytes:
    out = bytearray()
    for idx in range(0, len(pcmu_data), PCMU_FRAME_BYTES_20MS):
        bridge.push_pcmu(pcmu_data[idx : idx + PCMU_FRAME_BYTES_20MS])
        while True:
            chunk = bridge.pop_pcm16()
            if not chunk:
                break
            out.extend(chunk)

    # Drain remaining buffered output from ffmpeg pipe.
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        chunk = bridge.pop_pcm16()
        if chunk:
            out.extend(chunk)
            deadline = time.monotonic() + 0.05
        else:
            time.sleep(0.002)
    return bytes(out)


def _stream_pcm16_to_pcmu(bridge: FfmpegAudioBridge, pcm16_data: bytes) -> bytes:
    out = bytearray()
    for idx in range(0, len(pcm16_data), PCM16_FRAME_BYTES_20MS):
        bridge.push_pcm16(pcm16_data[idx : idx + PCM16_FRAME_BYTES_20MS])
        while True:
            chunk = bridge.pop_pcmu()
            if not chunk:
                break
            out.extend(chunk)

    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline:
        chunk = bridge.pop_pcmu()
        if chunk:
            out.extend(chunk)
            deadline = time.monotonic() + 0.05
        else:
            time.sleep(0.002)
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

    bridge = FfmpegAudioBridge(buffer_limit_ms=TEST_BUFFER_LIMIT_MS)
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

    bridge = FfmpegAudioBridge(buffer_limit_ms=TEST_BUFFER_LIMIT_MS)
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
    bridge = FfmpegAudioBridge(buffer_limit_ms=TEST_BUFFER_LIMIT_MS, pcm16_sample_rate=24000)
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


def test_transcode_stats_and_rtf_logging(caplog: pytest.LogCaptureFixture) -> None:
    """Verify that transcode calls populate RTF stats and emit log lines."""
    _, _, _, pcmu_data = _read_wav_chunks(PCMU_FIXTURE)

    bridge = FfmpegAudioBridge(buffer_limit_ms=TEST_BUFFER_LIMIT_MS)
    try:
        with caplog.at_level("INFO"):
            pcm16 = bridge.transcode_pcmu_to_pcm16(pcmu_data)
            assert pcm16
            pcmu_rt = bridge.transcode_pcm16_to_pcmu(pcm16)
            assert pcmu_rt

        # --- Validate log output contains RTF lines ---
        ingress_logs = [r for r in caplog.records if "FFmpeg ingress transcode" in r.message]
        egress_logs = [r for r in caplog.records if "FFmpeg egress transcode" in r.message]
        assert len(ingress_logs) >= 1, "Expected at least one ingress transcode log"
        assert len(egress_logs) >= 1, "Expected at least one egress transcode log"
        assert "RTF=" in ingress_logs[0].message
        assert "transcode_ms=" in ingress_logs[0].message

        # --- Validate get_stats() returns populated RTF fields ---
        stats = bridge.get_stats()
        assert stats.ingress_transcode_count >= 1
        assert stats.egress_transcode_count >= 1
        assert stats.ingress_transcode_avg_ms > 0.0
        assert stats.egress_transcode_avg_ms > 0.0
        # RTF should be well under 1.0 (faster than real-time)
        assert 0.0 < stats.ingress_rtf_max < 1.0, f"ingress RTF too high: {stats.ingress_rtf_max}"
        assert 0.0 < stats.egress_rtf_max < 1.0, f"egress RTF too high: {stats.egress_rtf_max}"
    finally:
        bridge.close()


def test_get_percentile_stats() -> None:
    """Verify percentile stats are populated after transcode calls."""
    _, _, _, pcmu_data = _read_wav_chunks(PCMU_FIXTURE)

    bridge = FfmpegAudioBridge(buffer_limit_ms=TEST_BUFFER_LIMIT_MS)
    try:
        pcm16 = bridge.transcode_pcmu_to_pcm16(pcmu_data)
        assert pcm16
        _ = bridge.transcode_pcm16_to_pcmu(pcm16)

        p = bridge.get_percentile_stats()
        assert p["ingress_n"] >= 1
        assert p["egress_n"] >= 1
        for direction in ("ingress", "egress"):
            for metric in ("rtf", "latency_ms"):
                assert p[f"{direction}_{metric}_p50"] > 0.0
                assert p[f"{direction}_{metric}_p99"] >= p[f"{direction}_{metric}_p50"]
                assert p[f"{direction}_{metric}_max"] >= p[f"{direction}_{metric}_p99"]
            # RTF should be well under 1.0
            assert p[f"{direction}_rtf_max"] < 1.0, f"{direction} RTF too high"
    finally:
        bridge.close()


@pytest.mark.asyncio
async def test_periodic_analytics_emits_log(caplog: pytest.LogCaptureFixture) -> None:
    """Verify the periodic analytics task emits a log line after the interval."""
    _, _, _, pcmu_data = _read_wav_chunks(PCMU_FIXTURE)

    bridge = FfmpegAudioBridge(buffer_limit_ms=TEST_BUFFER_LIMIT_MS)
    try:
        # Generate some samples
        pcm16 = bridge.transcode_pcmu_to_pcm16(pcmu_data)
        _ = bridge.transcode_pcm16_to_pcmu(pcm16)

        # Start analytics with a very short interval for testing
        with caplog.at_level("INFO"):
            task = await start_bridge_periodic_analytics(
                bridge, "test-session-12345678", interval_s=0.1,
            )
            await asyncio.sleep(0.3)  # Let at least one tick fire
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

        periodic_logs = [r for r in caplog.records if "periodic analytics" in r.message]
        assert len(periodic_logs) >= 1, "Expected at least one periodic analytics log"
        msg = periodic_logs[0].message
        assert "INGRESS" in msg
        assert "EGRESS" in msg
        assert "p50=" in msg
        assert "p99=" in msg
    finally:
        bridge.close()
