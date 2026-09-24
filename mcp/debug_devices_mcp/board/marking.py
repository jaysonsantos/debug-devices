"""Match a marking that is visible on the board (silkscreen) to boardview parts.

The visible marking stays the evidence. Boardview names are candidates for it: the silkscreen can be cut off,
hidden under a part, or read wrong (O/0, I/1, ...).
"""

import re
from enum import StrEnum

from pydantic import BaseModel

from debug_devices_mcp.board.dump import Side
from debug_devices_mcp.board.model import Board, Box, Mm, Part, Point, on_side

# Case, spaces, dashes, and underscores do not count.
IGNORED_CHARS = re.compile(r"[\s\-_]+")
# Characters that a camera or a reader confuses. Each group maps to one character.
CONFUSION_GROUPS = ("o0", "il1", "s5", "b8", "z2", "g6")
CONFUSION_TABLE = str.maketrans({char: group[-1] for group in CONFUSION_GROUPS for char in group})
MAX_CANDIDATES = 20
MAX_NETS = 5
# With a photo position: the nearest candidate is the best only when the next one is at least this many times farther.
BEST_DISTANCE_RATIO = 2.0


class Resolution(StrEnum):
    EXACT = "exact"
    PREFIX_CANDIDATES = "prefix_candidates"
    CONTAINS_CANDIDATES = "contains_candidates"
    CONFUSION_CANDIDATES = "confusion_candidates"
    NONE = "none"


class PixelPosition(BaseModel):
    x_px: float
    y_px: float


class Candidate(BaseModel):
    refdes: str
    side: Side
    center: Point
    box: Box
    pin_count: int
    mfgcode: str
    nets: list[str]
    # With a photo registration: where the part center is in the photo, and how far from the given point.
    photo_position: PixelPosition | None = None
    distance_px: Mm | None = None


class MarkingMatch(BaseModel):
    # The marking as the caller saw it. Quote this, not a candidate name, when you tell the user what is visible.
    visible_marking: str
    resolution: Resolution
    candidates: list[Candidate]
    total_candidates: int
    best_candidate: str | None
    message: str


def normalize(text: str) -> str:
    return IGNORED_CHARS.sub("", text).casefold()


def unconfuse(text: str) -> str:
    return normalize(text).translate(CONFUSION_TABLE)


def find_marking_parts(board: Board, marking: str, side: Side | None) -> tuple[Resolution, list[Part]]:
    """Exact, then prefix, then contained, then OCR-confusion matches. The first level with a match wins."""
    key = normalize(marking)
    parts = [part for part in board.parts.values() if on_side(part.side, side)]
    if not key:
        return Resolution.NONE, []
    names = {part.name: normalize(part.name) for part in parts}
    exact = [part for part in parts if names[part.name] == key]
    if exact:
        return Resolution.EXACT, exact
    prefix = [part for part in parts if names[part.name].startswith(key)]
    if prefix:
        return Resolution.PREFIX_CANDIDATES, sorted(prefix, key=lambda part: part.name)
    contains = [part for part in parts if key in names[part.name]]
    if contains:
        return Resolution.CONTAINS_CANDIDATES, sorted(contains, key=lambda part: part.name)
    loose = unconfuse(marking)
    confused = [part for part in parts if unconfuse(part.name).startswith(loose)]
    if confused:
        # Equal first (an exact read with confused characters), then longer names (cut-off silkscreen).
        return Resolution.CONFUSION_CANDIDATES, sorted(
            confused, key=lambda part: (unconfuse(part.name) != loose, part.name)
        )
    return Resolution.NONE, []


def candidate(part: Part) -> Candidate:
    return Candidate(
        refdes=part.name,
        side=part.side,
        center=part.center,
        box=part.box,
        pin_count=part.pin_count,
        mfgcode=part.mfgcode,
        nets=part.nets[:MAX_NETS],
    )


def best_by_distance(candidates: list[Candidate]) -> str | None:
    """The nearest candidate (the list is sorted by distance), when it is clearly nearer than the next one."""
    distances = [item.distance_px for item in candidates if item.distance_px is not None]
    if not distances:
        return None
    if len(distances) == 1 or distances[1] >= BEST_DISTANCE_RATIO * distances[0]:
        return candidates[0].refdes
    return None


def message(marking: str, resolution: Resolution, names: list[str], best: str | None, ranked: bool) -> str:
    quoted = f'Visible marking "{marking}"'
    listed = ", ".join(names)
    match resolution:
        case Resolution.EXACT:
            return f"{quoted} is boardview part {names[0]} (exact match)."
        case Resolution.NONE:
            return (
                f"{quoted} has no boardview part, also not as a start, a part of a name, or with O/0, I/1, S/5, B/8, "
                "Z/2, G/6 read wrong. Check the marking in a fresh phone_snapshot."
            )
        case Resolution.PREFIX_CANDIDATES:
            reason = "The silkscreen can be cut off or hidden."
        case Resolution.CONTAINS_CANDIDATES:
            reason = "The marking is only a part of these names. The silkscreen can be cut off at the start."
        case Resolution.CONFUSION_CANDIDATES:
            reason = "These names match only when some characters are read differently (for example O as 0)."
    text = f"{quoted} has no exact boardview part. Candidates: {listed}. {reason}"
    if best is not None:
        return f"{text} By photo position, the best candidate is {best}."
    if ranked:
        return f"{text} The photo position does not tell them apart clearly. Compare their neighbours."
    return (
        f"{text} Tell them apart by position (board_register_photo, then x_px/y_px of the marking) "
        "or by their neighbours."
    )
