# Voice Agent Implementation — Technical Overview

*Summary for technical discussion (e.g. with Wathek).*

---

## High-level components

| Component | Role | Repo / location |
|-----------|------|------------------|
| **Genesys Cloud** | Inbound/outbound calls, RTP audio | External (Genesys) |
| **Azure Genesis AudioHook** | WebSocket bridge: Genesys ↔ ART. Receives/sends PCMU 8kHz. | `azure-genesis-audiohook` (separate repo) |
| **ART Voice Agent Accelerator** | Voice agent backend: STT → LLM → TTS, agents, tools, handoffs | `art-voice-agent-accelerator` (this repo) |

End-to-end: **Genesys (caller)** ↔ **AudioHook (WebSocket)** ↔ **ART Backend (orchestrators)**.

---

## Orchestration modes (ART backend)

| Mode | Orchestrator | Audio path | Use case |
|------|--------------|------------|----------|
| **Cascade (media)** | `CascadeOrchestratorAdapter` | Azure Speech STT/TTS, PCM 16kHz | Fine-grained control, custom VAD |
| **VoiceLive** | Voice Live API | Can use G.711 passthrough (PCMU end-to-end) | Lower latency, less conversion |

Agents and scenarios are shared; only the audio pipeline and API differ.

---

## Audio flow (Cascade mode)

- **Inbound (Genesys → ART):**  
  PCMU 8kHz → `audioop.ulaw2lin` → resample 8→16 kHz (`audioop.ratecv`) → Azure Speech STT → transcript → LLM → TTS.

- **Outbound (ART → Genesys):**  
  TTS PCM 16kHz → resample 16→8 kHz → `audioop.lin2ulaw` → WebSocket binary → Genesys.

Conversion is in **ART**: `apps/artagent/backend/api/v1/handlers/audiohook_handler.py` (uses Python stdlib `audioop`; deprecated in 3.11+).

---

## Key code and docs (this repo)

| Topic | Where |
|-------|--------|
| AudioHook WebSocket endpoint | `apps/artagent/backend/api/v1/endpoints/audiohook.py` |
| Audio conversion + orchestration | `apps/artagent/backend/api/v1/handlers/audiohook_handler.py` |
| Bidirectional flow (incl. Mermaid) | `docs/audiohook-bidirectional-flow.md` |
| Genesys AudioHook integration | `docs/integration/genesys-audiohook.md` |
| Orchestration (Cascade vs VoiceLive) | `docs/architecture/orchestration/README.md` |
| Cascade orchestrator | `docs/architecture/orchestration/cascade.md` |
| VoiceLive | `docs/architecture/orchestration/voicelive.md` |

---

## How to share / discuss with Wathek

1. **Share this file**  
   Send `docs/VOICE_AGENT_TECHNICAL_OVERVIEW.md` (and optionally `docs/audiohook-bidirectional-flow.md`).

2. **Point to the repo**  
   `art-voice-agent-accelerator` (and, if relevant, `azure-genesis-audiohook`).

3. **Connect for a call**  
   Use your usual channel (email, Teams, Slack) to set up a meeting; use this doc as the agenda for the technical components of the voice agent implementation.

I can’t create calendar invites or send messages; use your normal tools to invite Wathek and attach or link this overview.
