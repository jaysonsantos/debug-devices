"""Send the tool calls of a secondary MCP server to the primary monitor, so one page shows every agent.

The primary serves the page on the configured UI port. A secondary finds it through `/api/whoami` and sends each
call event and its images to the ingest routes, with the token that the primary wrote to a mode-600 file. The
sender never blocks a tool call: the bus hands the events to one worker, and each request has a short timeout.
"""

import asyncio
import contextlib
import logging
import os
import secrets
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

import httpx
from pydantic import BaseModel

from debug_devices_mcp.remote_webcam import RemoteMonitor
from debug_devices_mcp.ui.constants import STATE_DIR_NAME, defaults, env, http, ingest, tools
from debug_devices_mcp.ui.events import BusMessage, CallStatus, EventBus, EventKind, ToolCallEvent
from debug_devices_mcp.ui.settings import state_dir

logger = logging.getLogger(__name__)

type Clock = Callable[[], float]


# region: token


def token_dir(environ: Mapping[str, str] = os.environ) -> Path:
    """`$XDG_RUNTIME_DIR/debug-devices` (a per-user tmpfs), or the state folder without it."""
    runtime = environ.get(env.XDG_RUNTIME_DIR)
    return Path(runtime) / STATE_DIR_NAME if runtime else state_dir(environ)


def token_path(directory: Path, port: int) -> Path:
    return directory / f"{ingest.TOKEN_FILE_PREFIX}{port}{ingest.TOKEN_FILE_SUFFIX}"


def write_token(directory: Path, port: int) -> str:
    """A new random token for the page on `port`, readable only by this user."""
    directory.mkdir(parents=True, exist_ok=True)
    path = token_path(directory, port)
    token = secrets.token_urlsafe(ingest.TOKEN_BYTES)
    path.unlink(missing_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, ingest.TOKEN_FILE_MODE)
    with os.fdopen(fd, "w") as file:
        file.write(token)
    return token


def read_token(directory: Path, port: int) -> str | None:
    try:
        return token_path(directory, port).read_text().strip() or None
    except OSError:
        return None


def remove_token(directory: Path, port: int, token: str) -> None:
    """Remove the file only when it is still ours (a new primary can have written its own)."""
    if read_token(directory, port) == token:
        token_path(directory, port).unlink(missing_ok=True)


# endregion: token


class IngestCall(BaseModel):
    """The body of POST /api/ingest/calls."""

    origin: str
    event: ToolCallEvent


def redacted(event: ToolCallEvent) -> ToolCallEvent:
    """The user's instructions text must not leave this server: keep only the tool name and the status."""
    if event.tool not in tools.REDACTED:
        return event
    return event.model_copy(update={"summary": ingest.REDACTED_TEXT, "details": {}, "images": []})


@dataclass
class ForwarderOptions:
    directory: Path | None = None
    timeout: timedelta = ingest.REQUEST_TIMEOUT
    clock: Clock = time.monotonic
    # Tests pass a mock transport for the requests to the primary.
    transport: httpx.AsyncBaseTransport | None = None


class CallForwarder:
    """A bus subscriber of a secondary: it sends the local calls to the primary, in order, and never waits long."""

    def __init__(
        self,
        bus: EventBus,
        remote: RemoteMonitor,
        is_secondary: Callable[[], bool],
        origin: Callable[[], str],
        options: ForwarderOptions | None = None,
    ) -> None:
        options = options or ForwarderOptions()
        self._bus = bus
        self._remote = remote
        self._is_secondary = is_secondary
        self._origin = origin
        self._directory = options.directory or token_dir()
        self._clock = options.clock
        self._http = httpx.AsyncClient(timeout=options.timeout.total_seconds(), transport=options.transport)
        self._task: asyncio.Task[None] | None = None
        self._queue: asyncio.Queue[BusMessage] | None = None
        self._subscription: contextlib.AbstractContextManager[asyncio.Queue[BusMessage]] | None = None
        self._busy = False
        self._primary: str | None = None
        self._token: str | None = None
        self._next_lookup = 0.0
        self._backoff = ingest.BACKOFF_START
        self.sent = 0

    @property
    def primary(self) -> str | None:
        return self._primary

    def start(self) -> None:
        """Subscribe now (not in the task): the call that finds the primary publishes its events right after."""
        if self._task is None:
            self._subscription = self._bus.subscribe()
            self._queue = self._subscription.__enter__()
            self._task = asyncio.create_task(self._run(self._queue), name="call-forwarder")

    async def stop(self) -> None:
        """Send what is still in the queue (at most `DRAIN_TIME`), then stop."""
        deadline = self._clock() + ingest.DRAIN_TIME.total_seconds()
        while self._task is not None and (self._busy or (self._queue is not None and not self._queue.empty())):
            if self._clock() >= deadline:
                break
            await asyncio.sleep(ingest.DRAIN_POLL.total_seconds())
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._subscription is not None:
            self._subscription.__exit__(None, None, None)
            self._subscription = None
        await self._http.aclose()

    async def _run(self, queue: asyncio.Queue[BusMessage]) -> None:
        while True:
            message = await queue.get()
            if message.kind is not EventKind.CALL or not isinstance(message.data, ToolCallEvent):
                continue
            if message.data.origin is not None or not self._is_secondary():
                continue
            self._busy = True
            try:
                await self.forward(message.data)
            finally:
                self._busy = False

    # region: primary lookup

    async def _find_primary(self) -> bool:
        """Look for the primary. After a failure, wait with a backoff (1 s, 2 s, ... 30 s) before the next look."""
        if self._primary is not None:
            return True
        now = self._clock()
        if now < self._next_lookup:
            return False
        identity = await self._remote.find_monitor()
        port = self._remote.port
        token = read_token(self._directory, port) if identity is not None else None
        if identity is None or token is None:
            self._next_lookup = now + self._backoff.total_seconds()
            self._backoff = min(self._backoff * 2, ingest.BACKOFF_MAX)
            return False
        self._primary, self._token = identity.url.rstrip("/"), token
        self._backoff = ingest.BACKOFF_START
        logger.info("sending the tool calls to the monitor at %s", identity.url)
        return True

    def _lost(self, reason: str) -> None:
        """The primary stopped or refused (a restarted primary has a new token): look for it again at once."""
        logger.info("the monitor at %s does not take calls now: %s", self._primary, reason)
        self._primary = self._token = None
        self._next_lookup = self._clock()

    # endregion: primary lookup

    async def forward(self, event: ToolCallEvent) -> None:
        """Send one call event, then (at its end) its images. Failures only log."""
        event = redacted(event)
        body = IngestCall(origin=self._origin(), event=event).model_dump_json().encode()
        # A second try after a failure: the primary can have restarted with a new token.
        for _ in range(ingest.SEND_ATTEMPTS):
            if not await self._find_primary():
                return
            if await self._post(ingest.CALLS_PATH, body, http.JSON_MEDIA_TYPE):
                break
        else:
            return
        self.sent += 1
        if event.status is CallStatus.RUNNING or event.tool in tools.REDACTED:
            return
        call = self._bus.find_call(event.id)
        for index, image in enumerate(call.images if call is not None else []):
            path = ingest.IMAGE_PATH.format(call_id=event.id)
            params = {
                ingest.ORIGIN_PARAM: self._origin(),
                ingest.LABEL_PARAM: image.label,
                ingest.INDEX_PARAM: str(index),
            }
            if not await self._post(path, image.jpeg, http.JPEG_MEDIA_TYPE, params):
                return

    async def _post(self, path: str, body: bytes, media_type: str, params: dict[str, str] | None = None) -> bool:
        if self._primary is None or self._token is None:
            return False
        headers = {ingest.TOKEN_HEADER: self._token, http.CONTENT_TYPE_HEADER: media_type}
        try:
            response = await self._http.post(f"{self._primary}{path}", content=body, headers=headers, params=params)
        except httpx.HTTPError as exc:
            self._lost(repr(exc))
            return False
        if response.status_code >= httpx.codes.BAD_REQUEST:
            self._lost(f"HTTP {response.status_code}")
            return False
        return True


def origin_label(client_name: str | None) -> str:
    """For example "codex 1824788": the MCP client name from initialize, and the process id."""
    return f"{client_name or defaults.UNKNOWN_CLIENT} {os.getpid()}"
