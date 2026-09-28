"""The highlight layout of docs/overlay-layout.md ("Geometry"): tags, colours, badges, legend, leaders, inset.

One pure function, `layout()`. The annotated image of phone_highlight and the monitor page use it (the page asks
the server). The phone app has the same function in Kotlin; docs/overlay-layout-vectors.json holds the shared test
vectors that both must pass.
"""

import math
import string

from pydantic import BaseModel

COLOURS = ("#00E676", "#00E5FF", "#FFEA00", "#FF4081")
MIN_BOX_IMAGE = 32.0
MIN_BOX_PHONE_DP = 24.0
BADGE_BASE_WIDTH = 8.0
BADGE_CHAR_WIDTH = 9.0
BADGE_HEIGHT = 18.0
TAG_MAX = 3
LEGEND_PAD_TOTAL = 12.0
LEGEND_CHAR_WIDTH = 8.0
LEGEND_ROW_HEIGHT = 18.0
LEGEND_MARGIN = 8.0
CLEARANCE = 4.0
GAPS = (4.0, 12.0, 24.0, 40.0)
LEADER_OFFSET = 16.0
ARROW_INSET = 28.0
INSET_THRESHOLD = 0.03
INSET_SCALE = 3.0
INSET_MAX_WIDTH = 0.25
INSET_MARGIN = 8.0
DECIMALS = 2
TAG_LETTERS = string.ascii_uppercase


# region: models


class Rect(BaseModel):
    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    @property
    def cx(self) -> float:
        return self.x + self.width / 2

    @property
    def cy(self) -> float:
        return self.y + self.height / 2

    def grown(self, by: float) -> Rect:
        return Rect(x=self.x - by, y=self.y - by, width=self.width + 2 * by, height=self.height + 2 * by)

    def overlaps(self, other: Rect) -> bool:
        """More than 0 in both axes: touching edges do not overlap."""
        return min(self.right, other.right) - max(self.x, other.x) > 0 and (
            min(self.bottom, other.bottom) - max(self.y, other.y) > 0
        )

    def distance(self, other: Rect) -> float:
        dx = max(other.x - self.right, self.x - other.right, 0.0)
        dy = max(other.y - self.bottom, self.y - other.bottom, 0.0)
        return math.hypot(dx, dy)

    def inside(self, width: float, height: float) -> bool:
        return self.x >= 0 and self.y >= 0 and self.right <= width and self.bottom <= height

    def rounded(self) -> Rect:
        return Rect(
            x=round(self.x, DECIMALS),
            y=round(self.y, DECIMALS),
            width=round(self.width, DECIMALS),
            height=round(self.height, DECIMALS),
        )


class LayoutBox(BaseModel):
    """A box in view units (the real area; it is drawn at least `min_box` wide and high)."""

    x: float
    y: float
    width: float
    height: float
    label: str = ""
    tag: str | None = None


class LayoutArrow(BaseModel):
    angle_deg: float
    label: str = ""
    tag: str | None = None


class LayoutInput(BaseModel):
    width: float
    height: float
    min_box: float = MIN_BOX_IMAGE
    boxes: list[LayoutBox] = []
    arrows: list[LayoutArrow] = []
    inset: bool = True


class DrawnBox(BaseModel):
    tag: str
    colour: str
    label: str
    # The drawn rectangle (at least min_box); the real area is the input box.
    rect: Rect


class Badge(BaseModel):
    tag: str
    colour: str
    rect: Rect
    # True when no place next to the box was free: the badge is outside the cluster, with a leader line.
    outside: bool
    leader: tuple[float, float, float, float] | None = None


class LegendRow(BaseModel):
    tag: str
    label: str
    colour: str


class Legend(BaseModel):
    rect: Rect
    rows: list[LegendRow]
    # True when every corner covers a box: it is below the view (an extra strip).
    outside: bool


class ArrowMark(BaseModel):
    tag: str
    colour: str
    label: str
    angle_deg: float
    anchor: tuple[float, float]


class Inset(BaseModel):
    source: Rect
    dest: Rect
    scale: float


class Layout(BaseModel):
    boxes: list[DrawnBox]
    badges: list[Badge]
    legend: Legend | None
    arrows: list[ArrowMark]
    inset: Inset | None
    # The extra height below the view for a legend outside (0 when none).
    extra_height: float


# endregion: models


def assign_tags(given: list[str | None]) -> list[str]:
    """Given tags (cut to 3 characters) stay; the others get the first free letter A-Z, in order."""
    cut = [tag[:TAG_MAX] if tag else None for tag in given]
    used = {tag for tag in cut if tag}
    free = (letter for letter in TAG_LETTERS if letter not in used)
    return [tag if tag else next(free, "?") for tag in cut]


def drawn_rect(box: LayoutBox, min_box: float) -> Rect:
    width, height = max(box.width, min_box), max(box.height, min_box)
    cx, cy = box.x + box.width / 2, box.y + box.height / 2
    return Rect(x=cx - width / 2, y=cy - height / 2, width=width, height=height)


def badge_size(tag: str) -> tuple[float, float]:
    return BADGE_BASE_WIDTH + BADGE_CHAR_WIDTH * len(tag), BADGE_HEIGHT


def badge_candidates(box: Rect, width: float, height: float, gap: float) -> list[Rect]:
    """Right, left, above, below, above-right, above-left, below-right, below-left."""
    right, left = box.right + gap, box.x - gap - width
    above, below = box.y - gap - height, box.bottom + gap
    middle_x, middle_y = box.cx - width / 2, box.cy - height / 2
    points = [
        (right, middle_y),
        (left, middle_y),
        (middle_x, above),
        (middle_x, below),
        (right, above),
        (left, above),
        (right, below),
        (left, below),
    ]
    return [Rect(x=x, y=y, width=width, height=height) for x, y in points]


def place_legend(rows: list[LegendRow], drawn: list[Rect], view: tuple[float, float]) -> Legend | None:
    if not rows:
        return None
    longest = max(len(f"{row.tag}: {row.label}") for row in rows)
    width = LEGEND_PAD_TOTAL + LEGEND_CHAR_WIDTH * longest
    height = LEGEND_PAD_TOTAL + LEGEND_ROW_HEIGHT * len(rows)
    view_width, view_height = view
    corners = [
        Rect(x=LEGEND_MARGIN, y=LEGEND_MARGIN, width=width, height=height),
        Rect(x=view_width - LEGEND_MARGIN - width, y=LEGEND_MARGIN, width=width, height=height),
        Rect(x=LEGEND_MARGIN, y=view_height - LEGEND_MARGIN - height, width=width, height=height),
        Rect(x=view_width - LEGEND_MARGIN - width, y=view_height - LEGEND_MARGIN - height, width=width, height=height),
    ]
    if not drawn:
        return Legend(rect=corners[0], rows=rows, outside=False)
    # Farthest from every box first; equal distances keep the corner order (a stable sort).
    ranked = sorted(corners, key=lambda corner: -min(corner.distance(box) for box in drawn))
    for corner in ranked:
        if not any(corner.overlaps(box.grown(CLEARANCE)) for box in drawn):
            return Legend(rect=corner, rows=rows, outside=False)
    below = Rect(x=LEGEND_MARGIN, y=view_height + LEGEND_MARGIN, width=width, height=height)
    return Legend(rect=below, rows=rows, outside=True)


def ray_exit(center: tuple[float, float], direction: tuple[float, float], bounds: Rect) -> tuple[float, float]:
    """Where the ray from `center` (inside `bounds`) leaves `bounds`."""
    limits = []
    for value, low, high, step in (
        (center[0], bounds.x, bounds.right, direction[0]),
        (center[1], bounds.y, bounds.bottom, direction[1]),
    ):
        if step > 0:
            limits.append((high - value) / step)
        elif step < 0:
            limits.append((low - value) / step)
    t = min(limits) if limits else 0.0
    return center[0] + direction[0] * t, center[1] + direction[1] * t


def fallback_badge(box: Rect, cluster: Rect, size: tuple[float, float], view: tuple[float, float]) -> Badge:
    width, height = size
    dx, dy = box.cx - cluster.cx, box.cy - cluster.cy
    length = math.hypot(dx, dy)
    direction = (dx / length, dy / length) if length > 0 else (1.0, 0.0)
    px, py = ray_exit((cluster.cx, cluster.cy), direction, cluster)
    reach = LEADER_OFFSET + max(width, height) / 2
    cx, cy = px + direction[0] * reach, py + direction[1] * reach
    x = min(max(cx - width / 2, 0.0), view[0] - width)
    y = min(max(cy - height / 2, 0.0), view[1] - height)
    rect = Rect(x=x, y=y, width=width, height=height)
    near_x = min(max(rect.cx, box.x), box.right)
    near_y = min(max(rect.cy, box.y), box.bottom)
    return Badge(tag="", colour="", rect=rect, outside=True, leader=(rect.cx, rect.cy, near_x, near_y))


def place_badges(drawn: list[DrawnBox], legend: Legend | None, view: tuple[float, float]) -> list[Badge]:
    rects = [box.rect for box in drawn]
    grown = [rect.grown(CLEARANCE) for rect in rects]
    blockers = [legend.rect] if legend is not None and not legend.outside else []
    if not rects:
        return []
    cluster = bounds(rects)
    badges: list[Badge] = []
    for box in drawn:
        size = badge_size(box.tag)
        chosen: Badge | None = None
        for gap in GAPS:
            for candidate in badge_candidates(box.rect, size[0], size[1], gap):
                free = candidate.inside(*view) and not any(candidate.overlaps(other) for other in grown)
                free = free and not any(candidate.overlaps(badge.rect.grown(CLEARANCE)) for badge in badges)
                if free and not any(candidate.overlaps(block) for block in blockers):
                    chosen = Badge(tag=box.tag, colour=box.colour, rect=candidate, outside=False)
                    break
            if chosen is not None:
                break
        if chosen is None:
            chosen = fallback_badge(box.rect, cluster, size, view).model_copy(
                update={"tag": box.tag, "colour": box.colour}
            )
        badges.append(chosen)
    return badges


def bounds(rects: list[Rect]) -> Rect:
    left, top = min(rect.x for rect in rects), min(rect.y for rect in rects)
    right, bottom = max(rect.right for rect in rects), max(rect.bottom for rect in rects)
    return Rect(x=left, y=top, width=right - left, height=bottom - top)


def arrow_anchor(angle_deg: float, view: tuple[float, float]) -> tuple[float, float]:
    width, height = view
    center = (width / 2, height / 2)
    radians = math.radians(angle_deg)
    direction = (math.cos(radians), math.sin(radians))
    inner = Rect(x=ARROW_INSET, y=ARROW_INSET, width=width - 2 * ARROW_INSET, height=height - 2 * ARROW_INSET)
    return ray_exit(center, direction, inner)


def place_inset(boxes: list[LayoutBox], blockers: list[Rect], view: tuple[float, float]) -> Inset | None:
    width, height = view
    if not boxes or max(max(box.width, box.height) for box in boxes) >= INSET_THRESHOLD * width:
        return None
    area = bounds([Rect(x=box.x, y=box.y, width=box.width, height=box.height) for box in boxes])
    grow = max(area.width, area.height) / 2
    source = area.grown(grow)
    source_width, source_height = min(source.width, width), min(source.height, height)
    x = min(max(source.x, 0.0), width - source_width)
    y = min(max(source.y, 0.0), height - source_height)
    source = Rect(x=x, y=y, width=source_width, height=source_height)
    scale = min(INSET_SCALE, INSET_MAX_WIDTH * width / source.width)
    dest_width, dest_height = source.width * scale, source.height * scale
    corners = [
        (INSET_MARGIN, INSET_MARGIN),
        (width - INSET_MARGIN - dest_width, INSET_MARGIN),
        (INSET_MARGIN, height - INSET_MARGIN - dest_height),
        (width - INSET_MARGIN - dest_width, height - INSET_MARGIN - dest_height),
    ]
    for cx, cy in corners:
        dest = Rect(x=cx, y=cy, width=dest_width, height=dest_height)
        if dest.inside(width, height) and not any(dest.overlaps(block) for block in blockers):
            return Inset(source=source, dest=dest, scale=scale)
    return None


def layout(data: LayoutInput) -> Layout:
    """The layout of docs/overlay-layout.md for these boxes and arrows in this view (stable: no random choice)."""
    view = (data.width, data.height)
    tags = assign_tags([box.tag for box in data.boxes] + [arrow.tag for arrow in data.arrows])
    box_tags, arrow_tags = tags[: len(data.boxes)], tags[len(data.boxes) :]
    drawn = [
        DrawnBox(tag=tag, colour=COLOURS[index % len(COLOURS)], label=box.label, rect=drawn_rect(box, data.min_box))
        for index, (tag, box) in enumerate(zip(box_tags, data.boxes, strict=True))
    ]
    arrows = [
        ArrowMark(
            tag=tag,
            colour=COLOURS[(len(drawn) + index) % len(COLOURS)],
            label=arrow.label,
            angle_deg=arrow.angle_deg % 360,
            anchor=arrow_anchor(arrow.angle_deg, view),
        )
        for index, (tag, arrow) in enumerate(zip(arrow_tags, data.arrows, strict=True))
    ]
    rows = [LegendRow(tag=box.tag, label=box.label, colour=box.colour) for box in drawn] + [
        LegendRow(tag=arrow.tag, label=arrow.label, colour=arrow.colour) for arrow in arrows
    ]
    legend = place_legend(rows, [box.rect for box in drawn], view)
    badges = place_badges(drawn, legend, view)
    blockers = [box.rect.grown(CLEARANCE) for box in drawn] + [badge.rect for badge in badges]
    if legend is not None and not legend.outside:
        blockers.append(legend.rect)
    inset = place_inset(data.boxes, blockers, view) if data.inset else None
    extra = legend.rect.height + 2 * LEGEND_MARGIN if legend is not None and legend.outside else 0.0
    return rounded(Layout(boxes=drawn, badges=badges, legend=legend, arrows=arrows, inset=inset, extra_height=extra))


def rounded(result: Layout) -> Layout:
    def point(values: tuple[float, ...]) -> tuple[float, ...]:
        return tuple(round(value, DECIMALS) for value in values)

    return Layout(
        boxes=[box.model_copy(update={"rect": box.rect.rounded()}) for box in result.boxes],
        badges=[
            badge.model_copy(
                update={"rect": badge.rect.rounded(), "leader": point(badge.leader) if badge.leader else None}
            )
            for badge in result.badges
        ],
        legend=result.legend.model_copy(update={"rect": result.legend.rect.rounded()}) if result.legend else None,
        arrows=[arrow.model_copy(update={"anchor": point(arrow.anchor)}) for arrow in result.arrows],
        inset=(
            result.inset.model_copy(
                update={
                    "source": result.inset.source.rounded(),
                    "dest": result.inset.dest.rounded(),
                    "scale": round(result.inset.scale, DECIMALS),
                }
            )
            if result.inset
            else None
        ),
        extra_height=round(result.extra_height, DECIMALS),
    )
