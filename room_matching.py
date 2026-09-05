"""Pure participant-matching helpers for the voice bridge.

LiveKit identities for MatrixRTC members look like '@user:server:deviceId'
(the Matrix user ID plus a device suffix), so an identity's FORMAT alone
cannot tell a human apart from another bridge device. The distinction is
made by comparing the embedded mxid against the configured allowed-human
users (MATRIX_ALLOWED_USERS) and our own account (MATRIX_USER_ID):
  - mxid == MATRIX_USER_ID        → our own account (other bridge devices)
  - mxid in  MATRIX_ALLOWED_USERS → a real human this bridge may serve
  - anything else                 → ignore

Kept dependency-free (stdlib only) so it can be unit-tested without LiveKit.
"""

from __future__ import annotations

from typing import Iterable, Optional


def parse_mxid_from_identity(identity: str) -> Optional[str]:
    """Extract the Matrix user ID from a LiveKit identity.

    Identities are '@user:server:deviceId' (3+ colon-separated segments);
    the mxid is the first two segments rejoined with ':'. Returns None for
    malformed identities (fewer than two segments).
    """
    segments = identity.split(":")
    if len(segments) < 2:
        return None
    return f"{segments[0]}:{segments[1]}"


def parse_allowed_users(raw: str) -> set[str]:
    """Parse MATRIX_ALLOWED_USERS (comma-separated mxids) into a set."""
    return {entry.strip() for entry in raw.split(",") if entry.strip()}


def find_allowed_humans(participant_identities: Iterable[str], allowed_users: set[str]) -> set[str]:
    """Return the allowed-human mxids present among the given LiveKit identities."""
    humans = set()
    for identity in participant_identities:
        mxid = parse_mxid_from_identity(identity)
        if mxid is not None and mxid in allowed_users:
            humans.add(mxid)
    return humans


def room_matches_members(
    participant_identities: Iterable[str],
    allowed_users: set[str],
    matrix_user_id: str,
    room_members: set[str],
) -> bool:
    """True if at least one participant is an allowed HUMAN Matrix member.

    Ignores our own account (other bridge devices share it), any mxid not in
    the allowed set, malformed identities, and allowed users who are not
    joined members of the Matrix room. This is what prevents two bridges —
    each a member of the other's Matrix room — from mutually latching onto
    a room that contains only the other bridge's agent.
    """
    for identity in participant_identities:
        mxid = parse_mxid_from_identity(identity)
        if mxid is None:
            continue
        if mxid == matrix_user_id:
            continue  # our own account — other bridge devices
        if mxid in allowed_users and mxid in room_members:
            return True
    return False
