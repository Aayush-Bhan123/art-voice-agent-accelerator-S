# ROO CODE

# Genesys AudioHook WebSocket Error Analysis

## Error Summary

**Error:** `trigger_voicelive_response failed: Failed to send event: Cannot write to closing transport`

**Location:** [`apps/artagent/backend/registries/agentstore/base.py:1141`](apps/artagent/backend/registries/agentstore/base.py:1141)

**Context:** Genesys AudioHook integration using VoiceLive endpoint (not GPT Realtime)

---

## Root Cause Analysis

### Error Location

The error occurs in [`UnifiedAgent.trigger_voicelive_response()`](apps/artagent/backend/registries/agentstore/base.py:1091) when attempting to send a greeting to the VoiceLive connection:

```python
# Line 1132-1138 in base.py
await conn.send(
    ClientEventResponseCreate(
        response=ResponseCreateParams(
            instructions=verbatim_instruction,
        )
    )
)
```

### Call Stack Trace

The error propagates through this call chain:

1. **Genesys Endpoint** → [`genesys_audiohook_stream()`](apps/artagent/backend/api/v1/endpoints/genesys.py:163)
   - Line 308: `await handler.start()`

2. **VoiceLive Handler** → [`VoiceLiveSDKHandler.start()`](apps/artagent/backend/voice/voicelive/handler.py:897)
   - Line 963: Establishes VoiceLive connection via `connect()`
   - Line 1100: Creates `LiveOrchestrator` instance
   - Line 1184: Calls `orchestrator.start()`

3. **Live Orchestrator** → [`LiveOrchestrator.start()`](apps/artagent/backend/voice/voicelive/orchestrator.py:969)
   - Line 988: Calls `_switch_to(self.active, self._system_vars)`

4. **Agent Switch** → [`LiveOrchestrator._switch_to()`](apps/artagent/backend/voice/voicelive/orchestrator.py:1358)
   - Line 1484: Calls `agent.apply_voicelive_session()`

5. **Session Application** → [`UnifiedAgent.apply_voicelive_session()`](apps/artagent/backend/registries/agentstore/base.py:975)
   - Line 1077: `await conn.session.update(session=session_payload)`
   - Line 1089: **TRIGGERS ERROR** → `await self.trigger_voicelive_response(conn, say=say)`

6. **Response Trigger** → [`UnifiedAgent.trigger_voicelive_response()`](apps/artagent/backend/registries/agentstore/base.py:1091)
   - Line 1132: **FAILS HERE** → `await conn.send(...)`

---

## Connection Lifecycle Issues

### Problem 1: Race Condition Between Connection Establishment and Greeting

The VoiceLive connection (`conn`) is created asynchronously but the greeting is sent **immediately** after session.update() without verifying the connection is fully ready to accept events.

**Evidence:**
- Line 963 in handler.py: Connection established via context manager `__aenter__()`
- Line 1077 in base.py: Session update sent
- Line 1089 in base.py: Greeting sent **immediately without validation**

### Problem 2: No Connection State Validation Before Send

[`trigger_voicelive_response()`](apps/artagent/backend/registries/agentstore/base.py:1091) attempts to send without checking:
- Whether the connection is open
- Whether the connection is in a valid state
- Whether any previous operations failed

**Current Code (No Validation):**
```python
async def trigger_voicelive_response(
    self,
    conn,
    *,
    say: str | None = None,
    cancel_active: bool = True,
) -> None:
    """Trigger a response from the agent on a VoiceLive connection."""
    try:
        from azure.ai.voicelive.models import (
            ClientEventResponseCreate,
            ResponseCreateParams,
        )
    except ImportError:
        return

    if not say:
        return

    # Cancel any active response first to avoid conflicts
    if cancel_active:
        try:
            await conn.response.cancel()
        except Exception:
            pass  # No active response to cancel

    # NO CONNECTION STATE VALIDATION HERE ❌
    
    try:
        await conn.send(...)  # FAILS if connection is closing
    except Exception as e:
        logger.warning("trigger_voicelive_response failed: %s", e)
```

### Problem 3: Greeting Mechanism Timing

The greeting is scheduled to fire when the session becomes "ready", but there are **three competing mechanisms** that can trigger greetings:

1. **Direct trigger** in [`apply_voicelive_session()`](apps/artagent/backend/registries/agentstore/base.py:1089) - fires immediately
2. **Fallback trigger** via [`_schedule_greeting_fallback()`](apps/artagent/backend/voice/voicelive/orchestrator.py:2075) - fires after 350ms delay
3. **Session-ready trigger** in [`_handle_session_updated()`](apps/artagent/backend/voice/voicelive/orchestrator.py:1124) - fires when SESSION_UPDATED event arrives

These mechanisms can race, causing the error when the connection isn't ready.

### Problem 4: Error Propagation from VoiceLive Connection Establishment

If the VoiceLive connection fails during establishment (lines 957-963 in handler.py), the error propagates but may not be caught early enough, leading to a partially-initialized state where `conn` exists but is closing.

---

## Comparison with Expected Behavior

### What Should Happen (Successful Flow):

1. WebSocket accepted (Genesys → ART)
2. OPEN message received from Genesys
3. OPENED response sent to Genesys
4. VoiceLive connection established **and fully ready**
5. Session configuration applied
6. Greeting triggered **when connection is confirmed ready**

### What Actually Happens (Error Flow):

1. WebSocket accepted ✓
2. OPEN → OPENED ✓
3. VoiceLive connection initiated ✓
4. Session configuration starts ✓
5. Greeting triggered **before connection is fully ready** ❌
6. Error: "Cannot write to closing transport" ❌

---

## Key Differences from Working GPT Realtime Implementation

The working GPT Realtime implementation likely:
- Has different connection lifecycle timing
- May use synchronous connection establishment
- Might have built-in connection state validation
- Could have different error handling for partial failures

---

## Proposed Fixes

### Fix 1: Add Connection State Validation (HIGH PRIORITY)

**File:** [`apps/artagent/backend/registries/agentstore/base.py`](apps/artagent/backend/registries/agentstore/base.py:1091)

```python
async def trigger_voicelive_response(
    self,
    conn,
    *,
    say: str | None = None,
    cancel_active: bool = True,
) -> None:
    """Trigger a response from the agent on a VoiceLive connection."""
    try:
        from azure.ai.voicelive.models import (
            ClientEventResponseCreate,
            ResponseCreateParams,
        )
    except ImportError:
        return

    if not say:
        return

    # ✅ ADD: Validate connection is ready before attempting to send
    if not conn or not hasattr(conn, 'send'):
        logger.warning("trigger_voicelive_response: Invalid or missing connection")
        return
    
    # ✅ ADD: Check if connection is open (if API provides this)
    if hasattr(conn, 'closed') and conn.closed:
        logger.warning("trigger_voicelive_response: Connection is closed")
        return

    # Cancel any active response first to avoid conflicts
    if cancel_active:
        try:
            await conn.response.cancel()
        except Exception:
            pass  # No active response to cancel

    # Create response with explicit instruction to say the greeting verbatim
    verbatim_instruction = (
        f"Say exactly the following greeting to the user, word for word. "
        f"Do not add anything before or after. Do not modify the wording:\n\n"
        f'"{say}"'
    )

    try:
        await conn.send(
            ClientEventResponseCreate(
                response=ResponseCreateParams(
                    instructions=verbatim_instruction,
                )
            )
        )
        logger.debug("[%s] Triggered verbatim greeting response", self.name)
    except Exception as e:
        logger.warning("trigger_voicelive_response failed: %s", e)
```

### Fix 2: Don't Trigger Greeting Immediately (RECOMMENDED)

**File:** [`apps/artagent/backend/registries/agentstore/base.py`](apps/artagent/backend/registries/agentstore/base.py:1082)

Change line 1082-1089 to defer greeting to session-ready event:

```python
# Apply session
session_payload = RequestSession(**kwargs)
await conn.session.update(session=session_payload)

logger.info("[%s] Session updated successfully", self.name)
span.set_status(Status(StatusCode.OK))

# ✅ CHANGE: Don't trigger greeting immediately - let the orchestrator
# handle it via _handle_session_updated() when SESSION_UPDATED event confirms ready
# This prevents racing with connection establishment
if say:
    logger.info(
        "[%s] Greeting queued for session-ready event: %s",
        self.name,
        say[:50] + "..." if len(say) > 50 else say,
    )
    # Greeting will be triggered in orchestrator's _handle_session_updated()
```

Then ensure the orchestrator's [`_handle_session_updated()`](apps/artagent/backend/voice/voicelive/orchestrator.py:1089) method handles the pending greeting.

### Fix 3: Add Connection Health Check Before Handler Start (HIGH PRIORITY)

**File:** [`apps/artagent/backend/api/v1/endpoints/genesys.py`](apps/artagent/backend/api/v1/endpoints/genesys.py:307)

```python
try:
    await handler.start()
    
    # ✅ ADD: Verify connection is healthy before proceeding
    if not handler._connection or not handler._running:
        raise RuntimeError("Handler started but connection not established")
    
    # ✅ ADD: Small delay to ensure connection is fully ready
    await asyncio.sleep(0.1)  # 100ms for connection to stabilize
        
except Exception as e:
    logger.exception(
        "[%s] VoiceLive handler.start() failed: %s",
        session_id,
        e,
    )
    await websocket.close(1011, reason="VoiceLive start failed")
    span.set_status(Status(StatusCode.ERROR, str(e)))
    return
```

### Fix 4: Improve Error Handling in VoiceLive Connection Establishment

**File:** [`apps/artagent/backend/voice/voicelive/handler.py`](apps/artagent/backend/voice/voicelive/handler.py:957)

```python
# Trace VoiceLive connection establishment
conn_attrs = create_service_dependency_attrs(
    source_service="voicelive_sdk_handler",
    target_service="azure_voicelive",
    call_connection_id=self.call_connection_id,
    session_id=self.session_id,
    ws=True,
)
with tracer.start_as_current_span(
    "voicelive.connect",
    kind=SpanKind.SERVER,
    attributes=conn_attrs,
) as conn_span:
    try:
        self._credential = self._build_credential(self._settings)
        self._connection_cm = connect(
            endpoint=self._settings.azure_voicelive_endpoint,
            credential=self._credential,
            model=self._settings.azure_voicelive_model,
            connection_options=connection_options,
        )
        self._connection = await self._connection_cm.__aenter__()
        
        # ✅ ADD: Verify connection is open
        if not self._connection:
            raise RuntimeError("VoiceLive connection established but is None")
        
        conn_span.set_attribute("voicelive.model", self._settings.azure_voicelive_model)
        conn_span.set_attribute("voicelive.connection_ready", True)
        
    except Exception as e:
        # ✅ IMPROVE: Better cleanup on connection failure
        logger.error(
            "Failed to establish VoiceLive connection | endpoint=%s error=%s",
            self._settings.azure_voicelive_endpoint,
            e,
        )
        conn_span.set_status(StatusCode.ERROR, str(e))
        self._connection = None
        self._connection_cm = None
        raise  # Re-raise to fail fast
```

---

## Implementation Priority

1. **HIGH PRIORITY** - Fix 1: Add connection state validation in `trigger_voicelive_response()`
2. **HIGH PRIORITY** - Fix 3: Add connection health check before proceeding
3. **RECOMMENDED** - Fix 2: Defer greeting to session-ready event
4. **MEDIUM PRIORITY** - Fix 4: Improve connection establishment error handling

---

## Validation Checklist

Before deploying fixes, verify:

- [ ] VoiceLive connection establishment completes before greeting trigger
- [ ] Connection state is validated before sending events  
- [ ] Error handling gracefully fails without corrupting WebSocket state
- [ ] SESSION_UPDATED event is received before triggering greetings
- [ ] Handler health check passes before Genesys starts audio forwarding
- [ ] Error messages provide clear debugging information
- [ ] Connection cleanup happens properly on failures

---

## Testing Strategy

### Unit Tests

1. Test `trigger_voicelive_response()` with closed connection
2. Test `trigger_voicelive_response()` with None connection
3. Test `trigger_voicelive_response()` with missing attributes

### Integration Tests

1. Test Genesys → VoiceLive flow with connection delays
2. Test greeting mechanism with SESSION_UPDATED event timing
3. Test error recovery when VoiceLive connection fails

### Manual Testing

1. Connect Genesys client to `/api/v1/genesys/stream`
2. Verify OPEN → OPENED handshake
3. Verify audio bidirectional flow
4. Verify greeting is delivered without errors
5. Test reconnection after connection drop

---

## Next Steps

1. Review with working GPT Realtime implementation for comparison
2. Implement Fix 1 (connection validation) as immediate hotfix
3. Implement Fix 3 (health check) for robustness
4. Consider Fix 2 (defer greeting) for architectural improvement
5. Add comprehensive logging at each step for easier debugging
6. Create monitoring alerts for "Cannot write to closing transport" errors
