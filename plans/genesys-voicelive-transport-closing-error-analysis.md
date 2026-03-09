# Copilot Sonnet
# Root Cause Analysis: "Cannot write to closing transport" — Genesys VoiceLive Integration

**Error:** `trigger_voicelive_response failed: Failed to send event: Cannot write to closing transport`
**Location:** `apps/artagent/backend/registries/agentstore/base.py` line 1141
**Context:** Genesys AudioHook WebSocket endpoint migrated from GPT Realtime to VoiceLive

---

## Summary

The error is a **teardown race condition** in `VoiceLiveSDKHandler.stop()`. A greeting fallback `asyncio.Task` holds a direct reference to the VoiceLive connection object (`conn`) and can wake from an `asyncio.sleep(0.35)` window during the connection's close handshake, attempting to write to a transport that is already transitioning to `CLOSING`.

This race is latent in the normal ACS flow but surfaces reliably in the Genesys path because the Genesys WebSocket may disconnect very shortly after the `OPEN`→`OPENED` handshake (test clients, short calls, network resets), which is a tight enough window for the 350 ms greeting fallback task to still be pending.

---

## Files Involved

| File | Role |
|------|------|
| `apps/artagent/backend/api/v1/endpoints/genesys.py` | Genesys WebSocket endpoint; creates `VoiceLiveSDKHandler`, calls `handler.stop()` in `finally` |
| `apps/artagent/backend/voice/voicelive/handler.py` | `VoiceLiveSDKHandler.stop()` — ordering bug lives here |
| `apps/artagent/backend/voice/voicelive/orchestrator.py` | `LiveOrchestrator._schedule_greeting_fallback()` — creates the leaking task |
| `apps/artagent/backend/registries/agentstore/base.py` | `trigger_voicelive_response()` — where the error is raised (line 1141) |

---

## Execution Flow

### 1. Startup (inside `handler.start()`)

```
VoiceLiveSDKHandler.start()
  └─ orchestrator.start()
       └─ _switch_to(start_agent, system_vars)
            ├─ apply_voicelive_session(conn)
            │    └─ await conn.session.update(...)   ← sends session.update to Azure VoiceLive
            └─ _schedule_greeting_fallback(agent_name)
                 └─ asyncio.create_task(_fallback())  ← 350 ms sleep, then conn.send(...)
  └─ self._event_task = asyncio.create_task(_event_loop())
  └─ handler.start() RETURNS CONTROL to genesys endpoint
```

After startup, **two concurrent tasks are active**:

| Task | What it does |
|------|-------------|
| `_event_task` | Reads VoiceLive events. On `SESSION_UPDATED`, fires the **primary** greeting via `trigger_voicelive_response(conn, ...)` |
| Greeting fallback task (in `orchestrator._greeting_tasks`) | Sleeps 350 ms as a safety net, then fires the **backup** greeting via `trigger_voicelive_response(conn, ...)` |

### 2. Genesys disconnect triggers teardown

```
genesys_audiohook_stream()  →  finally block
  └─ await handler.stop()
```

### 3. Ordering bug inside `VoiceLiveSDKHandler.stop()` (current code)

```
stop()
  ├─ _running = False; _shutdown.set()
  ├─ bridge.close()
  ├─ _dtmf_processor.cleanup()
  ├─ _event_task.cancel(); await _event_task         ← event loop killed ✓
  │
  ├─ await _connection_cm.__aexit__(None, None, None) ← (A) TRANSPORT → CLOSING
  │     ~~ await yields to event loop ~~
  │     ~~ greeting fallback task wakes from sleep(0.35) ~~
  │     ~~ task calls: await conn.send(ClientEventResponseCreate(...)) ~~
  │     ~~ ERROR: "Cannot write to closing transport" ~~                    ← BUG HERE
  │
  └─ orchestrator.cleanup()                           ← (B) TOO LATE
       └─ _cancel_pending_greeting_tasks()            ← greeting tasks cancelled after connection closed
```

The `await _connection_cm.__aexit__()` call is an async operation. The event loop yields control during the close handshake. During that yield window, the greeting fallback task — which was sleeping for 350 ms — can wake up and call `await conn.send(...)` on a connection whose transport is already in `CLOSING` state.

---

## Why the Greeting Task Is Not Caught by the Handler's Own Cleanup

`VoiceLiveSDKHandler._cancel_all_background_tasks()` only drains `self._pending_background_tasks`, which is populated via `self._background_task(coro, label=...)`. The orchestrator's greeting tasks created by `_schedule_greeting_fallback()` are stored in `orchestrator._greeting_tasks` — a completely separate set on the orchestrator object, invisible to the handler's bookkeeping. They are only cancelled by `orchestrator.cleanup()` → `_cancel_pending_greeting_tasks()`, which arrives too late under the current ordering.

---

## Why the Primary Greeting Path Also Races

Even the `SESSION_UPDATED` primary path (`_handle_session_updated` in the orchestrator) calls `trigger_voicelive_response(conn, ...)` directly. If `SESSION_UPDATED` arrives at the same moment teardown starts, the same race applies — `_event_task` is cancelled (`CancelledError` raised) but any in-flight `await conn.send()` inside the orchestrator's event handler can still execute partially.

---

## Why `trigger_voicelive_response` Has No Guard

The method in `base.py` calls `await conn.send(...)` unconditionally; there is no check on VoiceLive transport/connection state before attempting the write. Compare this to `_forward_event_to_acs()` in `handler.py`, which guards every write with:

```python
if not self._websocket_open:
    return
```

No equivalent guard exists for outbound writes to the VoiceLive connection.

---

## Why This Is Specific to Genesys

In the normal ACS call flow, the WebSocket connection lives for the full duration of the call (minutes). By the time teardown occurs, the 350 ms greeting window has long expired and the fallback task has either fired successfully or been cleared by `_pending_greeting = None`. In the Genesys path:

- The test HTML client (`tests/genesys_test.html`) is often closed seconds after connection.
- The Genesys CLOSE message may arrive immediately after OPENED is sent.
- Short integration tests disconnect after the handshake, before the agent has finished greeting.

All of these produce a teardown window that overlaps with the 350 ms fallback sleep.

---

## Proposed Fix

**Single change: reorder three blocks in `VoiceLiveSDKHandler.stop()`**
(`apps/artagent/backend/voice/voicelive/handler.py`)

### Current order (buggy)

```
1. Cancel + await _event_task
2. await _connection_cm.__aexit__()     ← A: transport → CLOSING
3. orchestrator.cleanup()               ← B: cancels greeting tasks (too late)
```

### Correct order

```
1. Cancel + await _event_task
2. orchestrator._cancel_pending_greeting_tasks()   ← cancel greeting tasks FIRST
3. await asyncio.sleep(0)                          ← yield so cancellations are delivered
4. await _connection_cm.__aexit__()                ← NOW safe to close transport
5. orchestrator.cleanup()                          ← full cleanup of remaining references
```

### Diff (conceptual)

```python
# After: await self._event_task  (the event loop task)
# BEFORE the connection close block, insert:

if self._orchestrator:
    try:
        self._orchestrator._cancel_pending_greeting_tasks()
    except Exception:
        logger.debug("Failed to cancel greeting tasks before connection close", exc_info=True)
# Yield to the event loop so tasks that have already exited sleep()
# receive their CancelledError before the transport closes.
await asyncio.sleep(0)

# Then: await self._connection_cm.__aexit__(None, None, None)   (unchanged)
# Then: self._orchestrator.cleanup()                             (unchanged)
```

### Why `await asyncio.sleep(0)` is necessary

Calling `task.cancel()` only *schedules* a `CancelledError` to be raised at the task's next `await`. If the greeting task has already exited `asyncio.sleep(0.35)` and is currently executing synchronous code before reaching `await conn.send(...)`, then `cancel()` alone won't stop it in time. Yielding with `await asyncio.sleep(0)` gives the event loop one cycle to deliver the pending `CancelledError` to that task before the transport closes.

---

## Optional Secondary Hardening

After the primary fix is applied, an additional defensive guard in `trigger_voicelive_response` would eliminate any residual risk from other teardown sequences:

```python
# base.py — inside trigger_voicelive_response(), before await conn.send(...)
# Check that the connection is still usable before attempting to write.
if getattr(conn, 'closed', False):
    logger.debug("[%s] VoiceLive connection already closed; skipping response trigger", self.name)
    return
```

The exact attribute name to check depends on the `azure-ai-voicelive` SDK's connection class (likely `conn.closed` or a similar boolean property). This is a belt-and-suspenders guard, not a replacement for the primary fix.

---

## Testing the Fix

1. Run the Genesys HTML test page (`/api/v1/genesys/test`), connect, then immediately close the browser tab.
2. Confirm the warning `"trigger_voicelive_response failed: Failed to send event: Cannot write to closing transport"` no longer appears in logs.
3. Normal call flows (full conversation, graceful CLOSE message from Genesys) should be unaffected.
4. Existing test coverage: `tests/genesys_protocol_test.py`, `tests/test_voice_handler_components.py`.
