import asyncio
from matrix_call import MatrixCallClient
import os

# Load env variables from /opt/data/.env if it exists
_env_path = "/opt/data/.env"
if os.path.exists(_env_path):
    with open(_env_path, 'r') as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#'):
                _k, _, _v = _line.partition('=')
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER", "")
MATRIX_ACCESS_TOKEN = os.getenv("MATRIX_VOICE_ACCESS_TOKEN", "")
MATRIX_USER_ID = os.getenv("MATRIX_USER_ID", "")
MATRIX_DEVICE_ID = os.getenv("MATRIX_VOICE_DEVICE_ID", "")
MATRIX_RECOVERY_KEY = os.getenv("MATRIX_RECOVERY_KEY", "")
MATRIX_ROOM_ID = os.getenv("VOICE_BRIDGE_MATRIX_ROOM_ID", "")

async def main():
    mx = MatrixCallClient(
        MATRIX_HOMESERVER,
        MATRIX_ACCESS_TOKEN,
        MATRIX_USER_ID,
        MATRIX_DEVICE_ID,
        MATRIX_ROOM_ID,
        MATRIX_RECOVERY_KEY,
        None
    )
    await mx.start()
    await mx.leave_call()
    await mx.stop()
    print("Call state cleared!")

if __name__ == "__main__":
    asyncio.run(main())
