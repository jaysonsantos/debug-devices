"""Development only: a stdio proxy that restarts the MCP server when its code changes.

`debug-devices-mcp-dev [server arguments...]` starts `debug-devices-mcp` as a child and forwards the
newline-delimited JSON-RPC of the MCP stdio transport in both directions. When a file of the package changes, it
stops the child, starts a new one, replays the client's `initialize` and `notifications/initialized`, and sends
`notifications/tools/list_changed` to the client. The MCP client (Claude Code, Codex) keeps its session.

Stdout carries only JSON-RPC. The proxy logs to stderr, and the child's stderr goes to the proxy's stderr.
"""

import asyncio
import contextlib
import json
import os
import signal
import sys
from collections.abc import Awaitable, Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel

PACKAGE_DIR = Path(__file__).resolve().parent
WATCH_SUFFIXES = frozenset({".py", ".html", ".js", ".css"})
SKIP_DIRS = frozenset({"__pycache__"})
LOG_PREFIX = "[devreload]"
# Messages can carry base64 images of several MB.
LINE_LIMIT_BYTES = 64 * 1024 * 1024

JSONRPC_VERSION = "2.0"
METHOD_INITIALIZE = "initialize"
METHOD_INITIALIZED = "notifications/initialized"
METHOD_TOOLS_LIST_CHANGED = "notifications/tools/list_changed"
REPLAY_ID_PREFIX = "devreload-initialize-"
# JSON-RPC "server error" range (-32000 to -32099).
ERROR_RELOADED = -32001
ERROR_UNAVAILABLE = -32002
RELOADED_MESSAGE = "debug-devices reloaded its code; call the tool again"
UNAVAILABLE_MESSAGE = "debug-devices cannot start after a code change: {problem}. Fix the code; it retries then."

type Message = dict[str, Any]
type WriteLine = Callable[[bytes], Awaitable[None]]


class ReloadOptions(BaseModel):
    command: list[str]
    watch_dir: Path = PACKAGE_DIR
    poll_interval: timedelta = timedelta(seconds=1)
    # After the last change, wait this long for more changes (an editor writes several files).
    debounce: timedelta = timedelta(milliseconds=300)
    # Time for the child to exit after its stdin closes (the server cleans up ffmpeg, scrcpy, and adb then).
    stop_timeout: timedelta = timedelta(seconds=5)
    # Time for the child to answer the replayed initialize.
    start_timeout: timedelta = timedelta(seconds=30)


def log(text: str) -> None:
    print(f"{LOG_PREFIX} {text}", file=sys.stderr, flush=True)


def snapshot(directory: Path) -> dict[Path, int]:
    """Modification times of the watched files."""
    times: dict[Path, int] = {}
    for root, dirs, files in os.walk(directory):
        dirs[:] = [name for name in dirs if name not in SKIP_DIRS]
        for name in files:
            path = Path(root) / name
            if path.suffix in WATCH_SUFFIXES:
                with contextlib.suppress(FileNotFoundError):
                    times[path] = path.stat().st_mtime_ns
    return times


def changed_files(before: dict[Path, int], after: dict[Path, int]) -> int:
    return sum(1 for path in before.keys() | after.keys() if before.get(path) != after.get(path))


def is_request(message: Message) -> bool:
    return "method" in message and "id" in message


def is_response(message: Message) -> bool:
    return "method" not in message and "id" in message


def error_response(message_id: object, code: int, text: str) -> Message:
    return {"jsonrpc": JSONRPC_VERSION, "id": message_id, "error": {"code": code, "message": text}}


def encode(message: Message) -> bytes:
    return json.dumps(message, separators=(",", ":")).encode() + b"\n"


class ChildStartError(Exception):
    """The new server did not start or did not answer initialize."""


class Child:
    """One run of the real server."""

    def __init__(self, process: asyncio.subprocess.Process) -> None:
        self.process = process
        self.replay_answer: asyncio.Future[Message] | None = None
        self.replay_id: str | None = None
        # The task that reads the child's stdout (kept, so the loop does not drop it).
        self.reader: asyncio.Task[None] | None = None

    async def send(self, message: Message) -> None:
        stdin = self.process.stdin
        if stdin is None or stdin.is_closing():
            raise ConnectionResetError("child stdin is closed")
        stdin.write(encode(message))
        await stdin.drain()

    async def stop(self, timeout: timedelta) -> None:
        """Close stdin (a clean shutdown), then SIGTERM, then SIGKILL to the process group."""
        if self.process.returncode is not None:
            return
        if self.process.stdin is not None and not self.process.stdin.is_closing():
            self.process.stdin.close()
        for step in (None, signal.SIGTERM, signal.SIGKILL):
            if step is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, step)
            try:
                await asyncio.wait_for(self.process.wait(), timeout.total_seconds())
                return
            except TimeoutError:
                log(f"the server did not stop; sending {'SIGTERM' if step is None else 'SIGKILL'}")


class DevReloadProxy:
    def __init__(self, options: ReloadOptions, client_in: asyncio.StreamReader, client_out: WriteLine) -> None:
        self.options = options
        self.client_in = client_in
        self.client_out = client_out
        self.child: Child | None = None
        self.ready = asyncio.Event()
        # Set when the last start failed: requests get an error that names it.
        self.problem: str | None = None
        self.initialize: Message | None = None
        self.initialized: Message | None = None
        self.client_initialize_id: object = None
        self.pending: set[str] = set()
        self.queue: list[Message] = []
        self.replays = 0
        # True after client EOF: the child stops because the proxy stops, not because it crashed.
        self.closing = False
        self._write_lock = asyncio.Lock()

    # region: client side

    async def to_client(self, message: Message) -> None:
        async with self._write_lock:
            await self.client_out(encode(message))

    async def from_client(self) -> None:
        """Read the client until EOF. Every message goes to the child, or waits in the queue."""
        while line := await self.client_in.readline():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                log(f"not JSON from the client, skipped: {line[:80]!r}")
                continue
            method = message.get("method")
            if method == METHOD_INITIALIZE:
                self.initialize = message
                self.client_initialize_id = message.get("id")
            elif method == METHOD_INITIALIZED:
                self.initialized = message
            await self.forward(message)

    async def forward(self, message: Message) -> None:
        if is_request(message) and self.problem is not None and not self.ready.is_set():
            await self.to_client(
                error_response(message["id"], ERROR_UNAVAILABLE, UNAVAILABLE_MESSAGE.format(problem=self.problem))
            )
            return
        if not self.ready.is_set() or self.child is None:
            self.queue.append(message)
            return
        if is_request(message):
            self.pending.add(json.dumps(message["id"]))
        try:
            await self.child.send(message)
        except ConnectionResetError:
            self.queue.append(message)

    # endregion: client side

    # region: child side

    async def from_child(self, child: Child) -> None:
        stdout = child.process.stdout
        assert stdout is not None
        while line := await stdout.readline():
            try:
                message = json.loads(line)
            except json.JSONDecodeError:
                log(f"not JSON from the server, skipped: {line[:80]!r}")
                continue
            if is_response(message):
                if child.replay_id is not None and message["id"] == child.replay_id:
                    if child.replay_answer is not None and not child.replay_answer.done():
                        child.replay_answer.set_result(message)
                    continue
                self.pending.discard(json.dumps(message["id"]))
                if message["id"] == self.client_initialize_id and "result" in message:
                    capabilities = message["result"].setdefault("capabilities", {})
                    capabilities.setdefault("tools", {})["listChanged"] = True
            await self.to_client(message)
        if child is self.child and self.ready.is_set() and not self.closing:
            # The server stopped by itself (a crash), not because of a reload.
            self.ready.clear()
            self.problem = f"the server exited with code {await child.process.wait()}"
            log(f"{self.problem}; it starts again on the next code change")
            await self.fail_pending(ERROR_UNAVAILABLE, UNAVAILABLE_MESSAGE.format(problem=self.problem))

    async def fail_pending(self, code: int, text: str) -> None:
        for key in sorted(self.pending):
            await self.to_client(error_response(json.loads(key), code, text))
        self.pending.clear()

    async def spawn(self) -> Child:
        process = await asyncio.create_subprocess_exec(
            *self.options.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,
            limit=LINE_LIMIT_BYTES,
            # Own process group: a SIGKILL also reaches the server's ffmpeg and scrcpy.
            start_new_session=True,
        )
        child = Child(process)
        child.reader = asyncio.create_task(self.from_child(child))
        return child

    async def replay(self, child: Child) -> None:
        """Send the client's initialize (with our id) and initialized to a new server."""
        if self.initialize is None:
            return
        self.replays += 1
        child.replay_id = f"{REPLAY_ID_PREFIX}{self.replays}"
        child.replay_answer = asyncio.get_running_loop().create_future()
        await child.send({**self.initialize, "id": child.replay_id})
        try:
            answer = await asyncio.wait_for(
                asyncio.shield(child.replay_answer), self.options.start_timeout.total_seconds()
            )
        except TimeoutError as exc:
            code = child.process.returncode
            raise ChildStartError(
                f"the server exited with code {code}" if code is not None else "no answer to initialize"
            ) from exc
        if "error" in answer:
            raise ChildStartError(f"initialize failed: {answer['error'].get('message', answer['error'])}")
        if self.initialized is not None:
            await child.send(self.initialized)

    async def start_child(self) -> bool:
        """Start a server. On success, send the queue to it. On failure, keep the problem for the error answers."""
        try:
            child = await self.spawn()
            self.child = child
            exited = asyncio.create_task(child.process.wait())
            replayed = asyncio.create_task(self.replay(child))
            done, _ = await asyncio.wait({exited, replayed}, return_when=asyncio.FIRST_COMPLETED)
            if replayed in done:
                exited.cancel()
                replayed.result()
            else:
                replayed.cancel()
                raise ChildStartError(f"the server exited with code {exited.result()}")
        except (ChildStartError, OSError, ConnectionResetError) as exc:
            self.problem = str(exc)
            log(f"start failed: {self.problem}; it retries on the next code change")
            queued, self.queue = self.queue, []
            for message in queued:
                await self.forward(message)
            return False
        self.problem = None
        self.ready.set()
        queued, self.queue = self.queue, []
        for message in queued:
            await self.forward(message)
        return True

    async def reload(self, files: int) -> None:
        self.ready.clear()
        if self.child is not None:
            await self.child.stop(self.options.stop_timeout)
        await self.fail_pending(ERROR_RELOADED, RELOADED_MESSAGE)
        if await self.start_child():
            if self.initialized is not None:
                await self.to_client({"jsonrpc": JSONRPC_VERSION, "method": METHOD_TOOLS_LIST_CHANGED})
            log(f"reloaded ({files} files changed)")

    # endregion: child side

    async def watch(self) -> None:
        poll = self.options.poll_interval.total_seconds()
        known = await asyncio.to_thread(snapshot, self.options.watch_dir)
        while True:
            await asyncio.sleep(poll)
            current = await asyncio.to_thread(snapshot, self.options.watch_dir)
            files = changed_files(known, current)
            if not files:
                continue
            # Debounce: wait until the files stop changing.
            while True:
                await asyncio.sleep(self.options.debounce.total_seconds())
                latest = await asyncio.to_thread(snapshot, self.options.watch_dir)
                if latest == current:
                    break
                current = latest
            files = changed_files(known, current)
            known = current
            await self.reload(files)

    async def run(self) -> None:
        await self.start_child()
        watcher = asyncio.create_task(self.watch())
        try:
            await self.from_client()
        finally:
            self.closing = True
            self.ready.clear()
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
            if self.child is not None:
                await self.child.stop(self.options.stop_timeout)


async def _run_stdio(options: ReloadOptions) -> None:
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader(limit=LINE_LIMIT_BYTES)
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    stdout = sys.stdout.buffer

    async def write(data: bytes) -> None:
        with contextlib.suppress(BrokenPipeError):
            stdout.write(data)
            stdout.flush()

    await DevReloadProxy(options, reader, write).run()


def main() -> None:
    command = [sys.executable, "-m", "debug_devices_mcp", *sys.argv[1:]]
    log(f"watching {PACKAGE_DIR} for {', '.join(sorted(WATCH_SUFFIXES))} changes")
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(_run_stdio(ReloadOptions(command=command)))
