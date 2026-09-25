"""The Board panel of the monitor page: open, names, search, highlight, register (synthetic markings.json board)."""

from datetime import timedelta
from pathlib import Path
from uuid import uuid7

import anyio
import pytest
from starlette.testclient import TestClient

from debug_devices_mcp.board.loader import LoaderOptions
from debug_devices_mcp.board.tools import BoardSession
from debug_devices_mcp.config import Settings
from debug_devices_mcp.process import CommandResult
from debug_devices_mcp.server import Services, build_server
from debug_devices_mcp.ui.app import create_app
from debug_devices_mcp.ui.board import NO_REGISTRATION, OTHER_SIDE, STALE_REGISTRATION, BoardPanel
from debug_devices_mcp.ui.events import CallSource, CallStatus, ToolCallEvent
from debug_devices_mcp.ui.monitor import Monitor, MonitorOptions
from debug_devices_mcp.ui.settings import EffectiveSettings, SettingsStore

from .conftest import FakeRunner, make_jpeg
from .test_board import fixture
from .test_board_marking import board, to_photo
from .test_highlight import OverlayPhone
from .test_server import make_services, no_vision

START = EffectiveSettings(vision_model="m", webcam_warmup_frames=0, webcam_crop=None)
BASE_URL = "http://127.0.0.1:18766"
# The fake photo of test_board_marking: 1200x1000 px (below the snapshot max_side, so the agent sees this size).
PHOTO_WIDTH, PHOTO_HEIGHT = 1200, 1000
REFERENCE_PARTS = ("J4", "Q12", "TP9", "C8850", "R10")


def reference_points() -> list[dict]:
    parts = board().parts
    points = []
    for name in REFERENCE_PARTS:
        x, y = to_photo(parts[name].center.x, parts[name].center.y)
        points.append({"refdes": name, "x": x / PHOTO_WIDTH, "y": y / PHOTO_HEIGHT})
    return points


class Bench:
    def __init__(self, settings: Settings, tmp_path: Path) -> None:
        self.phone = OverlayPhone()
        self.phone.snapshot = make_jpeg(PHOTO_WIDTH, PHOTO_HEIGHT)
        self.services: Services = make_services(settings, self.phone, no_vision())
        runner = FakeRunner(lambda _: CommandResult(returncode=0, stdout=fixture("markings.json"), stderr=b""))
        self.services.board = BoardSession.create(
            runner, LoaderOptions(dump_bin="obv-dump", timeout=timedelta(seconds=5))
        )
        self.monitor = Monitor(START, SettingsStore.in_dir(tmp_path), MonitorOptions(open_browser=False, port=0))
        self.monitor.instrument(build_server(self.services))
        self.services.scene.add_listener(self.monitor.scene_changed)
        self.monitor.board_panel = BoardPanel(self.services, self.monitor.call_from_ui)
        self.client = TestClient(create_app(self.monitor), base_url=BASE_URL)
        self.board_file = tmp_path / "markings.cad"
        self.board_file.write_text("the fake obv-dump does not read this")

    def open(self) -> dict:
        response = self.client.post("/api/board/open", json={"path": str(self.board_file)})
        assert response.status_code == 200, response.text
        return response.json()

    def search(self, query: str) -> dict:
        response = self.client.post("/api/board/search", json={"query": query})
        assert response.status_code == 200, response.text
        return response.json()

    def register(self) -> dict:
        assert self.client.post("/api/phone/snapshot").status_code == 200
        response = self.client.post("/api/board/register", json={"side": "top", "points": reference_points()})
        assert response.status_code == 200, response.text
        return response.json()


@pytest.fixture
def bench(settings: Settings, tmp_path: Path) -> Bench:
    return Bench(settings, tmp_path)


def test_open_and_names(bench: Bench) -> None:
    assert bench.client.get("/api/board").json()["summary"] is None
    assert bench.client.get("/api/board/names").status_code == 409
    assert bench.client.post("/api/board/search", json={"query": "U7301"}).status_code == 502
    view = bench.open()
    assert view["summary"]["parts"] == len(board().parts)
    names = bench.client.get("/api/board/names").json()
    assert "U7301" in names["parts"]
    assert "PP_SYN_1V0" in names["nets"]
    calls = [(call.tool, call.source) for call in bench.monitor.bus.calls()]
    assert calls == [("board_open", CallSource.UI)]


def test_search_a_part_without_a_registration(bench: Bench) -> None:
    bench.open()
    result = bench.search("u7301")
    assert result["kind"] == "part"
    assert result["part"]["name"] == "U7301"
    assert result["part"]["side"] == "top"
    assert result["part"]["pin_count"] == 8
    assert "PP_SYN_1V0" in result["part"]["nets"]
    assert result["highlight"] == {"shown": False, "message": NO_REGISTRATION}
    image = bench.client.get(f"/api/calls/{result['render_call_id']}/images/0")
    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/")
    tools = [call.tool for call in bench.monitor.bus.calls()]
    assert tools == ["board_open", "board_find_part", "board_render"]
    [render] = [call for call in bench.monitor.bus.calls() if call.tool == "board_render"]
    assert render.arguments["green"] is True


def test_register_then_highlight_and_the_other_side(bench: Bench) -> None:
    bench.open()
    registration = bench.register()
    assert registration["side"] == "top"
    assert registration["checked"] is True
    assert registration["max_error_px"] < 1
    assert bench.client.get("/api/board").json()["registration"]["refdes"] == list(REFERENCE_PARTS)

    shown = bench.search("U7301")
    assert shown["highlight"]["shown"] is True, shown["highlight"]
    [box] = bench.phone.sent[-1]
    assert box["label"] == "U7301"
    assert len(bench.monitor.bus.phone.highlights) == 1

    other = bench.search("U7303")
    assert other["highlight"] == {"shown": False, "message": OTHER_SIDE.format(side="bottom")}
    assert other["render_call_id"]


def test_search_a_net(bench: Bench) -> None:
    bench.open()
    bench.register()
    result = bench.search("PP_SYN_1V0")
    assert result["kind"] == "net"
    assert result["net"]["name"] == "PP_SYN_1V0"
    assert "U7301" in result["net"]["parts"]
    assert len(result["net"]["parts"]) == result["net"]["part_count"]
    assert result["highlight"]["shown"] is True
    labels = {box["label"] for box in bench.phone.sent[-1]}
    assert "U7301" in labels
    assert "U7303" not in labels  # bottom side: not on the registered photo


def test_a_moved_board_needs_a_new_registration(bench: Bench) -> None:
    bench.open()
    bench.register()
    anyio.run(bench.services.scene.mark_changed)
    assert bench.client.get("/api/board").json()["registration"]["stale"] is True
    result = bench.search("U7301")
    assert result["highlight"] == {"shown": False, "message": STALE_REGISTRATION}
    assert result["render_call_id"]  # the board drawing stays
    again = bench.register()
    assert again["stale"] is False
    assert bench.search("U7301")["highlight"]["shown"] is True


def test_register_needs_four_points_and_a_snapshot(bench: Bench) -> None:
    bench.open()
    points = reference_points()
    assert bench.client.post("/api/board/register", json={"side": "top", "points": points[:3]}).status_code == 400
    no_snapshot = bench.client.post("/api/board/register", json={"side": "top", "points": points})
    assert no_snapshot.status_code == 502
    assert "snapshot" in no_snapshot.json()["error"]


def test_a_board_of_another_server_is_offered(bench: Bench) -> None:
    event = ToolCallEvent(
        id=uuid7(),
        tool="board_open",
        source=CallSource.MCP,
        arguments={"path": "/boards/other.cad"},
        started_at="2026-09-25T10:00:00Z",
        duration_ms=12.0,
        status=CallStatus.OK,
        summary="",
        error=None,
        details={},
        images=[],
    )
    bench.monitor.bus.ingest(event, "codex 4242")
    assert bench.client.get("/api/board").json()["remote"] == {"path": "/boards/other.cad", "origin": "codex 4242"}
