"""Quick test: verify echo mode on genesys-debug endpoint."""
import asyncio
import json
import os

import websockets


async def test_echo():
    url = "ws://localhost:8000/api/v1/genesys-debug/stream?echo=true"
    async with websockets.connect(url) as ws:
        session_id = "test-echo-" + os.urandom(4).hex()

        # Phase 1: OPEN handshake
        open_msg = json.dumps({
            "version": "2",
            "type": "open",
            "seq": 1,
            "serverseq": 0,
            "id": session_id,
            "parameters": {
                "organizationId": "test-org",
                "conversationId": "test-conv-001",
                "participant": {
                    "id": "part-001",
                    "ani": "+15551234567",
                    "dnis": "+15559876543",
                },
                "media": [
                    {
                        "type": "audio",
                        "format": "PCMU",
                        "channels": ["external"],
                        "rate": 8000,
                    }
                ],
                "customConfig": {},
            },
        })
        await ws.send(open_msg)
        resp = await ws.recv()
        data = json.loads(resp)
        print(f"OPENED: type={data['type']}, seq={data['seq']}")

        # Phase 2: Send 5 audio chunks and collect echoes
        fake_pcmu = bytes([0x7F] * 800)  # 0.1 sec PCMU silence
        echo_count = 0

        for i in range(5):
            await ws.send(fake_pcmu)
            print(f"  Sent audio chunk #{i + 1}: {len(fake_pcmu)} bytes")
            await asyncio.sleep(0.05)

        # Collect echoed frames
        while True:
            try:
                echo = await asyncio.wait_for(ws.recv(), timeout=0.5)
                if isinstance(echo, bytes):
                    echo_count += 1
                    print(f"  ECHO RECEIVED: {len(echo)} bytes (echo #{echo_count})")
                else:
                    print(f"  Got text: {echo[:200]}")
            except asyncio.TimeoutError:
                break

        print(f"\nTotal echoes received: {echo_count} / 5 sent")
        assert echo_count == 5, f"Expected 5 echoes, got {echo_count}"

        # Phase 3: CLOSE
        close_msg = json.dumps({
            "version": "2",
            "type": "close",
            "seq": 2,
            "serverseq": 1,
            "id": session_id,
            "parameters": {},
        })
        await ws.send(close_msg)
        resp = await ws.recv()
        data = json.loads(resp)
        print(f"CLOSED: type={data['type']}, seq={data['seq']}")
        print("\nAll checks passed!")


if __name__ == "__main__":
    asyncio.run(test_echo())
