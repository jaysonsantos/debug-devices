"""The journal of unsafe readings that the bench state file could not take (N80, bench_journal.py)."""

import contextlib
import fcntl
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError

from debug_devices_mcp import bench_journal, bench_state
from debug_devices_mcp.bench_journal import BenchJournal, journal_path
from debug_devices_mcp.bench_state import BenchStateStore, gate, note_meter_reading
from debug_devices_mcp.config import Settings
from debug_devices_mcp.multimeter import MeterResult
from debug_devices_mcp.server import build_server

from .test_bench_state import meter, other_writer
from .test_meter_frames import answers
from .test_multimeter import READING
from .test_server import FakePhone, make_services

READ_ONLY = 0o500
WRITABLE = 0o700
KILLED = -signal.SIGKILL


@pytest.fixture(autouse=True)
def short_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bench_state, "LOCK_TIMEOUT", timedelta(milliseconds=50))


def unknown(result: MeterResult) -> str:
    return f"unknown point {result.capture_id}"


def points(store: BenchStateStore) -> list[str]:
    return [point.point for point in store.load().residual_points]


def file_points(path: Path) -> list[str]:
    """The points in bench-state.json itself (no journal)."""
    return [point.point for point in bench_state.BenchState.model_validate_json(path.read_bytes()).residual_points]


async def test_a_busy_lock_sends_the_reading_to_the_journal(tmp_path: Path) -> None:
    path = tmp_path / "bench-state.json"
    store, other_server = BenchStateStore(path), BenchStateStore(path)
    unsafe = meter("dc_voltage", "V", 7.0, "7.00")
    with other_writer(path):
        notice = await note_meter_reading(store, unsafe)

    assert notice is not None
    assert "went into the journal bench-state.journal.jsonl" in notice
    assert len(BenchJournal(journal_path(path)).read().entries) == 1
    assert not path.exists()
    # The gate counts the line at once, in this server and in another one on the same folder.
    for server in (store, other_server):
        assert points(server) == [unknown(unsafe)]
        assert gate(server.load()).unpowered_tests_allowed is False


async def test_the_next_write_merges_the_journal_once(tmp_path: Path) -> None:
    path = tmp_path / "bench-state.json"
    store = BenchStateStore(path)
    unsafe = meter("dc_voltage", "V", 7.0, "7.00")
    with other_writer(path):
        await note_meter_reading(store, unsafe)
    store.update(lambda _state: None)
    store.update(lambda _state: None)

    assert file_points(path) == [unknown(unsafe)]
    assert journal_path(path).read_bytes() == b""
    assert len(bench_state.BenchState.model_validate_json(path.read_bytes()).journal_ids) == 1


async def test_a_merge_after_a_crash_before_the_cut_adds_nothing_twice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bench-state.json"
    store = BenchStateStore(path)
    unsafe = meter("dc_voltage", "V", 7.0, "7.00")
    with other_writer(path):
        await note_meter_reading(store, unsafe)
    # The state is saved, then the process stops before it cuts the lines.
    with monkeypatch.context() as patch:
        patch.setattr("debug_devices_mcp.bench_journal.BenchJournal.cut", lambda _self, _ids: None)
        store.update(lambda _state: None)
    assert len(BenchJournal(journal_path(path)).read().entries) == 1
    # Meanwhile the capture gets its point name; the old line must not bring the unknown point back.
    store.update(
        lambda state: bench_state.gate_event(state, unsafe, "C12.1", None)  # type: ignore[func-returns-value]
    )

    assert file_points(path) == ["c12.1"]
    assert journal_path(path).read_bytes() == b""


async def test_a_cut_last_line_is_kept_and_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "bench-state.json"
    journal = BenchJournal(journal_path(path))
    first = meter("dc_voltage", "V", 7.0, "7.00")
    journal.append(first)
    with journal.path.open("ab") as file:
        file.write(b'{"id": "cut-by-a-crash", "result": {"display')
    later = meter("dc_voltage", "V", 9.0, "9.00")
    journal.append(later)
    with caplog.at_level(logging.WARNING):
        BenchStateStore(path).update(lambda _state: None)

    # Both good lines are merged; the cut line stays (the later append started on a new line).
    assert sorted(file_points(path)) == sorted([unknown(first), unknown(later)])
    assert journal.path.read_bytes() == b'{"id": "cut-by-a-crash", "result": {"display\n'
    assert "a bad line in the bench journal" in caplog.text


CHILD = """
import asyncio, os, signal, sys
from datetime import timedelta
from pathlib import Path
from debug_devices_mcp import bench_state
from debug_devices_mcp.multimeter import MeterResult
bench_state.LOCK_TIMEOUT = timedelta(milliseconds=50)
result = MeterResult.model_validate_json(Path(sys.argv[2]).read_text())
print(asyncio.run(bench_state.note_meter_reading(bench_state.BenchStateStore(Path(sys.argv[1])), result)), flush=True)
os.kill(os.getpid(), signal.SIGKILL)
"""


def test_a_process_killed_after_the_journal_write_loses_nothing(tmp_path: Path) -> None:
    path = tmp_path / "bench-state.json"
    unsafe = meter("dc_voltage", "V", 7.0, "7.00")
    result_file = tmp_path / "result.json"
    result_file.write_text(unsafe.model_dump_json())
    with other_writer(path):
        child = subprocess.run(
            [sys.executable, "-c", CHILD, str(path), str(result_file)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    assert child.returncode == KILLED, child.stderr
    assert "went into the journal" in child.stdout
    next_server = BenchStateStore(path)
    assert points(next_server) == [unknown(unsafe)]
    next_server.update(lambda _state: None)
    assert file_points(path) == [unknown(unsafe)]


@pytest.mark.skipif(os.geteuid() == 0, reason="root can write to a read-only folder")
async def test_a_read_only_folder_gives_a_clear_tool_error(tmp_path: Path) -> None:
    folder = tmp_path / "state"
    folder.mkdir()
    folder.chmod(READ_ONLY)
    store = BenchStateStore(folder / "bench-state.json")
    unsafe = meter("dc_voltage", "V", 7.0, "7.00")
    try:
        with pytest.raises(ToolError, match="it is NOT SAVED") as error:
            await note_meter_reading(store, unsafe)
        state = store.load()
        notice = store.view().notice
    finally:
        folder.chmod(WRITABLE)

    assert "The gate stays closed for this server only" in str(error.value)
    assert "check the state folder (free space, write permission)" in str(error.value)
    assert [point.point for point in state.residual_points] == [unknown(unsafe)]
    assert notice is not None
    assert "NOT saved" in notice


async def test_multimeter_read_names_the_journal(settings: Settings, tmp_path: Path) -> None:
    path = tmp_path / "bench-state.json"
    services = make_services(settings, FakePhone(), answers({**READING, "display_text": "7.00", "value": 7.0}))
    services.bench = BenchStateStore(path)
    with other_writer(path):
        async with Client(build_server(services)) as client:
            result = await client.call_tool("multimeter_read", {})

    assert result.structured_content is not None, result.content
    assert "went into the journal" in result.structured_content["bench_notice"]
    assert points(BenchStateStore(path)) == [f"unknown point {result.structured_content['capture_id']}"]


# region: N81 and N82


async def test_an_append_does_not_wait_for_a_long_write_of_another_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # N81: another server holds the state lock in a long write, and the (empty) journal file is there after a first use.
    monkeypatch.setattr(bench_journal, "JOURNAL_LOCK_TIMEOUT", timedelta(milliseconds=200))
    path = tmp_path / "bench-state.json"
    journal_path(path).touch()
    other_server = BenchStateStore(path)
    holding, release = threading.Event(), threading.Event()

    def long_write(_state: bench_state.BenchState) -> None:
        holding.set()
        release.wait(5)

    write = threading.Thread(target=other_server.update, args=(long_write,))
    write.start()
    try:
        assert holding.wait(5)
        unsafe = meter("dc_voltage", "V", 7.0, "7.00")
        notice = await note_meter_reading(BenchStateStore(path), unsafe)
    finally:
        release.set()
        write.join(5)

    assert notice is not None
    assert "went into the journal" in notice
    # The other server's cut after its save kept the line that came during its write.
    assert len(BenchJournal(journal_path(path)).read().entries) == 1
    assert points(BenchStateStore(path)) == [unknown(unsafe)]
    BenchStateStore(path).update(lambda _state: None)
    assert file_points(path) == [unknown(unsafe)]
    assert journal_path(path).read_bytes() == b""


@contextlib.contextmanager
def journal_held(path: Path) -> Iterator[None]:
    """Another server holds the journal lock."""
    with journal_path(path).open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


async def test_not_saved_names_a_busy_journal_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bench_journal, "JOURNAL_LOCK_TIMEOUT", timedelta(milliseconds=50))
    path = tmp_path / "bench-state.json"
    with other_writer(path), journal_held(path), pytest.raises(ToolError) as error:
        await note_meter_reading(BenchStateStore(path), meter("dc_voltage", "V", 7.0, "7.00"))

    text = str(error.value)
    assert "it is NOT SAVED: the bench state file could not take it (the bench state is locked" in text
    assert "another server holds the bench journal lock" in text
    assert text.endswith("another server holds the journal lock: read the meter again")


def test_load_reads_the_journal_before_the_state_file(tmp_path: Path) -> None:
    # N82: another server merges and cuts the line between the two reads of a load.
    path = tmp_path / "bench-state.json"
    unsafe = meter("dc_voltage", "V", 7.0, "7.00")
    BenchJournal(journal_path(path)).append(unsafe)
    reader, merger = BenchStateStore(path), BenchStateStore(path)
    read_state = reader._read

    def state_read_then_a_merge() -> bench_state.BenchState:
        state = read_state()
        merger.update(lambda _state: None)
        return state

    reader._read = state_read_then_a_merge  # type: ignore[method-assign]
    state = reader.load()

    assert journal_path(path).read_bytes() == b""
    assert [point.point for point in state.residual_points] == [unknown(unsafe)]
    assert gate(state).unpowered_tests_allowed is False


# endregion


def test_a_load_during_a_cut_sees_the_kept_line(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # N86: the cut empties the file, then writes the kept lines back; a gate read must not fall into that gap.
    path = tmp_path / "bench-state.json"
    journal = BenchJournal(journal_path(path))
    merged = journal.append(meter("dc_voltage", "V", 5.0, "5.00"))
    kept = meter("dc_voltage", "V", 7.0, "7.00")
    journal.append(kept)
    truncated = threading.Event()
    write_back = os.pwrite

    def slow_write_back(fd: int, data: bytes, offset: int) -> int:
        truncated.set()
        time.sleep(0.3)
        return write_back(fd, data, offset)

    monkeypatch.setattr(bench_journal.os, "pwrite", slow_write_back)
    cut = threading.Thread(target=journal.cut, args=({merged.id},))
    cut.start()
    try:
        assert truncated.wait(5)
        state = BenchStateStore(path).load()
    finally:
        cut.join(5)

    assert unknown(kept) in [point.point for point in state.residual_points]
    assert gate(state).unpowered_tests_allowed is False
