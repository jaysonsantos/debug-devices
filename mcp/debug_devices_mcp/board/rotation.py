"""Read a marking that was seen upside down (the photo or the board turned 180 degrees).

A turned marking reads backwards, and some characters look like other characters: a turned 6 reads as 9, a turned
3 as E, a turned 7 as L, a turned 1 (with its flag and foot) as T. This module keeps that table in one place.

Characters that a reader also confuses without a turn (O/0, I/1, S/5, Z/2, B/8) stay as they are here: the
OCR-confusion match of marking.py handles them.
"""

from enum import StrEnum

from pydantic import BaseModel

# Characters that look the same after a turn of 180 degrees.
SELF_SYMMETRIC = "0Oo1Il8SsZzXxNH5 2-_"
# Pairs that turn into each other: a turned 6 reads as 9, and a turned 9 reads as 6.
SYMMETRIC_PAIRS = (("6", "9"), ("3", "E"), ("7", "L"), ("n", "u"), ("d", "p"), ("b", "q"), ("M", "W"), ("m", "w"))
# One way only: a turned 1 is often read as T. A turned T reads as nothing.
ONE_WAY = (("T", "1"),)


def rotation_table() -> dict[str, str]:
    """Seen character (in the turned photo) -> the upright character."""
    table = {char: char for char in SELF_SYMMETRIC}
    for first, second in SYMMETRIC_PAIRS:
        table[first] = second
        table[second] = first
    for seen, upright in ONE_WAY:
        table[seen] = upright
    return table


ROTATION_180 = rotation_table()


class Reading(StrEnum):
    """Which reading of the marking gave the match."""

    AS_SEEN = "as_seen"
    ROTATED_180 = "rotated_180"


class RotatedReading(BaseModel):
    # The upright text, or None when a character has no upright form (see `unmapped`).
    text: str | None
    unmapped: list[str]


def read_rotated_180(seen: str) -> RotatedReading:
    """The upright text of a marking that was read upside down: reversed, and each character turned."""
    unmapped = sorted({char for char in seen if char not in ROTATION_180})
    if unmapped:
        return RotatedReading(text=None, unmapped=unmapped)
    return RotatedReading(text="".join(ROTATION_180[char] for char in reversed(seen)), unmapped=[])
