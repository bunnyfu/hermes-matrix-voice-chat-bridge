#!/bin/bash
# =============================================================================
# Hermes Voice Bridge — Container Startup Script
# =============================================================================
#
# CRYPTO STORE NOTES:
#   The bridge uses a SQLite crypto store at /opt/data/matrix-store-voice-bridge/
#   This store holds Olm keys that MUST match what's on the Matrix server.
#
#   When changing the voice device (MATRIX_VOICE_DEVICE_ID in .env):
#     1. Delete the old device from the server (Element X → Settings → Sessions)
#     2. Create & cross-sign the new device using setup-device.py
#     3. Run setup-device.py writing to the PERSISTENT path as the hermes user:
#          docker exec -u hermes <container> python3 /opt/hermes/voice-bridge/setup-device.py \
#            --homeserver http://synapse:8008 \
#            --user '@user:server' \
#            --device-id <new-device-id> \
#            --access-token '<token>' \
#            --recovery-key '<key>' \
#            --store-path /opt/data/matrix-store-voice-bridge \
#            --clean-store
#     4. Update .env: MATRIX_VOICE_ACCESS_TOKEN, MATRIX_VOICE_DEVICE_ID, AGENT_IDENTITY
#     5. Restart the container (do NOT wipe the crypto store again)
#
#   Common pitfalls:
#     - Running setup-device.py as root → crypto.db owned by root → bridge can't write
#     - Wiping the crypto store after setup-device.py → OTK conflicts (server has
#       old keys, new store generates conflicting ones with the same indices)
#     - Stale crypto store from a different user/device → BAD_ACCOUNT_KEY error
#       (pickle key is derived from user_id:device_id)
#
# =============================================================================

# Install required dependencies for the bridge
uv pip install --python /opt/hermes/.venv -r /opt/hermes/voice-bridge/requirements.txt

# Start the Hermes gateway in the background
/opt/hermes/.venv/bin/hermes gateway run &

# Clean up any orphaned Matrix call state events from previous crashed/restarted sessions.
# This prevents LiveKit from showing stale participants from a prior bridge instance.
echo "Clearing orphaned call state..."
/opt/hermes/.venv/bin/python /opt/hermes/voice-bridge/clear_state.py

# Start the bridge in the foreground
exec /opt/hermes/.venv/bin/python -u /opt/hermes/voice-bridge/bridge.py
