# Testing the Genesys endpoint locally

You can test the Genesys AudioHook WebSocket (`/api/v1/genesys/stream`) locally without the gc-audioconnector-voiceagent-test repo.

## 1. Set environment variables

### Reference: full .env for Genesys + Voice Agent (ART)

Use this block in your `.env` and replace placeholders (`<your-endpoint>`, `<your-key>`) with real values:

```bash
# Optional: API key for Genesys test UI / ART (if GENESYS_WS_API_KEY set on ART)
WEBSOCKET_SERVER_API_KEY=5sglhberkgasqi5zgyy75paqi4
WEBSOCKET_SERVER_CLIENT_SECRET=dmc2Zmd2M2h0bjJ5aQ==

# ART backend (deployed or local)
ART_VOICE_AGENT_ACCELERATOR_URL=https://rtaudioagent-backend-0qr1dgnc.redwater-dc66b596.eastus2.azurecontainerapps.io
ART_VOICE_AGENT_API_KEY=5sglhberkgasqi5zgyy75paqi4
ART_VOICE_AGENT_CLIENT_SECRET=dmc2Zmd2M2h0bjJ5aQ==
ART_VOICE_AGENT_ACCELERATOR_WS_URL=wss://rtaudioagent-backend-0qr1dgnc.redwater-dc66b596.eastus2.azurecontainerapps.io
ART_VOICE_AGENT_SCENARIO=insurance
ART_VOICE_AGENT_STREAMING_MODE=VOICE_LIVE

# Voice Agent / AudioHook
VOICE_AGENT_BACKEND_URL=wss://rtaudioagent-backend-0qr1dgnc.redwater-dc66b596.eastus2.azurecontainerapps.io
VOICE_AGENT_SCENARIO=insurance
SPEECH_PROVIDER=voice-agent
AUDIOHOOK_MODE=integration
USE_MEDIA_ENDPOINT=true
G711_PASSTHROUGH=true

# Genesys
GENESYS_INTEGRATION_ID=769cb1ec-e521-4576-b86c-f7cd901ecab6

# --- For ART backend when running locally ---
# Skip Redis/Cosmos so backend starts without Azure Redis
SKIP_REDIS=1
# PCMU ↔ PCM16 (Genesys sends PCMU)
BRIDGE_MODE=pcmu_pcm16
# Optional
ACS_STREAMING_MODE=VOICE_LIVE

# Azure OpenAI Realtime (Voice Live) – replace with your values
AZURE_VOICELIVE_ENDPOINT=https://<your-endpoint>.openai.azure.com/
AZURE_VOICELIVE_API_KEY=<your-key>
AZURE_VOICELIVE_MODEL=gpt-4o-realtime-preview
```

Without `AZURE_VOICELIVE_*` (real endpoint/key), the WebSocket will accept OPEN→OPENED but `handler.start()` will fail and you won’t get bot audio.

## 2. Start the backend

From the project root, **without Redis** (for local audio testing):

```bash
SKIP_REDIS=1 make start_backend
```

Or:

```bash
SKIP_REDIS=1 uv run uvicorn apps.artagent.backend.main:app --host 0.0.0.0 --port 8010 --reload
```

Set `SKIP_REDIS=1` (or leave `REDIS_HOST` unset) so the backend starts without Azure Redis. With Redis configured you can use `make start_backend` as usual.

Backend will be at `http://localhost:8010`.

## 3. Open the test page

Either:

- **Served by backend:**  
  Open in a browser:  
  **http://localhost:8010/api/v1/genesys/test**  
  The page will use `ws://localhost:8010/api/v1/genesys/stream` by default (leave WebSocket URL empty).

- **From disk:**  
  Open `tests/genesys_test.html` in your browser (e.g. double-click).  
  Set WebSocket URL to: `ws://localhost:8010/api/v1/genesys/stream`.

## 4. Run the test

1. Click **Connect** — you should see `OPEN → OPENED` in the log and status “Connected”.
2. Click **Start Mic** — allow microphone access.
3. Speak — you should hear the bot reply (if Azure VoiceLive credentials are set).

If credentials are missing, you’ll still see OPENED and can confirm the Genesys handshake works; audio will only flow once VoiceLive is configured.
