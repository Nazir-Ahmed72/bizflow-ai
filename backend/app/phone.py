"""Phone number normalisation. Every phone in the DB (and every thread_id) is E.164: +923001234567."""
from __future__ import annotations

import re


def normalize_phone(raw: str) -> str:
    """'+92 300 1234567', '0300-1234567', '923001234567' -> '+923001234567'.

    Raises ValueError for input that cannot be a Pakistani/E.164 number.
    """
    if raw is None:
        raise ValueError("phone is empty")
    s = raw.strip()
    plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if s.startswith("00"):
        digits, plus = digits[2:], True
    if not plus:
        if digits.startswith("0") and len(digits) == 11:  # local mobile 03xxxxxxxxx
            digits = "92" + digits[1:]
        elif not digits.startswith("92"):
            raise ValueError(f"cannot normalise phone: {raw!r}")
    if not 10 <= len(digits) <= 15:
        raise ValueError(f"cannot normalise phone: {raw!r}")
    return "+" + digits
