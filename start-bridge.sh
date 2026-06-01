#!/bin/bash
# Install required dependencies for the bridge
uv pip install --python /opt/hermes/.venv -r /opt/hermes/voice-bridge/requirements.txt

# Start the Hermes gateway in the background
/opt/hermes/.venv/bin/hermes gateway run &

# Clean up any orphaned Matrix call state events from previous crashed/restarted sessions
echo "Clearing orphaned call state..."
/opt/hermes/.venv/bin/python /opt/hermes/voice-bridge/clear_state.py

# Start the bridge in the foreground
exec /opt/hermes/.venv/bin/python -u /opt/hermes/voice-bridge/bridge.py
