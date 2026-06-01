#!/usr/bin/env python3
"""Create a new Matrix device and cross-sign it with a recovery key.

Three-step flow:
  1. Login via MAS compat endpoint → device_id + access_token
  2. Initialize OlmMachine, upload device keys (identity + OTKs)
  3. Fetch cross-signing keys from SSSS via recovery key, self-sign device

Example (with password):
  python setup-device.py \
      --homeserver https://matrix.yourdomain.com \
      --user @hermes:yourdomain.com \
      --device-id voice-bridge-01 \
      --password my-secret-password \
      --recovery-key 'XXXX XXXX XXXX XXXX XXXX XXXX XXXX XXXX XXXX XXXX XXXX XXXX' \
      --store-path /opt/data/matrix-store-voice-bridge

  Or set environment variables (see --help).

Requires: pip install "mautrix[encryption]"
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("setup-device")


# ---------------------------------------------------------------------------
# Step 1 — Login via MAS compat → device_id + access_token
# ---------------------------------------------------------------------------
async def step1_login(
    homeserver: str,
    user_id: str,
    password: str,
    device_id: str,
) -> dict:
    """Login via /_matrix/client/v3/login and return the response dict."""
    import aiohttp

    # Extract the localpart from a full MXID: @user:server → user
    localpart = user_id.split(":")[0].lstrip("@")

    payload = {
        "type": "m.login.password",
        "identifier": {"type": "m.id.user", "user": localpart},
        "password": password,
        "device_id": device_id,
        "initial_device_display_name": f"Voice Bridge ({device_id})",
    }

    login_url = f"{homeserver.rstrip('/')}/_matrix/client/v3/login"

    async with aiohttp.ClientSession() as session:
        async with session.post(login_url, json=payload) as resp:
            body = await resp.json()
            if resp.status != 200:
                raise RuntimeError(
                    f"Login failed ({resp.status}): "
                    f"{body.get('errcode', '?')} — {body.get('error', body)}"
                )

    log.info("✅ Step 1 — Logged in as %s (device: %s)", body["user_id"], body["device_id"])
    log.info("   Access token: %s", body["access_token"])
    return body


# ---------------------------------------------------------------------------
# Step 2 — Initialize crypto store, upload device keys
# ---------------------------------------------------------------------------
async def step2_share_keys(
    homeserver: str,
    user_id: str,
    device_id: str,
    access_token: str,
    store_path: Path,
) -> "OlmMachine":
    """Create an OlmMachine, upload identity + one-time keys, return it."""
    from mautrix.client import Client
    from mautrix.api import HTTPAPI
    from mautrix.crypto import OlmMachine
    from mautrix.crypto.store.asyncpg import PgCryptoStore
    from mautrix.types import UserID, DeviceID, SyncToken, TrustState
    from mautrix.util.async_db import Database

    store_path.mkdir(parents=True, exist_ok=True)
    db_path = store_path / "crypto.db"

    # Open SQLite-backed crypto store
    crypto_db = Database.create(
        f"sqlite:///{db_path}",
        upgrade_table=PgCryptoStore.upgrade_table,
    )
    await crypto_db.start()

    account_id = user_id
    pickle_key = f"{user_id}:{device_id}"
    crypto_store = PgCryptoStore(
        account_id=account_id,
        pickle_key=pickle_key,
        db=crypto_db,
    )
    await crypto_store.open()

    # Bind device_id before any put_account
    await crypto_store.put_device_id(DeviceID(device_id))

    # Create Matrix client handle
    http_api = HTTPAPI(base_url=homeserver, token=access_token)
    api = Client(api=http_api)
    api.mxid = UserID(user_id)
    api.device_id = DeviceID(device_id)

    # Minimal state store stub — cross-signing doesn't need room state
    class _MinimalStateStore:
        async def is_encrypted(self, room_id):
            return True
        async def find_shared_rooms(self, user_id):
            return []

    olm = OlmMachine(api, crypto_store, _MinimalStateStore())
    olm.share_keys_min_trust = TrustState.UNVERIFIED
    olm.send_keys_min_trust = TrustState.UNVERIFIED

    await olm.load()

    # Upload identity keys + one-time keys
    await olm.share_keys()

    log.info("✅ Step 2 — Device keys uploaded for %s", device_id)

    # Stash refs for cleanup
    olm._setup_crypto_db = crypto_db
    olm._setup_api = api

    return olm


# ---------------------------------------------------------------------------
# Step 3 — Cross-sign with recovery key
# ---------------------------------------------------------------------------
async def step3_cross_sign(olm: "OlmMachine", recovery_key: str) -> None:
    """Fetch cross-signing keys from SSSS and self-sign this device."""
    await olm.verify_with_recovery_key(recovery_key)
    log.info("✅ Step 3 — Device cross-signed via recovery key")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------
async def cleanup(olm: "OlmMachine") -> None:
    """Close the database and HTTP session."""
    try:
        await olm._setup_crypto_db.stop()
    except Exception:
        pass
    try:
        await olm._setup_api.session.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Verify (optional post-check)
# ---------------------------------------------------------------------------
async def verify_signatures(
    homeserver: str, access_token: str, user_id: str, device_id: str
) -> bool:
    """Check the homeserver for cross-signing signatures on this device."""
    import aiohttp

    url = f"{homeserver.rstrip('/')}/_matrix/client/v3/keys/query"
    payload = {"device_keys": {user_id: [device_id]}}

    async with aiohttp.ClientSession() as session:
        async with session.post(
            url,
            json=payload,
            headers={"Authorization": f"Bearer {access_token}"},
        ) as resp:
            body = await resp.json()

    device_keys = body.get("device_keys", {}).get(user_id, {}).get(device_id, {})
    ssk = body.get("self_signing_keys", {}).get(user_id, {})
    ssk_key_id = None
    if ssk:
        ssk_keys = ssk.get("keys", {})
        if ssk_keys:
            ssk_key_id = list(ssk_keys.keys())[0]  # ed25519:<key>

    signatures = device_keys.get("signatures", {}).get(user_id, {})

    if ssk_key_id and ssk_key_id in signatures:
        log.info("🔒 Verified: device %s is signed by self-signing key %s", device_id, ssk_key_id)
        return True
    else:
        log.warning("⚠️  Device %s is NOT signed by the self-signing key", device_id)
        log.warning("   Signatures present: %s", list(signatures.keys()))
        if ssk_key_id:
            log.warning("   Expected SSK: %s", ssk_key_id)
        return False


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Create a Matrix device and cross-sign it with a recovery key.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Create a new voice-bridge device:
  python3 setup-device.py \\
      --homeserver https://matrix.yourdomain.com \\
      --user @hermes:yourdomain.com \\
      --device-id voice-bridge-01 \\
      --password my-secret-password \\
      --recovery-key 'XXXX XXXX XXXX XXXX XXXX XXXX XXXX XXXX XXXX XXXX XXXX XXXX' \\
      --store-path ./crypto-store-vb08

  # Re-sign an existing device (skip login, provide token):
  python3 setup-device.py \\
      --homeserver https://matrix.yourdomain.com \\
      --user @hermes:yourdomain.com \\
      --device-id voice-bridge-01 \\
      --access-token mct_xxxxxxxxxxxxxxxxxxxxxxxxxxx \\
      --recovery-key 'XXXX XXXX XXXX XXXX XXXX XXXX XXXX XXXX XXXX XXXX XXXX XXXX' \\
      --store-path /opt/data/matrix-store-voice-bridge \\
      --clean-store

Environment variables (override defaults):
  MATRIX_HOMESERVER, MATRIX_USER_ID, MATRIX_PASSWORD,
  MATRIX_DEVICE_ID, MATRIX_ACCESS_TOKEN, MATRIX_RECOVERY_KEY
        """,
    )
    parser.add_argument("--homeserver", default=os.getenv("MATRIX_HOMESERVER"),
                        help="Matrix homeserver URL")
    parser.add_argument("--user", default=os.getenv("MATRIX_USER_ID"),
                        help="Full Matrix user ID (@user:server)")
    parser.add_argument("--password", default=os.getenv("MATRIX_PASSWORD"),
                        help="User password (for Step 1 login; skip if --access-token given)")
    parser.add_argument("--device-id", default=os.getenv("MATRIX_DEVICE_ID"),
                        help="Device ID to create or re-sign")
    parser.add_argument("--access-token", default=os.getenv("MATRIX_ACCESS_TOKEN"),
                        help="Existing access token (skip Step 1 login)")
    parser.add_argument("--recovery-key", default=os.getenv("MATRIX_RECOVERY_KEY"),
                        help="Recovery key for cross-signing (space-separated groups)")
    parser.add_argument("--store-path", default=os.getenv("MATRIX_STORE_PATH", "./crypto-store"),
                        help="Path for the SQLite crypto store (default: ./crypto-store)")
    parser.add_argument("--clean-store", action="store_true",
                        help="Delete existing crypto store before starting (fresh keys)")
    parser.add_argument("--verify-only", action="store_true",
                        help="Only check if the device is cross-signed, don't modify anything")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Enable debug logging")

    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    # Validate required args
    if not args.homeserver:
        parser.error("--homeserver is required (or set MATRIX_HOMESERVER)")
    if not args.user:
        parser.error("--user is required (or set MATRIX_USER_ID)")
    if not args.device_id:
        parser.error("--device-id is required (or set MATRIX_DEVICE_ID)")

    # Verify-only mode
    if args.verify_only:
        if not args.access_token:
            parser.error("--access-token is required for --verify-only mode")
        ok = await verify_signatures(args.homeserver, args.access_token, args.user, args.device_id)
        return 0 if ok else 1

    if not args.recovery_key:
        parser.error("--recovery-key is required (or set MATRIX_RECOVERY_KEY)")

    # Strip quotes from recovery key (env vars often have them)
    recovery_key = args.recovery_key.strip().strip('"').strip("'")

    store_path = Path(args.store_path)
    access_token = args.access_token

    # ── Step 1: Login (if no token provided) ──────────────────────────
    if not access_token:
        if not args.password:
            parser.error("--password is required for login (or provide --access-token)")
        login_resp = await step1_login(
            args.homeserver, args.user, args.password, args.device_id,
        )
        access_token = login_resp["access_token"]
        log.info("")
    else:
        log.info("⏭️  Step 1 — Skipped login (using provided access token)")
        log.info("")

    # ── Step 2: Upload device keys ────────────────────────────────────
    if args.clean_store and store_path.exists():
        log.info("🧹 Removing existing crypto store at %s", store_path)
        shutil.rmtree(store_path)

    olm = await step2_share_keys(
        args.homeserver, args.user, args.device_id, access_token, store_path,
    )
    log.info("")

    # ── Step 3: Cross-sign ────────────────────────────────────────────
    try:
        await step3_cross_sign(olm, recovery_key)
    finally:
        await cleanup(olm)
    log.info("")

    # ── Post-check ────────────────────────────────────────────────────
    ok = await verify_signatures(args.homeserver, access_token, args.user, args.device_id)

    if ok:
        log.info("")
        log.info("━" * 60)
        log.info("Done! Device %s is verified.", args.device_id)
        log.info("Access token: %s", access_token)
        log.info("Store path:   %s", store_path)
        log.info("━" * 60)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
