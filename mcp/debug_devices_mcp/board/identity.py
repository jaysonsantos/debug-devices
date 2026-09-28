"""Physical part identity: a claim about which boardview part a place in a photo is.

States:
- visible_marking: the photo shows a marking; the boardview names that fit it are only candidates.
- candidate: boardview estimates (a position from a registration, or look-alike parts). Never a visual fact.
- confirmed: a current phone_snapshot, a valid checked registration, and a visible marking or a unique landmark.

A confirmation holds only for its scene: a move of the board or the phone (the photo id becomes stale, the
registration becomes stale) turns it back into a candidate. A part on the other side cannot be confirmed.
"""

import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from enum import StrEnum
from typing import Protocol

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import AwareDatetime, BaseModel

from debug_devices_mcp.board.dump import Side
from debug_devices_mcp.board.homography import unmap_point
from debug_devices_mcp.board.marking import Resolution, find_marking_parts
from debug_devices_mcp.board.model import Board, Part, Point, on_side

# The pointed place belongs to a part when it is inside the part box grown by this margin.
POINT_MARGIN_MM = 1.0
# Without a part box at the point: the parts whose center is this near are candidates.
NEAR_RADIUS_MM = 3.0
# A marking is printed next to its part: the pointed marking can be this far from the part center.
MARKING_TOLERANCE_MM = 6.0
# Look-alike parts: same refdes letters, same pin count, similar box, and this near to the pointed part.
LOOKALIKE_RADIUS_MM = 15.0
LOOKALIKE_SIZE_RATIO = 1.3
MAX_CLAIMS = 100
MAX_CANDIDATES = 12

CLOSER_PHOTO = (
    "Ask the user for a closer photo (phone closer to the board, then phone_zoom and a fresh phone_snapshot) so that "
    "a marking or a unique feature is readable, then call board_identify again."
)
REGISTER_FIRST = (
    "Register this photo with board_register_photo (5 or more pairs, so the fit is checked), then call "
    "board_identify with the registration_id and the pixel position of the part."
)


class IdentityState(StrEnum):
    VISIBLE_MARKING = "visible_marking"
    CANDIDATE = "candidate"
    CONFIRMED = "confirmed"


class IdentityBasis(StrEnum):
    MARKING = "marking"
    LANDMARK = "landmark"
    NONE = "none"


class IdentityClaim(BaseModel):
    claim_id: str
    created_at: AwareDatetime
    # The phone_snapshot capture id that the claim is about.
    photo_id: str
    registration_id: str | None
    side: Side | None
    x_px: float | None
    y_px: float | None
    # The marking as seen in the photo, quoted exactly. Null when the part shows no readable marking.
    visible_marking: str | None
    state: IdentityState
    # The confirmed part. Null unless `state` is "confirmed".
    refdes: str | None
    # Boardview names that can be this part (estimates).
    candidates: list[str]
    basis: IdentityBasis
    reason: str
    # What to do to confirm; null when confirmed.
    request: str | None


class IdentityReport(BaseModel):
    confirmed: list[str]
    claims: list[IdentityClaim]


class PhotoChecker(Protocol):
    """The capture log of the MCP server (evidence.py)."""

    def require_current_photo(self, capture_id: str) -> object: ...

    def status(self, capture_id: str) -> object: ...


class RegistrationLike(Protocol):
    registration_id: str
    side: Side
    checked: bool
    stale: bool

    @property
    def fit(self) -> object: ...


def _prefix(name: str) -> str:
    return "".join(char for char in name if char.isalpha()).upper()


def lookalikes(board: Board, part: Part, side: Side | None) -> list[Part]:
    """Parts that a photo cannot tell apart from `part` without a marking."""
    similar = []
    for other in board.parts.values():
        if other.name == part.name or not board.side_ok(other.side, side):
            continue
        if _prefix(other.name) != _prefix(part.name) or other.pin_count != part.pin_count:
            continue
        sizes = sorted((max(part.box.width, part.box.height), max(other.box.width, other.box.height)))
        if sizes[0] <= 0 or sizes[1] / sizes[0] > LOOKALIKE_SIZE_RATIO:
            continue
        if other.center.distance(part.center) <= LOOKALIKE_RADIUS_MM:
            similar.append(other)
    return sorted(similar, key=lambda other: other.center.distance(part.center))


def parts_at(board: Board, point: Point, side: Side) -> list[Part]:
    """Parts at a board point: inside a box (grown by the margin), else with a center near it. Nearest first."""
    grown = POINT_MARGIN_MM
    inside = [
        part
        for part in board.parts.values()
        if board.side_ok(part.side, side)
        and part.box.min_x - grown <= point.x <= part.box.max_x + grown
        and part.box.min_y - grown <= point.y <= part.box.max_y + grown
    ]
    if not inside:
        inside = [
            part
            for part in board.parts.values()
            if board.side_ok(part.side, side) and part.center.distance(point) <= NEAR_RADIUS_MM
        ]
    return sorted(inside, key=lambda part: part.center.distance(point))


class IdentityRegistry:
    def __init__(self) -> None:
        self._claims: OrderedDict[str, IdentityClaim] = OrderedDict()

    def add(self, claim: IdentityClaim) -> IdentityClaim:
        self._claims[claim.claim_id] = claim
        while len(self._claims) > MAX_CLAIMS:
            self._claims.popitem(last=False)
        return claim

    def clear(self) -> None:
        self._claims.clear()

    def report(self, photos: PhotoChecker | None, registrations: dict[str, RegistrationLike]) -> IdentityReport:
        """The claims now: a confirmation from an older scene is shown as a candidate again."""
        claims = [effective(claim, photos, registrations) for claim in self._claims.values()]
        confirmed = sorted(
            {claim.refdes for claim in claims if claim.state is IdentityState.CONFIRMED and claim.refdes}
        )
        return IdentityReport(confirmed=confirmed, claims=claims)


def effective(
    claim: IdentityClaim, photos: PhotoChecker | None, registrations: dict[str, RegistrationLike]
) -> IdentityClaim:
    if claim.state is not IdentityState.CONFIRMED:
        return claim
    photo_valid = photos is None or getattr(photos.status(claim.photo_id), "valid_for_position_claims", False)
    registration = registrations.get(claim.registration_id or "")
    registration_valid = registration is not None and not registration.stale
    if photo_valid and registration_valid:
        return claim
    return claim.model_copy(
        update={
            "state": IdentityState.CANDIDATE,
            "refdes": None,
            "candidates": [claim.refdes, *claim.candidates] if claim.refdes else claim.candidates,
            "reason": "the board or the phone moved after this confirmation: it no longer holds",
            "request": "Take a fresh phone_snapshot and call board_identify again.",
        }
    )


def new_claim(**fields: object) -> IdentityClaim:
    return IdentityClaim.model_validate({"claim_id": str(uuid.uuid7()), "created_at": datetime.now(UTC), **fields})


def identify(
    board: Board,
    photo_id: str,
    registration: RegistrationLike | None,
    point: tuple[float, float] | None,
    marking: str | None,
) -> IdentityClaim:
    """Decide the state of one claim. The caller checked that the photo is current and the registration valid."""
    base: dict[str, object] = {
        "photo_id": photo_id,
        "registration_id": registration.registration_id if registration else None,
        "side": registration.side if registration else None,
        "x_px": point[0] if point else None,
        "y_px": point[1] if point else None,
        "visible_marking": marking,
    }
    at: list[Part] = []
    board_point: Point | None = None
    if registration is not None and point is not None:
        x, y = unmap_point(registration.fit.matrix, point)  # type: ignore[attr-defined]
        board_point = Point(x=x, y=y)
        at = parts_at(board, board_point, registration.side)
    if marking:
        return _by_marking(board, base, registration, board_point, marking)
    return _by_landmark(board, base, registration, at)


def _by_marking(
    board: Board,
    base: dict[str, object],
    registration: RegistrationLike | None,
    board_point: Point | None,
    marking: str,
) -> IdentityClaim:
    resolution, parts = find_marking_parts(board, marking, None)
    names = [part.name for part in parts][:MAX_CANDIDATES]
    if resolution is not Resolution.EXACT:
        reason = (
            f'the visible marking "{marking}" has no exact boardview part ({resolution})'
            if parts
            else f'the visible marking "{marking}" has no boardview part'
        )
        return new_claim(
            **base,
            state=IdentityState.VISIBLE_MARKING,
            refdes=None,
            candidates=names,
            basis=IdentityBasis.NONE,
            reason=reason,
            request=CLOSER_PHOTO,
        )
    part = parts[0]
    problem = None
    if registration is None or board_point is None:
        problem, request = "no photo registration and position", REGISTER_FIRST
    elif not registration.checked:
        problem, request = "the registration has only 4 pairs (its error is not checked)", REGISTER_FIRST
    elif not on_side(part.side, registration.side) and not board.mixed_sides:
        problem = f"{part.name} is on the {part.side} side, the photo shows the {registration.side} side"
        request = (
            "The current photo cannot confirm it. Before the user turns the board, they must isolate the power "
            "safely (charger and battery or bench supply off) and confirm it."
        )
    elif part.center.distance(board_point) > MARKING_TOLERANCE_MM:
        problem = f"the pointed marking is {part.center.distance(board_point):.1f} mm from {part.name}"
        request = CLOSER_PHOTO
    if problem is not None:
        return new_claim(
            **base,
            state=IdentityState.VISIBLE_MARKING,
            refdes=None,
            candidates=[part.name],
            basis=IdentityBasis.NONE,
            reason=f'the visible marking "{marking}" names {part.name}, but {problem}',
            request=request,
        )
    reason = f'the visible marking "{marking}" is {part.name}, at the registered position in a current photo'
    if registration is not None and not on_side(part.side, registration.side):
        reason += f"; the file labels it {part.side}, but its side labels are mixed (the photo decides)"
    return new_claim(
        **base,
        state=IdentityState.CONFIRMED,
        refdes=part.name,
        candidates=[],
        basis=IdentityBasis.MARKING,
        reason=reason,
        request=None,
    )


def _by_landmark(
    board: Board, base: dict[str, object], registration: RegistrationLike | None, at: list[Part]
) -> IdentityClaim:
    if registration is None:
        raise ToolError("give a `marking`, or a `registration_id` with `x_px` and `y_px`")
    if not at:
        return new_claim(
            **base,
            state=IdentityState.CANDIDATE,
            refdes=None,
            candidates=[],
            basis=IdentityBasis.NONE,
            reason="no boardview part at this position of the registered side",
            request=CLOSER_PHOTO,
        )
    nearest = at[0]
    similar = lookalikes(board, nearest, registration.side)
    candidates = list(dict.fromkeys([part.name for part in at] + [part.name for part in similar]))[:MAX_CANDIDATES]
    if len(at) == 1 and not similar and registration.checked:
        return new_claim(
            **base,
            state=IdentityState.CONFIRMED,
            refdes=nearest.name,
            candidates=[],
            basis=IdentityBasis.LANDMARK,
            reason=f"{nearest.name} is the only part at this position, and no look-alike part is near",
            request=None,
        )
    if similar:
        reason = f"look-alike parts near {nearest.name} ({', '.join(part.name for part in similar[:4])})"
    elif len(at) > 1:
        reason = "several parts at this position"
    else:
        reason = "the registration has only 4 pairs (its error is not checked)"
    return new_claim(
        **base,
        state=IdentityState.CANDIDATE,
        refdes=None,
        candidates=candidates,
        basis=IdentityBasis.NONE,
        reason=f"boardview estimate only: {reason}",
        request=CLOSER_PHOTO if similar or len(at) > 1 else REGISTER_FIRST,
    )
