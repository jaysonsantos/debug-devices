"""MCP tools for boardview files: open a board, find parts and nets, and draw it."""

import asyncio
import functools
import io
import logging
import math
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from PIL import Image as PilImage
from PIL import ImageDraw, ImageFont, ImageOps
from pydantic import BaseModel, Field, ValidationError

from debug_devices_mcp.board.constants import defaults
from debug_devices_mcp.board.dump import BoardFormat, Side
from debug_devices_mcp.board.homography import (
    Fit,
    HomographyError,
    fit_homography,
    likely_outlier,
    map_points,
    unmap_point,
)
from debug_devices_mcp.board.identity import (
    IdentityClaim,
    IdentityRegistry,
    IdentityReport,
    IdentityState,
    PhotoChecker,
    VisualInput,
    identify,
)
from debug_devices_mcp.board.loader import BoardLoadError, BoardviewLoader, LoaderOptions
from debug_devices_mcp.board.marking import (
    MAX_CANDIDATES,
    MarkingMatch,
    PixelPosition,
    Resolution,
    best_by_distance,
    candidate,
    find_marking_parts,
    message,
)
from debug_devices_mcp.board.marking_readings import lookup_marking, reading_message
from debug_devices_mcp.board.model import Board, Mm, Part, Pin, Point, SideLabels, TestPoint, on_side
from debug_devices_mcp.board.render import RenderError, RenderLegend, RenderOptions, colors, render_board
from debug_devices_mcp.board.session_store import BoardSessionRecord, BoardSessionStore
from debug_devices_mcp.constants import phone
from debug_devices_mcp.evidence import photo_scale
from debug_devices_mcp.highlight import PixelBox
from debug_devices_mcp.pointer import PointResult
from debug_devices_mcp.process import CommandRunner

PNG_FORMAT = "png"
JPEG_FORMAT = "jpeg"
PIL_JPEG_FORMAT = "JPEG"
JPEG_QUALITY = 85
MARK_RADIUS_PX = 18
# A highlight box around a tiny part is at least this large, so it stays visible.
MIN_BOX_PX = 8.0
MARK_WIDTH_PX = 4
MARK_FONT_SIZE = 28

type Limit = Annotated[int, Field(ge=1, description="Largest number of items in each list of the result.")]

# region: results


class SideCounts(BaseModel):
    top: int
    bottom: int
    both: int


class BoardSummary(BaseModel):
    path: str
    format: BoardFormat
    sha256: str
    cached: bool
    load_seconds: float
    parts: int
    pins: int
    nets: int
    nails: int
    test_points: int
    width_mm: Mm
    height_mm: Mm
    part_sides: SideCounts
    # Through-hole parts and mounting holes count as "both" (visible from both sides), whatever the label.
    relabeled_both_sides: int = 0
    # Set when the side labels of the file look mixed: the tools then warn instead of ruling parts out by side.
    side_warning: str | None = None


class FindMatch(StrEnum):
    # Refdes (exact or glob) or mfgcode text.
    NAME_OR_MFGCODE = "name_or_mfgcode"
    # No such part: parts whose name starts with the query (a cut-off or hidden silkscreen marking).
    PREFIX = "prefix"
    NONE = "none"


class PartMatches(BaseModel):
    query: str
    match: FindMatch
    total: int
    parts: list[Part]
    truncated: bool
    note: str | None = None


class PartPins(BaseModel):
    part: Part
    pins: list[Pin]


class TestPointDistance(BaseModel):
    test_point: TestPoint
    distance_mm: Mm


class NetPart(BaseModel):
    name: str
    side: Side
    center: Point
    pin_numbers: list[str]
    nearest_test_point: TestPointDistance | None


class NetReport(BaseModel):
    name: str
    pin_count: int
    part_count: int
    parts: list[NetPart]
    test_points: list[TestPoint]
    truncated: bool


class NetMatches(BaseModel):
    query: str
    total: int
    nets: list[NetReport]
    truncated: bool


class PartDistance(BaseModel):
    part: Part
    distance_mm: Mm


class NearParts(BaseModel):
    center: Point
    radius_mm: Mm
    side: Side | None
    parts: list[PartDistance]
    truncated: bool


class PhotoPair(BaseModel):
    """A part center on the board and the same point in the photo (pixels, from the top left corner)."""

    refdes: str
    x_px: float
    y_px: float


class CarryQuality(BaseModel):
    """How well the registered photo matched the new phone_snapshot (image features, RANSAC homography)."""

    inliers: int
    matches: int
    inlier_ratio: float
    # The RMS error of the matched features, in pixels of the new snapshot.
    error_px: float | None


class Registration(BaseModel):
    registration_id: str
    board_sha256: str
    side: Side
    photo_width_px: int
    photo_height_px: int
    refdes: list[str]
    fit: Fit
    # False with only 4 pairs: the fit is exact, so the error says nothing. Give 5 or more pairs.
    checked: bool
    # True after the board or the phone moved (scene change): the photo positions are for the old scene.
    stale: bool = False
    # Why it is stale when not a move (for example an app restart); None: the move message. Set both with mark_stale:
    # a new reason replaces an old one.
    stale_reason: str | None = None
    # Live tracking of this registration in the phone screen stream (on, or why not).
    tracking: str | None = None
    # The capture id of the phone_snapshot that was the last photo at registration time (the registered photo).
    photo_id: str | None = None
    # A registration carried over to a newer phone_snapshot by image features (after the board or the phone moved):
    # the registration it came from, and the quality of the match.
    carried_from: str | None = None
    carry_quality: CarryQuality | None = None

    def mark_stale(self, reason: str | None = None) -> None:
        """Stale for this reason (a message with `{id}`; None: the move message). It replaces an older reason."""
        self.stale = True
        self.stale_reason = reason


class LocatedPart(BaseModel):
    name: str
    side: Side
    x_px: float
    y_px: float
    in_photo: bool
    # False when the part is on the other side of the board than the registration.
    on_registered_side: bool


class LocatedPin(BaseModel):
    part: str
    number: str
    net: str
    x_px: float
    y_px: float
    in_photo: bool


class Locations(BaseModel):
    registration_id: str
    photo_width_px: int
    photo_height_px: int
    parts: list[LocatedPart]
    pins: list[LocatedPin]
    annotated: bool
    notes: list[str]
    # With `highlight`: the result of phone_point_to for the located parts (boxes in view, arrows outside).
    highlight: PointResult | None = None
    # Boardview positions are estimates: a located part is a candidate, never a visual fact (board_identify).
    identity: IdentityState = IdentityState.CANDIDATE


# endregion: results


# Refuses photo work after a scene change, unless the live tracker follows that registration.
type SceneGuard = Callable[[str | None], None]
# (registration_id, refdes, boxes in pixels of the registered photo, its size) -> the pointing result and the
# annotated snapshot.
type Highlighter = Callable[
    [str, list[str], list[PixelBox], tuple[int, int]], Awaitable[tuple[PointResult, bytes | None]]
]
# A new registration: start the live tracking. Returns its note.
type RegistrationHook = Callable[[Registration], Awaitable[str]]
# A board tool function (async, any arguments).
type ToolFunction = Callable[..., Awaitable[object]]

logger = logging.getLogger(__name__)

STALE_REGISTRATION = (
    "registration {id!r} is from before the board or the phone moved: take a fresh phone_snapshot and call "
    "board_register_photo again"
)
RESTORED_STALE_REASON = (
    "registration {id!r} was restored after a server restart, so its scene cannot be checked: take a fresh "
    "phone_snapshot and call board_register_photo again"
)


def scene_stale_reason(scene_reason: str) -> str:
    """A registration message (with `{id}`) from a scene change reason (scene.py)."""
    escaped = scene_reason.replace("{", "{{").replace("}", "}}")
    return f"registration {{id!r}}: {escaped}, then call board_register_photo again"


class BoardSession:
    """The loader and the board that board_open loaded last. The other board tools use that board."""

    def __init__(self, loader: BoardviewLoader) -> None:
        self.loader = loader
        self.board: Board | None = None
        self.registrations: dict[str, Registration] = {}
        # The newest registration (the monitor page uses it to highlight a searched part).
        self.last_registration_id: str | None = None
        # Set by the MCP server: refuse photo work after a scene change, and send highlight boxes to the phone.
        self.scene_guard: SceneGuard | None = None
        self.highlighter: Highlighter | None = None
        self.on_registered: RegistrationHook | None = None
        # Part identity claims (board/identity.py), and the capture log that checks their photo ids.
        self.identities = IdentityRegistry()
        self.photos: PhotoChecker | None = None
        # Set by the MCP server: the capture id of the last phone_snapshot, and the check that pixels of that photo can
        # use a registration (same scene, same camera view).
        self.current_photo_id: Callable[[], str | None] | None = None
        self.snapshot_guard: Callable[[str | None], None] | None = None
        # region: kept over a server restart (board/session_store.py)
        self.store = BoardSessionStore()
        self.board_path: str | None = None
        self._restored = False
        self.side_labels = SideLabels.AUTO
        # Why the last restore did not bring the board back (for example the file is gone), or None.
        self.restore_note: str | None = None
        # endregion

    def check_scene(self, registration_id: str | None = None) -> None:
        if self.scene_guard is not None:
            self.scene_guard(registration_id)

    def mark_registrations_stale(self, reason: str | None = None) -> None:
        """`reason`: a message with `{id}` for the tools that refuse the registration (None: the move message)."""
        for registration in self.registrations.values():
            registration.mark_stale(reason)

    def open_board(self, board: Board, path: str, side_labels: SideLabels) -> None:
        """board_open: the user's side label choice, then the board."""
        board.apply_side_labels(side_labels)
        self.side_labels = side_labels
        self.use_board(board, path)

    def use_board(self, board: Board, path: str | None = None) -> None:
        """board_open: another board makes the part identity claims meaningless."""
        if self.board is None or self.board.sha256 != board.sha256:
            self.identities.clear()
        self.board = board
        if path is not None:
            self.board_path = str(Path(path).expanduser().absolute())
        self._restored = True

    # region: restart (board/session_store.py)

    def with_store(self, store: BoardSessionStore) -> BoardSession:
        self.store = store
        return self

    def save(self) -> None:
        """Write the open board and the registrations to the session record."""
        if self.board is None or self.board_path is None:
            return
        self.store.save(
            BoardSessionRecord(
                board_path=self.board_path,
                board_sha256=self.board.sha256,
                registrations=[registration.model_dump(mode="json") for registration in self.registrations.values()],
                last_registration_id=self.last_registration_id,
                side_labels=self.side_labels.value,
            )
        )

    async def restore(self) -> None:
        """After a server restart: open the recorded board again, one time. Its registrations come back stale."""
        if self._restored or self.board is not None:
            return
        self._restored = True
        record = self.store.load()
        if record.board_path is None:
            return
        try:
            board, loaded = await self.loader.load(Path(record.board_path))
        except BoardLoadError as exc:
            self.restore_note = f"the board of the last session ({record.board_path}) did not open again: {exc}"
            logger.warning("%s", self.restore_note)
            return
        self.side_labels = SideLabels(record.side_labels) if record.side_labels in set(SideLabels) else SideLabels.AUTO
        board.apply_side_labels(self.side_labels)
        self.board, self.board_path = board, record.board_path
        if loaded.sha256 != record.board_sha256:
            self.restore_note = "the board file changed since the last session: its photo registrations are dropped"
            return
        for data in record.registrations:
            try:
                registration = Registration.model_validate(data)
            except ValidationError:
                continue
            # A new process cannot check the scene of an old photo: register a fresh phone_snapshot.
            registration.mark_stale(RESTORED_STALE_REASON)
            self.registrations[registration.registration_id] = registration
        self.last_registration_id = record.last_registration_id
        self.restore_note = (
            f"opened {record.board_path} again after a server restart; the photo registrations are stale "
            "(take a fresh phone_snapshot and call board_register_photo)"
        )

    # endregion: restart

    @classmethod
    def create(cls, runner: CommandRunner, options: LoaderOptions) -> BoardSession:
        return cls(BoardviewLoader(runner, options))

    def current(self) -> Board:
        if self.board is None:
            raise ToolError("no board is open. Call board_open with the path of a boardview file first.")
        return self.board

    def registration(self, registration_id: str) -> Registration:
        registration = self.registrations.get(registration_id)
        if registration is None:
            raise ToolError(f"no registration {registration_id!r}. Call board_register_photo first.")
        if self.board is None or registration.board_sha256 != self.board.sha256:
            raise ToolError("that registration belongs to another board. Open that board, or register again.")
        if registration.stale:
            raise ToolError((registration.stale_reason or STALE_REGISTRATION).format(id=registration_id))
        return registration

    def part(self, refdes: str) -> Part:
        part = self.current().part(refdes)
        if part is None:
            raise ToolError(f"no part {refdes!r} on this board. Use board_find_part with a glob, for example 'U1*'.")
        return part


@contextmanager
def board_errors() -> Iterator[None]:
    try:
        yield
    except (BoardLoadError, RenderError) as exc:
        raise ToolError(str(exc)) from None


def summarize(board: Board, sha256: str, cached: bool, load_seconds: float) -> BoardSummary:
    sides = [part.side for part in board.parts.values()]
    return BoardSummary(
        path=board.path,
        format=board.format,
        sha256=sha256,
        cached=cached,
        load_seconds=round(load_seconds, 3),
        parts=len(board.parts),
        pins=sum(len(pins) for pins in board.pins_by_part.values()),
        nets=len(board.pins_by_net),
        nails=sum(1 for point in board.test_points if point.kind == "nail"),
        test_points=len(board.test_points),
        width_mm=board.bounds.width,
        height_mm=board.bounds.height,
        part_sides=SideCounts(top=sides.count(Side.TOP), bottom=sides.count(Side.BOTTOM), both=sides.count(Side.BOTH)),
        relabeled_both_sides=sum(1 for part in board.parts.values() if part.labeled_side is not None),
        side_warning=board.side_check.warning,
    )


def net_report(board: Board, net: str, limit: int) -> NetReport:
    pins = board.pins_by_net[net]
    numbers: dict[str, list[str]] = {}
    for pin in pins:
        numbers.setdefault(pin.part, []).append(pin.number)
    parts = []
    for name, pin_numbers in list(numbers.items())[:limit]:
        part = board.parts[name]
        nearest = board.nearest_test_point(part, net)
        parts.append(
            NetPart(
                name=name,
                side=part.side,
                center=part.center,
                pin_numbers=pin_numbers,
                nearest_test_point=(
                    TestPointDistance(test_point=nearest, distance_mm=nearest.position.distance(part.center))
                    if nearest
                    else None
                ),
            )
        )
    test_points = board.test_points_by_net.get(net, [])
    return NetReport(
        name=net,
        pin_count=len(pins),
        part_count=len(numbers),
        parts=parts,
        test_points=test_points[:limit],
        truncated=len(numbers) > limit or len(test_points) > limit,
    )


def register(session: BoardSession, side: Side, width: int, height: int, pairs: list[PhotoPair]) -> Registration:
    parts = [session.part(pair.refdes) for pair in pairs]
    board_mm = [(part.center.x, part.center.y) for part in parts]
    photo_px = [(pair.x_px, pair.y_px) for pair in pairs]
    try:
        fit = fit_homography(board_mm, photo_px)
        if fit.error_limit_px is not None and fit.max_error_px > fit.error_limit_px:
            suspect = likely_outlier(board_mm, photo_px)
            hint = (
                f"The most likely wrong pair is {parts[suspect].name}: the other pairs fit well without it."
                if suspect is not None
                else "Give 6 or more pairs, so that the wrong pair can be found."
            )
            raise ToolError(
                f"the pairs do not fit one flat board: largest error {fit.max_error_px:.1f} px (limit "
                f"{fit.error_limit_px:.1f} px). {hint} Check the pairs and the side ({side})."
            )
    except HomographyError as exc:
        raise ToolError(str(exc)) from None
    registration = Registration(
        registration_id=str(uuid.uuid7()),
        board_sha256=session.current().sha256,
        side=side,
        photo_width_px=width,
        photo_height_px=height,
        refdes=[part.name for part in parts],
        fit=fit,
        checked=fit.error_limit_px is not None,
    )
    session.registrations[registration.registration_id] = registration
    session.last_registration_id = registration.registration_id
    return registration


def locate(board: Board, registration: Registration, refdes: list[str], net: str | None) -> Locations:
    notes: list[str] = []
    parts = []
    for name in refdes:
        part = board.part(name)
        if part is None:
            notes.append(f"part {name!r}: not on this board")
            continue
        parts.append(part)
    pins = [pin for net_name in (board.net_names(net) if net else []) for pin in board.pins_by_net[net_name]]
    if net and not pins:
        notes.append(f"net {net!r}: no match")
    matrix = registration.fit.matrix
    part_px = map_points(matrix, [(part.center.x, part.center.y) for part in parts])
    pin_px = map_points(matrix, [(pin.position.x, pin.position.y) for pin in pins])

    def inside(x: float, y: float) -> bool:
        return 0 <= x < registration.photo_width_px and 0 <= y < registration.photo_height_px

    return Locations(
        registration_id=registration.registration_id,
        photo_width_px=registration.photo_width_px,
        photo_height_px=registration.photo_height_px,
        parts=[
            LocatedPart(
                name=part.name,
                side=part.side,
                x_px=round(x, 1),
                y_px=round(y, 1),
                in_photo=inside(x, y),
                on_registered_side=on_side(part.side, registration.side),
            )
            for part, (x, y) in zip(parts, part_px, strict=True)
        ],
        pins=[
            LocatedPin(
                part=pin.part, number=pin.number, net=pin.net, x_px=round(x, 1), y_px=round(y, 1), in_photo=inside(x, y)
            )
            for pin, (x, y) in zip(pins, pin_px, strict=True)
            if board.side_ok(pin.side, registration.side)
        ],
        annotated=False,
        notes=[
            *notes,
            *([board.side_check.warning] if board.side_check.warning else []),
            "positions are boardview estimates (identity: candidate); board_identify confirms a part",
        ],
    )


def part_boxes(board: Board, registration: Registration, locations: Locations) -> list[PixelBox]:
    """Pixel boxes around the located parts that are in the photo and on the registered side (at most 8)."""
    boxes = []
    for located in locations.parts:
        part = board.part(located.name)
        if part is None or not located.in_photo or not located.on_registered_side:
            continue
        box = part.box
        corners = [(box.min_x, box.min_y), (box.max_x, box.min_y), (box.max_x, box.max_y), (box.min_x, box.max_y)]
        xs, ys = zip(*map_points(registration.fit.matrix, corners), strict=True)
        width, height = max(max(xs) - min(xs), MIN_BOX_PX), max(max(ys) - min(ys), MIN_BOX_PX)
        label = located.name[: phone.OVERLAY_MAX_LABEL]
        boxes.append(PixelBox(x=min(xs), y=min(ys), width=width, height=height, label=label))
    return boxes[: phone.OVERLAY_MAX_BOXES]


def annotate_photo(photo: bytes, locations: Locations, max_side: int) -> bytes:
    """Draw circles and names on the photo. The photo can have another size than the registered one."""
    with PilImage.open(io.BytesIO(photo)) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    sx = image.width / locations.photo_width_px
    sy = image.height / locations.photo_height_px
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=MARK_FONT_SIZE)
    palette = colors.HIGHLIGHTS
    for index, part in enumerate(locations.parts):
        color = palette[index % len(palette)]
        x, y, r = part.x_px * sx, part.y_px * sy, MARK_RADIUS_PX
        draw.ellipse((x - r, y - r, x + r, y + r), outline=color, width=MARK_WIDTH_PX)
        draw.text((x + r, y - r), part.name, fill=color, font=font, anchor="lb")
    for pin in locations.pins:
        x, y, r = pin.x_px * sx, pin.y_px * sy, MARK_RADIUS_PX / 2
        draw.ellipse((x - r, y - r, x + r, y + r), outline=palette[-1], width=MARK_WIDTH_PX // 2)
    image.thumbnail((max_side, max_side), PilImage.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format=PIL_JPEG_FORMAT, quality=JPEG_QUALITY)
    return output.getvalue()


def read_photo(photo_path: str) -> bytes:
    return Path(photo_path).expanduser().read_bytes()


def board_tool(server: MCPServer, session: BoardSession) -> Callable[[ToolFunction], ToolFunction]:
    """`@server.tool()` for the board tools: restore the board after a restart first, and save the session after."""

    def decorate(function: ToolFunction) -> ToolFunction:
        @functools.wraps(function)
        async def wrapper(*args: object, **kwargs: object) -> object:
            await session.restore()
            try:
                return await function(*args, **kwargs)
            finally:
                session.save()

        server.tool()(wrapper)
        return wrapper  # type: ignore[return-value]

    return decorate


def register_board_tools(server: MCPServer, session: BoardSession) -> None:
    register_query_tools(server, session)
    register_photo_tools(server, session)
    register_identity_tools(server, session)


def register_query_tools(server: MCPServer, session: BoardSession) -> None:
    @board_tool(server, session)
    async def board_open(path: str, side_labels: SideLabels = SideLabels.AUTO) -> BoardSummary:
        """Load a boardview file (GenCAD .cad, .brd, .bvr, .fz, .pcb, and more) with obv-dump.

        The other board tools then use this board. Positions are in mm. A file that was loaded before comes from
        the cache (by SHA-256). Through-hole parts and mounting holes count as on both sides. `side_labels`: "auto"
        (a check warns when the top/bottom labels look mixed), "mixed" (the user says that the labels are not
        reliable: the tools warn instead of ruling parts out by side), or "trust". See `side_warning`.
        """
        with board_errors():
            board, loaded = await session.loader.load(Path(path))
        session.open_board(board, path, side_labels)
        return summarize(board, loaded.sha256, loaded.cached, loaded.load_seconds)

    @board_tool(server, session)
    async def board_find_part(query: str, limit: Limit = defaults.FIND_LIMIT) -> PartMatches:
        """Find parts by refdes ("U1", case does not matter), glob ("C1*", "U?"), or mfgcode/value text.

        Name matches come first, then mfgcode matches. Without a match, the parts whose name starts with the query
        (`match: "prefix"`, with a note). Each part has its side, center, box, pin count, and nets. Boardview data is
        supporting evidence: for a marking that you see on the board, use board_match_marking.
        """
        board = session.current()
        parts = board.find_parts(query)
        match, note = FindMatch.NAME_OR_MFGCODE, None
        if not parts:
            resolution, parts = find_marking_parts(board, query, None)
            if resolution is Resolution.PREFIX_CANDIDATES:
                match = FindMatch.PREFIX
                note = (
                    f"No part is named {query!r}. These parts start with it. If {query!r} is a marking that you see "
                    "on the board, call board_match_marking."
                )
            else:
                match, parts = FindMatch.NONE, []
        return PartMatches(
            query=query, match=match, total=len(parts), parts=parts[:limit], truncated=len(parts) > limit, note=note
        )

    @board_tool(server, session)
    async def board_part_pins(refdes: str) -> PartPins:
        """All pins of one part: number, name, net, position (mm), and side."""
        part = session.part(refdes)
        return PartPins(part=part, pins=session.current().pins_by_part.get(part.name, []))

    @board_tool(server, session)
    async def board_find_net(query: str, limit: Limit = defaults.FIND_LIMIT) -> NetMatches:
        """Find nets by name or glob ("PP3V3*", "*VCORE*"). For each net: the parts and pin numbers on it, its
        test points (nails and TP parts), and the nearest test point to each part.
        """
        board = session.current()
        names = board.net_names(query)
        if not names:
            raise ToolError(f"no net matches {query!r}. Use a glob, for example '*{query.strip('*')}*'.")
        nets = [net_report(board, net, limit) for net in names[:limit]]
        return NetMatches(query=query, total=len(names), nets=nets, truncated=len(names) > limit)

    @board_tool(server, session)
    async def board_parts_near(
        refdes: str | None = None,
        x_mm: float | None = None,
        y_mm: float | None = None,
        radius_mm: Annotated[float, Field(gt=0)] = defaults.NEAR_RADIUS_MM,
        side: Side | None = None,
        limit: Limit = defaults.FIND_LIMIT,
    ) -> NearParts:
        """Parts whose center is within `radius_mm` of a part (`refdes`) or of a point (`x_mm`, `y_mm`).

        Sorted by distance. `side` keeps only parts on that side (parts on "both" always count).
        """
        board = session.current()
        excluded = ""
        if refdes is not None:
            origin = session.part(refdes)
            center, excluded = origin.center, origin.name
        elif x_mm is not None and y_mm is not None:
            center = Point(x=x_mm, y=y_mm)
        else:
            raise ToolError("give `refdes`, or both `x_mm` and `y_mm`")
        near = board.parts_near(center, radius_mm, side, exclude=excluded)
        items = [PartDistance(part=part, distance_mm=part.center.distance(center)) for part in near]
        return NearParts(
            center=center, radius_mm=radius_mm, side=side, parts=items[:limit], truncated=len(items) > limit
        )

    @board_tool(server, session)
    async def board_render(
        side: Side | None = None,
        highlight_parts: list[str] | None = None,
        highlight_nets: list[str] | None = None,
        crop_to_part: str | None = None,
        max_side: Annotated[int, Field(ge=200)] = defaults.RENDER_MAX_SIDE,
        green: bool = False,
    ) -> Annotated[CallToolResult, RenderLegend]:
        """Draw one side of the board as a PNG: outline, part boxes, pins, and highlighted parts and nets.

        This is a drawing from the boardview file, not a photo. It is never proof of what is physically visible:
        use phone_snapshot for that.

        `side` "top" (default) or "bottom" (mirrored in X, as seen from below). With `crop_to_part` and no `side`,
        the side of that part. Nets accept globs. The legend gives the color of each highlight and the pixel
        position of each highlighted part. `green` draws every highlight in green (the phone highlight color).
        """
        board = session.current()
        if side is None:
            crop = board.part(crop_to_part) if crop_to_part else None
            side = crop.side if crop is not None and crop.side is not Side.BOTH else Side.TOP
        options = RenderOptions(
            side=side,
            highlight_parts=highlight_parts or [],
            highlight_nets=highlight_nets or [],
            crop_to_part=crop_to_part,
            max_side=max_side,
            green=green,
        )
        with board_errors():
            png, legend = await asyncio.to_thread(render_board, board, options)
        content = [
            TextContent(type="text", text=legend.model_dump_json()),
            Image(data=png, format=PNG_FORMAT).to_image_content(),
        ]
        return CallToolResult(content=content, structured_content=legend.model_dump(mode="json"))


def photo_to_registered(session: BoardSession, registration: Registration, photo_id: str | None) -> tuple[float, float]:
    """Scale factors from pixels of a phone_snapshot (`photo_id`; None: the latest) to pixels of the registered photo.

    Refuses a photo of another scene or camera view than the registered photo (register that photo), and a photo
    of another shape. Another size of the same view is the same photo scaled: the factors scale it (from the image
    sizes that the agent got for both photos).
    """
    if session.photos is None or registration.photo_id is None:
        # A registration of a photo that is not a phone_snapshot of this session: the old scene and view check.
        session.check_scene(registration.registration_id)
        return 1.0, 1.0
    problem = session.photos.reuse_problem(registration.photo_id, photo_id)
    if problem is not None:
        raise ToolError(f"registration {registration.registration_id}: {problem}")
    registered_size = session.photos.photo_size(registration.photo_id)
    if registered_size is None:
        return 1.0, 1.0
    return photo_scale(session.photos.photo_size(photo_id), registered_size)


def match_marking(
    session: BoardSession,
    marking: str,
    side: Side | None,
    registration_id: str | None,
    point: tuple[float, float] | None,
    try_rotations: bool = True,
    photo_id: str | None = None,
) -> MarkingMatch:
    board = session.current()
    lookup = lookup_marking(board, marking, side, try_rotations)
    resolution = lookup.resolution
    candidates = [candidate(part) for part in lookup.parts]
    ranked = registration_id is not None and point is not None and bool(candidates)
    if ranked:
        registration = session.registration(registration_id)
        # The positions and distances are in pixels of the agent's photo (the registered photo can have another size).
        scale_x, scale_y = photo_to_registered(session, registration, photo_id)
        pixels = map_points(registration.fit.matrix, [(item.center.x, item.center.y) for item in candidates])
        for item, (registered_x, registered_y) in zip(candidates, pixels, strict=True):
            x, y = registered_x / scale_x, registered_y / scale_y
            item.photo_position = PixelPosition(x_px=round(x, 1), y_px=round(y, 1))
            item.distance_px = math.hypot(x - point[0], y - point[1])
        candidates.sort(key=lambda item: item.distance_px or 0.0)
    best = candidates[0].refdes if resolution is Resolution.EXACT else None
    if ranked and resolution is not Resolution.EXACT:
        best = best_by_distance(candidates)
    names = [item.refdes for item in candidates[:MAX_CANDIDATES]]
    return MarkingMatch(
        visible_marking=marking,
        resolution=resolution,
        candidates=candidates[:MAX_CANDIDATES],
        total_candidates=len(candidates),
        best_candidate=best,
        message=reading_message(marking, lookup, message(marking, resolution, names, best, ranked)),
        reading=lookup.reading,
        rotated_reading=lookup.rotated_text,
        value_interpretations=lookup.value_interpretations,
    )


def register_photo_tools(server: MCPServer, session: BoardSession) -> None:
    tool = board_tool(server, session)

    @tool
    async def board_match_marking(
        marking: str,
        side: Side | None = None,
        registration_id: str | None = None,
        x_px: float | None = None,
        y_px: float | None = None,
        try_rotations: bool = True,
        photo_id: str | None = None,
    ) -> MarkingMatch:
        """Match a marking that you see on the board (silkscreen, from phone_snapshot) to boardview parts.

        Quote the marking exactly as you see it. The result says if it is an exact boardview part or only
        candidates: a start of a name ("U730" for U7301, U7302: cut-off or hidden silkscreen), a part of a name, or
        a match with O/0, I/1, S/5, B/8, Z/2, G/6 read wrong. Tell the user which one it is, and never replace the
        visible marking with a boardview name without saying so. With `registration_id` (board_register_photo) and
        `x_px`/`y_px` of the marking, the candidates are sorted by distance, and `best_candidate` is set only when
        one is clearly nearest. The pixels are of `photo_id` (default: your last phone_snapshot): the registered
        photo, or a phone_snapshot of the same scene and camera view (another size is scaled). With `try_rotations`
        (default), the marking is also read upside down
        ("00T" in a turned photo is "100"); `reading` says which reading matched, and `rotated_reading` gives the
        upright text. `value_interpretations` reads the marking as an SMD value code ("100" = 10 Ω, "4R7" = 4.7 Ω):
        an interpretation only, never a part name.
        """
        if (x_px is None) != (y_px is None):
            raise ToolError("give both `x_px` and `y_px`, or neither")
        point = (x_px, y_px) if x_px is not None and y_px is not None else None
        if point is not None and registration_id is None:
            raise ToolError("`x_px`/`y_px` need a `registration_id` from board_register_photo")
        return match_marking(session, marking, side, registration_id, point, try_rotations, photo_id)

    @tool
    async def board_register_photo(
        side: Side,
        photo_width_px: Annotated[int, Field(gt=0)],
        photo_height_px: Annotated[int, Field(gt=0)],
        pairs: list[PhotoPair],
    ) -> Registration:
        """Map the board to a photo of one side (for example a phone_snapshot) from 4 or more part pairs.

        Each pair is a part refdes and the pixel position of its center in the photo (origin top left). The size
        is the size of the photo that the pixels refer to. Use 5-6 large parts far apart (ICs, connectors). With
        5 or more pairs, a fit with a large error is refused. Returns the registration_id for board_locate_in_photo.
        After the board or the phone moves, a registration is stale: take a fresh phone_snapshot and register again.
        """
        session.check_scene()
        registration = register(session, side, photo_width_px, photo_height_px, pairs)
        if session.current_photo_id is not None:
            registration.photo_id = session.current_photo_id()
        if session.on_registered is not None:
            registration.tracking = await session.on_registered(registration)
        return registration

    @tool
    async def board_locate_in_photo(
        registration_id: str,
        refdes: list[str] | None = None,
        net: str | None = None,
        photo_path: str | None = None,
        max_side: Annotated[int, Field(ge=200)] = defaults.RENDER_MAX_SIDE,
        highlight: bool = False,
    ) -> Annotated[CallToolResult, Locations]:
        """Pixel positions of parts (`refdes`) and of the pins of a net (`net`, glob allowed) in the registered photo.

        With `photo_path` (for example a phone_snapshot save_path, any size of the same photo), the result also
        has the photo with circles on the parts and pins. Pins on the other side are left out.
        `highlight: true` also points to the parts on the phone screen and the monitor page (phone_point_to): a green
        box in view, an arrow outside it. Your last phone_snapshot must show the scene of the registered photo with
        the same camera view (zoom, lens, turn, flips; another size is fine), unless the live tracking follows the
        registration. The boxes are boardview estimates: say so, and clear them (phone_highlight clear) when done.
        """
        session.check_scene(registration_id)
        registration = session.registration(registration_id)
        if not refdes and not net:
            raise ToolError("give `refdes`, `net`, or both")
        locations = locate(session.current(), registration, refdes or [], net)
        content: list = [TextContent(type="text", text="")]
        if highlight:
            if session.highlighter is None:
                raise ToolError("highlight boxes need the phone tools of this server")
            names = [part.name for part in locations.parts][: phone.OVERLAY_MAX_BOXES]
            if not names:
                raise ToolError("no located part: nothing to highlight (a net alone has no boxes; give `refdes`)")
            boxes = part_boxes(session.current(), registration, locations)
            size = (registration.photo_width_px, registration.photo_height_px)
            locations.highlight, highlighted = await session.highlighter(registration_id, names, boxes, size)
            if highlighted is not None:
                content.append(Image(data=highlighted, format=JPEG_FORMAT).to_image_content())
        if photo_path:
            try:
                photo = await asyncio.to_thread(read_photo, photo_path)
                jpeg = await asyncio.to_thread(annotate_photo, photo, locations, max_side)
            except OSError as exc:
                raise ToolError(f"cannot read the photo {photo_path}: {exc}") from None
            locations.annotated = True
            content.append(Image(data=jpeg, format=JPEG_FORMAT).to_image_content())
        content[0] = TextContent(type="text", text=locations.model_dump_json())
        return CallToolResult(content=content, structured_content=locations.model_dump(mode="json"))


# region: part identity tools (board/identity.py)

PARTS_AT_EVIDENCE = (
    "supporting evidence: boardview positions mapped through the photo registration, not a visual fact "
    "(identity: candidate; board_identify confirms a part)"
)
DEFAULT_RADIUS_PX = 20.0
MAX_PARTS_AT = 10


class PartAtPhoto(BaseModel):
    refdes: str
    side: Side
    center: Point
    # Distance from the pointed board position to the part box (0 inside the box), and to the part center.
    distance_mm: Mm
    center_distance_mm: Mm
    inside_box: bool


class PartsAtPhoto(BaseModel):
    registration_id: str
    # The point as given, in pixels of `photo_id` (null: the last phone_snapshot).
    x_px: float
    y_px: float
    photo_id: str | None = None
    # Factor from those pixels to the pixels of the registered photo (1 for the same size).
    scale: float = 1.0
    board_point: Point
    radius_mm: Mm
    side: Side
    parts: list[PartAtPhoto]
    identity: IdentityState = IdentityState.CANDIDATE
    evidence: str = PARTS_AT_EVIDENCE
    # A mixed-side file: parts of both labels are listed (see board_open side_warning).
    side_warning: str | None = None


def box_distance(part: Part, point: Point) -> float:
    dx = max(part.box.min_x - point.x, 0.0, point.x - part.box.max_x)
    dy = max(part.box.min_y - point.y, 0.0, point.y - part.box.max_y)
    return math.hypot(dx, dy)


def parts_at_photo(
    board: Board, registration: Registration, x_px: float, y_px: float, radius_px: float
) -> PartsAtPhoto:
    """The inverse of board_locate_in_photo: a photo pixel to board mm, and the parts near it on that side."""
    x, y = unmap_point(registration.fit.matrix, (x_px, y_px))
    point = Point(x=x, y=y)
    edges = [
        unmap_point(registration.fit.matrix, (x_px + dx, y_px + dy)) for dx, dy in ((radius_px, 0), (0, radius_px))
    ]
    radius_mm = max(math.hypot(ex - x, ey - y) for ex, ey in edges)
    near = []
    for part in board.parts.values():
        if not board.side_ok(part.side, registration.side):
            continue
        distance = box_distance(part, point)
        if distance <= radius_mm:
            near.append(
                PartAtPhoto(
                    refdes=part.name,
                    side=part.side,
                    center=part.center,
                    distance_mm=distance,
                    center_distance_mm=part.center.distance(point),
                    inside_box=distance == 0,
                )
            )
    near.sort(key=lambda item: (item.distance_mm, item.center_distance_mm))
    return PartsAtPhoto(
        registration_id=registration.registration_id,
        x_px=x_px,
        y_px=y_px,
        board_point=point,
        radius_mm=radius_mm,
        side=registration.side,
        parts=near[:MAX_PARTS_AT],
        side_warning=board.side_check.warning,
    )


def registration_for_photo(
    session: BoardSession, registration_id: str, photo_id: str
) -> tuple[Registration, str | None]:
    """The registration for a claim about `photo_id`: this one, or the one that was carried over from it to that
    photo (after the board or the phone moved). Returns it and a note when it is a carried one."""
    registration = session.registrations.get(registration_id)
    current_id = registration_id
    # Follow the carry-overs (a stale registration, carried to a newer photo, possibly more than once).
    while registration is not None and registration.stale:
        carried = next((item for item in session.registrations.values() if item.carried_from == current_id), None)
        if carried is None:
            break
        registration, current_id = carried, carried.registration_id
    if current_id != registration_id and registration is not None and registration.photo_id == photo_id:
        return session.registration(current_id), f"registration {registration_id} carried over as {current_id}"
    return session.registration(registration_id), None


def register_identity_tools(server: MCPServer, session: BoardSession) -> None:
    tool = board_tool(server, session)

    @tool
    async def board_parts_at_photo(
        registration_id: str,
        x_px: float,
        y_px: float,
        radius_px: Annotated[float, Field(gt=0)] = DEFAULT_RADIUS_PX,
        photo_id: str | None = None,
    ) -> PartsAtPhoto:
        """Which boardview parts are at a pixel of the photo: the inverse of board_locate_in_photo.

        `x_px`/`y_px` (and `radius_px`) are in pixels of `photo_id` (default: your last phone_snapshot): the
        registered photo, or a phone_snapshot of the same scene with the same zoom, lens, and orientation (another
        size is scaled; another view is refused: register that photo). Returns the parts of the registered side
        within `radius_px` of that point, nearest first, with their distance in mm. This is supporting evidence
        (identity: candidate), never a visual fact: board_identify confirms a part.
        """
        registration = session.registration(registration_id)
        scale_x, scale_y = photo_to_registered(session, registration, photo_id)
        found = parts_at_photo(session.current(), registration, x_px * scale_x, y_px * scale_y, radius_px * scale_x)
        return found.model_copy(update={"x_px": x_px, "y_px": y_px, "photo_id": photo_id, "scale": scale_x})

    @tool
    async def board_identify(
        photo_id: str,
        marking: str | None = None,
        registration_id: str | None = None,
        x_px: float | None = None,
        y_px: float | None = None,
        user_confirmed: bool = False,
    ) -> IdentityClaim:
        """Decide which boardview part a place in a current phone_snapshot is: visible_marking, candidate, or confirmed.

        `photo_id` is the capture_id of a current phone_snapshot (a stale one is refused). `marking` is the text that
        the photo shows at that place, quoted exactly (null when there is none). `registration_id` (board_register_photo
        on this photo, 5+ pairs) and `x_px`/`y_px` (pixels of `photo_id`) give the position. The registration must be
        of this photo, or of a photo with the same scene and camera view (another size is scaled); a registration
        that was carried over to this photo is used for its old id. Confirmed needs a current photo, a valid checked
        registration, and a visual input: a visible marking read in this photo, or a unique landmark (one part at the
        point, no look-alike near) that the user confirmed (`user_confirmed: true`, only after the user said so).
        Without a visual input, a landmark stays a candidate. Look-alike parts (for example similar coils) stay
        candidates: ask for a closer photo. A part on the other board side cannot be confirmed. Never call a
        candidate a visual fact.
        """
        if (x_px is None) != (y_px is None):
            raise ToolError("give both `x_px` and `y_px`, or neither")
        if session.photos is not None:
            session.photos.require_current_photo(photo_id)
        point = (x_px, y_px) if x_px is not None and y_px is not None else None
        registration = None
        carried_note = None
        if registration_id is not None:
            if point is None:
                raise ToolError("a `registration_id` needs `x_px` and `y_px` of the part in that photo")
            registration, carried_note = registration_for_photo(session, registration_id, photo_id)
            scale_x, scale_y = photo_to_registered(session, registration, photo_id)
            point = (point[0] * scale_x, point[1] * scale_y)
        seen = VisualInput(marking=marking, user_confirmed=user_confirmed)
        claim = identify(session.current(), photo_id, registration, point, seen)
        # The claim keeps the point as given (pixels of `photo_id`).
        update: dict[str, object] = {"x_px": x_px, "y_px": y_px}
        if carried_note is not None:
            update["reason"] = f"{claim.reason} ({carried_note})"
        return session.identities.add(claim.model_copy(update=update))

    @tool
    async def board_identity() -> IdentityReport:
        """The part identity claims of this session, as they hold now.

        A confirmation holds only in its scene: after the board or the phone moved, it shows as a candidate again.
        """
        return session.identities.report(session.photos, session.registrations)  # type: ignore[arg-type]


# endregion: part identity tools
