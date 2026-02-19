# Genesys PCMU ↔ PCM16 Audio Bridge — Technical Integration Guide

**Audience:** Technical teams integrating Genesys Cloud with ART (Real-Time Voice Agent Accelerator)
**Last Updated:** February 2026

---

## Important: Two Orchestration Modes — Both Supported

ART supports **two orchestration modes**. The audio bridge is integrated into both:

| Mode | How It Works | Bridge Target Rate | Status |
|---|---|---|---|
| **SpeechCascade** (`MEDIA` stream mode) | Audio → Azure Speech STT → LLM → Azure TTS → Audio | 16 kHz | **Integrated** |
| **VoiceLive** (`VOICE_LIVE` stream mode) | Audio → OpenAI Realtime API (managed STT+LLM+TTS) | 24 kHz | **Integrated** |

Both modes activate with the same `BRIDGE_MODE=pcmu_pcm16` environment variable. The bridge automatically targets the correct sample rate for each mode (16kHz for Azure Speech, 24kHz for OpenAI Realtime).

---

## The Problem You're Hitting

When Genesys sends audio into ART over WebSocket, you're seeing garbled speech, STT failures, or silence. Here's why:

| | Genesys Sends | ART Expects (SpeechCascade) | ART Expects (VoiceLive) |
|---|---|---|---|
| **Codec** | G.711 µ-law (PCMU) | Linear PCM (PCM16) | Linear PCM (PCM16) |
| **Sample Rate** | 8,000 Hz | 16,000 Hz | 24,000 Hz |
| **Bit Depth** | 8-bit (companded) | 16-bit signed LE | 16-bit signed LE |
| **Frame Size** | 160 bytes / 20ms | 640 bytes / 20ms | 960 bytes / 20ms |

These are fundamentally different audio encodings. Feeding µ-law bytes directly into a PCM16 pipeline produces noise — the STT engine can't recognize anything because it's interpreting companded logarithmic samples as linear amplitudes.

---

## What the Bridge Does

The audio bridge sits between the WebSocket ingress and ART's speech processing pipeline. It transparently converts between the two formats in real-time:

```
Genesys Cloud                    ART Voice Pipeline
─────────────                    ──────────────────

Phone call                       Azure Speech STT
    │                                  ▲
    ▼                                  │
Genesys AudioHook ──WebSocket──► ┌─────────────┐ ──► PCM16 @ 16kHz
(PCMU @ 8kHz)                    │ Audio Bridge │
                                 │  (FFmpeg)    │
                                 └─────────────┘
```

**At the code level**, the bridge intercepts the audio bytes after base64-decode but before they're fed to Azure Speech SDK. No changes to your Genesys AudioHook configuration or WebSocket message format are required — the bridge operates on the raw audio payload inside the existing ART message handling.

---

## How It Works (Under the Hood)

### The Transcoding Engine

The bridge uses **FFmpeg**, a battle-tested audio/video processing tool used in virtually every media pipeline on the planet (Netflix, YouTube, Zoom, Twitch — all rely on FFmpeg or its libraries).

Two FFmpeg subprocesses run per session:

| Process | Direction | What It Does |
|---|---|---|
| **U2L** (µ-law → Linear) | Genesys → ART | Decodes PCMU, upsamples 8kHz→16kHz, outputs PCM16 |
| **L2U** (Linear → µ-law) | ART → Genesys | Downsamples 16kHz→8kHz, encodes to PCMU |

Each process uses stdin/stdout pipes — audio bytes go in one side, converted bytes come out the other. There's no disk I/O, no temp files, no network calls. It's pure in-memory streaming.

**FFmpeg command (PCMU → PCM16):**
```
ffmpeg -f mulaw -ar 8000 -ac 1 -i pipe:0 -f s16le -ar 16000 -ac 1 pipe:1
```

This is a well-known, deterministic transformation. µ-law decoding is defined by ITU-T G.711 — there's exactly one correct output for any given input. The resampling from 8kHz to 16kHz uses FFmpeg's default high-quality sinc interpolation filter.

### Bounded Buffering & Backpressure

The bridge maintains bounded output buffers (configurable, default 500ms). If downstream processing stalls:

- **Drop-oldest policy** — old frames are discarded to prevent unbounded memory growth
- **Dropped frame counters** — exposed in bridge stats for monitoring
- **Buffer depth telemetry** — current buffered audio in milliseconds, available for alerting

This means the bridge will never cause an OOM condition or unbounded latency accumulation in production.

### Session Lifecycle

```
Call connects → VoiceHandler.create() → Bridge initialized (2 FFmpeg processes start)
                                              │
Call active   → Each audio frame transcoded in real-time (~1-5ms per frame)
                                              │
Call ends     → VoiceHandler.stop() → Bridge closed (FFmpeg processes terminated)
```

Each call gets its own bridge instance. There's no shared state between calls. If a bridge fails for one call, other calls are unaffected.

---

## What You Need to Do

### 1. Set One Environment Variable

```env
BRIDGE_MODE=pcmu_pcm16
```

That's it. When this is set, every ART session will transcode incoming PCMU audio to PCM16 before feeding it to the speech pipeline.

When `BRIDGE_MODE=off` (the default), the bridge is completely inactive — zero overhead, zero FFmpeg processes. You can toggle this without code changes.

### 2. Optional Configuration

| Variable | Default | Purpose |
|---|---|---|
| `BRIDGE_MODE` | `off` | Set to `pcmu_pcm16` to enable |
| `AUDIO_BRIDGE_BUFFER_LIMIT_MS` | `500` | Max buffered audio before dropping frames. Increase if you see dropped frames in telemetry, decrease for tighter latency control |
| `AUDIO_BRIDGE_FAIL_CLOSED` | `true` | If the bridge fails to initialize, should the session fail (`true`) or fall back to raw passthrough (`false`). Recommend `true` for production — a silent passthrough of PCMU as if it were PCM16 will produce garbage |

### 3. Ensure FFmpeg Is in Your Container Image

The bridge requires the `ffmpeg` binary at runtime. The ART backend Dockerfile already includes it:

```dockerfile
RUN apt-get update && \
    apt-get install -y --no-install-recommends gcc build-essential ffmpeg && \
    rm -rf /var/lib/apt/lists/*
```

**If you're using a custom base image**, verify FFmpeg is installed:
```bash
docker exec <container> ffmpeg -version
```

If missing, add `ffmpeg` to your package installation step. On Debian/Ubuntu-based images: `apt-get install -y ffmpeg`. On Alpine: `apk add ffmpeg`.

The bridge verifies FFmpeg availability at startup and will fail fast with a clear error message if it's not found — you won't get a cryptic runtime failure.

### 4. No Changes to Your Genesys AudioHook

Your existing Genesys AudioHook WebSocket integration stays exactly the same. The bridge operates inside ART's audio processing pipeline, downstream of the WebSocket message parsing. Genesys continues to send PCMU frames in its standard format; ART now knows how to consume them.

---

## Why This Should Work — Engineering Confidence

Let's be direct: **we haven't tested this end-to-end with a live Genesys instance.** Here's why we're confident it will work, and where the remaining risk sits.

### What We Know Is Correct (High Confidence)

**1. The codec math is deterministic.**
G.711 µ-law decoding is a standardized lookup table (ITU-T G.711, published 1988). Every implementation in the world produces the same output for the same input. FFmpeg's µ-law decoder has been shipping in production systems since 2000. There is no ambiguity in this conversion.

**2. The resampling is well-understood.**
8kHz → 16kHz is an integer-ratio upsample (×2). This is the simplest class of sample rate conversion — no fractional interpolation artifacts. FFmpeg's resampler handles this efficiently and with high fidelity.

**3. We tested with real representative audio.**
The test suite uses a real speech recording (insurance subrogation scenario) encoded in both formats:
- **Forward test:** PCMU → PCM16 conversion, output correlated against golden reference — **passes at >0.80 correlation**
- **Roundtrip test:** PCM16 → PCMU → PCM16, verifying lossy roundtrip distortion is bounded — **passes at >0.65 correlation** (µ-law is inherently lossy, so perfect roundtrip is impossible)
- **Format validation:** fixtures verified as correct WAV with expected codec, sample rate, and bit depth

**4. The integration point is minimal and well-isolated.**
The bridge touches exactly one code path: after base64-decode, before `write_audio()`. It's a pure byte-in/byte-out transformation. The rest of ART's pipeline (STT, LLM, TTS, WebSocket management) is completely unchanged.

**5. FFmpeg subprocess piping is mature technology.**
This isn't novel engineering. Piping audio through FFmpeg via stdin/stdout is how half the media industry works. The specific pattern (process lifecycle, pipe buffering, reader threads) is well-tested across platforms.

### Where the Risk Sits (Low-Medium)

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| **Genesys AudioHook message framing differs from ACS** | Low-Medium | Bridge receives unexpected byte sequences | The bridge operates on raw audio bytes post-decode. As long as Genesys delivers raw PCMU bytes (not wrapped in additional framing), conversion will work. Verify by capturing a sample WebSocket frame from Genesys and confirming the audio payload is raw G.711 µ-law |
| **Genesys sends A-law (PCMA) instead of µ-law (PCMU)** | Low | Wrong codec, garbled output | G.711 has two variants. Genesys Cloud typically uses µ-law (PCMU) for North American deployments. Verify your Genesys config. If A-law, the FFmpeg command needs `-f alaw` instead of `-f mulaw` — a one-line change |
| **Windows pipe buffering in local dev** | Low | Slightly delayed audio in dev environments | Only affects developer workstations. Container deployments (Linux) use standard POSIX pipes with no buffering issues |
| **FFmpeg startup latency on first frame** | Very Low | 5-20ms delay on first audio frame of each call | FFmpeg process starts when the call connects. By the time the first audio frame arrives (~100-200ms later), the process is warm. Not a concern in practice |

### What We Recommend Before Go-Live

1. **Capture a raw Genesys WebSocket frame** — Save one or two audio messages from your Genesys AudioHook integration. Decode the base64 payload. Feed those exact bytes through `bridge.transcode_pcmu_to_pcm16()` and play back the result. If you hear intelligible speech, the bridge works for your traffic.

2. **Run a controlled call** — Enable `BRIDGE_MODE=pcmu_pcm16` in a staging environment. Place a real call through Genesys. Verify that Azure Speech STT produces correct transcripts. This single test validates the entire chain.

3. **Monitor bridge stats after activation** — The bridge exposes `dropped_frames`, `buffer_depth_ms`, and `ffmpeg_pid_*` through `get_stats()`. Watch these during initial deployment. Dropped frames > 0 means your downstream processing isn't keeping up — consider increasing `AUDIO_BRIDGE_BUFFER_LIMIT_MS`.

---

## Architecture Diagrams (Genesys + ART)

### SpeechCascade Mode (`ACS_STREAMING_MODE=MEDIA`)

Discrete pipeline — separate STT, LLM, and TTS services. Bridge targets **16 kHz**.

```
┌──────────────┐     ┌─────────────────────────────────────────────────────┐
│              │     │                  ART Backend                        │
│   Genesys    │     │                                                     │
│   Cloud      │     │  ┌──────────┐    ┌─────────────┐    ┌───────────┐  │
│              │     │  │WebSocket │    │ Audio Bridge │    │ Azure     │  │
│  AudioHook   │─WS──│─►│Endpoint  │──►│ PCMU 8kHz    │──►│ Speech    │  │
│  (PCMU 8kHz) │     │  │(media.py)│    │  → PCM16     │    │ STT       │  │
│              │     │  │          │    │    16kHz     │    │ (16kHz)   │  │
│              │     │  └──────────┘    └─────────────┘    └─────┬─────┘  │
│              │     │                                           │        │
│              │     │                                    ┌──────▼──────┐ │
│              │     │                                    │ Azure OpenAI│ │
│              │     │                                    │ (LLM)       │ │
│              │     │                                    └──────┬──────┘ │
│              │     │                                           │        │
│              │     │  ┌──────────┐    ┌─────────────┐    ┌─────▼─────┐  │
│              │◄─WS─│──│WebSocket │◄───│ Audio Bridge │◄───│ Azure     │  │
│  (PCMU 8kHz) │     │  │Endpoint  │    │ PCM16 16kHz  │    │ TTS       │  │
│              │     │  │          │    │  → PCMU 8kHz │    │ (16kHz)   │  │
│              │     │  └──────────┘    └─────────────┘    └───────────┘  │
│              │     │                                                     │
└──────────────┘     └─────────────────────────────────────────────────────┘
                     BRIDGE_MODE=pcmu_pcm16 · ACS_STREAMING_MODE=MEDIA
```

### VoiceLive Mode (`ACS_STREAMING_MODE=VOICE_LIVE`)

Unified pipeline — OpenAI Realtime API handles STT+LLM+TTS. Bridge targets **24 kHz**.

```
┌──────────────┐     ┌─────────────────────────────────────────────────────┐
│              │     │                  ART Backend                        │
│   Genesys    │     │                                                     │
│   Cloud      │     │  ┌──────────┐    ┌─────────────┐    ┌───────────┐  │
│              │     │  │WebSocket │    │ Audio Bridge │    │           │  │
│  AudioHook   │─WS──│─►│Endpoint  │──►│ PCMU 8kHz    │──►│  OpenAI   │  │
│  (PCMU 8kHz) │     │  │(media.py)│    │  → PCM16     │    │GPT-Realtime│  │
│              │     │  │          │    │    24kHz     │    │           │  │
│              │     │  │          │    │              │    │           │  │
│              │     │  │          │    │              │    │           │  │
│              │     │  │          │    │              │    │           │  │
│              │     │  │          │    │              │    │           │  │
│              │     │  │          │    │              │    │ (24kHz    │  │
│              │◄─WS─│──│          │◄───│ PCM16 24kHz  │◄───│  in/out)  │  │
│  (PCMU 8kHz) │     │  │          │    │  → PCMU 8kHz │    │           │  │
│              │     │  └──────────┘    └─────────────┘    └───────────┘  │
│              │     │                                                     │
└──────────────┘     └─────────────────────────────────────────────────────┘
                     BRIDGE_MODE=pcmu_pcm16 · ACS_STREAMING_MODE=VOICE_LIVE
```

Both modes use the same `BRIDGE_MODE=pcmu_pcm16` environment variable. The bridge sample rate is selected automatically based on the orchestration mode.

### Component & Data Flow (Mermaid)

```mermaid
flowchart TB
    subgraph Genesys["Genesys Cloud"]
        Phone["📞 Phone Call"]
        AH["AudioHook<br/><i>PCMU · G.711 µ-law · 8 kHz</i>"]
        Phone --> AH
    end

    subgraph ART["ART Backend Container"]
        subgraph WS["WebSocket Endpoint<br/><small>media.py</small>"]
            WSIn["Receive<br/>base64 decode"]
            WSOut["Send<br/>base64 encode"]
        end

        subgraph Bridge["FfmpegAudioBridge<br/><small>src/audio_bridge/</small>"]
            direction TB
            U2L["<b>U2L Pipe</b><br/>FFmpeg subprocess<br/><code>mulaw 8kHz → s16le target_kHz</code>"]
            L2U["<b>L2U Pipe</b><br/>FFmpeg subprocess<br/><code>s16le target_kHz → mulaw 8kHz</code>"]
            Buf["Bounded Buffer<br/><small>drop-oldest · configurable limit</small>"]
            U2L ~~~ Buf
            L2U ~~~ Buf
        end

        subgraph Cascade["SpeechCascade Mode"]
            direction TB
            STT["Azure Speech STT<br/><i>PCM16 · 16 kHz</i>"]
            LLM1["Azure OpenAI<br/><i>GPT-4o</i>"]
            TTS["Azure Speech TTS<br/><i>PCM16 · 16 kHz</i>"]
            STT --> LLM1 --> TTS
        end

        subgraph VL["VoiceLive Mode"]
            RT["OpenAI Realtime API<br/><i>PCM16 · 24 kHz</i><br/><small>STT + LLM + TTS unified</small>"]
        end
    end

    subgraph Config["Configuration"]
        ENV1["BRIDGE_MODE=pcmu_pcm16"]
        ENV2["AUDIO_BRIDGE_BUFFER_LIMIT_MS=500"]
        ENV3["AUDIO_BRIDGE_FAIL_CLOSED=true"]
    end

    %% Ingress flow
    AH -- "PCMU 8kHz<br/>WebSocket" --> WSIn
    WSIn -- "raw PCMU bytes" --> U2L
    U2L -- "PCM16 16kHz" --> STT
    U2L -. "PCM16 24kHz" .-> RT

    %% Egress flow
    TTS -- "PCM16 16kHz" --> L2U
    RT -. "PCM16 24kHz" .-> L2U
    L2U -- "PCMU 8kHz" --> WSOut
    WSOut -- "PCMU 8kHz<br/>WebSocket" --> AH

    %% Config wiring
    Config -. "env vars" .-> Bridge

    %% Styling
    style Bridge fill:#e8f4fd,stroke:#0078d4,stroke-width:2px
    style Cascade fill:#f0f9e8,stroke:#498205
    style VL fill:#fff4e5,stroke:#ca5010
    style Genesys fill:#fce4ec,stroke:#c62828
    style Config fill:#f5f5f5,stroke:#888,stroke-dasharray:5 5
```

#### Sequence: Single Call Lifecycle

```mermaid
sequenceDiagram
    participant G as Genesys AudioHook
    participant WS as ART WebSocket
    participant B as Audio Bridge<br/>(FFmpeg)
    participant AI as STT / OpenAI Realtime

    Note over WS,B: BRIDGE_MODE=pcmu_pcm16

    G->>WS: WebSocket connect
    WS->>B: Initialize bridge<br/>(start 2 FFmpeg processes)

    loop Every 20ms audio frame
        G->>WS: AudioData {base64 PCMU}
        WS->>WS: base64 decode
        WS->>B: PCMU bytes (160B)
        B->>B: µ-law decode + upsample
        B->>AI: PCM16 bytes (640B/960B)
        AI->>AI: Speech recognition + LLM + synthesis
        AI->>B: PCM16 response audio
        B->>B: downsample + µ-law encode
        B->>WS: PCMU bytes (160B)
        WS->>WS: base64 encode
        WS->>G: AudioData {base64 PCMU}
    end

    G->>WS: WebSocket close
    WS->>B: Close bridge<br/>(terminate FFmpeg)
```

---

## FAQ

**Q: Does this add latency to the call?**
Negligible. FFmpeg processes audio faster than real-time by orders of magnitude. Per-frame transcoding adds ~1-5ms. With 20ms audio frames, this is well within the frame period. You will not notice any perceptible delay.

**Q: Does this increase resource consumption?**
Two FFmpeg processes per concurrent call, each consuming <5MB RSS memory and negligible CPU when processing 8kHz mono audio. For 100 concurrent calls, expect ~1GB additional memory and <1 CPU core total. The FFmpeg binary itself adds ~30-50MB to the container image.

**Q: What if FFmpeg crashes mid-call?**
With `AUDIO_BRIDGE_FAIL_CLOSED=true` (default), the session will terminate cleanly. The caller will experience a dropped call — preferable to continuing with garbled audio. Bridge stats include `ffmpeg_restarts` for monitoring.

**Q: Can we use this with other telephony providers that send PCMU?**
Yes. The bridge is codec-aware, not provider-aware. Any source that delivers raw G.711 µ-law bytes will work. The bridge doesn't know or care that the bytes came from Genesys.

**Q: What about PCMA (A-law)?**
G.711 has two variants: µ-law (PCMU, common in North America/Japan) and A-law (PCMA, common in Europe). The current bridge handles PCMU. Supporting PCMA requires changing the FFmpeg input format flag from `mulaw` to `alaw`. This is a configuration change, not an architectural one — reach out and we can enable it.

**Q: Do we need to change our Genesys AudioHook configuration?**
No. The bridge works inside ART. Your Genesys setup — WebSocket URL, audio format settings, AudioHook event handling — stays exactly the same.

---

## Quick Start Checklist

- [ ] Verify your container image includes `ffmpeg` (`docker exec <container> which ffmpeg`)
- [ ] Set `BRIDGE_MODE=pcmu_pcm16` in your environment / container config
- [ ] Deploy to staging
- [ ] Place a test call through Genesys
- [ ] Verify STT produces correct transcripts
- [ ] Check bridge stats for `dropped_frames == 0`
- [ ] Deploy to production

---

*For questions, contact your Microsoft Global Black Belt or the ART engineering team.*
