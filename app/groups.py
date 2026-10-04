"""Pure group rules, kept free of DB/IO so they can be unit-tested directly."""

from collections.abc import Iterable
from datetime import datetime


def key_recipients_error(
    required: Iterable[str], provided: Iterable[str], sender: str
) -> str | None:
    """Check the per-member wrapped keys on a group message.

    `required` are the current members that have a public key. The sender's
    own entry is allowed (and expected, so they can read their own message)
    but not required; every other required member must be covered exactly
    once, and entries for anyone who is not a member are rejected.
    """
    required_set = set(required)
    provided_list = list(provided)
    provided_set = set(provided_list)

    if len(provided_list) != len(provided_set):
        return "duplicate recipient keys"
    if not provided_set <= required_set:
        return "recipient keys include non-members"
    missing = (required_set - {sender}) - provided_set
    if missing:
        return "missing recipient keys"
    return None


def pick_successor_admin(members: Iterable[tuple[str, str, datetime]]) -> str | None:
    """Given (username, role, joined_at) of the members who remain after
    someone left, return who to promote so the group never has no admin:
    None if an admin already remains (or nobody does), else the oldest member."""
    remaining = sorted(members, key=lambda m: (m[2], m[0]))
    if not remaining or any(role == "admin" for _, role, _ in remaining):
        return None
    return remaining[0][0]
