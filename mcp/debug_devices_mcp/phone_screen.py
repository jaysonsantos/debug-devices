"""The phone screen in real time: the scrcpy server on the phone sends raw H.264 through an ADB forward.

The server comes from the installed scrcpy (`/usr/share/scrcpy/scrcpy-server`). Its version must be the version of
the installed scrcpy client. The stream has video only: no audio and no control.
"""

import asyncio
import contextlib
import logging
import os
import re
import secrets
import struct
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from enum import IntEnum, StrEnum
from pathlib import Path

from pydantic import BaseModel

from debug_devices_mcp.constants import adb
from debug_devices_mcp.h264 import AccessUnit, AccessUnitAssembler, AnnexBParser
from debug_devices_mcp.process import CommandError, CommandRunner
from debug_devices_mcp.ui.constants import defaults, screen
from debug_devices_mcp.ui.desktop import ChildProcess, Spawner, spawn

logger = logging.getLogger(__name__)

ADB_PUSH = "push"
ADB_REMOVE = "--remove"


class ScreenError(Exception):
    """The screen stream could not start."""


class ScreenStatus(StrEnum):
    OFF = "off"
    STARTING = "starting"
    STREAMING = "streaming"
    ERROR = "error"


class MessageKind(IntEnum):
    """The kind byte of a message to the page."""

    CONFIG = 0
    KEY = 1
    DELTA = 2


class ScreenConfig(BaseModel):
    """The first message to the page: the codec string for `VideoDecoder.configure`."""

    codec: str


def encode_message(kind: MessageKind, payload: bytes) -> bytes:
    return struct.pack(">IB", len(payload), kind) + payload


def unit_message(unit: AccessUnit) -> bytes:
    return encode_message(MessageKind.KEY if unit.key else MessageKind.DELTA, unit.data)


def config_message(codec: str) -> bytes:
    return encode_message(MessageKind.CONFIG, ScreenConfig(codec=codec).model_dump_json().encode())


def parse_version(output: str) -> str | None:
    """`scrcpy 4.1 <https://...>` gives `4.1`."""
    match = re.search(screen.VERSION_PATTERN, output, re.MULTILINE)
    return match.group(1) if match else None


def server_args(adb_path: str, serial: str, version: str, scid: int, max_size: int) -> list[str]:
    """`adb shell` for the scrcpy server: raw H.264 only, on the abstract socket `scrcpy_<scid>`."""
    return [
        adb_path,
        adb.SERIAL_FLAG,
        serial,
        adb.SHELL,
        f"CLASSPATH={screen.DEVICE_PATH}",
        "app_process",
        "/",
        screen.SERVER_CLASS,
        version,
        f"scid={scid:08x}",
        "tunnel_forward=true",
        "audio=false",
        "control=false",
        "raw_stream=true",
        "video_codec=h264",
        f"max_size={max_size}",
        f"video_codec_options=i-frame-interval={screen.I_FRAME_INTERVAL}",
        "cleanup=true",
    ]


def socket_name(scid: int) -> str:
    return f"{screen.SOCKET_PREFIX}{scid:08x}"


class GopCache:
    """The access units since the last key frame. A new page starts with them, so it decodes at once."""

    def __init__(self, max_bytes: int = screen.MAX_CACHE_BYTES) -> None:
        self._units: list[AccessUnit] = []
        self._bytes = 0
        self._max_bytes = max_bytes

    def add(self, unit: AccessUnit) -> None:
        if unit.key:
            self._units, self._bytes = [], 0
        elif not self._units:
            return
        self._units.append(unit)
        self._bytes += len(unit.data)
        if self._bytes > self._max_bytes:
            # Too long without a key frame: wait for the next one.
            self._units, self._bytes = [], 0

    def clear(self) -> None:
        self._units, self._bytes = [], 0

    def units(self) -> list[AccessUnit]:
        return list(self._units)


@dataclass(eq=False)
class Subscriber:
    queue: asyncio.Queue[bytes] = field(default_factory=lambda: asyncio.Queue(maxsize=screen.SUBSCRIBER_QUEUE_SIZE))
    # Set when the page is too slow and lost frames. The page gets the cache again, from a key frame.
    resync: bool = False


class PhoneScreenOptions(BaseModel):
    adb_path: str
    server_path: Path = screen.SERVER_PATH
    # Empty: ask the installed scrcpy client (`scrcpy --version`). The server refuses another version.
    version: str = ""
    scrcpy_path: str = defaults.SCRCPY
    max_size: int = screen.MAX_SIZE
    adb_timeout: timedelta = timedelta(seconds=15)
    connect_timeout: timedelta = screen.CONNECT_TIMEOUT
    restart_delay: timedelta = screen.RESTART_DELAY
    log_path: Path | None = None


class ScreenState(BaseModel):
    status: ScreenStatus
    error: str | None = None


type StateListener = Callable[[ScreenState], None]


class PhoneScreen:
    """Keeps one scrcpy server stream for the selected serial and hands the frames to the pages."""

    def __init__(
        self,
        options: PhoneScreenOptions,
        runner: CommandRunner,
        spawner: Spawner = spawn,
        on_state: StateListener = lambda state: None,
    ) -> None:
        self._options = options
        self._runner = runner
        self._spawner = spawner
        self._on_state = on_state
        self._serial: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._subscribers: set[Subscriber] = set()
        self._cache = GopCache()
        self._parser = AnnexBParser()
        self._assembler = AccessUnitAssembler()
        self.codec: str | None = None
        self.state = ScreenState(status=ScreenStatus.OFF)

    # region: control

    @property
    def serial(self) -> str | None:
        return self._serial

    def ensure_running(self, serial: str) -> bool:
        """Start the stream for `serial` in the background. Return True when this call started it."""
        if self._task is not None and not self._task.done() and self._serial == serial:
            return False
        if self._task is not None:
            self._task.cancel()
        self._serial = serial
        self._task = asyncio.create_task(self._run(serial), name="phone-screen")
        return True

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._set_state(ScreenStatus.OFF)

    def _set_state(self, status: ScreenStatus, error: str | None = None) -> None:
        self.state = ScreenState(status=status, error=error)
        self._on_state(self.state)

    # endregion: control

    # region: pages

    @contextlib.contextmanager
    def subscribe(self) -> Iterator[Subscriber]:
        """A queue of wire messages. It starts with the config and the frames since the last key frame."""
        subscriber = Subscriber()
        self._fill(subscriber)
        self._subscribers.add(subscriber)
        try:
            yield subscriber
        finally:
            self._subscribers.discard(subscriber)

    def resync(self, subscriber: Subscriber) -> None:
        subscriber.resync = False
        while not subscriber.queue.empty():
            subscriber.queue.get_nowait()
        self._fill(subscriber)

    def _fill(self, subscriber: Subscriber) -> None:
        if self.codec is None:
            return
        subscriber.queue.put_nowait(config_message(self.codec))
        for unit in self._cache.units()[: subscriber.queue.maxsize - 1]:
            subscriber.queue.put_nowait(unit_message(unit))

    def _publish(self, unit: AccessUnit) -> None:
        new_codec = unit.codec is not None and unit.codec != self.codec
        if new_codec:
            self.codec = unit.codec
        self._cache.add(unit)
        for subscriber in self._subscribers:
            if subscriber.resync:
                continue
            try:
                if new_codec and unit.codec is not None:
                    subscriber.queue.put_nowait(config_message(unit.codec))
                subscriber.queue.put_nowait(unit_message(unit))
            except asyncio.QueueFull:
                subscriber.resync = True

    # endregion: pages

    # region: stream

    async def _run(self, serial: str) -> None:
        while True:
            self._set_state(ScreenStatus.STARTING)
            try:
                await self._session(serial)
                error = "the phone closed the screen stream"
            except (ScreenError, CommandError, OSError) as exc:
                error = str(exc)
            logger.warning("phone screen %s: %s", serial, error)
            self._set_state(ScreenStatus.ERROR, error)
            self._cache.clear()
            await asyncio.sleep(self._options.restart_delay.total_seconds())

    async def _adb(self, *args: str) -> str:
        options = self._options
        result = await self._runner.run(
            [options.adb_path, adb.SERIAL_FLAG, self._serial or "", *args], options.adb_timeout
        )
        if not result.ok:
            stderr = result.stderr.decode(errors="replace").strip()
            raise ScreenError(f"adb {' '.join(args)} failed (exit {result.returncode}): {stderr}")
        return result.stdout.decode(errors="replace").strip()

    async def _session(self, serial: str) -> None:
        options = self._options
        if not options.server_path.is_file():
            raise ScreenError(f"scrcpy server not found at {options.server_path}. Install scrcpy.")
        version = options.version or await detect_version(self._runner, options.scrcpy_path, options.adb_timeout)
        if not version:
            raise ScreenError(f"cannot read the version of {options.scrcpy_path}; set --scrcpy-server-version")
        await self._adb(ADB_PUSH, str(options.server_path), screen.DEVICE_PATH)
        scid = secrets.randbits(screen.SCID_BITS)
        local = await self._adb(adb.FORWARD, screen.ANY_LOCAL_PORT, screen.LOCALABSTRACT_PREFIX + socket_name(scid))
        if not local.isdigit():
            raise ScreenError(f"adb forward returned no port: {local!r}")
        port = int(local)
        log = None
        process: ChildProcess | None = None
        # From here on, every failure removes the forward again.
        try:
            if options.log_path is not None:
                options.log_path.parent.mkdir(parents=True, exist_ok=True)
                log = options.log_path.open("ab")
            args = server_args(options.adb_path, serial, version, scid, options.max_size)
            process = await self._spawner(args, os.environ, log)
            reader, writer = await self._connect(port, process)
            try:
                await self._read(reader)
            finally:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(process.wait(), defaults.PROCESS_STOP_TIMEOUT.total_seconds())
            if log is not None:
                log.close()
            with contextlib.suppress(ScreenError, CommandError):
                await self._adb(adb.FORWARD, ADB_REMOVE, f"{adb.TCP_PREFIX}{port}")

    async def _connect(self, port: int, process: ChildProcess) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        """Connect to the ADB forward. Before the server listens, ADB accepts and closes at once: try again."""
        options = self._options
        async with asyncio.timeout(options.connect_timeout.total_seconds()):
            while True:
                if process.returncode is not None:
                    raise ScreenError(f"the scrcpy server stopped (exit {process.returncode}); see the scrcpy log")
                with contextlib.suppress(OSError):
                    reader, writer = await asyncio.open_connection(defaults.HOST, port)
                    first = await reader.read(screen.READ_SIZE)
                    if first:
                        self._feed_start(first)
                        return reader, writer
                    writer.close()
                await asyncio.sleep(screen.CONNECT_RETRY.total_seconds())

    def _feed_start(self, first: bytes) -> None:
        self._parser = AnnexBParser()
        self._assembler = AccessUnitAssembler()
        self._set_state(ScreenStatus.STREAMING)
        self._feed(first)

    def _feed(self, data: bytes) -> None:
        for unit in self._parser.feed(data):
            complete = self._assembler.feed(unit)
            if complete is not None:
                self._publish(complete)

    async def _read(self, reader: asyncio.StreamReader) -> None:
        while chunk := await reader.read(screen.READ_SIZE):
            self._feed(chunk)
        # The end of the stream completes the last units.
        for unit in self._parser.flush():
            if (complete := self._assembler.feed(unit)) is not None:
                self._publish(complete)
        if (last := self._assembler.flush()) is not None:
            self._publish(last)

    # endregion: stream


async def detect_version(runner: CommandRunner, scrcpy_path: str, timeout: timedelta) -> str | None:
    """The version of the installed scrcpy client. The server version must be the same."""
    try:
        result = await runner.run([scrcpy_path, screen.VERSION_FLAG], timeout)
    except CommandError:
        return None
    return parse_version(result.stdout.decode(errors="replace")) if result.ok else None
