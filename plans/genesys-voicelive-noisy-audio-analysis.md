# Copilot Sonnet

# Root Cause Analysis: Noisy Audio Output — Genesys AudioHook VoiceLive Integration

**Symptom:** Audio output to the Genesys client is extremely noisy and degraded in quality.
**Endpoint:** `/api/v1/genesys/stream` (WebSocket, Genesys AudioHook protocol)
**Mode:** VoiceLive backend (migrated from GPT Realtime)

---

## TL;DR — Four distinct bugs compound to produce noise

| # | Bug | Severity | Location |
|---|-----|----------|----------|
| 1 | **Bridge disabled by default** — PCMU↔PCM16 conversion never runs | Critical | `config/settings.py` |
| 2 | **Input: raw PCMU bytes fed to a PCM16-configured VoiceLive buffer** | Critical | `voice/voicelive/handler.py` |
| 3 | **Output: PCM16 bytes sent to Genesys as if they were PCMU** | Critical | `voice/voicelive/handler.py` |
| 4 | **Genesys interleaved channels: both capture AND playback forwarded as user audio** | High | `api/v1/endpoints/genesys.py` |
| 5 | **`get_voicelive_audio_formats()` dead ternary — always returns PCM16** | Medium | `registries/agentstore/base.py` |

---

## Audio Format Expectations (the ground truth)

| Party | What it speaks | What it expects to receive |
|-------|---------------|--------------------------|
| **Genesys client** | PCMU (G.711 μ-law), 8 kHz, 8-bit/sample | PCMU, 8 kHz, 8-bit/sample |
| **VoiceLive** | PCM16, 24 kHz, 16-bit/sample | PCM16 (or PCM16 at a configured rate) |

Correct data flow requires:

```
Genesys → PCMU 8kHz → [convert] → PCM16 24kHz → VoiceLive
VoiceLive → PCM16 24kHz → [convert] → PCMU 8kHz → Genesys
```

---

## Bug 1: Audio Bridge Is Off by Default (Critical)

**File:** `apps/artagent/backend/config/settings.py` line 324

```python
BRIDGE_MODE: str = os.getenv("BRIDGE_MODE", "off").strip().lower()
```

`BRIDGE_MODE` defaults to `"off"`. The `FfmpegAudioBridge` (which correctly converts PCMU↔PCM16 via FFmpeg) is only instantiated in `VoiceLiveSDKHandler.start()` when `_BRIDGE_MODE == "pcmu_pcm16"`:

```python
# handler.py ~line 924
if _BRIDGE_MODE == "pcmu_pcm16":
    self._audio_bridge = FfmpegAudioBridge(
        buffer_limit_ms=_BRIDGE_BUFFER_LIMIT_MS,
        pcm16_sample_rate=24000,   # correctly targets VoiceLive's 24kHz
    )
```

With the default `BRIDGE_MODE=off`, `self._audio_bridge` is always `None`. All codec conversion code is guarded by `if self._audio_bridge is not None:`, so **nothing converts between PCMU and PCM16**. This is the root enabler for bugs 2 and 3.

**Fix:** Set environment variable `BRIDGE_MODE=pcmu_pcm16` for the Genesys deployment. The bridge FFmpeg pipes are already correctly configured for this use case (u2l: mulaw 8kHz → PCM16 24kHz; l2u: PCM16 24kHz → mulaw 8kHz).

---

## Bug 2: PCMU Input Bytes Sent Raw to PCM16-Configured VoiceLive (Critical)

**File:** `apps/artagent/backend/voice/voicelive/handler.py` — `handle_audio_data()` ~line 1349

With bridge off, the code path is:

```python
if kind == "AudioData":
    # ...
    encoded = audio_section.get("data")   # base64 of raw PCMU 8kHz bytes from Genesys
    if self._audio_bridge is not None:    # FALSE when BRIDGE_MODE=off
        # transcodes PCMU → PCM16 here...
        pass
    # Falls through: 'encoded' is still raw PCMU bytes, base64-encoded
    await self._connection.input_audio_buffer.append(audio=encoded)
```

VoiceLive's input buffer is configured to receive `InputAudioFormat.PCM16` (see Bug 5). It therefore interprets the incoming bytes as signed 16-bit little-endian integers. PCMU (G.711 μ-law) uses logarithmic compression — the bit patterns are completely different from linear PCM. VoiceLive reads noise.

**Effect on upstream:** VoiceLive's VAD (voice activity detection), transcription, and response generation all receive garbled input. STT produces garbage or silence. The model may not even detect user speech starts/stops correctly, causing turn-management failures.

**Fix:** Set `BRIDGE_MODE=pcmu_pcm16` (Bug 1 fix). When bridge is active, `handle_audio_data()` already calls:
```python
pcm16_bytes = await asyncio.to_thread(
    self._audio_bridge.transcode_pcmu_to_pcm16, pcmu_bytes
)
encoded = base64.b64encode(pcm16_bytes).decode("utf-8")
await self._connection.input_audio_buffer.append(audio=encoded)
```
This correctly converts PCMU 8kHz → PCM16 24kHz before feeding VoiceLive.

---

## Bug 3: PCM16 Output Bits Sent to Genesys as If They Were PCMU (Critical)

**File:** `apps/artagent/backend/voice/voicelive/handler.py` — `_send_audio_delta()` ~line 1712

When VoiceLive fires a `RESPONSE_AUDIO_DELTA` event:

```python
async def _send_audio_delta(self, audio_bytes: bytes, *, response_id: str | None) -> None:
    pcm_bytes = self._to_pcm_bytes(audio_bytes)     # decodes base64 → raw PCM16 24kHz bytes
    
    if self._audio_bridge is not None:               # FALSE when BRIDGE_MODE=off
        # converts PCM16 24kHz → PCMU 8kHz here...
        pass
    else:
        # Falls through to resample path:
        resampled = self._resample_audio(pcm_bytes)  # PCM16 24kHz → PCM16 8kHz (b64)
    
    # Sends {kind:"AudioData", AudioData:{data: resampled}} to self.websocket
    # self.websocket is _GenesysWebSocketWrapper
    await self.websocket.send_json(message)
```

`_resample_audio()` with `_acs_sample_rate=8000` (set by the AudioMetadata injection) resamples from 24kHz to 8kHz using sinc/Catmull-Rom interpolation. The output is **PCM16 at 8kHz** — a 2-byte-per-sample linear signal.

`_GenesysWebSocketWrapper.send_json()` then decodes the base64 and calls `send_bytes()` on the real WebSocket:

```python
pcmu_bytes = base64.b64decode(data_b64)   # variable name is misleading: this is PCM16
await self._real.send_bytes(pcmu_bytes)    # sends raw PCM16 bytes to browser/Genesys
```

Genesys was told to expect PCMU 8kHz in the `OPENED` message. It receives raw signed 16-bit PCM bytes instead. The G.711 decoder misinterprets linear integer values as logarithmically compressed μ-law samples → **extreme noise**.

There is also a **bit-rate mismatch**: PCMU 8kHz = 8,000 bytes/sec (1 byte/sample), PCM16 8kHz = 16,000 bytes/sec (2 bytes/sample). Genesys consumes bytes at the PCMU rate, effectively playing the audio at 2× speed with scrambled amplitude.

**Fix:** Set `BRIDGE_MODE=pcmu_pcm16` (Bug 1 fix). When the bridge is active, the egress path correctly converts:
```python
pcmu_bytes = await asyncio.to_thread(
    self._audio_bridge.transcode_pcm16_to_pcmu, pcm_bytes
)
resampled = base64.b64encode(pcmu_bytes).decode("utf-8") if pcmu_bytes else None
```
which produces proper PCMU 8kHz output.

---

## Bug 4: Both Genesys Channels (Capture + Playback) Forwarded as User Audio (High)

**File:** `apps/artagent/backend/api/v1/endpoints/genesys.py` — `OPENED` message construction

The endpoint negotiates **two channels** with Genesys:

```python
"media": [
    {
        "type": "audio",
        "codec": "PCMU",
        "rate": 8000,
        "channels": ["capture", "playback"],   # ← both channels!
    }
]
```

Per the [Genesys AudioHook protocol](https://developer.genesys.cloud/devapps/audiohook/protocol-reference), when multiple channels are negotiated, each binary frame contains audio for **all negotiated channels interleaved** — typically as sequential channel blocks within every packet (N bytes of channel-0/capture followed by N bytes of channel-1/playback, or interleaved sample-by-sample depending on the implementation).

The endpoint treats every binary frame as pure microphone audio and forwards it entirely to VoiceLive:

```python
if binary:
    b64 = base64.b64encode(binary).decode("utf-8")
    acs_msg = json.dumps({"kind": "AudioData", "audioData": {"data": b64, "silent": False}})
    await handler.handle_audio_data(acs_msg)
```

**Effect:**
- VoiceLive receives the bot's own TTS output (playback channel) mixed into the user audio stream → echo feedback loop. The model hears itself talking and starts responding to its own voice.
- The byte stream is doubled in length (two channels) and the interleaving pattern does not match mono PCMU 8kHz → the signal is effectively scrambled.
- VAD and transcription are confused by the mixed signal.

**Fix:** Only request the `"capture"` channel from Genesys:

```python
"channels": ["capture"]
```

With a single channel, Genesys sends only the inbound (user) microphone audio, which is the correct source for STT input. The bot's output audio is sent back via the handler's `_send_audio_delta` path, not via the Genesys binary frame.

---

## Bug 5: `get_voicelive_audio_formats()` Dead Ternary — Always Returns PCM16 (Medium)

**File:** `apps/artagent/backend/registries/agentstore/base.py` lines 970–971

```python
in_fmt_str = (self.session.get("input_audio_format") or "PCM16").lower()
out_fmt_str = (self.session.get("output_audio_format") or "PCM16").lower()

in_fmt = InputAudioFormat.PCM16 if in_fmt_str == "pcm16" else InputAudioFormat.PCM16
out_fmt = OutputAudioFormat.PCM16 if out_fmt_str == "pcm16" else OutputAudioFormat.PCM16
```

Both branches of both ternaries are identical — `InputAudioFormat.PCM16` and `OutputAudioFormat.PCM16`. This means **regardless of what is written in the agent YAML** (`output_audio_format: G711Ulaw`, for example), the VoiceLive session is always configured for PCM16.

As a consequence:
- There is no way to configure VoiceLive's native G.711 μ-law output mode through agent YAML, even if the Azure VoiceLive SDK supports it.
- VoiceLive always outputs PCM16 24kHz — the FFmpeg bridge then becomes mandatory to convert back to PCMU.

**Fix:** Implement the else-branches correctly:

```python
try:
    from azure.ai.voicelive.models import InputAudioFormat, OutputAudioFormat
except ImportError:
    return None, None

in_fmt_str = (self.session.get("input_audio_format") or "PCM16").lower()
out_fmt_str = (self.session.get("output_audio_format") or "PCM16").lower()

_IN_FMT_MAP = {
    "pcm16": InputAudioFormat.PCM16,
    "g711_ulaw": InputAudioFormat.G711_ULAW,
    "g711ulaw": InputAudioFormat.G711_ULAW,
}
_OUT_FMT_MAP = {
    "pcm16": OutputAudioFormat.PCM16,
    "g711_ulaw": OutputAudioFormat.G711_ULAW,
    "g711ulaw": OutputAudioFormat.G711_ULAW,
}

in_fmt = _IN_FMT_MAP.get(in_fmt_str, InputAudioFormat.PCM16)
out_fmt = _OUT_FMT_MAP.get(out_fmt_str, OutputAudioFormat.PCM16)
return in_fmt, out_fmt
```

> **Note:** The availability of `G711_ULAW` variants depends on the installed `azure-ai-voicelive` SDK version. Confirm with SDK docs before using. The FFmpeg bridge approach (Bug 1 fix) does not require the SDK to support native μ-law and is therefore more portable.

---

## End-to-End Audio Flow (Current Broken State vs. Correct State)

### INPUT PATH (User Mic → VoiceLive)

```
CURRENT (BRIDGE_MODE=off, 2-channel negotiation):
Genesys binary frame
  = interleaved PCMU 8kHz capture + playback (2 channels)
  → base64-encoded as-is
  → connection.input_audio_buffer.append(audio=encoded)
  → VoiceLive reads as PCM16 (session configured PCM16)
  → VoiceLive receives noise on doubled-length signal with echo

CORRECT (BRIDGE_MODE=pcmu_pcm16, capture-only):
Genesys binary frame
  = PCMU 8kHz capture only (1 channel)
  → base64-decoded to raw PCMU bytes
  → FfmpegAudioBridge.transcode_pcmu_to_pcm16() via asyncio.to_thread
  → produces PCM16 24kHz bytes
  → re-encoded to base64
  → connection.input_audio_buffer.append(audio=encoded)
  → VoiceLive receives clean PCM16 24kHz mono user audio ✓
```

### OUTPUT PATH (VoiceLive → Genesys Client)

```
CURRENT (BRIDGE_MODE=off):
VoiceLive RESPONSE_AUDIO_DELTA.delta
  = base64 PCM16 24kHz
  → _to_pcm_bytes() → raw PCM16 24kHz bytes
  → _resample_audio(pcm_bytes) → PCM16 8kHz bytes (via Catmull-Rom)
  → base64-encoded to string
  → _GenesysWebSocketWrapper.send_json() intercepts kind="AudioData"
  → base64.b64decode(data_b64) → raw PCM16 8kHz bytes
  → websocket.send_bytes(pcm16_bytes_disguised_as_pcmu)
  → Client receives PCM16 8kHz, interprets as PCMU 8kHz → NOISE + 2× speed

CORRECT (BRIDGE_MODE=pcmu_pcm16):
VoiceLive RESPONSE_AUDIO_DELTA.delta
  = base64 PCM16 24kHz
  → _to_pcm_bytes() → raw PCM16 24kHz bytes
  → FfmpegAudioBridge.transcode_pcm16_to_pcmu() via asyncio.to_thread
  → produces PCMU 8kHz bytes
  → base64-encoded to string
  → _GenesysWebSocketWrapper.send_json() intercepts kind="AudioData"
  → base64.b64decode(data_b64) → raw PCMU 8kHz bytes
  → websocket.send_bytes(pcmu_bytes)
  → Client receives PCMU 8kHz as declared in OPENED → clean audio ✓
```

---

## Secondary Observation: `_resample_audio()` Performance (Low)

`_resample_audio()` implements Catmull-Rom spline interpolation in a **pure Python per-sample loop**. For a 20ms frame at the 24kHz → 8kHz path (480 input samples → 160 output samples), this is 160 iterations of the inner loop plus numpy array operations. This runs **synchronously in the async event loop** (not offloaded to a thread), stalling audio delivery for every `RESPONSE_AUDIO_DELTA` event.

When the bridge is active this method is not called (the `if self._audio_bridge is not None:` branch returns early), so enabling the bridge also eliminates this latency source.

---

## Recommended Fix Summary

### Minimum Required (Immediate)

**1. Set environment variable:**
```
BRIDGE_MODE=pcmu_pcm16
```
Activate the FFmpeg bridge for PCMU↔PCM16 conversion. The bridge is already correctly implemented and configured for Genesys (PCM16 at 24kHz ↔ PCMU at 8kHz via FFmpeg pipes in `FfmpegAudioBridge`).

**2. Fix Genesys channel negotiation** in `apps/artagent/backend/api/v1/endpoints/genesys.py`:
```python
# Change from:
"channels": ["capture", "playback"]
# To:
"channels": ["capture"]
```
This ensures Genesys sends only user microphone audio per frame, eliminating the playback echo and channel interleaving.

### Recommended (Code Quality)

**3. Fix the dead ternary** in `apps/artagent/backend/registries/agentstore/base.py` `get_voicelive_audio_formats()`: implement the else-branch to map `g711_ulaw` values to the appropriate `InputAudioFormat`/`OutputAudioFormat` enum if the SDK supports them (or document that the bridge is the intended conversion path).

---

## Verification Test Plan

1. Set `BRIDGE_MODE=pcmu_pcm16` and change `"channels": ["capture"]`.
2. Open `/api/v1/genesys/test` page, connect, speak a phrase.
3. Listen to the response audio in the browser — should be clear speech, not noise.
4. Confirm in logs: `"Audio bridge initialized for VoiceLive"` appears at session start.
5. Confirm `_u2l` FFmpeg process receives PCMU data and outputs PCM16 (check `get_stats()` `frames_in` > 0 and `bytes_out` ≈ 3× `bytes_in` for 8kHz→24kHz).
6. Confirm `_l2u` FFmpeg process receives PCM16 24kHz output and produces PCMU (check `bytes_out` ≈ `bytes_in / 6`).
7. Verify no `"Audio bridge ingress conversion failed"` or `"Audio bridge egress conversion failed"` warnings in logs.
8. Run `tests/genesys_protocol_test.py` and `tests/test_audio_bridge_ffmpeg.py` to confirm no regressions.

---

## Reference: Working GPT Realtime Path (for comparison)

The reference implementation in `/azure-genesys-audiohook` (GPT Realtime) likely sends audio directly using the OpenAI Realtime API's native G.711 μ-law mode (`input_audio_format: "g711_ulaw"`, `output_audio_format: "g711_ulaw"`). In that mode the PCMU bytes from Genesys can be forwarded without any transcoding, and the model's PCMU output can be sent straight to Genesys. No FFmpeg bridge is needed. The VoiceLive migration broke this clean path because:

- VoiceLive's audio formats are managed separately (via `InputAudioFormat`/`OutputAudioFormat` SDK enums).
- The `get_voicelive_audio_formats()` implementation never actually wires up alternative formats.
- The FFmpeg bridge was added as the conversion mechanism but left disabled by default.
