#!/bin/bash
VENV=/opt/data/.hermes-venv

# Create venv only if it doesn't exist or is missing key binaries
if [ ! -f "$VENV/bin/python" ] || [ ! -f "$VENV/bin/hermes" ]; then
  rm -rf "$VENV"
  python3 -m venv "$VENV"
  uv pip freeze --python /opt/hermes/.venv > /tmp/reqs.txt
  echo 'livekit>=1.0.0,<2.0.0' >> /tmp/reqs.txt
  echo 'livekit-api>=1.0.0,<2.0.0' >> /tmp/reqs.txt
  echo 'numpy' >> /tmp/reqs.txt
  uv pip install --python "$VENV" -r /tmp/reqs.txt
fi

# Start hermes gateway in background
"$VENV/bin/hermes" gateway run &

sleep 2

# Clear orphaned call state
"$VENV/bin/python" /opt/hermes/voice-bridge/clear_state.py || true

# Start bridge in foreground
exec "$VENV/bin/python" -u /opt/hermes/voice-bridge/bridge.py
