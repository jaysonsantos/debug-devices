import asyncio
import struct
from collections.abc import Mapping, Sequence
from datetime import timedelta
from pathlib import Path
from typing import IO

from debug_devices_mcp.h264 import AccessUnit
from debug_devices_mcp.phone_screen import (
    GopCache,
    MessageKind,
    PhoneScreen,
    PhoneScreenOptions,
    ScreenState,
    ScreenStatus,
    parse_version,
    server_args,
)
from debug_devices_mcp.process import CommandResult

from .conftest import FakeRunner, ok
from .test_h264 import IDR, P1, PPS, SPS, stream
from .test_scrcpy import FakeProcess


def unit(key: bool, size: int = 10) -> AccessUnit:
    return AccessUnit(data=b"x" * size, key=key, codec="avc1.640020")


def read_messages(queue: asyncio.Queue[bytes]) -> list[tuple[int, bytes]]:
    messages = []
    while not queue.empty():
        raw = queue.get_nowait()
        length, kind = struct.unpack(">IB", raw[:5])
        assert len(raw) == 5 + length
        messages.append((kind, raw[5:]))
    return messages


def test_parse_version() -> None:
    assert parse_version("scrcpy 4.1 <https://github.com/Genymobile/scrcpy>\n\nDependencies") == "4.1"
    assert parse_version("something else") is None


def test_server_args_ask_for_raw_h264_only() -> None:
    args = server_args("adb", "0a1b2c3d", "4.1", 0x1A2B3C4D, 1280)
    assert args[:4] == ["adb", "-s", "0a1b2c3d", "shell"]
    assert args[4:9] == [
        "CLASSPATH=/data/local/tmp/debug-devices-scrcpy-server.jar",
        "app_process",
        "/",
        "com.genymobile.scrcpy.Server",
        "4.1",
    ]
    for option in ("scid=1a2b3c4d", "tunnel_forward=true", "audio=false", "control=false", "raw_stream=true"):
        assert option in args
    assert "max_size=1280" in args


def test_gop_cache_starts_at_a_key_frame_and_has_a_limit() -> None:
    cache = GopCache(max_bytes=35)
    cache.add(unit(key=False))
    assert cache.units() == []
    cache.add(unit(key=True))
    cache.add(unit(key=False))
    assert [u.key for u in cache.units()] == [True, False]
    cache.add(unit(key=True))
    assert [u.key for u in cache.units()] == [True]
    cache.add(unit(key=False, size=30))
    assert cache.units() == []


class FakeServer:
    """The phone side of the ADB forward: the first connection closes at once (server not ready yet)."""

    def __init__(self, data: bytes) -> None:
        self.data = data
        self.connections = 0
        self.server: asyncio.Server | None = None

    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connections += 1
        if self.connections > 1:
            writer.write(self.data)
            await writer.drain()
            await asyncio.sleep(0.2)
        writer.close()

    async def start(self) -> int:
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]


async def test_session_streams_to_subscribers(tmp_path: Path) -> None:
    server_file = tmp_path / "scrcpy-server"
    server_file.write_bytes(b"jar")
    fake_server = FakeServer(stream(SPS, PPS, IDR, P1, P1, IDR))
    port = await fake_server.start()

    def respond(command: list[str]) -> CommandResult:
        if command[-1] == "--version":
            return ok(b"scrcpy 4.1 <https://github.com/Genymobile/scrcpy>\n")
        if "forward" in command and "--remove" not in command:
            return ok(f"{port}\n".encode())
        return ok()

    runner = FakeRunner(respond)
    spawned: list[list[str]] = []

    async def spawner(args: Sequence[str], environ: Mapping[str, str], log: IO[bytes] | None) -> FakeProcess:
        spawned.append(list(args))
        return FakeProcess()

    states: list[ScreenState] = []
    options = PhoneScreenOptions(
        adb_path="adb",
        server_path=server_file,
        connect_timeout=timedelta(seconds=5),
        restart_delay=timedelta(seconds=10),
    )
    phone_screen = PhoneScreen(options, runner, spawner=spawner, on_state=states.append)
    with phone_screen.subscribe() as subscriber:
        assert phone_screen.ensure_running("0a1b2c3d")
        assert not phone_screen.ensure_running("0a1b2c3d")
        for _ in range(200):
            if any(state.status == ScreenStatus.ERROR for state in states):
                break
            await asyncio.sleep(0.01)
        messages = read_messages(subscriber.queue)
    await phone_screen.stop()
    fake_server.server.close()

    assert [state.status for state in states][:3] == [ScreenStatus.STARTING, ScreenStatus.STREAMING, ScreenStatus.ERROR]
    assert states[-1].status == ScreenStatus.OFF
    kinds = [kind for kind, _ in messages]
    assert kinds == [MessageKind.CONFIG, MessageKind.KEY, MessageKind.DELTA, MessageKind.DELTA, MessageKind.KEY]
    assert messages[0][1] == b'{"codec":"avc1.640020"}'
    assert messages[1][1] == stream(SPS, PPS, IDR)
    assert spawned[0][:4] == ["adb", "-s", "0a1b2c3d", "shell"]
    assert "4.1" in spawned[0]
    commands = [" ".join(call) for call in runner.calls]
    assert f"adb -s 0a1b2c3d push {server_file} /data/local/tmp/debug-devices-scrcpy-server.jar" in commands
    assert f"adb -s 0a1b2c3d forward --remove tcp:{port}" in commands
    # A page that opens now gets the config and the frames since the last key frame.
    with phone_screen.subscribe() as late:
        late_kinds = [kind for kind, _ in read_messages(late.queue)]
    assert late_kinds == [MessageKind.CONFIG]  # the cache was cleared when the stream stopped


async def test_missing_server_is_an_error_state(tmp_path: Path) -> None:
    states: list[ScreenState] = []
    options = PhoneScreenOptions(
        adb_path="adb", server_path=tmp_path / "missing", version="4.1", restart_delay=timedelta(seconds=10)
    )
    phone_screen = PhoneScreen(options, FakeRunner(lambda command: ok()), on_state=states.append)
    phone_screen.ensure_running("0a1b2c3d")
    for _ in range(100):
        if states and states[-1].status == ScreenStatus.ERROR:
            break
        await asyncio.sleep(0.01)
    await phone_screen.stop()
    error = next(state.error for state in states if state.status == ScreenStatus.ERROR)
    assert "scrcpy server not found" in error


async def test_failed_start_removes_the_forward(tmp_path: Path) -> None:
    server_file = tmp_path / "scrcpy-server"
    server_file.write_bytes(b"jar")

    def respond(command: list[str]) -> CommandResult:
        if "forward" in command and "--remove" not in command:
            return ok(b"40000\n")
        return ok()

    async def spawner(args: Sequence[str], environ: Mapping[str, str], log: IO[bytes] | None) -> FakeProcess:
        raise FileNotFoundError("adb")

    runner = FakeRunner(respond)
    states: list[ScreenState] = []
    options = PhoneScreenOptions(
        adb_path="adb", server_path=server_file, version="4.1", restart_delay=timedelta(seconds=10)
    )
    phone_screen = PhoneScreen(options, runner, spawner=spawner, on_state=states.append)
    phone_screen.ensure_running("0a1b2c3d")
    for _ in range(100):
        if states and states[-1].status == ScreenStatus.ERROR:
            break
        await asyncio.sleep(0.01)
    await phone_screen.stop()
    assert ["adb", "-s", "0a1b2c3d", "forward", "--remove", "tcp:40000"] in runner.calls
