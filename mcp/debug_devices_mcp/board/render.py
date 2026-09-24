"""Draw one side of a board as a PNG: outline, part boxes, pins, and highlighted parts and nets."""

import io
from itertools import cycle

from PIL import Image, ImageDraw, ImageFont
from pydantic import BaseModel, Field

from debug_devices_mcp.board.constants import defaults
from debug_devices_mcp.board.dump import Side
from debug_devices_mcp.board.model import Board, Box, Part, Point, on_side


class colors:
    BACKGROUND = (255, 255, 255)
    OUTLINE = (0, 0, 0)
    PART = (150, 150, 150)
    PART_OTHER_SIDE = (225, 225, 225)
    PIN = (120, 120, 120)
    LABEL = (60, 60, 60)
    LABEL_BACKGROUND = (255, 255, 255)
    # Distinct colors for highlighted parts and nets, in this order.
    HIGHLIGHTS = (
        (220, 20, 60),
        (0, 110, 220),
        (0, 160, 70),
        (230, 120, 0),
        (140, 50, 190),
        (0, 150, 160),
        (200, 0, 150),
        (120, 90, 0),
    )


class style:
    MARGIN_PX = 20
    OUTLINE_WIDTH = 3
    PART_WIDTH = 1
    HIGHLIGHT_WIDTH = 4
    # Pin dots grow with the zoom: this size in mm, but not below or above the pixel limits.
    PIN_RADIUS_MM = 0.2
    PIN_RADIUS_PX = (2, 8)
    HIGHLIGHT_PIN_RADIUS_PX = (5, 14)
    FONT_SIZE = 16
    # When a label does not fit in the part box at FONT_SIZE, this smaller size is tried.
    SMALL_FONT_SIZE = 11
    HIGHLIGHT_FONT_SIZE = 22
    # Crop to a part: the view is the part box plus this margin, and at least this size.
    CROP_MARGIN_MM = 4.0
    CROP_MIN_SIDE_MM = 15.0
    PNG_FORMAT = "PNG"
    IMAGE_MODE = "RGB"


class RenderOptions(BaseModel):
    side: Side = Side.TOP
    highlight_parts: list[str] = Field(default_factory=list)
    highlight_nets: list[str] = Field(default_factory=list)
    crop_to_part: str | None = None
    max_side: int = defaults.RENDER_MAX_SIDE


class PixelPoint(BaseModel):
    x: int
    y: int


class HighlightedPart(BaseModel):
    name: str
    found: bool
    color: str
    side: Side | None = None
    # False when the part is on the other side: the image shows it only as a faint box.
    visible: bool = False
    center_px: PixelPoint | None = None


class HighlightedNet(BaseModel):
    query: str
    nets: list[str]
    color: str
    pins_drawn: int


class RenderLegend(BaseModel):
    side: Side
    # True for the bottom side: the image is mirrored in X, as seen from below.
    mirrored_x: bool
    width_px: int
    height_px: int
    px_per_mm: float
    view_mm: Box
    parts_drawn: int
    labels_drawn: int
    highlighted_parts: list[HighlightedPart]
    highlighted_nets: list[HighlightedNet]
    notes: list[str]


class RenderError(Exception):
    """The render options do not fit the board."""


def hex_color(rgb: tuple[int, int, int]) -> str:
    return "#{:02x}{:02x}{:02x}".format(*rgb)


class _View:
    """Board mm to image px. Y goes down in the image. The bottom side is mirrored in X."""

    def __init__(self, box: Box, side: Side, max_side: int) -> None:
        self.box = box
        self.mirrored = side is Side.BOTTOM
        usable = max_side - 2 * style.MARGIN_PX
        self.scale = usable / max(box.width, box.height, 1e-6)
        self.width = round(box.width * self.scale) + 2 * style.MARGIN_PX
        self.height = round(box.height * self.scale) + 2 * style.MARGIN_PX

    def px(self, point: Point) -> tuple[float, float]:
        dx = self.box.max_x - point.x if self.mirrored else point.x - self.box.min_x
        return style.MARGIN_PX + dx * self.scale, style.MARGIN_PX + (self.box.max_y - point.y) * self.scale

    def rect(self, box: Box) -> tuple[float, float, float, float]:
        x1, y1 = self.px(Point(x=box.min_x, y=box.min_y))
        x2, y2 = self.px(Point(x=box.max_x, y=box.max_y))
        return min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2)


def _view_box(board: Board, options: RenderOptions) -> Box:
    if options.crop_to_part is None:
        return board.bounds
    part = board.part(options.crop_to_part)
    if part is None:
        raise RenderError(f"crop_to_part: no part {options.crop_to_part!r} on this board")
    half = max(max(part.box.width, part.box.height) / 2 + style.CROP_MARGIN_MM, style.CROP_MIN_SIDE_MM / 2)
    center = part.center
    return Box(min_x=center.x - half, min_y=center.y - half, max_x=center.x + half, max_y=center.y + half)


def _visible(part: Part, view: Box) -> bool:
    box = part.box
    return box.max_x >= view.min_x and box.min_x <= view.max_x and box.max_y >= view.min_y and box.min_y <= view.max_y


class _Renderer:
    def __init__(self, board: Board, options: RenderOptions) -> None:
        self.board = board
        self.options = options
        self.box = _view_box(board, options)
        self.view = _View(self.box, options.side, options.max_side)
        self.image = Image.new(style.IMAGE_MODE, (self.view.width, self.view.height), colors.BACKGROUND)
        self.draw = ImageDraw.Draw(self.image)
        self.font = ImageFont.load_default(size=style.FONT_SIZE)
        self.small_font = ImageFont.load_default(size=style.SMALL_FONT_SIZE)
        self.big_font = ImageFont.load_default(size=style.HIGHLIGHT_FONT_SIZE)
        self.palette = cycle(colors.HIGHLIGHTS)
        self.notes: list[str] = []
        self.labels = 0

    def dot(self, point: Point, limits: tuple[int, int], color: tuple[int, int, int]) -> None:
        low, high = limits
        radius = min(max(style.PIN_RADIUS_MM * self.view.scale, low), high)
        x, y = self.view.px(point)
        self.draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=color)

    def outline(self) -> None:
        for line in self.board.outline:
            self.draw.line([self.view.px(point) for point in line], fill=colors.OUTLINE, width=style.OUTLINE_WIDTH)

    def parts(self) -> int:
        parts = [part for part in self.board.parts.values() if _visible(part, self.box)]
        for part in parts:
            this_side = on_side(part.side, self.options.side)
            rect = self.view.rect(part.box)
            outline = colors.PART if this_side else colors.PART_OTHER_SIDE
            self.draw.rectangle(rect, outline=outline, width=style.PART_WIDTH)
            if this_side:
                for pin in self.board.pins_by_part.get(part.name, []):
                    self.dot(pin.position, style.PIN_RADIUS_PX, colors.PIN)
                self.label(part.name, rect)
        return len(parts)

    def label(self, text: str, rect: tuple[float, float, float, float]) -> None:
        """A label only when it fits in the part box (normal, then small font), so the image stays readable."""
        width, height = rect[2] - rect[0], rect[3] - rect[1]
        for font in (self.font, self.small_font):
            left, top, right, bottom = self.draw.textbbox((0, 0), text, font=font)
            if right - left <= width and bottom - top <= height:
                center = ((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2)
                background = self.draw.textbbox(center, text, font=font, anchor="mm")
                self.draw.rectangle(background, fill=colors.LABEL_BACKGROUND)
                self.draw.text(center, text, fill=colors.LABEL, font=font, anchor="mm")
                self.labels += 1
                return

    def net(self, query: str) -> HighlightedNet:
        color = next(self.palette)
        names = self.board.net_names(query)
        pins = [pin for net in names for pin in self.board.pins_by_net[net] if on_side(pin.side, self.options.side)]
        for pin in pins:
            self.dot(pin.position, style.HIGHLIGHT_PIN_RADIUS_PX, color)
        if not names:
            self.notes.append(f"net {query!r}: no match")
        return HighlightedNet(query=query, nets=names, color=hex_color(color), pins_drawn=len(pins))

    def part(self, name: str) -> HighlightedPart:
        color = next(self.palette)
        part = self.board.part(name)
        if part is None:
            self.notes.append(f"part {name!r}: not on this board")
            return HighlightedPart(name=name, found=False, color=hex_color(color))
        visible = on_side(part.side, self.options.side)
        rect = self.view.rect(part.box)
        self.draw.rectangle(rect, outline=color, width=style.HIGHLIGHT_WIDTH if visible else style.PART_WIDTH)
        if visible:
            corner = (rect[0], rect[1] - style.HIGHLIGHT_WIDTH)
            self.draw.rectangle(
                self.draw.textbbox(corner, part.name, font=self.big_font, anchor="lb"), fill=colors.LABEL_BACKGROUND
            )
            self.draw.text(corner, part.name, fill=color, font=self.big_font, anchor="lb")
        else:
            self.notes.append(f"part {part.name} is on the {part.side} side; render side={part.side} to see it")
        cx, cy = self.view.px(part.center)
        return HighlightedPart(
            name=part.name,
            found=True,
            color=hex_color(color),
            side=part.side,
            visible=visible,
            center_px=PixelPoint(x=round(cx), y=round(cy)),
        )

    def png(self) -> bytes:
        output = io.BytesIO()
        self.image.save(output, format=style.PNG_FORMAT, optimize=True)
        return output.getvalue()


def render_board(board: Board, options: RenderOptions) -> tuple[bytes, RenderLegend]:
    renderer = _Renderer(board, options)
    crop_part = board.part(options.crop_to_part) if options.crop_to_part else None
    if crop_part is not None and not on_side(crop_part.side, options.side):
        renderer.notes.append(f"crop part {crop_part.name} is on the {crop_part.side} side, not on {options.side}")
    renderer.outline()
    parts_drawn = renderer.parts()
    nets = [renderer.net(query) for query in options.highlight_nets]
    parts = [renderer.part(name) for name in options.highlight_parts]
    legend = RenderLegend(
        side=options.side,
        mirrored_x=renderer.view.mirrored,
        width_px=renderer.view.width,
        height_px=renderer.view.height,
        px_per_mm=round(renderer.view.scale, 3),
        view_mm=renderer.box,
        parts_drawn=parts_drawn,
        labels_drawn=renderer.labels,
        highlighted_parts=parts,
        highlighted_nets=nets,
        notes=renderer.notes,
    )
    return renderer.png(), legend
