"""board_match_marking with more than one reading of the marking: as seen, and upside down (180 degrees).

The marking as seen stays the evidence. A rotated reading and a value-code interpretation are only other ways to
read the same text, and the result says which one matched.
"""

from dataclasses import dataclass, field

from debug_devices_mcp.board.dump import Side
from debug_devices_mcp.board.marking import Resolution, find_marking_parts, normalize
from debug_devices_mcp.board.model import Board, Part
from debug_devices_mcp.board.rotation import Reading, read_rotated_180
from debug_devices_mcp.board.value_code import ValueInterpretation, decode_value_marking

# A lower rank is a better match.
RESOLUTION_RANK = {
    Resolution.EXACT: 0,
    Resolution.PREFIX_CANDIDATES: 1,
    Resolution.CONTAINS_CANDIDATES: 2,
    Resolution.CONFUSION_CANDIDATES: 3,
    Resolution.NONE: 4,
}


@dataclass
class MarkingLookup:
    resolution: Resolution
    parts: list[Part]
    reading: Reading
    # The text of the reading that matched (the marking itself, or its upright text).
    matched_text: str
    rotated_text: str | None = None
    value_interpretations: list[ValueInterpretation] = field(default_factory=list)


def lookup_marking(board: Board, marking: str, side: Side | None, try_rotations: bool) -> MarkingLookup:
    """Match the marking as seen. With `try_rotations`, also its upright text, which wins only when it is better."""
    resolution, parts = find_marking_parts(board, marking, side)
    lookup = MarkingLookup(resolution, parts, Reading.AS_SEEN, marking)
    rotated = read_rotated_180(marking).text if try_rotations else None
    if rotated is not None and normalize(rotated) != normalize(marking):
        lookup.rotated_text = rotated
        rotated_resolution, rotated_parts = find_marking_parts(board, rotated, side)
        if RESOLUTION_RANK[rotated_resolution] < RESOLUTION_RANK[resolution]:
            lookup.resolution, lookup.parts = rotated_resolution, rotated_parts
            lookup.reading, lookup.matched_text = Reading.ROTATED_180, rotated
    for text in (marking, lookup.rotated_text):
        if text is not None:
            lookup.value_interpretations.extend(decode_value_marking(text))
    return lookup


def reading_message(marking: str, lookup: MarkingLookup, match_message: str) -> str:
    """The match message, plus which reading matched and the value-code interpretations."""
    parts = []
    if lookup.reading is Reading.ROTATED_180:
        parts.append(
            f'Read upside down (turned 180 degrees), the visible marking "{marking}" is "{lookup.matched_text}". '
            "The rotated reading matched, not the marking as seen: check the orientation in the photo."
        )
    parts.append(match_message)
    if lookup.value_interpretations:
        codes = "; ".join(
            f'"{item.code}" = {item.display} ({item.kind.value})' for item in lookup.value_interpretations
        )
        parts.append(
            f"As an SMD value code (interpretation only, not the part name): {codes}. "
            "A short code on a small part is often its value."
        )
    return " ".join(parts)
