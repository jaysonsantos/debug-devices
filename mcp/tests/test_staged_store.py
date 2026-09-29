"""The staged captures queue (staged.py): order, the limit, the time to live, a pending capture, private files, and a
second MCP server process that pops the captures of the page server."""

import asyncio
import stat
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid7

import pytest

from debug_devices_mcp.staged import (
    MAX_STAGED,
    PENDING_LIMIT,
    PENDING_TIMED_OUT,
    STAGED_TTL,
    QueueFullError,
    StagedCapture,
    StagedPhoto,
    StagedState,
    StagedStore,
)

START = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
PHOTO = StagedPhoto(width=4, height=3, turn_degrees=0, flip_horizontal=False, flip_vertical=False)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now


def names_in(folder: Path) -> list[str]:
    return sorted(path.name for path in folder.iterdir())


def capture_at(when: datetime) -> StagedCapture:
    return StagedCapture(capture_id=str(uuid7()), captured_at=when, origin="test 1")


async def stage(store: StagedStore, when: datetime, photo: bytes = b"photo", frames: int = 1) -> StagedCapture:
    capture = capture_at(when)
    await store.add(capture)
    ready = capture.model_copy(update={"state": StagedState.READY, "photo": PHOTO})
    assert await store.finish(ready, photo, [b"frame"] * frames)
    return ready


async def test_pop_gives_all_oldest_first_and_removes_them(tmp_path: Path) -> None:
    clock = Clock()
    store = StagedStore(tmp_path, clock)
    second = await stage(store, START + timedelta(seconds=5), b"second")
    first = await stage(store, START, b"first", frames=2)
    assert [item.capture_id for item in await store.list()] == [first.capture_id, second.capture_id]
    popped = await store.pop_all()
    assert [item.capture.capture_id for item in popped] == [first.capture_id, second.capture_id]
    assert (popped[0].photo, popped[0].frames) == (b"first", [b"frame", b"frame"])
    assert await store.list() == []
    # Every file is gone (the lock file stays).
    assert await asyncio.to_thread(names_in, tmp_path) == [".lock"]


async def test_a_full_queue_refuses_a_new_capture(tmp_path: Path) -> None:
    store = StagedStore(tmp_path, Clock())
    for index in range(MAX_STAGED):
        await stage(store, START + timedelta(seconds=index))
    with pytest.raises(QueueFullError, match=f"at most {MAX_STAGED}"):
        await store.add(capture_at(START + timedelta(minutes=1)))
    assert len(await store.list()) == MAX_STAGED


async def test_an_old_capture_expires(tmp_path: Path) -> None:
    clock = Clock()
    store = StagedStore(tmp_path, clock)
    await stage(store, START)
    clock.now = START + STAGED_TTL + timedelta(seconds=1)
    assert await store.list() == []
    assert await store.pop_all() == []


async def test_a_pending_capture_waits_and_then_fails(tmp_path: Path) -> None:
    clock = Clock()
    store = StagedStore(tmp_path, clock)
    await store.add(capture_at(START))
    # A pop does not take a pending capture from pop_ready; pop_all waits for it, then gives it as failed.
    assert await store.pop_ready() == []
    [item] = await store.pop_all(wait=timedelta(milliseconds=50))
    assert item.capture.state is StagedState.FAILED
    assert PENDING_TIMED_OUT in item.capture.notes
    # A pending capture of a stopped server becomes failed after PENDING_LIMIT.
    await store.add(capture_at(START))
    clock.now = START + PENDING_LIMIT + timedelta(seconds=1)
    [listed] = await store.list()
    assert listed.state is StagedState.FAILED


async def test_delete_clear_and_a_removed_capture(tmp_path: Path) -> None:
    store = StagedStore(tmp_path, Clock())
    first = await stage(store, START)
    await stage(store, START + timedelta(seconds=1))
    assert await store.delete(first.capture_id) is True
    assert await store.delete(first.capture_id) is False
    # A capture that finishes after it was deleted is not written again.
    assert await store.finish(first, b"late", []) is False
    assert await store.clear() == 1
    assert await store.list() == []


async def test_the_files_are_private(tmp_path: Path) -> None:
    folder = tmp_path / "staged"
    store = StagedStore(folder, Clock())
    capture = await stage(store, START)
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700
    for path in folder.glob(f"{capture.capture_id}*"):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path


POP_IN_ANOTHER_PROCESS = """
import asyncio, json, sys
from pathlib import Path
from debug_devices_mcp.staged import StagedStore
items = asyncio.run(StagedStore(Path(sys.argv[1])).pop_all())
print(json.dumps([[item.capture.capture_id, item.photo.decode()] for item in items]))
"""


async def test_another_server_process_pops_the_captures(tmp_path: Path) -> None:
    store = StagedStore(tmp_path)
    now = datetime.now(UTC)
    first = await stage(store, now - timedelta(seconds=2), b"one")
    second = await stage(store, now - timedelta(seconds=1), b"two")
    command = [sys.executable, "-c", POP_IN_ANOTHER_PROCESS, str(tmp_path)]
    result = await asyncio.to_thread(subprocess.run, command, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == f'[["{first.capture_id}", "one"], ["{second.capture_id}", "two"]]'
    assert await store.list() == []
