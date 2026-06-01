#!/usr/bin/env python3
"""Hermes Voice Bridge — Phase 0 Spike

Runs INSIDE the hermes-agent container. Bridges LiveKit voice calls
to the Hermes agent by using Hermes's native STT/TTS modules directly.

Architecture:
  1. Connects to the local LiveKit server as a room participant.
  2. Subscribes to user audio tracks, runs VAD to detect speech boundaries.
  3. On end-of-utterance: saves audio → calls Hermes STT (transcribe_audio).
  4. Sends transcript to Hermes API server (localhost) for full agent reasoning.
  5. Takes text response → calls Hermes TTS (text_to_speech_tool) → streams
     audio back into the LiveKit room.

All STT/TTS uses Hermes's own configured providers — zero
external API keys needed in this script.

Usage (inside the container):
    python bridge.py                   # Auto-discovers active rooms
    python bridge.py --room ROOM_NAME  # Joins a specific LiveKit room
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
import wave
from pathlib import Path
from typing import Optional

# Load env variables from /opt/data/.env if it exists
_env_path = "/opt/data/.env"
if os.path.exists(_env_path):
    with open(_env_path, 'r') as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith('#'):
                _k, _, _v = _line.partition('=')
                os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

import numpy as np
from livekit import api, rtc

# ---------------------------------------------------------------------------
# Hermes-native imports (available inside the container)
# ---------------------------------------------------------------------------
from tools.transcription_tools import transcribe_audio
from tools.tts_tool import text_to_speech_tool, _strip_markdown_for_tts

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# LiveKit server is on Docker host, reachable via bridge gateway
LIVEKIT_URL = os.getenv("LIVEKIT_URL", "ws://localhost:7880")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "")

# Hermes API server runs inside this same container
HERMES_API_URL = os.getenv("HERMES_API_URL", "http://127.0.0.1:8642/v1")
HERMES_API_KEY = os.getenv("HERMES_API_KEY", os.getenv("API_SERVER_KEY", ""))

AGENT_NAME = os.getenv("AGENT_NAME", "Hermes")

# LiveKit identity MUST match the MatrixRTC membershipID format (@user:server:deviceId)
# so Element X can map this participant to a call member for E2EE key lookup.
_mx_user = os.getenv("MATRIX_USER_ID", "")
_mx_device = os.getenv("MATRIX_VOICE_DEVICE_ID", os.getenv("MATRIX_DEVICE_ID", ""))
AGENT_IDENTITY = os.getenv("AGENT_IDENTITY", f"{_mx_user}:{_mx_device}")

# Matrix credentials (dedicated device for the voice bridge — set in .env)
MATRIX_HOMESERVER = os.getenv("MATRIX_HOMESERVER", "")
MATRIX_ACCESS_TOKEN = os.getenv("MATRIX_VOICE_ACCESS_TOKEN", "")
MATRIX_USER_ID = os.getenv("MATRIX_USER_ID", "")
MATRIX_DEVICE_ID = os.getenv("MATRIX_VOICE_DEVICE_ID", "")
MATRIX_RECOVERY_KEY = os.getenv("MATRIX_RECOVERY_KEY", "")

# Matrix room for voice calls
MATRIX_ROOM_ID = os.getenv("VOICE_BRIDGE_MATRIX_ROOM_ID", "")

# The LiveKit room name is dynamic — discovered via LiveKit API when a call is active.
LIVEKIT_ROOM = os.getenv("LIVEKIT_ROOM", "")

# VAD parameters
VAD_ENERGY_THRESHOLD = int(os.getenv("VAD_ENERGY_THRESHOLD", "200"))
VAD_SILENCE_DURATION = float(os.getenv("VAD_SILENCE_DURATION", "1.4"))
VAD_MIN_SPEECH_DURATION = float(os.getenv("VAD_MIN_SPEECH_DURATION", "0.3"))

# Audio constants
LIVEKIT_SAMPLE_RATE = 48000
STT_SAMPLE_RATE = 16000
NUM_CHANNELS = 1

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
)
logger = logging.getLogger("hermes-voice-bridge")


# ---------------------------------------------------------------------------
# Token generation
# ---------------------------------------------------------------------------

def generate_token(room_name: str, identity: str) -> str:
    """Generate a LiveKit access token for the agent to join a room."""
    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(AGENT_NAME)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
            )
        )
    )
    return token.to_jwt()


# ---------------------------------------------------------------------------
# Simple energy-based VAD
# ---------------------------------------------------------------------------

class SimpleVAD:
    """Minimal energy-based Voice Activity Detector.

    Good enough for Phase 0. Replace with silero-vad for production.
    """

    def __init__(
        self,
        energy_threshold: int = VAD_ENERGY_THRESHOLD,
        silence_duration: float = VAD_SILENCE_DURATION,
        min_speech_duration: float = VAD_MIN_SPEECH_DURATION,
    ):
        self.energy_threshold = energy_threshold
        self.silence_duration = silence_duration
        self.min_speech_duration = min_speech_duration

        self._is_speaking = False
        self._speech_start: Optional[float] = None
        self._last_speech_time: float = 0.0

    def process_frame(self, samples: np.ndarray) -> str:
        """Returns 'speech', 'silence', or 'end_of_speech'."""
        energy = np.sqrt(np.mean(samples.astype(np.float64) ** 2))
        now = time.monotonic()

        if energy > self.energy_threshold:
            if not self._is_speaking:
                self._is_speaking = True
                self._speech_start = now
                logger.debug("VAD: speech started")
            self._last_speech_time = now
            return "speech"

        if self._is_speaking:
            silence_elapsed = now - self._last_speech_time
            if silence_elapsed >= self.silence_duration:
                speech_duration = self._last_speech_time - (self._speech_start or now)
                self._is_speaking = False
                self._speech_start = None
                if speech_duration >= self.min_speech_duration:
                    logger.debug("VAD: end of speech (%.1fs)", speech_duration)
                    return "end_of_speech"
                else:
                    logger.debug("VAD: too short (%.2fs), ignoring", speech_duration)

        return "silence"

    @property
    def is_speaking(self) -> bool:
        return self._is_speaking


# ---------------------------------------------------------------------------
# Audio utilities
# ---------------------------------------------------------------------------

def resample(samples: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """Simple linear resample."""
    if from_rate == to_rate:
        return samples
    ratio = from_rate / to_rate
    n_out = int(len(samples) / ratio)
    indices = np.round(np.linspace(0, len(samples) - 1, n_out)).astype(int)
    return samples[indices]


def save_wav(samples: np.ndarray, path: str, sample_rate: int = STT_SAMPLE_RATE):
    """Save int16 samples to a WAV file."""
    with wave.open(path, "wb") as wf:
        wf.setnchannels(NUM_CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples.astype(np.int16).tobytes())


def load_wav_as_frames(
    path: str,
    target_rate: int = LIVEKIT_SAMPLE_RATE,
    frame_duration_ms: int = 20,
) -> list[rtc.AudioFrame]:
    """Load a WAV/audio file and return LiveKit AudioFrame chunks."""
    with wave.open(path, "rb") as wf:
        rate = wf.getframerate()
        channels = wf.getnchannels()
        width = wf.getsampwidth()
        raw = wf.readframes(wf.getnframes())

    if width == 2:
        samples = np.frombuffer(raw, dtype=np.int16)
    elif width == 4:
        samples = (np.frombuffer(raw, dtype=np.int32) >> 16).astype(np.int16)
    else:
        raise ValueError(f"Unsupported sample width: {width}")

    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1).astype(np.int16)

    samples = resample(samples, rate, target_rate)

    frame_size = int(target_rate * frame_duration_ms / 1000)
    frames = []
    for i in range(0, len(samples), frame_size):
        chunk = samples[i : i + frame_size]
        if len(chunk) < frame_size:
            chunk = np.pad(chunk, (0, frame_size - len(chunk)))
        frames.append(
            rtc.AudioFrame(
                data=chunk.tobytes(),
                sample_rate=target_rate,
                num_channels=NUM_CHANNELS,
                samples_per_channel=frame_size,
            )
        )
    return frames


# ---------------------------------------------------------------------------
# Hermes-native STT
# ---------------------------------------------------------------------------

async def hermes_stt(audio_path: str) -> str:
    """Transcribe audio using Hermes's configured STT provider."""
    result = await asyncio.to_thread(transcribe_audio, audio_path)

    if isinstance(result, str):
        try:
            result = json.loads(result)
        except (json.JSONDecodeError, TypeError):
            return result.strip()

    if isinstance(result, dict):
        if result.get("success"):
            text = result.get("transcript", result.get("text", "")).strip()
            logger.info('STT: "%s"', text)
            return text
        else:
            logger.warning("STT failed: %s", result.get("error", "unknown"))
            return ""

    return str(result).strip()


# ---------------------------------------------------------------------------
# Hermes agent (via local API server)
# ---------------------------------------------------------------------------

async def query_hermes(user_text: str, user_id: str) -> str:
    """Send user text to the Hermes API server for full agent reasoning."""
    import httpx

    url = f"{HERMES_API_URL}/chat/completions"
    payload = {
        "model": "default",
        "messages": [{"role": "user", "content": user_text}],
        "user": user_id,
    }

    async with httpx.AsyncClient(timeout=300.0) as client:
        response = await client.post(
            url,
            headers={
                "Authorization": f"Bearer {HERMES_API_KEY}",
                "Content-Type": "application/json",
                "X-Hermes-Session-Id": user_id,
            },
            json=payload,
        )

    if response.status_code != 200:
        logger.error("Hermes API error %d: %s", response.status_code, response.text[:200])
        return "I had trouble processing that."

    data = response.json()
    text = data["choices"][0]["message"]["content"]
    logger.info('Hermes: "%s"', text[:120] + ("..." if len(text) > 120 else ""))
    return text


# ---------------------------------------------------------------------------
# Hermes-native TTS
# ---------------------------------------------------------------------------

async def hermes_tts(text: str) -> Optional[str]:
    """Synthesize speech using Hermes's configured TTS provider.

    Returns the path to the generated audio file, or None on failure.
    """
    clean_text = _strip_markdown_for_tts(text[:4000])
    if not clean_text:
        return None

    import uuid
    audio_dir = os.path.join(tempfile.gettempdir(), "hermes_voice_bridge")
    os.makedirs(audio_dir, exist_ok=True)
    output_path = os.path.join(audio_dir, f"tts_{uuid.uuid4().hex[:12]}.mp3")

    result_json = await asyncio.to_thread(
        text_to_speech_tool, text=clean_text, output_path=output_path
    )

    try:
        result = json.loads(result_json)
    except (json.JSONDecodeError, TypeError):
        logger.warning("TTS returned invalid JSON: %s", str(result_json)[:200])
        return None

    actual_path = result.get("file_path", output_path)
    if not result.get("success") or not os.path.isfile(actual_path):
        logger.warning("TTS failed: %s", result.get("error"))
        return None

    logger.info("TTS: generated %s", actual_path)
    return actual_path


# ---------------------------------------------------------------------------
# Main bridge
# ---------------------------------------------------------------------------

class HermesVoiceBridge:
    """Bridges a LiveKit voice call to the Hermes agent."""

    def __init__(self, room_name: str, e2ee_key: Optional[bytes] = None):
        self.room_name = room_name
        self.e2ee_key = e2ee_key
        self.room: Optional[rtc.Room] = None
        self.audio_source = rtc.AudioSource(
            sample_rate=LIVEKIT_SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
        )
        self._is_playing = False
        self._processing = False
        self._thinking = False
        self._ambient_task: Optional[asyncio.Task] = None
        
        import uuid
        self.session_id = str(uuid.uuid4())

    def _make_room(self) -> rtc.Room:
        """Create a Room object, optionally with E2EE."""
        # Always enable E2EEOptions for MatrixRTC voice calls
        initial_key = self.e2ee_key or b"default_temp_key_32_bytes_long!!"
        e2ee_opts = rtc.E2EEOptions(
            key_provider_options=rtc.KeyProviderOptions(
                shared_key=initial_key,
                key_derivation_function=rtc.KeyDerivationFunction.HKDF,
            ),
        )
        room = rtc.Room()
        room.on("track_subscribed")(self._on_track_subscribed)
        room.on("participant_connected")(self._on_participant_connected)
        room.on("participant_disconnected")(self._on_participant_disconnected)
        self._e2ee_opts = e2ee_opts
        return room

    def _apply_pending_keys(self):
        """Apply all stored pending keys to active participants in the room."""
        if not hasattr(self, '_pending_keys') or not self._pending_keys:
            return

        try:
            if self.room and hasattr(self.room, 'e2ee_manager') and self.room.e2ee_manager:
                mgr = self.room.e2ee_manager
                if hasattr(mgr, 'key_provider') and mgr.key_provider:
                    # Check currently connected remote participants
                    for p_id in list(self.room.remote_participants.keys()):
                        for sender, (key, key_index) in self._pending_keys.items():
                            if p_id.startswith(sender):
                                mgr.key_provider.set_key(p_id, key, key_index)
                                logger.info("Applied pending key for active participant %s (sender %s, index=%d)", p_id, sender, key_index)
        except Exception as e:
            logger.error("Error applying pending keys: %s", e)

    def set_participant_key(self, participant_identity: str, key: bytes, key_index: int):
        """Install an encryption key for a specific participant."""
        if not hasattr(self, '_pending_keys'):
            self._pending_keys = {}
        self._pending_keys[participant_identity] = (key, key_index)

        # Update self.e2ee_key if it's the first key
        if not self.e2ee_key:
            self.e2ee_key = key

        # If connected, apply immediately
        self._apply_pending_keys()

    async def connect(self):
        """Connect to the LiveKit room."""
        logger.info("Connecting to room '%s' at %s with E2EE active", self.room_name, LIVEKIT_URL)

        # Wait for the room to exist (created when a user starts a call)
        while True:
            self.room = self._make_room()
            token = generate_token(self.room_name, AGENT_IDENTITY)
            try:
                opts = rtc.RoomOptions(encryption=self._e2ee_opts) if self._e2ee_opts else rtc.RoomOptions()
                await self.room.connect(LIVEKIT_URL, token, options=opts)
                break
            except Exception as e:
                if "does not exist" in str(e):
                    logger.info("Room not active yet. Waiting for a call to start...")
                    await asyncio.sleep(3)
                else:
                    raise

        # Apply any pending keys that were received before connect
        self._apply_pending_keys()

        track = rtc.LocalAudioTrack.create_audio_track("hermes_voice", self.audio_source)
        options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        await self.room.local_participant.publish_track(track, options)

        logger.info("Connected! Agent live in room '%s'.", self.room_name)

        # Play initial greeting in the background
        asyncio.create_task(self._play_greeting())

    async def _play_greeting(self):
        """Play an initial greeting when the agent connects."""
        try:
            # Let the connection stabilize for a second
            await asyncio.sleep(1.5)
            logger.info("Playing initial greeting...")
            greeting_text = f"Приветики! Как делишки?"
            tts_path = await hermes_tts(greeting_text)
            if tts_path:
                try:
                    await self._play_audio(tts_path)
                finally:
                    try:
                        os.unlink(tts_path)
                    except OSError:
                        pass
        except Exception as e:
            logger.error("Error playing greeting: %s", e)

    def _on_participant_connected(self, participant: rtc.RemoteParticipant):
        logger.info("Participant joined: %s", participant.identity)
        self._apply_pending_keys()

    def _on_participant_disconnected(self, participant: rtc.RemoteParticipant):
        logger.info("Participant left: %s", participant.identity)

    def _on_track_subscribed(
        self,
        track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ):
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            logger.info("Subscribed to audio from %s", participant.identity)
            asyncio.create_task(self._audio_loop(track, participant))

    async def _audio_loop(self, track: rtc.Track, participant: rtc.RemoteParticipant):
        """Main audio processing loop for a participant."""
        logger.info("Starting audio loop for track %s from participant %s", track.sid, participant.identity)
        audio_stream = rtc.AudioStream(track)
        vad = SimpleVAD()
        audio_buffer = []

        async for frame_event in audio_stream:
            if self._thinking:
                continue

            frame = frame_event.frame
            samples = np.frombuffer(frame.data, dtype=np.int16)
            state = vad.process_frame(samples)

            if state == "speech":
                if self._is_playing:
                    await self._interrupt_playback()
                audio_buffer.append(samples.copy())

            elif state == "end_of_speech" and audio_buffer:
                all_audio = np.concatenate(audio_buffer)
                audio_buffer.clear()
                if not self._processing:
                    asyncio.create_task(self._process_utterance(all_audio))

    async def _process_utterance(self, audio: np.ndarray):
        """STT → Hermes agent → TTS → play back."""
        self._processing = True
        self._thinking = True
        stt_path = None
        tts_path = None
        try:
            # Play a feedback blip to indicate VAD finalized
            try:
                await self._play_audio("/opt/hermes/voice-bridge/blip.wav")
            except Exception as e:
                logger.warning(f"Failed to play blip: {e}")
            
            self._start_ambient()

            # 1. Resample to 16kHz and save for STT
            audio_16k = resample(audio, LIVEKIT_SAMPLE_RATE, STT_SAMPLE_RATE)
            stt_path = os.path.join(
                tempfile.gettempdir(), f"bridge_stt_{int(time.time())}.wav"
            )
            save_wav(audio_16k, stt_path)

            # 2. Transcribe using Hermes-native STT
            text = await hermes_stt(stt_path)
            if not text or len(text.strip()) < 2:
                logger.debug("Empty transcription, skipping")
                return

            # 3. Query Hermes agent for response
            response = await query_hermes(text, self.session_id)
            if not response:
                return

            # 4. Synthesize using Hermes-native TTS
            tts_path = await hermes_tts(response)
            if not tts_path:
                return

            # 5. Play audio back into LiveKit room
            self._stop_ambient()
            self._thinking = False
            await self._play_audio(tts_path)

        except Exception:
            logger.exception("Error processing utterance")
        finally:
            self._stop_ambient()
            self._thinking = False
            self._processing = False
            for p in (stt_path, tts_path):
                if p:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass

    async def _play_audio(self, audio_path: str):
        """Stream an audio file into the LiveKit room."""
        self._is_playing = True
        try:
            # TTS may produce mp3/ogg — convert to wav for frame loading
            wav_path = audio_path
            if not audio_path.endswith(".wav"):
                wav_path = audio_path + ".wav"
                import subprocess
                result = subprocess.run(
                    ["ffmpeg", "-y", "-i", audio_path, "-ar", str(LIVEKIT_SAMPLE_RATE),
                     "-ac", "1", "-f", "wav", wav_path],
                    capture_output=True, timeout=15,
                )
                if result.returncode != 0:
                    logger.error("ffmpeg conversion failed: %s", result.stderr[:200])
                    return

            frames = load_wav_as_frames(wav_path, target_rate=LIVEKIT_SAMPLE_RATE)
            logger.info("Playing %d frames (%.1fs)", len(frames), len(frames) * 0.02)

            for frame in frames:
                if not self._is_playing:
                    logger.info("Playback interrupted by barge-in")
                    break
                await self.audio_source.capture_frame(frame)

            logger.info("Playback complete")
        except Exception:
            logger.exception("Error during playback")
        finally:
            self._is_playing = False
            if wav_path != audio_path:
                try:
                    os.unlink(wav_path)
                except OSError:
                    pass

    async def _ambient_loop(self):
        """Continuously loop the ambient sound until cancelled."""
        try:
            await asyncio.sleep(1.0)  # Pause before hum starts so it doesn't overlap the blip
            logger.info("Ambient loop: starting")
            loop_count = 0
            while True:
                frames = load_wav_as_frames("/opt/hermes/voice-bridge/ambient.wav", target_rate=LIVEKIT_SAMPLE_RATE)
                if not frames:
                    logger.error("Ambient loop: no frames loaded!")
                    return
                for frame in frames:
                    await self.audio_source.capture_frame(frame)
                loop_count += 1
                logger.info("Ambient loop: finished pushing loop %d", loop_count)
        except asyncio.CancelledError:
            logger.info("Ambient loop: cancelled")
            pass
        except Exception as e:
            logger.warning(f"Ambient loop error: {e}")

    def _start_ambient(self):
        self._stop_ambient()
        ambient_path = "/opt/hermes/voice-bridge/ambient.wav"
        if os.path.exists(ambient_path):
            self._ambient_task = asyncio.create_task(self._ambient_loop())
        else:
            logger.error(f"Ambient file {ambient_path} not found!")

    def _stop_ambient(self):
        if self._ambient_task and not self._ambient_task.done():
            self._ambient_task.cancel()

    async def _interrupt_playback(self):
        """Stop active playback (barge-in)."""
        self._stop_ambient()
        if self._is_playing:
            logger.info("Barge-in! Interrupting playback.")
            self._is_playing = False

    async def disconnect(self):
        if self.room:
            await self.room.disconnect()
        logger.info("Disconnected from room")


# ---------------------------------------------------------------------------
# Room discovery
# ---------------------------------------------------------------------------

async def get_matrix_room_members() -> set[str]:
    import aiohttp
    url = f"{MATRIX_HOMESERVER}/_matrix/client/v3/rooms/{MATRIX_ROOM_ID}/joined_members"
    headers = {"Authorization": f"Bearer {MATRIX_ACCESS_TOKEN}"}
    async with aiohttp.ClientSession() as session:
        async with session.get(url, headers=headers) as resp:
            data = await resp.json()
            return set(data.get("joined", {}).keys())

async def discover_room() -> Optional[str]:
    """Find the active LiveKit room for the configured Matrix room."""
    members = await get_matrix_room_members()
    lk = api.LiveKitAPI(LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
    try:
        rooms = await lk.room.list_rooms(api.ListRoomsRequest())
        for room in rooms.rooms:
            if room.num_participants > 0:
                parts = await lk.room.list_participants(api.ListParticipantsRequest(room=room.name))
                for p in parts.participants:
                    parts_split = p.identity.split(":")
                    if len(parts_split) >= 2:
                        mxid = f"{parts_split[0]}:{parts_split[1]}"
                        if mxid in members and mxid != MATRIX_USER_ID:
                            logger.info("Found active room: %s (%d participants) matching Matrix members", room.name, room.num_participants)
                            return room.name
    except Exception:
        logger.exception("Error listing rooms")
    finally:
        await lk.aclose()
    return None


async def wait_for_room() -> str:
    """Poll until a LiveKit room with participants appears."""
    logger.info("Waiting for a voice call to start in room %s...", MATRIX_ROOM_ID)
    while True:
        room_name = await discover_room()
        if room_name:
            return room_name
        await asyncio.sleep(3)


KEYS_FILE = "/opt/data/matrix-store/voice_call_keys.json"

def load_persisted_keys() -> dict[str, tuple[bytes, int]]:
    keys = {}
    if os.path.exists(KEYS_FILE):
        try:
            with open(KEYS_FILE, "r") as f:
                data = json.load(f)
                for sender, info in data.items():
                    key = bytes.fromhex(info["key_hex"])
                    key_index = info["key_index"]
                    keys[sender] = (key, key_index)
            logger.info("Loaded %d persisted encryption keys", len(keys))
        except Exception as e:
            logger.warning("Failed to load persisted keys: %s", e)
    return keys

def save_persisted_keys(keys: dict[str, tuple[bytes, int]]):
    try:
        os.makedirs(os.path.dirname(KEYS_FILE), exist_ok=True)
        data = {}
        for sender, (key, key_index) in keys.items():
            data[sender] = {
                "key_hex": key.hex(),
                "key_index": key_index
            }
        with open(KEYS_FILE, "w") as f:
            json.dump(data, f)
        logger.info("Persisted %d encryption keys", len(keys))
    except Exception as e:
        logger.warning("Failed to persist keys: %s", e)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

async def main():
    import argparse
    from matrix_call import MatrixCallClient

    parser = argparse.ArgumentParser(description="Hermes Voice Bridge")
    parser.add_argument("--room", type=str, help="LiveKit room name (auto-discovered if omitted)")
    args = parser.parse_args()

    if not LIVEKIT_API_KEY or not LIVEKIT_API_SECRET:
        logger.error("LIVEKIT_API_KEY and LIVEKIT_API_SECRET must be set")
        return

    # Determine LiveKit room name
    while True:
        room_name = args.room or LIVEKIT_ROOM
        if not room_name:
            room_name = await wait_for_room()

        # Set up Matrix call client first to obtain our pre-generated E2EE key
        mx_client = MatrixCallClient(
            homeserver=MATRIX_HOMESERVER,
            access_token=MATRIX_ACCESS_TOKEN,
            user_id=MATRIX_USER_ID,
            device_id=MATRIX_DEVICE_ID,
            room_id=MATRIX_ROOM_ID,
            recovery_key=MATRIX_RECOVERY_KEY,
            on_encryption_key=None,  # Will set callback next
        )

        # Set up Matrix call client for E2EE key exchange
        received_keys = load_persisted_keys()
        bridge = HermesVoiceBridge(room_name, e2ee_key=mx_client._own_key)

        # Apply persisted keys immediately
        for sender, (key, key_index) in received_keys.items():
            bridge.set_participant_key(sender, key, key_index)

        def on_encryption_key(sender: str, key: bytes, key_index: int):
            """Callback when we receive an encryption key from a call participant."""
            received_keys[sender] = (key, key_index)
            save_persisted_keys(received_keys)
            bridge.set_participant_key(sender, key, key_index)

        mx_client.on_encryption_key = on_encryption_key

        try:
            # Start Matrix client (sync, crypto, receive keys)
            logger.info("Starting Matrix call client...")
            await mx_client.start()

            # Join the MatrixRTC call
            await mx_client.join_call()

            # Send our own encryption key — triggers other participants to send theirs
            await mx_client.send_own_encryption_key()

            # Wait a moment for key exchange
            logger.info("Waiting for encryption key exchange...")
            for i in range(10):
                if received_keys:
                    break
                await asyncio.sleep(1)

            if not received_keys:
                logger.warning("No encryption keys received yet — connecting without E2EE")
            else:
                logger.info("Received keys from %d participant(s)", len(received_keys))

            # Connect to LiveKit
            await bridge.connect()

            # Wait a few seconds for initial connections to establish
            await asyncio.sleep(5)

            # Wait until everyone leaves
            while True:
                if bridge.room and len(bridge.room.remote_participants) == 0:
                    logger.info("All participants left. Disconnecting to clear E2EE state...")
                    break
                await asyncio.sleep(1)

        except (KeyboardInterrupt, asyncio.CancelledError):
            logger.info("Shutting down...")
            break
        except Exception as e:
            logger.exception("Error in main session loop")
        finally:
            await bridge.disconnect()
            try:
                await mx_client.leave_call()
            except Exception:
                pass
            await mx_client.stop()
            
            # Wait a moment before polling for the next room
            args.room = None  # Clear args.room so we wait_for_room next time
            await asyncio.sleep(3)


if __name__ == "__main__":
    asyncio.run(main())
