"""Tests for bridge room-discovery latching fix.

Covers:
  - room_matches_members(): allowed-human present → match; only self/other
    bridge accounts → no match; malformed identities ignored.
  - parse_allowed_users(): comma+space separated parsing.
  - GREETING_TEXT env override (default when unset, override wins when set)
    against the REAL bridge module (container-only deps stubbed).

Run: python -m pytest tests/ -v  (from repo root)
"""

from __future__ import annotations

import importlib
import os
import sys
import types
from unittest import mock

import pytest

from room_matching import (
    find_allowed_humans,
    parse_allowed_users,
    parse_mxid_from_identity,
    room_matches_members,
)

# --- Fixtures -------------------------------------------------------------

ALLOWED = {"@human:x.ikavt.com", "@human2:x.ikavt.com"}
SELF = "@hermes4:x.ikavt.com"
# All realistic accounts are joined members of the Matrix room — membership
# alone must NOT be enough to match (that was the original latching bug).
MEMBERS = {
    "@human:x.ikavt.com",
    "@human2:x.ikavt.com",
    "@hermes4:x.ikavt.com",  # self — other bridge's target account
    "@hermes3:x.ikavt.com",  # another bridge's Hermes account
}


# --- parse_mxid_from_identity ---------------------------------------------

def test_parse_mxid_from_identity_normal():
    assert parse_mxid_from_identity("@human:x.ikavt.com:DEVICEID") == "@human:x.ikavt.com"


def test_parse_mxid_from_identity_bridge_device():
    # Bridge devices: '@hermesN:x.ikavt.com:voice-bridge-XX'
    assert parse_mxid_from_identity("@hermes3:x.ikavt.com:voice-bridge-XX") == "@hermes3:x.ikavt.com"


def test_parse_mxid_from_identity_malformed():
    assert parse_mxid_from_identity("garbage") is None
    assert parse_mxid_from_identity("") is None


# --- parse_allowed_users ---------------------------------------------------

def test_parse_allowed_users_comma_and_spaces():
    raw = "@human:x.ikavt.com, @human2:x.ikavt.com"
    assert parse_allowed_users(raw) == {"@human:x.ikavt.com", "@human2:x.ikavt.com"}


def test_parse_allowed_users_empty_and_junk():
    assert parse_allowed_users("") == set()
    assert parse_allowed_users(" , ,") == set()


# --- room_matches_members ---------------------------------------------------

def test_room_matched_when_allowed_human_present():
    identities = [
        "@hermes4:x.ikavt.com:voice-bridge-XX",  # self (other bridge device)
        "@human:x.ikavt.com:ELEMENTXDEVICE",     # allowed human
    ]
    assert room_matches_members(identities, ALLOWED, SELF, MEMBERS) is True


def test_room_matched_with_only_human():
    assert room_matches_members(["@human2:x.ikavt.com:ABC"], ALLOWED, SELF, MEMBERS) is True


def test_no_match_when_only_other_bridge_self_account():
    # The production latch: room contains ONLY the other bridge's agent,
    # which shares our own Matrix account (self).
    identities = ["@hermes4:x.ikavt.com:voice-bridge-XX"]
    assert room_matches_members(identities, ALLOWED, SELF, MEMBERS) is False


def test_no_match_when_only_non_allowed_herms_account():
    # @hermes3: is neither self nor allowed → ignored entirely.
    identities = [
        "@hermes3:x.ikavt.com:voice-bridge-YY",
        "@hermes4:x.ikavt.com:voice-bridge-XX",
    ]
    assert room_matches_members(identities, ALLOWED, SELF, MEMBERS) is False


def test_no_match_when_self_misconfigured_into_allowed_users():
    # Defense in depth: even if our own account lands in MATRIX_ALLOWED_USERS
    # (misconfiguration), its devices must never trigger a match — that is the
    # original bridge-vs-bridge latch.
    identities = ["@hermes4:x.ikavt.com:voice-bridge-XX"]
    assert room_matches_members(identities, ALLOWED | {SELF}, SELF, MEMBERS) is False


def test_no_match_when_allowed_human_not_room_member():
    # Allowed human present in LiveKit but NOT joined in the Matrix room
    # → the existing membership check must still gate the match.
    identities = ["@human:x.ikavt.com:DEV"]
    members_without_human = MEMBERS - {"@human:x.ikavt.com"}
    assert room_matches_members(identities, ALLOWED, SELF, members_without_human) is False


def test_malformed_identity_ignored():
    identities = ["garbage", "", "@hermes4:x.ikavt.com:voice-bridge-XX"]
    assert room_matches_members(identities, ALLOWED, SELF, MEMBERS) is False
    # Malformed entries must not crash or mask a real human either:
    assert room_matches_members(
        ["garbage", "@human:x.ikavt.com:DEV"], ALLOWED, SELF, MEMBERS
    ) is True


def test_empty_room_no_match():
    assert room_matches_members([], ALLOWED, SELF, MEMBERS) is False


# --- find_allowed_humans (session idle-exit predicate) ----------------------

def test_find_allowed_humans_filters():
    identities = [
        "@hermes4:x.ikavt.com:voice-bridge-XX",
        "@human:x.ikavt.com:DEV",
        "malformed",
    ]
    assert find_allowed_humans(identities, ALLOWED) == {"@human:x.ikavt.com"}
    assert find_allowed_humans(["@hermes3:x.ikavt.com:voice-bridge-YY"], ALLOWED) == set()


# --- Real bridge module: env-configured greeting + allowed users ------------

def _stub_container_deps():
    """Stub container-only modules so bridge.py can be imported on a dev box."""
    if "livekit" not in sys.modules:
        livekit = types.ModuleType("livekit")
        livekit.api = types.ModuleType("livekit.api")
        livekit.rtc = types.ModuleType("livekit.rtc")
        sys.modules["livekit"] = livekit
        sys.modules["livekit.api"] = livekit.api
        sys.modules["livekit.rtc"] = livekit.rtc
    if "numpy" not in sys.modules:
        sys.modules["numpy"] = types.ModuleType("numpy")  # not touched at import time
    tools = types.ModuleType("tools")
    transcription = types.ModuleType("tools.transcription_tools")
    transcription.transcribe_audio = lambda *a, **k: None
    tts = types.ModuleType("tools.tts_tool")
    tts.text_to_speech_tool = lambda *a, **k: None
    tts._strip_markdown_for_tts = lambda s: s
    tools.transcription_tools = transcription
    tools.tts_tool = tts
    sys.modules["tools"] = tools
    sys.modules["tools.transcription_tools"] = transcription
    sys.modules["tools.tts_tool"] = tts


def _load_bridge(env: dict):
    """Import/reload the real bridge.py under a controlled environment."""
    _stub_container_deps()
    saved = {k: os.environ.get(k) for k in env}
    try:
        for key, value in env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        import bridge
        return importlib.reload(bridge)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_greeting_text_default_when_env_unset():
    bridge = _load_bridge({"BRIDGE_GREETING_TEXT": None})
    assert bridge.GREETING_TEXT == "Hey! How are you?"


def test_greeting_text_env_override_wins():
    bridge = _load_bridge({"BRIDGE_GREETING_TEXT": "Привет! Чем помочь?"})
    assert bridge.GREETING_TEXT == "Привет! Чем помочь?"


def test_bridge_allowed_users_parsed_from_env():
    bridge = _load_bridge(
        {"MATRIX_ALLOWED_USERS": "@human:x.ikavt.com, @human2:x.ikavt.com"}
    )
    assert bridge.MATRIX_ALLOWED_USERS == {"@human:x.ikavt.com", "@human2:x.ikavt.com"}


def test_bridge_idle_exit_default_and_override():
    bridge = _load_bridge({"BRIDGE_IDLE_EXIT_SECONDS": None})
    assert bridge.BRIDGE_IDLE_EXIT_SECONDS == 60.0
    bridge = _load_bridge({"BRIDGE_IDLE_EXIT_SECONDS": "120"})
    assert bridge.BRIDGE_IDLE_EXIT_SECONDS == 120.0
