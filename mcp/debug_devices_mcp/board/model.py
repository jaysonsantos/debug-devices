"""The board in mm, built from a `BoardDump`, with the queries that the tools use."""

import math
import re
from collections import defaultdict
from collections.abc import Iterable
from enum import StrEnum
from fnmatch import fnmatchcase
from typing import Annotated

from pydantic import BaseModel, PlainSerializer

from debug_devices_mcp.board.constants import PIN_BOX_MARGIN_MM, TEST_POINT_PREFIXES, sides
from debug_devices_mcp.board.dump import BoardDump, BoardFormat, DumpPart, DumpPin, MilPoint, Mounting, Side
from debug_devices_mcp.board.units import mil_to_mm

GLOB_CHARS = frozenset("*?[")
MOUNTING_HOLE = re.compile(sides.MOUNTING_HOLE_PATTERN)
MM_DECIMALS = 3

# A length in mm. Output keeps 3 decimals (1 µm), so JSON shows 13.97, not 13.969999999999999.
type Mm = Annotated[float, PlainSerializer(lambda value: round(value, MM_DECIMALS), return_type=float)]

# region: models


class Point(BaseModel):
    """A position in mm, in board coordinates (y up, as in the source file)."""

    x: Mm
    y: Mm

    def distance(self, other: Point) -> float:
        return math.hypot(self.x - other.x, self.y - other.y)


class Box(BaseModel):
    min_x: Mm
    min_y: Mm
    max_x: Mm
    max_y: Mm

    @property
    def center(self) -> Point:
        return Point(x=(self.min_x + self.max_x) / 2, y=(self.min_y + self.max_y) / 2)

    @property
    def width(self) -> float:
        return self.max_x - self.min_x

    @property
    def height(self) -> float:
        return self.max_y - self.min_y

    def grow(self, margin: float) -> Box:
        return Box(
            min_x=self.min_x - margin, min_y=self.min_y - margin, max_x=self.max_x + margin, max_y=self.max_y + margin
        )

    @classmethod
    def around(cls, points: list[Point]) -> Box:
        return cls(
            min_x=min(point.x for point in points),
            min_y=min(point.y for point in points),
            max_x=max(point.x for point in points),
            max_y=max(point.y for point in points),
        )


class Pin(BaseModel):
    part: str
    number: str
    name: str
    net: str
    position: Point
    side: Side
    radius_mm: Mm
    probe: int


class Part(BaseModel):
    name: str
    # The side to use: "both" for through-hole parts and mounting holes (visible from both sides).
    side: Side
    # The side label in the file, when it differs from `side`.
    labeled_side: Side | None = None
    mounting: Mounting
    center: Point
    box: Box
    # True when the file has no part box, so the box is the box around the pins.
    box_from_pins: bool
    rotation_deg: float | None
    mfgcode: str
    pin_count: int
    nets: list[str]


class TestPointKind(StrEnum):
    NAIL = "nail"
    PART = "part"


class TestPoint(BaseModel):
    kind: TestPointKind
    # Part name for a test point part, "nail <probe>" for a nail.
    name: str
    net: str
    position: Point
    side: Side


# endregion: models


def _point(point: MilPoint | DumpPin) -> Point:
    return Point(x=mil_to_mm(point.x), y=mil_to_mm(point.y))


def is_glob(query: str) -> bool:
    return any(char in GLOB_CHARS for char in query)


def matches(query: str, value: str) -> bool:
    """Exact or glob match, without case."""
    query, value = query.casefold(), value.casefold()
    return fnmatchcase(value, query) if is_glob(query) else value == query


def on_side(item_side: Side, side: Side | None) -> bool:
    return side is None or side is Side.BOTH or item_side in {side, Side.BOTH}


class SideLabels(StrEnum):
    # The heuristic decides (check_sides).
    AUTO = "auto"
    # The user knows that the labels are mixed: warn, and never rule a part out by its label.
    MIXED = "mixed"
    # The user trusts the labels: no warning.
    TRUST = "trust"


USER_MIXED_WARNING = (
    "the user said that the side labels of this board file are mixed: a part can be on the other physical side than "
    "its label. The tools warn, but they do not rule a part out by its side label. Check the side on the photo."
)


class SideCheck(BaseModel):
    """Do the side labels of the file look reliable?"""

    mixed: bool
    # Parts whose nearest same-type part (within sides.NEIGHBOUR_MM) has the opposite label, of all such pairs.
    opposite_pairs: int
    compared: int
    warning: str | None


MIXED_SIDES_WARNING = (
    "the side labels of this board file look mixed: {opposite} of {compared} neighbouring parts of the same type "
    "have opposite top/bottom labels. A part can be on the other physical side than its label: the tools warn, "
    "but they do not rule a part out by its side label. Check the side on the photo."
)


def _type_key(part: Part) -> tuple[str, int]:
    return "".join(char for char in part.name if char.isalpha()).upper(), part.pin_count


def check_sides(parts: Iterable[Part]) -> SideCheck:
    """Look for a mixed-side file: many near same-type neighbours with opposite top/bottom labels."""
    labeled = [part for part in parts if part.side is not Side.BOTH]
    cell = sides.NEIGHBOUR_MM
    grid: dict[tuple[int, int], list[Part]] = defaultdict(list)
    for part in labeled:
        grid[(int(part.center.x // cell), int(part.center.y // cell))].append(part)
    compared = opposite = 0
    for part in labeled:
        gx, gy = int(part.center.x // cell), int(part.center.y // cell)
        key = _type_key(part)
        neighbours = [
            other
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for other in grid.get((gx + dx, gy + dy), [])
            if other.name != part.name and _type_key(other) == key
        ]
        near = [other for other in neighbours if other.center.distance(part.center) <= sides.NEIGHBOUR_MM]
        if not near:
            continue
        compared += 1
        if min(near, key=lambda other: other.center.distance(part.center)).side is not part.side:
            opposite += 1
    mixed = opposite >= sides.MIN_OPPOSITE_PAIRS and opposite >= sides.MIN_OPPOSITE_FRACTION * compared
    warning = MIXED_SIDES_WARNING.format(opposite=opposite, compared=compared) if mixed else None
    return SideCheck(mixed=mixed, opposite_pairs=opposite, compared=compared, warning=warning)


class Board:
    """All positions are in mm. Part and net names keep their case; lookups ignore case."""

    def __init__(self, dump: BoardDump, sha256: str) -> None:
        self.sha256 = sha256
        self.path = dump.source.path
        self.format: BoardFormat = dump.source.format
        self.obv_version = dump.source.obv_version
        self.pins_by_part: dict[str, list[Pin]] = defaultdict(list)
        self.pins_by_net: dict[str, list[Pin]] = defaultdict(list)
        for raw in dump.pins:
            pin = Pin(
                part=raw.part,
                # Some formats (for example BRD2) have no pin numbers: number the pins in file order, as OBV does.
                number=raw.number or str(len(self.pins_by_part[raw.part]) + 1),
                name=raw.name,
                net=raw.net,
                position=_point(raw),
                side=raw.side,
                radius_mm=mil_to_mm(raw.radius),
                probe=raw.probe,
            )
            self.pins_by_part[raw.part].append(pin)
            if pin.net:
                self.pins_by_net[pin.net].append(pin)
        self.parts: dict[str, Part] = {raw.name: self._part(raw) for raw in dump.parts}
        self._parts_by_key = {name.casefold(): part for name, part in self.parts.items()}
        self._nets_by_key = {net.casefold(): net for net in self.pins_by_net}
        self.test_points: list[TestPoint] = [
            TestPoint(
                kind=TestPointKind.NAIL,
                name=f"nail {nail.probe}",
                net=nail.net,
                position=_point(nail),
                side=nail.side,
            )
            for nail in dump.nails
            if nail.net
        ]
        self.test_points += [
            TestPoint(kind=TestPointKind.PART, name=part.name, net=pin.net, position=pin.position, side=part.side)
            for part in self.parts.values()
            if part.name.upper().startswith(TEST_POINT_PREFIXES)
            for pin in self.pins_by_part[part.name]
            if pin.net
        ]
        self.test_points_by_net: dict[str, list[TestPoint]] = defaultdict(list)
        for point in self.test_points:
            self.test_points_by_net[point.net].append(point)
        self.outline: list[list[Point]] = self._outline(dump)
        self.bounds = self._bounds()
        self.side_check = check_sides(self.parts.values())

    @property
    def mixed_sides(self) -> bool:
        return self.side_check.mixed

    def apply_side_labels(self, choice: SideLabels) -> None:
        """The user's choice over the heuristic: "mixed" or "trust" replace it, "auto" computes it again."""
        check = check_sides(self.parts.values())
        if choice is SideLabels.MIXED:
            check = check.model_copy(update={"mixed": True, "warning": USER_MIXED_WARNING})
        elif choice is SideLabels.TRUST:
            check = check.model_copy(update={"mixed": False, "warning": None})
        self.side_check = check

    def side_ok(self, item_side: Side, side: Side | None) -> bool:
        """on_side, but a file with mixed side labels does not rule out a part by its label."""
        return self.mixed_sides or on_side(item_side, side)

    def _part(self, raw: DumpPart) -> Part:
        pins = self.pins_by_part.get(raw.name, [])
        file_box = Box.around([_point(raw.p1), _point(raw.p2)]) if raw.p1 is not None and raw.p2 is not None else None
        # Some files (for example GenCAD from converters) give p1 == p2: a point, not a box. Use the pins then.
        if file_box is not None and min(file_box.width, file_box.height) > 0:
            box = file_box
            from_pins = False
        elif pins:
            margin = max(PIN_BOX_MARGIN_MM, *(pin.radius_mm for pin in pins))
            box = Box.around([pin.position for pin in pins]).grow(margin)
            from_pins = True
        else:
            box = Box(min_x=0, min_y=0, max_x=0, max_y=0)
            from_pins = True
        both = raw.mounting is Mounting.THROUGH_HOLE or MOUNTING_HOLE.match(raw.name.upper()) is not None
        side = Side.BOTH if both else raw.side
        if side is Side.BOTH:
            for pin in pins:
                pin.side = Side.BOTH
        return Part(
            name=raw.name,
            side=side,
            labeled_side=raw.side if side is not raw.side else None,
            mounting=raw.mounting,
            center=box.center,
            box=box,
            box_from_pins=from_pins,
            rotation_deg=raw.rotation_deg,
            mfgcode=raw.mfgcode,
            pin_count=len(pins) or raw.pin_count,
            nets=sorted({pin.net for pin in pins if pin.net}),
        )

    @staticmethod
    def _outline(dump: BoardDump) -> list[list[Point]]:
        """Polylines: the closed outline polygon, then each segment."""
        lines: list[list[Point]] = []
        if dump.outline:
            polygon = [_point(point) for point in dump.outline]
            lines.append([*polygon, polygon[0]])
        lines += [[_point(segment.a), _point(segment.b)] for segment in dump.outline_segments]
        return lines

    def _bounds(self) -> Box:
        """The box around the outline and all pins. Part boxes can be larger than the board (connector bodies)."""
        points = [point for line in self.outline for point in line]
        points += [pin.position for pins in self.pins_by_part.values() for pin in pins]
        if not points:
            points = [part.center for part in self.parts.values()]
        return Box.around(points) if points else Box(min_x=0, min_y=0, max_x=0, max_y=0)

    # region: queries

    def part(self, refdes: str) -> Part | None:
        return self._parts_by_key.get(refdes.casefold())

    def net_names(self, query: str) -> list[str]:
        if not is_glob(query):
            net = self._nets_by_key.get(query.casefold())
            return [net] if net else []
        return sorted(net for net in self.pins_by_net if matches(query, net))

    def find_parts(self, query: str) -> list[Part]:
        """Parts whose name matches (exact or glob), then parts whose mfgcode matches (glob or substring)."""
        by_name = [part for part in self.parts.values() if matches(query, part.name)]
        if is_glob(query):
            by_code = [part for part in self.parts.values() if part.mfgcode and matches(query, part.mfgcode)]
        else:
            key = query.casefold()
            by_code = [part for part in self.parts.values() if part.mfgcode and key in part.mfgcode.casefold()]
        seen = {part.name for part in by_name}
        return by_name + [part for part in by_code if part.name not in seen]

    def nearest_test_point(self, part: Part, net: str) -> TestPoint | None:
        points = [point for point in self.test_points_by_net.get(net, []) if point.name != part.name]
        return min(points, key=lambda point: point.position.distance(part.center), default=None)

    def parts_near(self, center: Point, radius_mm: float, side: Side | None, exclude: str = "") -> list[Part]:
        near = [
            part
            for part in self.parts.values()
            if part.name != exclude and self.side_ok(part.side, side) and part.center.distance(center) <= radius_mm
        ]
        return sorted(near, key=lambda part: part.center.distance(center))

    # endregion: queries
