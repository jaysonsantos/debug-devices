"""MCP tools for boardview files: open a board, find parts and nets, and draw it."""

import asyncio
import io
import math
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from enum import StrEnum
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from PIL import Image as PilImage
from PIL import ImageDraw, ImageFont, ImageOps
from pydantic import BaseModel, Field

from debug_devices_mcp.board.constants import defaults
from debug_devices_mcp.board.dump import BoardFormat, Side
from debug_devices_mcp.board.homography import Fit, HomographyError, fit_homography, likely_outlier, map_points
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
from debug_devices_mcp.board.model import Board, Mm, Part, Pin, Point, TestPoint, on_side
from debug_devices_mcp.board.render import RenderError, RenderLegend, RenderOptions, colors, render_board
from debug_devices_mcp.process import CommandRunner

PNG_FORMAT = "png"
JPEG_FORMAT = "jpeg"
PIL_JPEG_FORMAT = "JPEG"
JPEG_QUALITY = 85
MARK_RADIUS_PX = 18
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


# endregion: results


class BoardSession:
    """The loader and the board that board_open loaded last. The other board tools use that board."""

    def __init__(self, loader: BoardviewLoader) -> None:
        self.loader = loader
        self.board: Board | None = None
        self.registrations: dict[str, Registration] = {}

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
            if on_side(pin.side, registration.side)
        ],
        annotated=False,
        notes=notes,
    )


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


def register_board_tools(server: MCPServer, session: BoardSession) -> None:
    register_query_tools(server, session)
    register_photo_tools(server, session)


def register_query_tools(server: MCPServer, session: BoardSession) -> None:
    @server.tool()
    async def board_open(path: str) -> BoardSummary:
        """Load a boardview file (GenCAD .cad, .brd, .bvr, .fz, .pcb, and more) with obv-dump.

        The other board tools then use this board. Positions are in mm. A file that was loaded before comes from
        the cache (by SHA-256).
        """
        with board_errors():
            board, loaded = await session.loader.load(Path(path))
        session.board = board
        return summarize(board, loaded.sha256, loaded.cached, loaded.load_seconds)

    @server.tool()
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

    @server.tool()
    async def board_part_pins(refdes: str) -> PartPins:
        """All pins of one part: number, name, net, position (mm), and side."""
        part = session.part(refdes)
        return PartPins(part=part, pins=session.current().pins_by_part.get(part.name, []))

    @server.tool()
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

    @server.tool()
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

    @server.tool()
    async def board_render(
        side: Side | None = None,
        highlight_parts: list[str] | None = None,
        highlight_nets: list[str] | None = None,
        crop_to_part: str | None = None,
        max_side: Annotated[int, Field(ge=200)] = defaults.RENDER_MAX_SIDE,
    ) -> Annotated[CallToolResult, RenderLegend]:
        """Draw one side of the board as a PNG: outline, part boxes, pins, and highlighted parts and nets.

        This is a drawing from the boardview file, not a photo. It is never proof of what is physically visible:
        use phone_snapshot for that.

        `side` "top" (default) or "bottom" (mirrored in X, as seen from below). With `crop_to_part` and no `side`,
        the side of that part. Nets accept globs. The legend gives the color of each highlight and the pixel
        position of each highlighted part.
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
        )
        with board_errors():
            png, legend = await asyncio.to_thread(render_board, board, options)
        content = [
            TextContent(type="text", text=legend.model_dump_json()),
            Image(data=png, format=PNG_FORMAT).to_image_content(),
        ]
        return CallToolResult(content=content, structured_content=legend.model_dump(mode="json"))


def match_marking(
    session: BoardSession,
    marking: str,
    side: Side | None,
    registration_id: str | None,
    point: tuple[float, float] | None,
) -> MarkingMatch:
    board = session.current()
    resolution, parts = find_marking_parts(board, marking, side)
    candidates = [candidate(part) for part in parts]
    ranked = registration_id is not None and point is not None and bool(candidates)
    if ranked:
        registration = session.registration(registration_id)
        pixels = map_points(registration.fit.matrix, [(item.center.x, item.center.y) for item in candidates])
        for item, (x, y) in zip(candidates, pixels, strict=True):
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
        message=message(marking, resolution, names, best, ranked),
    )


def register_photo_tools(server: MCPServer, session: BoardSession) -> None:
    @server.tool()
    async def board_match_marking(
        marking: str,
        side: Side | None = None,
        registration_id: str | None = None,
        x_px: float | None = None,
        y_px: float | None = None,
    ) -> MarkingMatch:
        """Match a marking that you see on the board (silkscreen, from phone_snapshot) to boardview parts.

        Quote the marking exactly as you see it. The result says if it is an exact boardview part or only
        candidates: a start of a name ("U730" for U7301, U7302: cut-off or hidden silkscreen), a part of a name, or
        a match with O/0, I/1, S/5, B/8, Z/2, G/6 read wrong. Tell the user which one it is, and never replace the
        visible marking with a boardview name without saying so. With `registration_id` (board_register_photo) and
        `x_px`/`y_px` of the marking in that photo, the candidates are sorted by distance, and `best_candidate` is
        set only when one is clearly nearest.
        """
        if (x_px is None) != (y_px is None):
            raise ToolError("give both `x_px` and `y_px`, or neither")
        point = (x_px, y_px) if x_px is not None and y_px is not None else None
        if point is not None and registration_id is None:
            raise ToolError("`x_px`/`y_px` need a `registration_id` from board_register_photo")
        return match_marking(session, marking, side, registration_id, point)

    @server.tool()
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
        """
        return register(session, side, photo_width_px, photo_height_px, pairs)

    @server.tool()
    async def board_locate_in_photo(
        registration_id: str,
        refdes: list[str] | None = None,
        net: str | None = None,
        photo_path: str | None = None,
        max_side: Annotated[int, Field(ge=200)] = defaults.RENDER_MAX_SIDE,
    ) -> Annotated[CallToolResult, Locations]:
        """Pixel positions of parts (`refdes`) and of the pins of a net (`net`, glob allowed) in the registered photo.

        With `photo_path` (for example a phone_snapshot save_path, any size of the same photo), the result also
        has the photo with circles on the parts and pins. Pins on the other side are left out.
        """
        registration = session.registration(registration_id)
        if not refdes and not net:
            raise ToolError("give `refdes`, `net`, or both")
        locations = locate(session.current(), registration, refdes or [], net)
        content: list = [TextContent(type="text", text="")]
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
