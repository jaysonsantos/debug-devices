"""The Board panel of the monitor page: open a board, search a part or a net, highlight it on the camera, and
register the snapshot. Every step runs the MCP tools through the instrumented call (source ui), so the log shows it.
"""

from enum import StrEnum
from typing import Any, Protocol

from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult
from pydantic import BaseModel, Field

from debug_devices_mcp.board.dump import Side
from debug_devices_mcp.board.model import Board
from debug_devices_mcp.board.tools import BoardSession, BoardSummary, Registration
from debug_devices_mcp.constants import phone
from debug_devices_mcp.focus import SnapshotGeometry
from debug_devices_mcp.orientation import OrientationState
from debug_devices_mcp.pointer import OTHER_SIDE_MESSAGE, PointResult
from debug_devices_mcp.ui.constants import tools
from debug_devices_mcp.ui.events import CallStatus

# How many search results and net parts the panel shows.
SEARCH_LIMIT = 5
NET_PARTS_SHOWN = 12
NETS_SHOWN = 6
# The drawing of a searched part or net on the page.
RENDER_MAX_SIDE = 900

NO_BOARD = "open a board first"
NO_REGISTRATION = "register the photo first: 4 reference parts"
STALE_REGISTRATION = "the board moved: register again"
# pointer.OTHER_SIDE_MESSAGE: no arrow for a part on the other board side (evidence rule 7).
OTHER_SIDE = OTHER_SIDE_MESSAGE
OTHER_BOARD = "the registration is for another board: register again"
NO_SNAPSHOT = "take a phone snapshot first"
ORIENTATION_CHANGED = "the snapshot flips changed after the snapshot: take a new snapshot"
# The words of the scene-change refusal of the tools (scene.SCENE_CHANGED_MESSAGE).
SCENE_MOVED = "moved since your last phone_snapshot"


class BoardAccess(Protocol):
    """What the Board panel reads from the MCP server (`Services`)."""

    board: BoardSession
    last_snapshot: SnapshotGeometry | None
    orientation: OrientationState


class SearchKind(StrEnum):
    PART = "part"
    NET = "net"
    NONE = "none"


# region: views


class RegistrationView(BaseModel):
    registration_id: str
    side: Side
    refdes: list[str]
    rms_error_px: float
    max_error_px: float
    checked: bool
    stale: bool


class RemoteBoard(BaseModel):
    """A board that another MCP server opened (its call came to this page)."""

    path: str
    origin: str


class BoardView(BaseModel):
    summary: BoardSummary | None
    remote: RemoteBoard | None
    registration: RegistrationView | None
    has_snapshot: bool


class BoardNames(BaseModel):
    parts: list[str]
    nets: list[str]


class OpenBody(BaseModel):
    path: str = Field(min_length=1)


class SearchBody(BaseModel):
    query: str = Field(min_length=1)


class RegisterPoint(BaseModel):
    """A reference part and its center on the shown snapshot, from 0 to 1 (as the page shows it: with the flips)."""

    refdes: str = Field(min_length=1)
    x: float = Field(ge=0, le=1)
    y: float = Field(ge=0, le=1)


class RegisterBody(BaseModel):
    side: Side
    points: list[RegisterPoint] = Field(min_length=4)


class PartFacts(BaseModel):
    name: str
    side: Side
    x_mm: float
    y_mm: float
    pin_count: int
    mfgcode: str
    nets: list[str]
    nets_total: int


class NetFacts(BaseModel):
    name: str
    part_count: int
    pin_count: int
    parts: list[str]
    test_points: list[str]


class Highlight(BaseModel):
    shown: bool
    message: str
    # True when the live tracking moves the boxes and arrows with the board.
    tracking: bool = False


class SearchView(BaseModel):
    query: str
    kind: SearchKind
    message: str | None = None
    part: PartFacts | None = None
    net: NetFacts | None = None
    # Other matches (a glob or a prefix search).
    others: list[str] = []
    # The board drawing: the image of this logged call.
    render_call_id: str | None = None
    highlight: Highlight | None = None


# endregion: views


def registration_view(registration: Registration) -> RegistrationView:
    return RegistrationView(
        registration_id=registration.registration_id,
        side=registration.side,
        refdes=registration.refdes,
        rms_error_px=round(registration.fit.rms_error_px, 2),
        max_error_px=round(registration.fit.max_error_px, 2),
        checked=registration.checked,
        stale=registration.stale,
    )


def latest_registration(session: BoardSession) -> Registration | None:
    if session.last_registration_id is None:
        return None
    return session.registrations.get(session.last_registration_id)


def board_names(board: Board) -> BoardNames:
    return BoardNames(parts=sorted(board.parts), nets=sorted(board.pins_by_net))


def search_kind(board: Board, query: str) -> SearchKind:
    """A part name (or a part glob) is a part; a net name or glob without such parts is a net."""
    if board.part(query) is not None or board.find_parts(query):
        return SearchKind.PART
    return SearchKind.NET if board.net_names(query) else SearchKind.PART


def highlight_blocker(access: BoardAccess) -> str | None:
    """Why the searched item cannot get a box or an arrow on the camera now, or None."""
    registration = latest_registration(access.board)
    board = access.board.board
    if registration is None:
        return NO_REGISTRATION
    if board is None or registration.board_sha256 != board.sha256:
        return OTHER_BOARD
    if registration.stale:
        return STALE_REGISTRATION
    return None


def pointing_message(result: PointResult) -> str:
    """One line per target, for example "J4: outside the view: follow the arrow, about ~4 cm"."""
    lines = [f"{target.refdes}: {target.message}" for target in result.targets]
    if result.tracking:
        lines.append("live tracking: the boxes and arrows follow the board")
    return " · ".join(lines)


class BoardPanel:
    """The page actions. `call` runs a tool for the page and logs it (Monitor.call_from_ui)."""

    def __init__(self, access: BoardAccess, call: Any) -> None:
        self._access = access
        self._call = call
        # The result of the last board_open of this server (the page or the agent). The monitor sets it.
        self.summary: BoardSummary | None = None

    @property
    def session(self) -> BoardSession:
        return self._access.board

    def view(self, remote: RemoteBoard | None) -> BoardView:
        board, registration = self.session.board, latest_registration(self.session)
        loaded = self.summary is not None and board is not None and self.summary.sha256 == board.sha256
        return BoardView(
            summary=self.summary if loaded else None,
            remote=remote,
            registration=registration_view(registration) if registration is not None else None,
            has_snapshot=self._access.last_snapshot is not None,
        )

    async def run(self, tool: str, arguments: dict[str, Any]) -> tuple[Any, CallToolResult]:
        """Run a tool. A tool failure raises ToolError with its message."""
        call, result = await self._call(tool, arguments)
        if result.is_error or call.status is CallStatus.ERROR:
            raise ToolError(call.error or call.summary)
        return call, result

    async def open(self, path: str) -> None:
        _, result = await self.run(tools.BOARD_OPEN, {"path": path})
        self.summary = BoardSummary.model_validate(result.structured_content)

    async def search(self, query: str) -> SearchView:
        board = self.session.board
        if board is None:
            raise ToolError(NO_BOARD)
        query = query.strip()
        if search_kind(board, query) is SearchKind.NET:
            return await self._search_net(query)
        return await self._search_part(query)

    async def _search_part(self, query: str) -> SearchView:
        _, found = await self.run(tools.BOARD_FIND_PART, {"query": query, "limit": SEARCH_LIMIT})
        matches = found.structured_content or {}
        parts = matches.get("parts", [])
        if not parts:
            return SearchView(query=query, kind=SearchKind.NONE, message=f"no part or net matches {query!r}")
        first = parts[0]
        facts = PartFacts(
            name=first["name"],
            side=first["side"],
            x_mm=round(first["center"]["x"], 2),
            y_mm=round(first["center"]["y"], 2),
            pin_count=first["pin_count"],
            mfgcode=first["mfgcode"],
            nets=first["nets"][:NETS_SHOWN],
            nets_total=len(first["nets"]),
        )
        render, _ = await self.run(
            tools.BOARD_RENDER,
            {"crop_to_part": facts.name, "highlight_parts": [facts.name], "green": True, "max_side": RENDER_MAX_SIDE},
        )
        highlight = await self._highlight([facts.name])
        return SearchView(
            query=query,
            kind=SearchKind.PART,
            message=matches.get("note"),
            part=facts,
            others=[part["name"] for part in parts[1:]],
            render_call_id=str(render.id),
            highlight=highlight,
        )

    async def _search_net(self, query: str) -> SearchView:
        # The limit also applies to the parts of each net.
        _, found = await self.run(tools.BOARD_FIND_NET, {"query": query, "limit": NET_PARTS_SHOWN})
        report = (found.structured_content or {})["nets"][0]
        parts = report["parts"]
        top = sum(part["side"] != Side.BOTTOM for part in parts)
        side = Side.TOP if top * 2 >= len(parts) else Side.BOTTOM
        facts = NetFacts(
            name=report["name"],
            part_count=report["part_count"],
            pin_count=report["pin_count"],
            parts=[part["name"] for part in parts[:NET_PARTS_SHOWN]],
            test_points=[point["name"] for point in report["test_points"]][:NET_PARTS_SHOWN],
        )
        render, _ = await self.run(
            tools.BOARD_RENDER,
            {"side": side, "highlight_nets": [facts.name], "green": True, "max_side": RENDER_MAX_SIDE},
        )
        registration = latest_registration(self.session)
        # The parts on the registered side first: boxes and arrows for them, the note for the others.
        on_side = [part["name"] for part in parts if registration and part["side"] in {registration.side, Side.BOTH}]
        others = [part["name"] for part in parts if part["name"] not in on_side]
        highlight = await self._highlight((on_side + others)[: phone.OVERLAY_MAX_BOXES])
        return SearchView(
            query=query, kind=SearchKind.NET, net=facts, render_call_id=str(render.id), highlight=highlight
        )

    async def _highlight(self, refdes: list[str]) -> Highlight:
        """phone_point_to: a green box in view, an arrow toward a part outside it, on the phone and the page."""
        blocker = highlight_blocker(self._access)
        if blocker is not None:
            return Highlight(shown=False, message=blocker)
        registration = latest_registration(self.session)
        assert registration is not None
        arguments = {"refdes": refdes, "registration_id": registration.registration_id}
        try:
            _, result = await self.run(tools.PHONE_POINT_TO, arguments)
        except ToolError as exc:
            text = str(exc)
            return Highlight(shown=False, message=STALE_REGISTRATION if SCENE_MOVED in text else text)
        pointed = PointResult.model_validate(result.structured_content)
        shown = bool(pointed.boxes or pointed.arrows)
        return Highlight(shown=shown, message=pointing_message(pointed), tracking=pointed.tracking)

    async def register(self, body: RegisterBody) -> RegistrationView:
        """Register the last snapshot from points that the user clicked on the page snapshot."""
        geometry = self._access.last_snapshot
        if geometry is None:
            raise ToolError(NO_SNAPSHOT)
        if geometry.orientation != self._access.orientation.current:
            raise ToolError(ORIENTATION_CHANGED)
        pairs = [
            {"refdes": point.refdes, "x_px": point.x * geometry.width, "y_px": point.y * geometry.height}
            for point in body.points
        ]
        arguments = {
            "side": body.side,
            "photo_width_px": geometry.width,
            "photo_height_px": geometry.height,
            "pairs": pairs,
        }
        _, result = await self.run(tools.BOARD_REGISTER_PHOTO, arguments)
        return registration_view(Registration.model_validate(result.structured_content))
