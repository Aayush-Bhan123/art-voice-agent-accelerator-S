# Design Document: FFmpeg-First Audio Bridge with Genesys Simulation (PCMU ⇄ PCM16) in ART

## 0. Purpose of This Document

Implement a **production-shaped** audio bridge for ART quickly, **without requiring Genesys access**, by:

1. Building the bridge and ART integration using **FFmpeg first**.
2. Creating a **Genesys Simulation Harness** that injects **PCMU frames** into the internal ART entrypoints that a future Genesys adapter would use.
3. Supporting **both directions**:

   * **Genesys → ACS:** PCMU @ 8 kHz → PCM16 @ 16 kHz
   * **ACS → Genesys (simulation):** PCM16 @ 16 kHz → PCMU @ 8 kHz

This document is written for an **AI coding agent** to explore the ART repo, locate the correct insertion points, and implement an end-to-end testable scaffolding.

---

## 0.1 Codebase Reality Check (Grounded Analysis)

> The original draft assumed hypothetical module names (`art/audio/...`) and vague ingress/egress points.
> This section grounds the design in the **actual** ART codebase as of Feb 2026.

### Audio flow as it actually exists

```
                         ACS Media WebSocket
                               │
                    ┌──────────▼──────────┐
                    │  media.py endpoint  │  apps/artagent/backend/api/v1/endpoints/media.py
                    │  acs_media_stream() │  — accepts WS, creates handler
                    └──────────┬──────────┘
                               │  raw ACS JSON messages
                    ┌──────────▼──────────┐
                    │   VoiceHandler      │  apps/artagent/backend/voice/handler.py
                    │                     │  — handle_media_message() dispatches on "kind"
                    │  AUDIO_DATA kind →  │  — base64-decodes → PCM16LE bytes
                    │  write_audio()      │  — feeds PushAudioInputStream
                    └──────────┬──────────┘
                               │  PCM16 @ 16 kHz
                    ┌──────────▼──────────┐
                    │  SpeechSDKThread    │  speech_cascade/handler.py
                    │  (Azure Speech STT) │  — continuous recognition
                    └──────────┬──────────┘
                               │  transcript text
                    ┌──────────▼──────────┐
                    │  RouteTurnThread    │  speech_cascade/handler.py
                    │  → Orchestrator     │  — LLM call → response text
                    └──────────┬──────────┘
                               │  response text
                    ┌──────────▼──────────┐
                    │  TTSPlayback        │  voice/tts/playback.py
                    │  play_to_acs()      │  — synthesize → PCM16 @ 16 kHz
                    │  _stream_to_acs()   │  — base64 frames → WS (direct send_json)
                    └──────────┬──────────┘
                               │  base64 PCM16 frames
                    ┌──────────▼──────────┐
                    │  ACS WebSocket out  │  same accepted websocket
                    └─────────────────────┘
```

Current ACS flow note: `api/v1/endpoints/media.py` parses JSON and calls
`VoiceHandler.handle_media_message()`. Outbound audio in the current `VoiceHandler`
path is sent by `TTSPlayback._stream_to_acs()` using `self._ws.send_json(...)`.
The `ws_helpers/shared_ws.py::send_response_to_acs()` path still exists for legacy
callers but is not the primary path for `VoiceHandler`.

### Key constants (from actual code)

| Constant | Value | Source |
|----------|-------|--------|
| `RATE` | 16000 | `config/constants.py` |
| `FORMAT` | 16 (PCM16) | `config/constants.py` |
| `CHANNELS` | 1 | `config/constants.py` |
| `TTS_SAMPLE_RATE_ACS` | 16000 | `config/settings.py` |
| `SAMPLE_RATE_ACS` | 16000 | `voice/tts/playback.py` |

### Identified ingress/egress touchpoints

| Direction | Touchpoint | Method | Location |
|-----------|-----------|--------|----------|
| **Audio ingress (ACS → STT)** | `VoiceHandler.write_audio(audio_bytes)` | Feeds `PushAudioInputStream` | `voice/handler.py` |
| **Audio ingress (ACS message → decode)** | `VoiceHandler.handle_media_message(msg)` | base64 decode → `write_audio()` | `voice/handler.py` |
| **Audio egress (TTS → ACS WS)** | `TTSPlayback._stream_to_acs(pcm_bytes)` | Split into 40ms frames (`chunk_size=1280`), base64 encode, send via WS | `voice/tts/playback.py` |
| **Audio egress (legacy)** | `send_response_to_acs()` | Same pattern, legacy code path | `ws_helpers/shared_ws.py` |

### TransportType enum (where the bridge fits)

The system already has `TransportType(str, Enum)` with values `BROWSER`, `ACS`, `VOICELIVE` in `voice/shared/context.py`. The bridge would introduce audio at the same boundary as ACS but with a pre-transcoding step.

### What does NOT exist (gaps to fill)

1. **No Genesys adapter or stub** — there are no Genesys-related files at all.
2. **No codec conversion** — ART does not implement PCMU/µ-law handling today.
3. **No FFmpeg runtime integration** — there is no streaming FFmpeg subprocess bridge in the backend today. `src/aoai/audio_util.py` imports `pydub` for utility conversion, but `pydub` is not declared in `pyproject.toml` core deps, so this is not a reliable production dependency path.
### Existing vs proposed (explicit)

| Item | Status today | This document proposes |
|------|--------------|------------------------|
| `BRIDGE_MODE` config | Not present | Add mode-based gating (`off` / `pcmu_pcm16`) |
| `src/audio_bridge/*` module | Not present | Add new bridge module under `src/` |

---

## 1. Locked Requirements

### 1.1 Codecs and Rates

* **PCMU (G.711 µ-law)**: 8 kHz, mono (Genesys-like)
* **PCM16**: signed 16-bit little-endian linear PCM, 16 kHz, mono (ACS-like)

### 1.2 Framing (target vs current)

* PCMU @ 8 kHz: **160 bytes per 20 ms**
* PCM16 @ 16 kHz mono: **640 bytes per 20 ms**

Current implementation note: `TTSPlayback._stream_to_acs()` currently emits
**40 ms** chunks (`1280` bytes). This design keeps 20 ms as the bridge contract,
but integration must either repack bridge output to 40 ms for ACS parity or
update ACS egress pacing deliberately.

### 1.3 Simulation Constraint

Genesys is not available; therefore:

* We must identify ART's **internal audio ingress/egress APIs** and drive them with synthetic/fixture audio.
* We must validate correctness through **fixtures + signal similarity tests**, not by live Genesys calls.

---

## 2. Strategy: FFmpeg First, Modular by Design

### Phase 1 (now)

* Implement an FFmpeg-based transcoder backend and wire it into ART.
* Implement a local harness that simulates Genesys by feeding **PCMU frames** into ART.

### Phase 2 (later milestone)

* Evaluate whether replacing FFmpeg with GStreamer is warranted (same interface).

### Design Constraints from ART's Coding Standards

Per `.github/instructions/coding-standards.instructions.md`:

* **Async everything** — bridge push/pop must have `async` wrappers for the event-loop integration.
* **No new dependencies without approval** — no new pip package is required for subprocess-based FFmpeg usage, but the FFmpeg binary must be installed explicitly in runtime images.
* **Use `get_logger(__name__)`** — not `logging.getLogger`.
* **Use `config.settings`** — add any new env-var-driven config there, not raw `os.getenv()`.
* **Functions over classes** — use classes only where state is genuinely needed (the bridge subprocess lifecycle justifies a class).
* **Connection pooling** — if multiple simultaneous calls need bridges, consider a bridge pool or per-session lifecycle.

---

## 3. Component Boundary: `AudioBridge` Interface (Bidirectional)

To support both directions cleanly, define a bidirectional bridge with two logical transforms:

* **U2L**: µ-law → linear PCM (PCMU → PCM16, includes upsample)
* **L2U**: linear PCM → µ-law (PCM16 → PCMU, includes downsample)

### 3.1 Interface (authoritative)

```python
class AudioBridge(Protocol):
    # Genesys -> ACS
    def push_pcmu(self, pcmu_bytes: bytes, *, frame_ms: int | None = None) -> None: ...
    def pop_pcm16(self) -> bytes: ...  # multiples of 640 bytes preferred

    # ACS -> Genesys (simulation / future)
    def push_pcm16(self, pcm16_bytes: bytes, *, frame_ms: int | None = None) -> None: ...
    def pop_pcmu(self) -> bytes: ...    # multiples of 160 bytes preferred

    def get_stats(self) -> AudioBridgeStats: ...
    def close(self) -> None: ...
```

> **AMENDMENT:** Fixed typo in original — `push_pcu` / `pop_pcu` → `push_pcmu` / `pop_pcmu` for clarity.
> Added `get_stats()` to expose encoding stats for diagnostics and tests.

### 3.2 Framing contract

* `pop_pcm16()` returns bytes in **multiples of 640** (20 ms @ 16 kHz PCM16) unless configured otherwise.
* `pop_pcmu()` returns bytes in **multiples of 160** (20 ms @ 8 kHz PCMU) unless configured otherwise.
* The bridge enforces bounded buffering and defines a drop policy on overflow.

### 3.3 Stats model

```python
@dataclass
class AudioBridgeStats:
  """Live encoding stats exposed for diagnostics and verification."""
    # Identity
    active_input_codec: str      # "pcmu" | "pcm16"
    active_output_codec: str     # "pcm16" | "pcmu"
    input_sample_rate: int       # 8000 or 16000
    output_sample_rate: int      # 16000 or 8000

    # Throughput (cumulative)
    frames_in: int               # total frames pushed
    frames_out: int              # total frames popped
    bytes_in: int                # total bytes pushed
    bytes_out: int               # total bytes popped

    # Buffer health
    buffer_depth_ms: float       # current output buffer depth
    dropped_frames: int          # frames dropped due to overflow

    # Process health (FFmpeg-specific)
    ffmpeg_pid_u2l: int | None   # PID of PCMU→PCM16 process
    ffmpeg_pid_l2u: int | None   # PID of PCM16→PCMU process
    ffmpeg_restarts: int         # total restart count

    # Timing
    bridge_uptime_s: float       # seconds since bridge init
```

---

## 4. FFmpeg Backend: `FfmpegAudioBridge` (Two Pipelines)

Because we need bidirectional conversion, the cleanest approach is **two FFmpeg subprocesses** (one per direction). This avoids format ambiguity and keeps each process single-purpose.

### 4.1 Process A: PCMU → PCM16 (Genesys → ACS)

* stdin: raw µ-law @ 8 kHz, mono
* stdout: raw PCM s16le @ 16 kHz, mono

Conceptual FFmpeg IO:

* Input: `-f mulaw -ar 8000 -ac 1 -i pipe:0`
* Output: `-f s16le -ar 16000 -ac 1 pipe:1`

### 4.2 Process B: PCM16 → PCMU (ACS → Genesys simulation)

* stdin: raw PCM s16le @ 16 kHz, mono
* stdout: raw µ-law @ 8 kHz, mono

Conceptual FFmpeg IO:

* Input: `-f s16le -ar 16000 -ac 1 -i pipe:0`
* Output: `-f mulaw -ar 8000 -ac 1 pipe:1`

### 4.3 Lifecycle + error policy

Each subprocess must:

* start on bridge init (or lazy-start on first push)
* continuously read stdout in a dedicated reader thread/task
* expose last exit code + restart counters
* fail session on hard error (initial default)

### 4.4 Async wrappers

Because ART's handlers are all async, the bridge must provide async wrappers that use `asyncio.to_thread()` for the blocking FFmpeg stdin write and stdout read operations. This mirrors how `TTSPlayback._synthesize()` already uses `asyncio.to_thread()` for the Azure Speech SDK.

```python
async def push_pcmu_async(self, pcmu_bytes: bytes) -> None:
    await asyncio.to_thread(self.push_pcmu, pcmu_bytes)

async def pop_pcm16_async(self) -> bytes:
    return await asyncio.to_thread(self.pop_pcm16)
```

---

## 5. Genesys Simulation Harness (Core Deliverable)

### 5.1 Goal

Create a harness that "acts like Genesys" from ART's perspective by injecting:

* **PCMU frames** into the internal ART audio ingress point that the Genesys adapter would use.
* optionally receiving "Genesys-bound" audio output as **PCMU frames** (generated from PCM16).

### 5.2 Identified ART Insertion Points (Grounded)

Based on codebase analysis, the concrete insertion points are:

1. **Ingress touchpoint: `VoiceHandler.write_audio(audio_bytes: bytes)`**
   * Location: `apps/artagent/backend/voice/handler.py`
   * Expects: raw PCM16LE bytes
   * The bridge converts PCMU → PCM16 and then calls `write_audio()`.

2. **Message-level ingress: `VoiceHandler.handle_media_message(msg: dict)`**
   * Location: `apps/artagent/backend/voice/handler.py`
   * Dispatches on `kind` field (`AudioData`, `AudioMetadata`, `StopAudio`, `DtmfData`)
   * Currently base64-decodes the `audioData.data` field and calls `write_audio()`
   * The bridge could either:
     * **(A) Intercept before decode** — wrap the PCMU→PCM16 conversion at the message level, or
     * **(B) Inject after decode** — convert PCMU→PCM16 in a pre-processing step and call `write_audio()` directly.
  * **Recommendation: Option (B)** — inject at `write_audio()` level behind `BRIDGE_MODE=pcmu_pcm16` first; avoid introducing a new transport type for this codec-only scope.

3. **Egress touchpoint: `TTSPlayback._stream_to_acs(pcm_bytes, ...)`**
   * Location: `apps/artagent/backend/voice/tts/playback.py`
  * Produces: PCM16 @ 16 kHz, split into 40ms base64 frames (`chunk_size=1280`), sent via WebSocket
   * For Genesys simulation: the bridge would intercept the PCM16 egress, convert to PCMU, and deliver to the harness.

4. **Session lifecycle: `VoiceHandler.create(config, app_state)`**
  * The bridge should be instantiated here when `BRIDGE_MODE=pcmu_pcm16`.
  * Keep `TransportType` unchanged (`BROWSER`/`ACS`/`VOICELIVE`) because this is a codec bridge, not a new transport.
  * Lower-risk first increment: add `bridge_mode` to `VoiceSessionContext` and create the bridge only when mode is `pcmu_pcm16`.
   * Bridge gets stored on `VoiceSessionContext` as a new optional field.

### 5.3 Harness responsibilities

* Read fixture audio and create real-time-ish cadence (20 ms ticks).
* Inject frames via `bridge.push_pcmu()` → `bridge.pop_pcm16()` → `handler.write_audio()`.
* Capture egress frames and validate:

  * correct format/framing
  * stable latency (bounded buffers)
  * signal similarity vs expected output

### 5.4 Modes

* **Offline deterministic mode (unit/integration test):**

  * run as fast as possible, no sleeping, just frame-by-frame
* **Real-time simulation mode (manual/local demo):**

  * sleep 20 ms between pushes, add jitter patterns

---

## 6. Testing Without Genesys: Fixtures and Golden Files

### 6.1 Canonical audio in repo

Use WAV PCM16 as the "truth," then derive the other representations:

Store under `tests/fixtures/audio_bridge/`:

* `source_16k_s16.wav` (canonical content)
* `source_8k_ulaw.raw` (or `.wav`) (derived from canonical)
* `expected_16k_s16.raw` (expected result of ulaw→pcm16 pipeline)
* `expected_8k_ulaw.raw` (expected result of pcm16→ulaw pipeline)

> **AMENDMENT:** Changed from `fixtures/` at repo root to `tests/fixtures/audio_bridge/` to match the existing test structure. ART keeps all test data under `tests/`.

### 6.2 Tests: forward direction (PCMU → PCM16)

* Feed `source_8k_ulaw.raw` in 160-byte chunks
* Drain `pop_pcm16()` and assert:

  * output size multiple of 640
  * correlation / RMS similarity with `expected_16k_s16.raw`

### 6.3 Tests: reverse direction (PCM16 → PCMU)

* Feed `source_16k_s16.raw` in 640-byte chunks
* Drain `pop_pcmu()` and assert:

  * output size multiple of 160
  * similarity with `expected_8k_ulaw.raw`

### 6.4 Roundtrip test (sanity)

* PCM16 → PCMU → PCM16
* Compare to original PCM16 with a *looser* threshold (lossy roundtrip due to µ-law companding).

---

## 7. Backpressure and Bounded Buffers

### 7.1 Why

In simulation, downstream may not drain at the same cadence; we must ensure the system doesn't accumulate unbounded audio.

### 7.2 Policy

* Maintain bounded output buffers for both directions:

  * configurable limit in **milliseconds** or **bytes**
* On overflow:

  * default: **drop-oldest** and increment `dropped_frames_total`
* Expose current buffered "audio lag" estimate:

  * `buffered_ms = buffered_bytes / bytes_per_second * 1000`

---

## 8. Validation Narratives: What Success Looks Like

### 8.1 Story 1: Test Fixtures (DevEx — "It works because we proved it offline")

A developer runs the audio bridge test suite. The terminal shows:

```
$ pytest tests/test_audio_bridge_forward.py tests/test_audio_bridge_roundtrip.py -v

tests/test_audio_bridge_forward.py::test_pcmu_to_pcm16_conversion
    ✓ Loaded source_8k_ulaw.raw (4800 bytes, 3.0s of PCMU audio)
    ✓ Fed 150 frames (160 bytes × 150) through FfmpegAudioBridge
    ✓ Drained 150 frames (640 bytes × 150) of PCM16 output
    ✓ Signal correlation vs expected_16k_s16.raw: 0.997 (threshold: 0.95)
    PASSED

tests/test_audio_bridge_forward.py::test_pcm16_to_pcmu_conversion
    ✓ Loaded source_16k_s16.wav (96000 bytes, 3.0s of PCM16 audio)
    ✓ Fed 150 frames (640 bytes × 150) through FfmpegAudioBridge
    ✓ Drained 150 frames (160 bytes × 150) of PCMU output
    ✓ Signal correlation vs expected_8k_ulaw.raw: 0.994 (threshold: 0.95)
    PASSED

tests/test_audio_bridge_roundtrip.py::test_pcm16_roundtrip_through_pcmu
    ✓ PCM16 → PCMU → PCM16 roundtrip (lossy, µ-law companding)
    ✓ Signal correlation: 0.981 (threshold: 0.90)
    PASSED

3 passed in 2.4s
```

**The proof sentence:** *"This specific PCMU file — the same format Genesys produces — went through our bridge and came out as valid PCM16 that Azure Speech STT can consume. Signal fidelity is 99.7%. The roundtrip through µ-law adds only 2% distortion."*
## 9. Codebase Integration Plan (Grounded)

### Step 1: Add bridge module under `src/`

ART's reusable core libraries live under `src/`. The bridge is a transport-level audio utility, fitting alongside `src/speech/`, `src/acs/`, `src/vad/`.

Create:

* `src/audio_bridge/__init__.py`
* `src/audio_bridge/base.py` — `AudioBridge` Protocol, `AudioBridgeStats` dataclass, constants
* `src/audio_bridge/ffmpeg_bridge.py` — `FfmpegAudioBridge` implementation (two subprocesses)

> **AMENDMENT:** Changed from `art/audio/bridge/` (does not exist) to `src/audio_bridge/` which follows ART's actual module structure. No factory module — per coding standards, avoid unnecessary abstractions when there's a single implementation.

### Step 2: Add simulation harness

* `src/audio_bridge/pcmu_pcm16_sim.py` — simulation harness
* `src/audio_bridge/cli.py` — CLI entrypoint: `python -m src.audio_bridge.cli --fixture ...`

### Step 3: Wire into VoiceHandler

* Add optional `audio_bridge: AudioBridge | None` field to `VoiceSessionContext` (`voice/shared/context.py`)
* In `VoiceHandler.handle_media_message()`, when bridge is present, route incoming audio through bridge before calling `write_audio()`
* Keep browser/realtime paths unchanged for this backend-only milestone
* Keep TTS egress unchanged in this milestone; bridge validation for reverse direction is handled in the simulation harness and tests

### Step 4: Tests

Create:

* `tests/test_audio_bridge_forward.py`
* `tests/test_audio_bridge_reverse.py`
* `tests/test_audio_bridge_roundtrip.py`
* `tests/fixtures/audio_bridge/` — fixture files

### Step 5: Configuration

Add to `apps/artagent/backend/config/settings.py`:

```python
# Audio Bridge
BRIDGE_MODE: str = os.getenv("BRIDGE_MODE", "off")  # off | pcmu_pcm16
AUDIO_BRIDGE_BUFFER_LIMIT_MS: int = _env_int("AUDIO_BRIDGE_BUFFER_LIMIT_MS", 500)
```

### Step 6: CI integration

* Ensure FFmpeg is available in the CI container (pinned version if possible).
* Run the integration tests inside the same container environment intended for prod parity.
* Do not rely on `pydub` as FFmpeg provisioning; wire FFmpeg installation explicitly in CI image setup.

### Step 7: Container deployment

The backend Dockerfile (`apps/artagent/backend/Dockerfile`) uses `python:3.11-slim-bookworm` and does **not** include FFmpeg. The FFmpeg binary is currently absent from the production image.

The audio bridge **requires** the FFmpeg binary at runtime. Add it to the Dockerfile:

```dockerfile
# Install FFmpeg for audio bridge (PCMU ⇄ PCM16 transcoding)
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*
```

**Impact considerations:**
* **Image size** — `ffmpeg` on slim-bookworm adds ~30-50 MB. Acceptable for an audio processing service.
* **Security** — FFmpeg is a well-maintained Debian package; pin to bookworm's stable version via `apt-get install ffmpeg=7:*` if exact version control is needed.
* **Feature-gated** — When `BRIDGE_MODE=off` (default), FFmpeg is never spawned. The binary presence is inert.
* **Startup guard** — The bridge should verify `shutil.which("ffmpeg")` on init and raise a clear error if missing, rather than failing with a cryptic subprocess error.
* **devcontainer** — The `.devcontainer/Dockerfile` also currently lacks FFmpeg; include it for local development parity.

---

## 10. Feasibility Assessment

### Is this realistic?

**Yes, with caveats:**

| Aspect | Assessment |
|--------|-----------|
| **Bridge module** | Straightforward. FFmpeg subprocess piping is well-understood. The main risk is Windows compatibility for `pipe:0`/`pipe:1` (use `subprocess.PIPE`). |
| **ART integration** | Clean insertion at ACS ingress (`handle_media_message`) and session lifecycle (`VoiceSessionContext`). No structural changes to the orchestrator pipeline. |
| **Simulation harness** | Moderate complexity. Requires bootstrapping a minimal `VoiceHandler` without a real WebSocket, which means mocking the WS and `app_state`. Consider a standalone test that exercises only the bridge + `PushAudioInputStream` → STT without the full handler stack. |
| **Latency** | FFmpeg subprocess adds ~5-20ms startup latency per process. After warmup, throughput is effectively zero-copy via pipes. Acceptable for the 20ms frame cadence. |
| **No new pip deps** | Confirmed — no new pip packages. However, the FFmpeg **binary** must be added to the backend Dockerfile (not currently present). See Step 7. |

### What could go wrong

1. **FFmpeg not installed** in dev/CI environments. Mitigation: add a guard that logs a clear error and falls back gracefully.
2. **Windows pipe buffering** — `subprocess.PIPE` on Windows has different buffering semantics. Test explicitly on Windows.
3. **Thread safety** — the bridge's reader threads must not contend with the async event loop. Use `asyncio.to_thread()` consistently.
4. **Large test fixtures** in git — keep fixture audio short (2-3 seconds) to minimize repo bloat.

### Clean modular integration?

**Yes.** The bridge sits cleanly as a `src/audio_bridge/` module with no coupling to orchestrator logic. It is consumed by `VoiceHandler` at ACS ingress through an optional field on `VoiceSessionContext`, and validated through backend simulation tests.

```
src/audio_bridge/          ← NEW: standalone, testable
    ├── base.py            ← Protocol + dataclasses
    ├── ffmpeg_bridge.py   ← Implementation
    ├── pcmu_pcm16_sim.py  ← Simulation harness
    └── cli.py             ← CLI entrypoint

voice/shared/context.py   ← MODIFIED: add optional audio_bridge field
voice/handler.py           ← MODIFIED: bridge integration in audio path
config/settings.py         ← MODIFIED: add bridge config vars
```

---

## 11. Resolved: VoiceLive / Realtime Path

> **Gap identified and resolved post-implementation.** The bridge was originally only wired
> into the SpeechCascade path (`VoiceHandler`). The VoiceLive path (`VoiceLiveSDKHandler`)
> now has full bridge integration.

### What was done

1. **`FfmpegAudioBridge` made configurable** — Added `pcm16_sample_rate` parameter
   (default 16000). VoiceLive uses 24000 to match OpenAI Realtime API expectations.
   FFmpeg args and frame sizes are computed dynamically from this rate.

2. **Ingress bridge in `VoiceLiveSDKHandler.handle_audio_data()`:**
   - After base64 decode, before `input_audio_buffer.append()`
   - Converts PCMU 8kHz → PCM16 24kHz via `asyncio.to_thread(bridge.transcode_pcmu_to_pcm16)`
   - Re-encodes to base64 for the OpenAI API

3. **Egress bridge in `VoiceLiveSDKHandler._send_audio_delta()`:**
   - When bridge is active, replaces `_resample_audio()` (which did 24kHz → 16kHz numpy resampling)
   - Converts PCM16 24kHz → PCMU 8kHz via `asyncio.to_thread(bridge.transcode_pcm16_to_pcmu)`
   - FFmpeg handles both codec conversion and resampling in one step

4. **Lifecycle:**
   - Bridge created in `start()` when `BRIDGE_MODE=pcmu_pcm16` (reads from main app settings)
   - Bridge closed in `stop()` before other cleanup
   - Uses same `AUDIO_BRIDGE_FAIL_CLOSED` / `AUDIO_BRIDGE_BUFFER_LIMIT_MS` settings

5. **Testing:**
   - Added `test_pcmu_to_pcm16_24k_produces_output` — verifies 24kHz bridge variant
     produces correctly-framed output (960 bytes per 20ms) and valid roundtrip sizing
