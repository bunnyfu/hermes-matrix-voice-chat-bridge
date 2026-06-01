# Hermes Matrix Voice Chat Bridge 🎙️🤖

A native Voice-to-LLM bridge for the Matrix ecosystem. This project bridges **MatrixRTC (Element Call)** and **LiveKit WebRTC** directly into an AI agent (like [Hermes](https://github.com/your-repo/hermes) or any OpenAI-compatible API). 

It allows you to seamlessly call your AI agent from within Element/Matrix, speak to it in real-time, and hear its synthesized voice responses back—all while maintaining **End-to-End Encryption (E2EE)** for the call signaling.

## Features
* **Native Element Call Support**: Works directly with Matrix's MSC3401 (Native Group VoIP) and LiveKit SFU.
* **Full E2EE Matrix Signaling**: Handles Olm/Megolm encryption automatically. The agent is provisioned as a fully trusted, cross-signed device so it can securely decrypt room states and WebRTC signaling.
* **Container-Native "Sidecar" Design**: Designed to run seamlessly inside or alongside an existing `hermes-agent` Docker container. It shares the same Python environment and relies on the Hermes API for the LLM brain.
* **Thinking Soundscape**: Plays a soft, dynamic ambient sine-wave pulse (configurable via `generate-pulse.py`) while the LLM is generating a response, so you always know it's "thinking".
* **Robust Session Management**: Binds ephemeral unique Session UUIDs per call, avoiding context-leakage across different calls, and cleanly handles orphaned `m.call.member` Matrix state events on container restarts.

## Architecture

```text
Element X ──(WebRTC)──> LiveKit ──(PCM)──> Bridge ──(STT)──> Transcription (xAI/Groq/Local)
                                                                  │
                                                          (Text Transcript)
                                                                  │
                                              Hermes API <────────┘
                                              (chat/completions)
                                                      │
                                                  (Response)
                                                      │
Element X <──(WebRTC)── LiveKit <──(PCM)── Bridge <──(TTS)── Speech Synthesis (xAI/OpenAI)
```

## Infrastructure Requirements

To run this bridge, you need:
1. **Matrix Homeserver**: Configured with MatrixRTC and a connected LiveKit SFU (e.g., Synapse + LiveKit JWT integration).
2. **LiveKit Server**: An active LiveKit SFU to handle the WebRTC audio streaming.
3. **Hermes Agent (or OpenAI-like API)**: The core brain that processes the STT transcripts and returns the LLM response.

## Quick Start (Standalone / Dev)

```bash
# 1. Create and activate venv
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure environment variables (see .env.example)
nano .env

# 4. Generate the ambient "thinking" pulse sound
python generate-pulse.py

# 5. Run the bridge
python bridge.py
```

---

## Deploying as a Hermes Docker Sidecar (Production)

The bridge is designed to be mounted directly into an existing `hermes-agent` container.

### 1. Matrix Room Configuration
Before running the bridge, you must configure the Matrix room where the voice calls will take place:
1. In Element (or your Matrix client), create a new Private Room.
2. Invite your Hermes agent's account to this room.
3. Open the Room Settings -> Advanced, and copy the **Internal Room ID** (it will look like `!abcdefghijk:yourdomain.com`).
4. Set this Room ID in your `.env` file as `VOICE_BRIDGE_MATRIX_ROOM_ID`.

### 2. Docker Configuration
Mount this repository to `/opt/hermes/voice-bridge` inside your container and modify the startup command.

Example `docker-compose.yml` snippet:
```yaml
  hermes:
    image: hermes-agent
    volumes:
      - ./users/hermes/hermes-data:/opt/data
      - /path/to/hermes-matrix-voice-chat-bridge:/opt/hermes/voice-bridge:ro
    env_file:
      - ./users/hermes/.env
    command: ["bash", "/opt/hermes/voice-bridge/start-bridge.sh"]
```

Example `start-bridge.sh`:
```bash
#!/bin/bash
# Install required dependencies for the bridge inside the container's venv
uv pip install --python /opt/hermes/.venv -r /opt/hermes/voice-bridge/requirements.txt

# Start the Hermes gateway in the background
/opt/hermes/.venv/bin/hermes gateway run &

# Clean up any orphaned Matrix call state events from previous crashed/restarted sessions
echo "Clearing orphaned call state..."
/opt/hermes/.venv/bin/python /opt/hermes/voice-bridge/clear_state.py

# Start the bridge in the foreground
exec /opt/hermes/.venv/bin/python -u /opt/hermes/voice-bridge/bridge.py
```

---

## Device Generation & Cross-Validation (E2EE)

For the bridge to read WebRTC signaling in E2EE Matrix rooms, it requires a verified crypto store.

### 1. Generate the Device & Token (via MAS)
Access your Matrix Authentication Service (MAS) and issue a compatibility token for the agent's username and a custom device ID:
```bash
docker exec mas /usr/local/bin/mas-cli --config /app/config/config.yaml \
    manage issue-compatibility-token hermes voice-bridge-01
```
Take note of the returned `access_token` (e.g., `mct_...`).

### 2. Verify & Cross-Sign the Device
Run the `setup-device.py` helper in your container to generate the initial E2EE crypto store using the user's recovery key:
```bash
docker exec hermes /opt/hermes/.venv/bin/python /opt/hermes/voice-bridge/setup-device.py \
    --homeserver https://matrix.yourdomain.com \
    --user @hermes:yourdomain.com \
    --device-id voice-bridge-01 \
    --access-token <access-token> \
    --recovery-key '<recovery-key>' \
    --store-path /opt/data/matrix-store-voice-bridge \
    --clean-store
```

Once completed, the new device will be fully trusted and the crypto store will be saved to your persistent `/opt/data` volume, allowing the agent to flawlessly decrypt call signaling.
