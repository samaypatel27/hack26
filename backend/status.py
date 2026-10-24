"""Post status rules (CLAUDE.md §5). Call after every write."""

from typing import Any

FAILING_MIN_APPLIED = 3
FAILING_MAX_RATE = 0.5


def is_failing(reply: dict[str, Any]) -> bool:
    applied = reply["times_applied"]
    return applied >= FAILING_MIN_APPLIED and reply["times_worked"] / applied < FAILING_MAX_RATE


def score(reply: dict[str, Any]) -> float:
    """Smoothed success rate used to rank replies (§5)."""
    return (reply["times_worked"] + 1) / (reply["times_applied"] + 2)


def after_reply(status: str) -> str:
    """A new reply moves open/reopened posts to answered."""
    return "answered" if status in ("open", "reopened") else status


def after_outcome(status: str, worked: bool, replies: list[dict[str, Any]]) -> str:
    """`replies` must already include the updated counters."""
    if worked:
        return "solved"
    if status in ("answered", "solved") and all(is_failing(r) for r in replies):
        return "reopened"
    return status
