"""schematic_find: find a part name (refdes) or a net name in the user's schematic PDF, with poppler.

Local only: the PDF, its text, and the crops stay on this PC and go only to the MCP client as the tool result. The
server never sends them to OpenRouter or any other web service. A schematic from a repair site is proprietary: never
copy it, or parts of it, into the repository.

`pdftotext -bbox` gives every word with its box (points, origin at the top left of the page). `pdftoppm` renders a
crop around a hit. Both come from poppler-utils in flake.nix.
"""

import asyncio
import io
import re
from dataclasses import dataclass, field
from datetime import timedelta
from enum import StrEnum
from pathlib import Path
from typing import Annotated
from xml.etree import ElementTree

from mcp.server.mcpserver import Image, MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult, TextContent
from PIL import Image as PilImage
from PIL import ImageDraw
from pydantic import BaseModel, Field

from debug_devices_mcp.process import CommandError, CommandRunner

# region: constants


class env:
    SCHEMATIC = "DEBUG_DEVICES_SCHEMATIC"


class defaults:
    PDFTOTEXT = "pdftotext"
    PDFTOPPM = "pdftoppm"
    TIMEOUT = timedelta(seconds=60)
    MAX_RESULTS = 20
    # The crop images: for the first hits only, each with this margin around the text.
    CROP_HITS = 3
    MARGIN_PT = 72.0
    CROP_DPI = 150
    # Words around a hit (inside the crop area) that the result lists as nearby text.
    NEARBY_WORDS = 40


MAX_RESULTS_LIMIT = 200
POINTS_PER_INCH = 72.0
PNG_FORMAT = "png"
HIT_OUTLINE_RGB = (230, 0, 0)
HIT_OUTLINE_PX = 3
# Characters that join several names in one word, for example "U7301,U7302" or "PP3V3(S5)".
TOKEN_SEPARATORS = re.compile(r"[,;:/()\[\]{}<>=]+")
XHTML_WORD = "{http://www.w3.org/1999/xhtml}word"
XHTML_PAGE = "{http://www.w3.org/1999/xhtml}page"
# Rows for the nearby text: words whose tops are this close are on one row.
ROW_TOLERANCE_PT = 3.0

NOT_SET_MESSAGE = (
    f"No schematic is set. Set {env.SCHEMATIC}=/path/to/schematic.pdf in the .env file at the repo root (or start "
    "the server with --schematic /path/to/schematic.pdf), then restart the MCP server. The PDF stays local."
)

# endregion: constants

# region: models


class MatchKind(StrEnum):
    # The whole word is the query (case does not count).
    EXACT = "exact"
    # The query is one name in a joined word, for example U7302 in "U7301,U7302".
    TOKEN = "token"
    # The query is only a part of a word (for example R19 in R190). Listed only when no exact or token hit exists.
    CONTAINS = "contains"


class PdfBox(BaseModel):
    """A box in PDF points, origin at the top left of the page, y down (as pdftotext -bbox gives it)."""

    x_min: float
    y_min: float
    x_max: float
    y_max: float

    def grown(self, margin: float, width: float, height: float) -> PdfBox:
        return PdfBox(
            x_min=max(0.0, self.x_min - margin),
            y_min=max(0.0, self.y_min - margin),
            x_max=min(width, self.x_max + margin),
            y_max=min(height, self.y_max + margin),
        )

    def contains_center_of(self, other: PdfBox) -> bool:
        x = (other.x_min + other.x_max) / 2
        y = (other.y_min + other.y_max) / 2
        return self.x_min <= x <= self.x_max and self.y_min <= y <= self.y_max


class CropInfo(BaseModel):
    # The area of the page in the crop image (points), and the image resolution. The hit has a red outline.
    area: PdfBox
    margin_pt: float
    dpi: int
    image_index: int


class SchematicHit(BaseModel):
    page: int
    text: str
    match: MatchKind
    box: PdfBox
    page_width_pt: float
    page_height_pt: float
    # The other words in the crop area, in reading order (rows from the top, left to right).
    nearby_text: str
    crop: CropInfo | None = None


class PageHits(BaseModel):
    page: int
    hits: int


class SchematicFindResult(BaseModel):
    schematic: str
    query: str
    total_hits: int
    # Pages with hits of the returned match kind, in page order.
    pages: list[PageHits]
    hits: list[SchematicHit]
    # Hits where the query is only a part of a word, when exact or token hits exist (they are not listed).
    other_contains_hits: int
    message: str
    local_only: str = "The schematic and the crops stay local. The server never sends them to a web service."


# endregion: models

# region: index


@dataclass
class Word:
    text: str
    key: str
    box: PdfBox


@dataclass
class Page:
    number: int
    width: float
    height: float
    words: list[Word] = field(default_factory=list)


def parse_bbox_xhtml(data: bytes) -> list[Page]:
    """Pages and words from `pdftotext -bbox` output (XHTML)."""
    root = ElementTree.fromstring(data)
    pages: list[Page] = []
    for number, element in enumerate(root.iter(XHTML_PAGE), start=1):
        page = Page(number, float(element.get("width", "0")), float(element.get("height", "0")))
        for word in element.iter(XHTML_WORD):
            text = (word.text or "").strip()
            if not text:
                continue
            box = PdfBox(
                x_min=float(word.get("xMin", "0")),
                y_min=float(word.get("yMin", "0")),
                x_max=float(word.get("xMax", "0")),
                y_max=float(word.get("yMax", "0")),
            )
            page.words.append(Word(text, text.casefold(), box))
        pages.append(page)
    return pages


def match_kind(word_key: str, query_key: str) -> MatchKind | None:
    if word_key == query_key:
        return MatchKind.EXACT
    if query_key in TOKEN_SEPARATORS.split(word_key):
        return MatchKind.TOKEN
    if query_key in word_key:
        return MatchKind.CONTAINS
    return None


def nearby_text(page: Page, hit: Word, area: PdfBox, limit: int) -> str:
    words = [word for word in page.words if word is not hit and area.contains_center_of(word.box)]
    words.sort(key=lambda word: (round(word.box.y_min / ROW_TOLERANCE_PT), word.box.x_min))
    return " ".join(word.text for word in words[:limit])


# endregion: index


class SchematicError(Exception):
    """No schematic, a missing file, or a poppler failure. The message says what to do."""


class SchematicOptions(BaseModel):
    path: Path | None = None
    pdftotext: str = defaults.PDFTOTEXT
    pdftoppm: str = defaults.PDFTOPPM
    timeout: timedelta = defaults.TIMEOUT
    crop_hits: int = defaults.CROP_HITS
    margin_pt: float = defaults.MARGIN_PT
    crop_dpi: int = defaults.CROP_DPI
    nearby_words: int = defaults.NEARBY_WORDS


@dataclass
class SchematicFinder:
    runner: CommandRunner
    options: SchematicOptions
    # The word index of the PDF, and the (size, mtime) of the file that it came from.
    _index: list[Page] | None = field(default=None, init=False)
    _index_key: tuple[int, int] | None = field(default=None, init=False)

    def note(self) -> str | None:
        """For bench_instructions: which schematic schematic_find reads. None when none is set."""
        path = self.options.path
        if path is None:
            return None
        state = "" if path.expanduser().is_file() else " The file does not exist now."
        return f"Schematic for schematic_find: {path} (local only).{state}"

    def schematic_path(self) -> Path:
        if self.options.path is None:
            raise SchematicError(NOT_SET_MESSAGE)
        path = self.options.path.expanduser()
        if not path.is_file():
            raise SchematicError(f"the schematic {path} does not exist. Check {env.SCHEMATIC} (or --schematic).")
        return path

    async def run(self, args: list[str]) -> bytes:
        try:
            result = await self.runner.run(args, self.options.timeout)
        except CommandError as exc:
            raise SchematicError(f"{exc}. Is poppler-utils installed (the dev shell has it)?") from exc
        if not result.ok:
            error = result.stderr.decode(errors="replace").strip()
            raise SchematicError(f"{args[0]} failed (exit {result.returncode}): {error}")
        return result.stdout

    async def pages(self) -> list[Page]:
        path = self.schematic_path()
        stat = path.stat()
        key = (stat.st_size, stat.st_mtime_ns)
        if self._index is None or self._index_key != key:
            data = await self.run([self.options.pdftotext, "-bbox", str(path), "-"])
            try:
                self._index = await asyncio.to_thread(parse_bbox_xhtml, data)
            except ElementTree.ParseError as exc:
                raise SchematicError(f"cannot read the text positions of {path}: {exc}") from exc
            self._index_key = key
        return self._index

    async def crop(self, page: Page, area: PdfBox, hit: PdfBox) -> bytes:
        """A PNG of `area`, with a red outline around the hit."""
        scale = self.options.crop_dpi / POINTS_PER_INCH
        x, y = round(area.x_min * scale), round(area.y_min * scale)
        width, height = round((area.x_max - area.x_min) * scale), round((area.y_max - area.y_min) * scale)
        args = [self.options.pdftoppm, "-f", str(page.number), "-l", str(page.number)]
        args += ["-r", str(self.options.crop_dpi), "-x", str(x), "-y", str(y), "-W", str(width), "-H", str(height)]
        args += ["-png", "-singlefile", str(self.schematic_path())]
        png = await self.run(args)
        outline = (
            (hit.x_min - area.x_min) * scale,
            (hit.y_min - area.y_min) * scale,
            (hit.x_max - area.x_min) * scale,
            (hit.y_max - area.y_min) * scale,
        )
        return await asyncio.to_thread(draw_outline, png, outline)

    async def find(self, query: str, max_results: int) -> tuple[SchematicFindResult, list[bytes]]:
        query_key = query.strip().casefold()
        if not query_key or any(char.isspace() for char in query_key):
            raise SchematicError("give one part name or one net name, without spaces (for example U7301 or PP3V3_S5)")
        path = self.schematic_path()
        found: dict[MatchKind, list[tuple[Page, Word]]] = {kind: [] for kind in MatchKind}
        for page in await self.pages():
            for word in page.words:
                kind = match_kind(word.key, query_key)
                if kind is not None:
                    found[kind].append((page, word))
        strong = found[MatchKind.EXACT] + found[MatchKind.TOKEN]
        listed = strong or found[MatchKind.CONTAINS]
        other_contains = len(found[MatchKind.CONTAINS]) if strong else 0
        pages: dict[int, int] = {}
        for page, _ in listed:
            pages[page.number] = pages.get(page.number, 0) + 1
        hits: list[SchematicHit] = []
        images: list[bytes] = []
        for page, word in listed[:max_results]:
            area = word.box.grown(self.options.margin_pt, page.width, page.height)
            hit = SchematicHit(
                page=page.number,
                text=word.text,
                match=match_kind(word.key, query_key) or MatchKind.CONTAINS,
                box=word.box,
                page_width_pt=page.width,
                page_height_pt=page.height,
                nearby_text=nearby_text(page, word, area, self.options.nearby_words),
            )
            if len(images) < self.options.crop_hits:
                images.append(await self.crop(page, area, word.box))
                hit.crop = CropInfo(
                    area=area, margin_pt=self.options.margin_pt, dpi=self.options.crop_dpi, image_index=len(images) - 1
                )
            hits.append(hit)
        result = SchematicFindResult(
            schematic=str(path),
            query=query,
            total_hits=len(listed),
            pages=[PageHits(page=number, hits=count) for number, count in sorted(pages.items())],
            hits=hits,
            other_contains_hits=other_contains,
            message=find_message(query, listed, strong, len(hits), len(images)),
        )
        return result, images


def draw_outline(png: bytes, box: tuple[float, float, float, float]) -> bytes:
    with PilImage.open(io.BytesIO(png)) as source:
        image = source.convert("RGB")
    ImageDraw.Draw(image).rectangle(box, outline=HIT_OUTLINE_RGB, width=HIT_OUTLINE_PX)
    output = io.BytesIO()
    image.save(output, format=PNG_FORMAT.upper())
    return output.getvalue()


def find_message(
    query: str, listed: list[tuple[Page, Word]], strong: list[tuple[Page, Word]], shown: int, crops: int
) -> str:
    if not listed:
        return (
            f"{query!r} is not in the text of the schematic. Check the spelling, or try a part of the name. "
            "A scanned PDF (only images) has no text: then this tool cannot find anything."
        )
    kind = "as a whole name" if strong else "only as a part of a word"
    pages = sorted({page.number for page, _ in listed})
    return (
        f"{query!r} found {len(listed)} times ({kind}) on pages {', '.join(map(str, pages))}. "
        f"Listed: {shown}; crop images: {crops} (the hit has a red outline). The schematic is supporting evidence: "
        "confirm on the board with phone_snapshot and the meter."
    )


def register_schematic_tools(server: MCPServer, finder: SchematicFinder) -> None:
    @server.tool()
    async def schematic_find(
        query: str,
        max_results: Annotated[int, Field(ge=1, le=MAX_RESULTS_LIMIT)] = defaults.MAX_RESULTS,
    ) -> Annotated[CallToolResult, SchematicFindResult]:
        """Find a part name (refdes, for example U7301) or a net name (for example PP3V3_S5) in the schematic PDF.

        Returns the pages with hits, the text around each hit, and crop images of the first hits (the hit has a red
        outline). Exact words and names in joined words ("U7301,U7302") come first; hits inside longer words (R19 in
        R190) are listed only when there is no exact hit. The schematic path comes from DEBUG_DEVICES_SCHEMATIC
        (or --schematic); without it, the tool says how to set it. Local only: the PDF and the crops never go to a
        web service. The schematic is supporting evidence, like boardview: confirm on the board.
        """
        try:
            result, images = await finder.find(query, max_results)
        except SchematicError as exc:
            raise ToolError(str(exc)) from exc
        content = [TextContent(type="text", text=result.model_dump_json())]
        content += [Image(data=png, format=PNG_FORMAT).to_image_content() for png in images]
        return CallToolResult(content=content, structured_content=result.model_dump(mode="json"))
