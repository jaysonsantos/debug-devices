"""Draw the highlight layout (docs/overlay-layout.md) on an image: the annotated image of phone_highlight.

The positions come from `overlay_layout.layout()`; this module only paints them: a dark outline under each
colour outline (contrast on any board colour), badges with the tags, leader lines, the legend (in a strip below the
image when every corner covers a box), and the inset (the area around small boxes, enlarged).
"""

import io

from PIL import Image as PilImage
from PIL import ImageDraw, ImageFont

from debug_devices_mcp.constants import images
from debug_devices_mcp.overlay_layout import (
    MIN_BOX_IMAGE,
    Layout,
    LayoutArrow,
    LayoutBox,
    LayoutInput,
    Rect,
    layout,
)

COLOUR_WIDTH = 3
# The dark outline: 2 px wider on each side than the colour outline, black at 60% opacity.
DARK_WIDTH = COLOUR_WIDTH + 4
DARK = (0, 0, 0, 153)
LEGEND_FILL = (0, 0, 0, 217)
BADGE_FILL = (0, 0, 0, 230)
STRIP_FILL = (24, 24, 24)
INSET_BORDER = (255, 255, 255)
BADGE_FONT_SIZE = 14
LEGEND_FONT_SIZE = 13
TEXT_PAD = 3
LEGEND_TEXT_PAD = 6
LEGEND_ROW = 18
LEADER_WIDTH = 2
ARROW_RADIUS = 10
ANNOTATED_QUALITY = 90


def corners(rect: Rect) -> tuple[float, float, float, float]:
    return rect.x, rect.y, rect.right, rect.bottom


def outline(draw: ImageDraw.ImageDraw, rect: Rect, colour: str) -> None:
    draw.rectangle(corners(rect), outline=DARK, width=DARK_WIDTH)
    draw.rectangle(corners(rect), outline=colour, width=COLOUR_WIDTH)


def paint(image: PilImage.Image, result: Layout) -> None:
    """Paint the layout on an RGBA image (the legend strip is already part of it when the legend is outside)."""
    draw = ImageDraw.Draw(image, "RGBA")
    badge_font = ImageFont.load_default(size=BADGE_FONT_SIZE)
    legend_font = ImageFont.load_default(size=LEGEND_FONT_SIZE)
    for box in result.boxes:
        outline(draw, box.rect, box.colour)
    for badge in result.badges:
        if badge.leader is not None:
            draw.line(badge.leader, fill=DARK, width=LEADER_WIDTH + 2)
            draw.line(badge.leader, fill=badge.colour, width=LEADER_WIDTH)
        draw.rectangle(corners(badge.rect), fill=BADGE_FILL, outline=badge.colour, width=1)
        draw.text((badge.rect.x + TEXT_PAD + 1, badge.rect.y + 1), badge.tag, fill=badge.colour, font=badge_font)
    for arrow in result.arrows:
        x, y = arrow.anchor
        ring = (x - ARROW_RADIUS, y - ARROW_RADIUS, x + ARROW_RADIUS, y + ARROW_RADIUS)
        draw.ellipse(ring, outline=DARK, width=DARK_WIDTH)
        draw.ellipse(ring, outline=arrow.colour, width=COLOUR_WIDTH)
        draw.text((x + ARROW_RADIUS + 2, y - ARROW_RADIUS), arrow.tag, fill=arrow.colour, font=badge_font)
    if result.legend is not None:
        legend = result.legend
        draw.rectangle(corners(legend.rect), fill=LEGEND_FILL)
        for index, row in enumerate(legend.rows):
            position = (legend.rect.x + LEGEND_TEXT_PAD, legend.rect.y + LEGEND_TEXT_PAD + LEGEND_ROW * index)
            draw.text(position, f"{row.tag}: {row.label}", fill=row.colour, font=legend_font)


def paint_inset(image: PilImage.Image, source: PilImage.Image, result: Layout) -> None:
    """The area around the small boxes, enlarged, with the same outlines (rule 7)."""
    inset = result.inset
    if inset is None:
        return
    crop = source.crop(
        (round(inset.source.x), round(inset.source.y), round(inset.source.right), round(inset.source.bottom))
    )
    enlarged = crop.resize((round(inset.dest.width), round(inset.dest.height)))
    image.paste(enlarged, (round(inset.dest.x), round(inset.dest.y)))
    draw = ImageDraw.Draw(image, "RGBA")
    draw.rectangle(corners(inset.dest), outline=INSET_BORDER, width=1)
    for box in result.boxes:
        rect = Rect(
            x=inset.dest.x + (box.rect.x - inset.source.x) * inset.scale,
            y=inset.dest.y + (box.rect.y - inset.source.y) * inset.scale,
            width=box.rect.width * inset.scale,
            height=box.rect.height * inset.scale,
        )
        inside = rect.x >= inset.dest.x and rect.y >= inset.dest.y
        if inside and rect.right <= inset.dest.right and rect.bottom <= inset.dest.bottom:
            outline(draw, rect, box.colour)


def draw_layout(jpeg: bytes, boxes: list[LayoutBox], arrows: list[LayoutArrow] | None = None) -> tuple[bytes, Layout]:
    """The annotated image and its layout (boxes in pixels of this image)."""
    with PilImage.open(io.BytesIO(jpeg)) as opened:
        source = opened.convert("RGBA")
    data = LayoutInput(
        width=source.width, height=source.height, min_box=MIN_BOX_IMAGE, boxes=boxes, arrows=arrows or [], inset=True
    )
    result = layout(data)
    image = PilImage.new("RGBA", (source.width, source.height + round(result.extra_height)), STRIP_FILL)
    image.paste(source, (0, 0))
    paint_inset(image, source, result)
    paint(image, result)
    output = io.BytesIO()
    image.convert(images.JPEG_MODE).save(output, format=images.PIL_JPEG_FORMAT, quality=ANNOTATED_QUALITY)
    return output.getvalue(), result
