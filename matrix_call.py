"""Matrix call signaling and E2EE key exchange for the voice bridge.

Handles:
  1. Logging in as a second device for @user
  2. Setting up Olm crypto to receive encrypted to-device events
  3. Joining the MatrixRTC call (sending call.member state)
  4. Extracting SFrame encryption keys from to-device events
  5. Providing keys to the LiveKit E2EE KeyProvider
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

from mautrix.api import HTTPAPI, Method, Path as MxPath
from mautrix.client import Client
from mautrix.crypto import OlmMachine
from mautrix.crypto.store.asyncpg import PgCryptoStore
from mautrix.types import RoomID, UserID
from mautrix.util.async_db import Database

logger = logging.getLogger("hermes-voice-bridge.matrix")

# Event type for MatrixRTC encryption keys (to-device)
CALL_ENCRYPTION_KEYS_EVENT = "io.element.call.encryption_keys"
CALL_MEMBER_EVENT = "org.matrix.msc3401.call.member"

# Crypto store path (separate from the gateway's store)
BRIDGE_CRYPTO_DIR = Path("/opt/data/matrix-store-voice-bridge")
BRIDGE_CRYPTO_DB = BRIDGE_CRYPTO_DIR / "crypto.db"


class MatrixCallClient:
    """Lightweight Matrix client for MatrixRTC call participation."""

    def __init__(
        self,
        homeserver: str,
        access_token: str,
        user_id: str,
        device_id: str,
        room_id: str,
        recovery_key: str = "",
        on_encryption_key: Optional[Callable] = None,
    ):
        self.homeserver = homeserver
        self.access_token = access_token
        self.user_id = user_id
        self.device_id = device_id
        self.room_id = room_id
        self.recovery_key = recovery_key
        self.on_encryption_key = on_encryption_key

        # Pre-generate our own SFrame key so it can be queried by the room manager
        import secrets
        self._own_key = secrets.token_bytes(16)
        self._own_key_index = 0

        self._client: Optional[Client] = None
        self._crypto_db: Optional[Database] = None
        self._sync_task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        """Initialize the Matrix client with E2EE and start syncing."""
        BRIDGE_CRYPTO_DIR.mkdir(parents=True, exist_ok=True)

        # Set up mautrix client
        api = HTTPAPI(base_url=self.homeserver, token=self.access_token)

        from mautrix.client.state_store.memory import MemoryStateStore
        state_store = MemoryStateStore()

        self._client = Client(api=api)
        self._client.state_store = state_store
        self._client.mxid = UserID(self.user_id)
        self._client.device_id = self.device_id

        # Set up crypto store
        crypto_db = Database.create(
            f"sqlite:///{BRIDGE_CRYPTO_DB}",
            upgrade_table=PgCryptoStore.upgrade_table,
        )
        await crypto_db.start()
        self._crypto_db = crypto_db

        account_id = self.user_id
        pickle_key = f"{account_id}:{self.device_id}"
        crypto_store = PgCryptoStore(
            account_id=account_id,
            pickle_key=pickle_key,
            db=crypto_db,
        )
        await crypto_store.open()

        # Monkey-patch add_session to use INSERT OR REPLACE instead of INSERT.
        # The stock mautrix PgCryptoStore fails with UNIQUE constraint when
        # both outbound and inbound Olm sessions exist for the same peer.
        _original_add_session = crypto_store.add_session

        async def _patched_add_session(sender_key, session):
            try:
                await _original_add_session(sender_key, session)
            except Exception as e:
                if "UNIQUE constraint" in str(e):
                    logger.warning("Matrix: duplicate Olm session, updating instead")
                    await crypto_db.execute(
                        "INSERT OR REPLACE INTO crypto_olm_session "
                        "(account_id, session_id, sender_key, session, created_at, last_encrypted, last_decrypted) "
                        "VALUES ($1, $2, $3, $4, $5, $6, $7)",
                        crypto_store.account_id, session.id, sender_key,
                        session.pickle(crypto_store.pickle_key),
                        session.creation_time, session.creation_time, session.creation_time,
                    )
                else:
                    raise

        crypto_store.add_session = _patched_add_session

        if self.device_id:
            await crypto_store.put_device_id(self.device_id)

        class CryptoState:
            def __init__(self, ss):
                self._ss = ss
            async def is_encrypted(self, room_id):
                return True
            async def get_encryption_info(self, room_id):
                from mautrix.types import RoomEncryptionStateEventContent, EncryptionAlgorithm
                return RoomEncryptionStateEventContent(algorithm=EncryptionAlgorithm.MEGOLM_V1)
            async def find_shared_rooms(self, user_id):
                return []

        olm = OlmMachine(self._client, crypto_store, CryptoState(state_store))
        olm.allow_unverified_devices = True

        # Try to set trust levels (attribute names vary by mautrix version)
        try:
            from mautrix.crypto import TrustState
            olm.share_keys_min_trust = TrustState.UNVERIFIED
            olm.send_keys_min_trust = TrustState.UNVERIFIED
        except (ImportError, AttributeError):
            pass

        # Load or create the Olm account from the store
        await olm.load()
        logger.info("Matrix: Olm account loaded")

        self._client.crypto = olm
        self._olm = olm  # Store reference for manual decryption in _process_to_device

        # Upload device keys to the server
        try:
            await olm.share_keys()
            logger.info("Matrix: shared device keys")
        except Exception as e:
            logger.warning("Matrix: key share error: %s", e)

        # Start sync loop
        self._running = True
        self._sync_task = asyncio.create_task(self._sync_loop())
        logger.info("Matrix call client started (user=%s, device=%s)", self.user_id, self.device_id)

    async def _import_cross_signing(self, olm):
        """Import cross-signing keys from SSSS using recovery key."""
        from mautrix.crypto.key_sharing import import_cross_signing_from_ssss
        recovery = self.recovery_key.replace(" ", "")
        await import_cross_signing_from_ssss(
            self._client, olm, recovery,
        )
        logger.info("Matrix: imported cross-signing keys")

    async def join_call(self, livekit_url: str = "https://matrix.yourdomain.com/livekit/jwt/"):
        """Send the call.member state event to join the MatrixRTC session."""
        membership_id = f"@{self.user_id.lstrip('@')}:{self.device_id}"
        state_key = f"_{self.user_id}_{self.device_id}_m.call"

        import time
        content = {
            "application": "m.call",
            "call_id": "",
            "scope": "m.room",
            "device_id": self.device_id,
            "membershipID": membership_id,
            "expires": 14400000,
            "expires_ts": int(time.time() * 1000) + 14400000,
            "created_ts": int(time.time() * 1000),
            "m.call.intent": "m.room",
            "focus_active": {
                "type": "livekit",
                "focus_selection": "oldest_membership",
            },
            "foci_preferred": [
                {
                    "livekit_alias": self.room_id,
                    "type": "livekit",
                    "livekit_service_url": livekit_url,
                }
            ],
        }

        await self._client.send_state_event(
            RoomID(self.room_id),
            CALL_MEMBER_EVENT,
            content,
            state_key=state_key,
        )
        logger.info("Matrix: joined call in room %s", self.room_id)

    async def send_own_encryption_key(self):
        """Send our own SFrame encryption key to all call participants.

        Keys MUST be sent as Olm-encrypted to-device events. Element X
        silently discards plaintext io.element.call.encryption_keys events.
        """
        from mautrix.types import EventType, UserID, DeviceID

        key_b64 = base64.b64encode(self._own_key).decode()

        import time as _time
        import uuid as _uuid
        membership_id = f"{self.user_id}:{self.device_id}"

        key_event_content = {
            "keys": {"index": self._own_key_index, "key": key_b64},
            "member": {
                "claimed_device_id": self.device_id,
                "id": str(_uuid.uuid4()),
            },
            "room_id": self.room_id,
            "sent_ts": int(_time.time() * 1000),
            "session": {
                "application": "m.call",
                "call_id": "",
                "scope": "m.room",
            },
        }

        evt_type = EventType.find(CALL_ENCRYPTION_KEYS_EVENT, t_class=EventType.Class.TO_DEVICE)

        # Get room members to send keys to
        try:
            members = await self._client.get_joined_members(RoomID(self.room_id))
            for user_id in members:
                uid = str(user_id)
                if uid == self.user_id:
                    continue  # Don't send to ourselves

                logger.info("Matrix: sending Olm-encrypted encryption key to %s", uid)
                try:
                    # Fetch all devices for this user so we can Olm-encrypt per-device
                    user_devices = await self._olm._fetch_keys([UserID(uid)], include_untracked=True)
                    device_map = user_devices.get(UserID(uid), {})
                    if not device_map:
                        logger.warning("Matrix: no devices found for %s, falling back to plaintext", uid)
                        await self._client.send_to_device(
                            evt_type,
                            {UserID(uid): {"*": key_event_content}},
                        )
                        continue

                    for dev_id, device in device_map.items():
                        try:
                            await self._olm.send_encrypted_to_device(
                                device, evt_type, key_event_content,
                            )
                            logger.info("Matrix: sent Olm-encrypted key to %s/%s", uid, dev_id)
                        except Exception as e:
                            logger.warning("Matrix: failed to Olm-encrypt key to %s/%s: %s", uid, dev_id, e)
                except Exception as e:
                    logger.warning("Matrix: failed to send key to %s: %s", uid, e)

        except Exception as e:
            logger.warning("Matrix: failed to send own encryption key: %s", e)
            return

        logger.info("Matrix: sent own encryption key (index=%d)", self._own_key_index)
        if self.on_encryption_key:
            # Also register our own key so we can encrypt our outgoing audio
            self.on_encryption_key(self.user_id, self._own_key, self._own_key_index)

    async def leave_call(self):
        """Clear the call.member state event to leave the call."""
        state_key = f"_{self.user_id}_{self.device_id}_m.call"
        try:
            await self._client.send_state_event(
                RoomID(self.room_id),
                CALL_MEMBER_EVENT,
                {},  # Empty content = left
                state_key=state_key,
            )
            logger.info("Matrix: left call")
        except Exception as e:
            logger.warning("Matrix: error leaving call: %s", e)

    async def _sync_loop(self):
        """Sync loop to receive to-device events (encryption keys)."""
        next_batch = None

        # Initial sync
        try:
            sync_data = await self._client.sync(timeout=10000, full_state=True)
            if isinstance(sync_data, dict):
                next_batch = sync_data.get("next_batch")
                # Decrypt and extract call keys BEFORE handle_sync
                await self._process_to_device(sync_data)
                # Let OlmMachine handle remaining crypto events
                try:
                    tasks = self._client.handle_sync(sync_data)
                    if tasks:
                        await asyncio.gather(*tasks)
                except Exception as e:
                    logger.debug("Matrix: initial sync dispatch: %s", e)

                if self._client.crypto:
                    await self._client.crypto.share_keys()
        except Exception as e:
            logger.warning("Matrix: initial sync error: %s", e)

        # Incremental sync
        while self._running:
            try:
                sync_data = await self._client.sync(
                    timeout=30000,
                    since=next_batch,
                    full_state=False,
                )
                if isinstance(sync_data, dict):
                    nb = sync_data.get("next_batch")
                    if nb:
                        next_batch = nb
                    await self._process_to_device(sync_data)
                    try:
                        tasks = self._client.handle_sync(sync_data)
                        if tasks:
                            await asyncio.gather(*tasks)
                    except Exception as e:
                        logger.debug("Matrix: sync dispatch: %s", e)
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning("Matrix: sync error: %s, retrying...", e)
                await asyncio.sleep(5)

    async def _process_to_device(self, sync_data: dict):
        """Decrypt and extract call encryption keys from to-device events.

        For encrypted events, manually decrypt using the OlmMachine and check
        if the decrypted content is a call encryption key. If so, extract it
        and REMOVE the event from sync_data so handle_sync doesn't try to
        decrypt it again (which would fail/double-consume the Olm session).
        """
        to_device = sync_data.get("to_device", {})
        events = to_device.get("events", [])
        keep_events = []

        for event in events:
            ev_type = event.get("type", "")
            content = event.get("content", {})
            sender = event.get("sender", "")

            if ev_type == "m.room.encrypted" and self._olm:
                logger.info("Matrix: encrypted to-device from %s, decrypting...", sender)
                try:
                    # Build a ToDeviceEvent for the OlmMachine
                    from mautrix.types import ToDeviceEvent
                    td_evt = ToDeviceEvent.deserialize(event)

                    # Decrypt using the OlmMachine
                    decrypted = await self._olm._decrypt_olm_event(td_evt)
                    decrypted_type = str(getattr(decrypted, 'type', ''))
                    logger.info("Matrix: decrypted to-device: type=%s", decrypted_type)

                    if decrypted_type == CALL_ENCRYPTION_KEYS_EVENT:
                        # Extract the call encryption key
                        dec_content = decrypted.content
                        if hasattr(dec_content, 'serialize'):
                            dec_content = dec_content.serialize()
                        elif not isinstance(dec_content, dict):
                            dec_content = {}
                        logger.info("Matrix: full encryption keys content from %s: %s", sender, dec_content)
                        self._handle_encryption_keys(dec_content, sender)
                        continue
                    elif decrypted_type == "m.room_key":
                        logger.info("Matrix: dispatching room key directly to OlmMachine")
                        await self._olm._receive_room_key(decrypted)
                        continue
                    elif decrypted_type == "m.forwarded_room_key":
                        logger.info("Matrix: dispatching forwarded room key directly to OlmMachine")
                        await self._olm._receive_forwarded_room_key(decrypted)
                        continue
                    else:
                        logger.info("Matrix: dropping decrypted to-device event of type %s", decrypted_type)
                        continue
                except Exception as e:
                    logger.warning("Matrix: to-device decrypt error: %s", e)
                    keep_events.append(event)  # Keep it for handle_sync to try
            elif ev_type == CALL_ENCRYPTION_KEYS_EVENT:
                # Unencrypted call key (shouldn't happen with E2EE but handle it)
                logger.info("Matrix: to-device event: type=%s sender=%s", ev_type, sender)
                self._handle_encryption_keys(content, sender)
                continue
            else:
                keep_events.append(event)

        # Replace events list in sync_data so handle_sync doesn't re-process consumed events
        if "to_device" in sync_data:
            sync_data["to_device"]["events"] = keep_events

    def _handle_encryption_keys(self, content: dict, sender: str):
        """Process an encryption keys to-device event."""
        keys = content.get("keys", [])
        if isinstance(keys, dict):
            keys = [keys]

        for key_entry in keys:
            key_b64 = key_entry.get("key", "")
            key_index = key_entry.get("index", 0)

            if key_b64:
                key_bytes = base64.b64decode(key_b64)
                logger.info(
                    "Matrix: received encryption key from %s (index=%d, len=%d bytes)",
                    sender, key_index, len(key_bytes),
                )
                if self.on_encryption_key:
                    self.on_encryption_key(sender, key_bytes, key_index)

    async def stop(self):
        """Stop the sync loop and clean up."""
        self._running = False
        if self._sync_task:
            self._sync_task.cancel()
            try:
                await self._sync_task
            except asyncio.CancelledError:
                pass

        await self.leave_call()

        if self._crypto_db:
            try:
                await self._crypto_db.stop()
            except Exception:
                pass

        if self._client:
            try:
                await self._client.api.session.close()
            except Exception:
                pass

        logger.info("Matrix call client stopped")
