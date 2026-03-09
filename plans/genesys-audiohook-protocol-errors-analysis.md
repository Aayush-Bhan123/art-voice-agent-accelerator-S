# Copilot Opus
# Genesys AudioHook Protocol Errors Analysis

**Errors investigated:**
1. `Sequence number mismatch: AudioHook session expected sequence 1 but received sequence 2`
2. `Unrecognized field "codec" in Media model`
3. `Error code Audiohook-0009`
4. Error flow: `validateServerMessage → FailureContext$Builder.build → AudioHookException → onWarning → terminate`

**Context:** Genesys AudioHook v2 protocol integration with VoiceLive endpoint at `/api/v1/genesys/stream`

---

## TL;DR — Three protocol compliance bugs cause session establishment failure

| # | Bug | Severity | Location |
|---|-----|----------|----------|
| 1 | **Wrong media field name: `codec` instead of `format`** | Critical | `api/v1/endpoints/genesys.py` line 234 |
| 2 | **Wrong channel identifiers: `capture`/`playback` instead of `external`** | High | `api/v1/endpoints/genesys.py` line 237 |
| 3 | **Sequence number drift: PING before OPEN consumes seq=1** | Medium | `api/v1/endpoints/genesys.py` lines 249-253 |
| 4 | **Same bugs propagated in test files** | Low | `tests/genesys_test.html`, `tests/genesys_protocol_test.py` |

The Genesys SDK validates every server message against the AudioHook v2 protocol schema via `validateServerMessage`. When validation fails, it builds a `FailureContext`, throws `AudioHookException`, invokes `onWarning`, and terminates the session. Error code `Audiohook-0009` is the protocol validation failure code.

---

## Bug 1: Media Field Name — `codec` vs `format` (Critical)

### The Error

```
Unrecognized field "codec" in Media model
```

### What Our Code Sends

**File:** `apps/artagent/backend/api/v1/endpoints/genesys.py` lines 230-238

```python
opened_params = {
    "startPaused": False,
    "media": [
        {
            "type": "audio",
            "codec": "PCMU",     # ← WRONG FIELD NAME
            "rate": 8000,
            "channels": ["capture", "playback"],
        }
    ],
}
```

### What the Protocol Expects

The Genesys AudioHook v2 protocol uses `format`, not `codec`, in the Media model. Evidence from the correct implementation in `tests/genesys_audioconnector_test.html` line 300:

```javascript
media: [{ type: 'audio', format: 'PCMU', channels: ['external'], rate: 8000 }]
```

### Why This Causes Audiohook-0009

The Genesys SDK has a strict model validator (`validateServerMessage`). The `Media` model schema defines these fields:
- `type` (required) — media type, e.g. `"audio"`
- `format` (required) — codec identifier, e.g. `"PCMU"`, `"PCMA"`, `"opus"`
- `rate` (required) — sample rate in Hz
- `channels` (required) — array of channel identifiers

When the SDK encounters the field `"codec"`, it is unrecognized by the schema. The validation error flow is:

```
validateServerMessage(opened_msg)
  → Media model validation fails: field "codec" not in schema
    → FailureContext$Builder.build({ field: "codec", message: "Unrecognized field" })
      → throw new AudioHookException(Audiohook-0009, failureContext)
        → onWarning(exception)
          → terminate()
```

The session is terminated before audio streaming can begin.

### Affected Files

| File | Line | Uses `codec` | Should be `format` |
|------|------|-------------|-------------------|
| `apps/artagent/backend/api/v1/endpoints/genesys.py` | 234 | `"codec": "PCMU"` | `"format": "PCMU"` |
| `tests/genesys_test.html` | 195 | `codec: "PCMU"` | `format: "PCMU"` |
| `tests/genesys_protocol_test.py` | 72 | `"codec": "PCMU"` | `"format": "PCMU"` |

The `tests/genesys_audioconnector_test.html` is the **only** file that uses the correct field name `format`.

### Fix

Replace `"codec"` with `"format"` in all three files listed above.

---

## Bug 2: Channel Identifiers — `capture`/`playback` vs `external` (High)

### What Our Code Sends

```python
"channels": ["capture", "playback"]
```

### What the Protocol Expects

The Genesys AudioHook v2 protocol defines these channel identifiers:
- `"external"` — the customer-facing audio channel (what the caller hears/says)
- `"internal"` — the agent-facing audio channel (optional, for agent-side audio)

Evidence from `tests/genesys_audioconnector_test.html` line 300:

```javascript
channels: ['external']
```

`"capture"` and `"playback"` are not recognized AudioHook v2 channel identifiers. They appear to be borrowed from the WebRTC/browser audio terminology used in the test UI (`genesys_test.html`).

### Impact

1. **Schema validation failure**: Depending on the strictness of the Genesys SDK's channel validation, unrecognized channel names may trigger another `Audiohook-0009` error or be silently ignored.

2. **Double audio forwarding**: If accepted, requesting both `capture` AND `playback` would cause Genesys to send interleaved two-channel audio (customer mic + agent mic). Our code treats all binary frames as single-channel user audio, so the playback channel's bytes would be misinterpreted as additional user speech — causing echo and noise. This was already documented in `plans/genesys-voicelive-noisy-audio-analysis.md` as Bug 4.

3. **Audio direction ambiguity**: With `external` only, Genesys sends the customer's audio on a single channel. With `capture`/`playback`, the byte layout per frame is undefined.

### Fix

Change `"channels": ["capture", "playback"]` to `"channels": ["external"]`.

---

## Bug 3: Sequence Number Drift — PING Before OPEN (Medium)

### The Error

```
Sequence number mismatch: AudioHook session expected sequence 1 but received sequence 2
```

### Root Cause

The pre-OPEN message loop in `genesys.py` handles PING messages by incrementing `server_seq` and sending PONG **before** OPENED is sent. If Genesys Cloud sends a PING between the WebSocket connection and the OPEN message, the server responds with PONG (consuming seq=1), and the subsequent OPENED gets seq=2.

```python
# genesys.py lines 192-253 (pre-OPEN loop)
server_seq = 0       # initialized at 0
client_seq = 0

while websocket connected:
    raw = await websocket.receive()
    # ...
    if msg_type == GENESYS_OPEN:
        server_seq += 1              # seq becomes 1 (normal case)
        # send OPENED with seq=server_seq
        break

    if msg_type == GENESYS_PING:
        server_seq += 1              # seq becomes 1 — CONSUMED by PONG
        # send PONG with seq=1
        continue                     # back to top of loop

    if msg_type == GENESYS_CLOSE:
        server_seq += 1
        # send CLOSED
        return
```

### Race Scenario

```
Timeline:
─────────────────────────────────────────────────────────

Client                         Server
  │                              │
  ├── WebSocket connect ────────►│
  │                              ├── accept()
  │                              │   server_seq = 0
  │   ┌─────────────────────┐    │
  ├── │ PING (seq=1)        │───►│  ← Genesys heartbeat fires before OPEN
  │   └─────────────────────┘    │
  │                              ├── server_seq += 1 → 1
  │   ┌─────────────────────┐    │
  │◄──│ PONG (seq=1)        │────┤  ← seq=1 consumed by PONG
  │   └─────────────────────┘    │
  │   ┌─────────────────────┐    │
  ├── │ OPEN (seq=2)        │───►│
  │   └─────────────────────┘    │
  │                              ├── server_seq += 1 → 2
  │   ┌─────────────────────┐    │
  │◄──│ OPENED (seq=2) ❌   │────┤  ← Genesys expects seq=1 for OPENED
  │   └─────────────────────┘    │
  │                              │
  ├── TERMINATE (Audiohook-0009) │
```

### Why This Happens

The Genesys AudioHook protocol specifies that:
1. The server's first message is `opened` (seq=1) in response to the client's `open`
2. The server MUST NOT send protocol messages before `opened`

By responding to PING with PONG before the OPEN/OPENED handshake completes, the server violates the protocol ordering. The Genesys SDK tracks the expected server sequence number and rejects the OPENED when it arrives with seq=2.

### Likelihood

This race requires Genesys Cloud to send PING before OPEN. This can happen when:
- The Genesys AudioHook integration has aggressive keepalive settings
- Network latency causes the heartbeat timer to fire before the OPEN message is processed
- The AudioHook SDK's internal scheduler sends PING on a fixed interval from WebSocket establishment, independent of the OPEN handshake

### Fix Options

**Option A (Recommended):** Do not handle PING in the pre-OPEN loop. Queue or ignore PINGs until after OPENED is sent:

```python
while websocket connected:
    raw = await websocket.receive()
    if msg_type == GENESYS_OPEN:
        server_seq += 1
        # send OPENED
        break
    if msg_type == GENESYS_PING:
        # Do NOT respond to PING before OPENED
        # Genesys will retry PING after OPENED
        continue
    if msg_type == GENESYS_CLOSE:
        server_seq += 1
        # send CLOSED
        return
```

**Option B:** Maintain a separate "pre-open" counter so OPENED always gets seq=1:

```python
server_seq = 0
# Ignore all PINGs in pre-OPEN loop
# After OPEN:
server_seq = 1  # Force seq=1 for OPENED
# send OPENED
```

---

## Audiohook-0009 Error Code

### Definition

`Audiohook-0009` is the Genesys AudioHook SDK error code for **server message validation failure**. It is thrown when `validateServerMessage()` detects any of:

- Unrecognized fields in a protocol model (e.g., `"codec"` in `Media`)
- Missing required fields in a protocol model
- Invalid field values (e.g., wrong data types)
- Sequence number violations (unexpected `seq` values)

### Error Flow

```
Server sends OPENED message
  ↓
Genesys SDK: validateServerMessage(opened_msg)
  ↓
Validation fails:
  • "Unrecognized field 'codec' in Media model" (Bug 1)
  • and/or "Sequence number mismatch" (Bug 3)
  ↓
FailureContext$Builder
  .setErrorCode("Audiohook-0009")
  .setMessage("Invalid server message")
  .setDetails(validationErrors)
  .build()
  ↓
throw new AudioHookException(failureContext)
  ↓
onWarning(exception)
  → logs warning
  → terminates session
  ↓
Client sends CLOSE to server
Server sends CLOSED
WebSocket closes
```

### Multiple Triggering Conditions

Both Bug 1 (`codec` field) and Bug 3 (sequence drift) can independently trigger `Audiohook-0009`. In the typical case (no PING-before-OPEN race), Bug 1 alone is sufficient to cause the error.

---

## Cross-Reference with Existing Plans

### `genesys-voicelive-noisy-audio-analysis.md`

Bug 4 in that document ("Both Genesys Channels: capture AND playback forwarded as user audio") is directly related to Bug 2 in this analysis. The channel naming issue causes both the protocol validation problem (channel names not recognized by AudioHook SDK) and the audio quality problem (interleaved channels treated as mono).

Correction: The noisy audio analysis states that `"channels": ["capture", "playback"]` causes Genesys to send interleaved multi-channel audio. This is true if the channels are accepted. But with the Genesys SDK's strict validation, the unrecognized channel names may cause the session to fail before audio streaming starts. The audio noise issue only manifests if the Genesys SDK accepts the channel names despite them being non-standard.

### `genesys-voicelive-transport-closing-error-analysis.md`

No conflicts. The transport closing error is independent of the protocol compliance bugs documented here. The transport error occurs during teardown; the protocol errors occur during session establishment.

### `genesys-websocket-error-analysis.md`

This older document covers the same transport closing error from a slightly different angle. It correctly identifies the lack of connection state validation in `trigger_voicelive_response()`. No conflicts with this analysis.

---

## Correct OPENED Message

Based on the Genesys AudioHook v2 protocol specification and the correct implementation in `tests/genesys_audioconnector_test.html`:

### Current (Broken)

```python
opened_params = {
    "startPaused": False,
    "media": [
        {
            "type": "audio",
            "codec": "PCMU",                        # ❌ Wrong field name
            "rate": 8000,
            "channels": ["capture", "playback"],     # ❌ Wrong channel identifiers
        }
    ],
}
```

### Correct

```python
opened_params = {
    "startPaused": False,
    "media": [
        {
            "type": "audio",
            "format": "PCMU",                        # ✅ Correct field name
            "rate": 8000,
            "channels": ["external"],                 # ✅ Correct channel identifier
        }
    ],
}
```

---

## Files Requiring Changes

| File | Change | Priority |
|------|--------|----------|
| `apps/artagent/backend/api/v1/endpoints/genesys.py` | `codec` → `format`, `["capture", "playback"]` → `["external"]`, suppress PING in pre-OPEN loop | Critical |
| `tests/genesys_test.html` | `codec` → `format`, `["capture", "playback"]` → `["external"]` | High |
| `tests/genesys_protocol_test.py` | `codec` → `format`, `["capture", "playback"]` → `["external"]` | High |

`tests/genesys_audioconnector_test.html` already uses the correct field names and should be used as the reference implementation.

---

## Summary of Error Causation Chain

```
genesys.py sends OPENED with { "codec": "PCMU", "channels": ["capture", "playback"] }
  ↓
Genesys SDK: validateServerMessage()
  ↓
Media model validation:
  • Field "codec" not in schema → "Unrecognized field 'codec' in Media model"
  • (if reached) Channel names not recognized → additional validation error
  ↓
FailureContext$Builder.build() → Audiohook-0009
  ↓
AudioHookException thrown
  ↓
onWarning() → logs protocol error
  ↓
terminate() → session closed, no audio streaming
```

If the `codec`/`format` issue is fixed but PING arrives before OPEN:

```
PING (seq=1 from client) → server responds with PONG (server seq=1)
  ↓
OPEN (seq=2 from client) → server responds with OPENED (server seq=2)
  ↓
Genesys SDK: validateServerMessage()
  • Expected server seq=1 for first OPENED, got seq=2
  → "Sequence number mismatch: expected sequence 1 but received sequence 2"
  ↓
Audiohook-0009 → terminate
```
