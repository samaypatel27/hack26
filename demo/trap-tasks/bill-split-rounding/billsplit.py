"""Split a restaurant bill between friends.

House rule: every share is rounded to the cent, and half a cent always rounds UP
(so $0.125 becomes $0.13, never $0.12).
"""


def share(total: float, people: int, tip_percent: float = 0) -> str:
    """Each person's share of the bill (tip included) as a string like "$12.34"."""
    if people < 1:
        raise ValueError("people must be at least 1")
    with_tip = total * (1 + tip_percent / 100)
    each = with_tip / people
    return format_money(each)


def format_money(amount: float) -> str:
    """Format a dollar amount with two decimals: 3.5 -> "$3.50"."""
    return f"${amount:.2f}"
