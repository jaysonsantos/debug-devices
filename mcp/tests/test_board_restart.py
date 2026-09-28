"""The board session over a server restart (board/session_store.py): the open board and the registrations.

Two servers share one session file in tmp_path; the second one is the server after the restart.
Fixture: mcp/tests/fixtures/boardview/identity.json (synthetic; all names are invented).
"""

import json
from datetime import timedelta
from pathlib import Path

from mcp import Client
from mcp.types import TextContent

from debug_devices_mcp.board.loader import LoaderOptions
from debug_devices_mcp.board.session_store import BoardSessionStore
from debug_devices_mcp.board.tools import BoardSession
from debug_devices_mcp.config import Settings
from debug_devices_mcp.process import CommandResult
from debug_devices_mcp.server import Services, build_server

from .conftest import FakeRunner
from .test_board import fixture
from .test_board_identity import SIZE, pairs, photo_id
from .test_server import FakePhone, make_services, no_vision


class Process:
    """One MCP server process with its own services, and the shared session file."""

    def __init__(self, settings: Settings, store_path: Path, dump: str = "identity.json") -> None:
        self.runner = FakeRunner(lambda _: CommandResult(returncode=0, stdout=fixture(dump), stderr=b""))
        options = LoaderOptions(dump_bin="obv-dump", timeout=timedelta(seconds=5))
        self.services: Services = make_services(settings, FakePhone(), no_vision())
        self.services.board = BoardSession.create(self.runner, options).with_store(BoardSessionStore(store_path))
        self.server = build_server(self.services)


def text(result: object) -> str:
    return next(block for block in result.content if isinstance(block, TextContent)).text  # type: ignore[attr-defined]


async def before_restart(settings: Settings, tmp_path: Path) -> tuple[Path, str]:
    """Open the board and register a photo in the first process. Returns the session file and the registration."""
    store_path = tmp_path / "state" / "board-session.json"
    board_file = tmp_path / "board.cad"
    board_file.write_text("the fake obv-dump does not read this")
    first = Process(settings, store_path)
    async with Client(first.server) as client:
        await client.call_tool("board_open", {"path": str(board_file)})
        photo_id(await client.call_tool("phone_snapshot", {}))
        registered = await client.call_tool("board_register_photo", {"side": "top", "pairs": pairs(), **SIZE})
    assert registered.structured_content is not None
    return store_path, registered.structured_content["registration_id"]


async def test_board_tools_work_after_a_restart(settings: Settings, tmp_path: Path) -> None:
    store_path, registration_id = await before_restart(settings, tmp_path)
    record = json.loads(store_path.read_text())
    assert record["board_path"] == str(tmp_path / "board.cad")
    assert [item["registration_id"] for item in record["registrations"]] == [registration_id]
    assert record["registrations"][0]["photo_id"]

    second = Process(settings, store_path)
    async with Client(second.server) as client:
        found = await client.call_tool("board_find_part", {"query": "U7301"})
        located = await client.call_tool(
            "board_locate_in_photo", {"registration_id": registration_id, "refdes": ["J4"]}
        )

    assert not found.is_error, found.content
    assert found.structured_content is not None
    assert [part["name"] for part in found.structured_content["parts"]] == ["U7301"]
    assert len(second.runner.calls) == 1  # opened again with obv-dump, one time
    # The registration came back, but stale: a new process cannot check its scene.
    assert located.is_error
    assert "board_register_photo again" in text(located)
    assert "server restart" in (second.services.board.restore_note or "")


async def test_changed_board_file_drops_the_registrations(settings: Settings, tmp_path: Path) -> None:
    store_path, registration_id = await before_restart(settings, tmp_path)
    record = json.loads(store_path.read_text())
    record["board_sha256"] = "0" * 64
    store_path.write_text(json.dumps(record))

    second = Process(settings, store_path)
    async with Client(second.server) as client:
        found = await client.call_tool("board_find_part", {"query": "J4"})
        located = await client.call_tool(
            "board_locate_in_photo", {"registration_id": registration_id, "refdes": ["J4"]}
        )

    assert not found.is_error
    assert located.is_error
    assert "no registration" in text(located)
    assert "changed since the last session" in (second.services.board.restore_note or "")


async def test_missing_board_file_gives_a_clear_note(settings: Settings, tmp_path: Path) -> None:
    store_path, _ = await before_restart(settings, tmp_path)
    (tmp_path / "board.cad").unlink()

    second = Process(settings, store_path)
    async with Client(second.server) as client:
        result = await client.call_tool("board_find_part", {"query": "J4"})

    assert result.is_error
    assert "no board is open" in text(result)
    assert "did not open again" in (second.services.board.restore_note or "")


async def test_without_a_record_nothing_is_restored(settings: Settings, tmp_path: Path) -> None:
    fresh = Process(settings, tmp_path / "empty" / "board-session.json")
    async with Client(fresh.server) as client:
        result = await client.call_tool("board_find_part", {"query": "J4"})

    assert result.is_error
    assert fresh.runner.calls == []
    assert not (tmp_path / "empty" / "board-session.json").exists()
