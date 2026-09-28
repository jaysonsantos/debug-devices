"""A secondary MCP server gets the phone screen frames from the primary monitor (live tracking, scene changes).

Only one scrcpy stream runs per phone: the primary's. The secondary asks the primary to start it (after its own
phone_connect), then reads the newest frame about 4 times per second. The requests carry the ingest token of the
primary (a file with mode 600, see `ui/forward.py`).
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
from pydantic import BaseModel

from debug_devices_mcp.remote_webcam import RemoteMonitor
from debug_devices_mcp.scene import SceneWatcher
from debug_devices_mcp.ui.constants import http, ingest
from debug_devices_mcp.ui.forward import read_token

logger = logging.getLogger(__name__)


class ScreenStart(BaseModel):
    serial: str


class ScreenStartResult(BaseModel):
    running: bool
    detail: str


class RemoteScreen:
    def __init__(
        self, remote: RemoteMonitor, directory: Path, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._remote = remote
        self._directory = directory
        self._http = httpx.AsyncClient(timeout=ingest.REQUEST_TIMEOUT.total_seconds(), transport=transport)
        # True after the primary started its stream for this server.
        self.active = False
        # The scene watcher (live tracking, scene changes) on the primary's frames: setup sets it.
        self.watcher: SceneWatcher | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _target(self) -> tuple[str, dict[str, str]] | None:
        identity = await self._remote.find_monitor()
        token = read_token(self._directory, self._remote.port) if identity is not None else None
        if identity is None or token is None:
            return None
        return identity.url.rstrip("/"), {ingest.TOKEN_HEADER: token}

    async def start(self, serial: str) -> ScreenStartResult:
        """Ask the primary to stream this phone. The result says if it runs, and why not."""
        target = await self._target()
        if target is None:
            self.active = False
            return ScreenStartResult(running=False, detail="no primary monitor")
        url, headers = target
        body = ScreenStart(serial=serial).model_dump_json()
        try:
            response = await self._http.post(
                f"{url}{ingest.SCREEN_START_PATH}",
                content=body,
                headers=headers | {http.CONTENT_TYPE_HEADER: http.JSON_MEDIA_TYPE},
            )
            result = ScreenStartResult.model_validate_json(response.content)
        except (httpx.HTTPError, ValueError) as exc:
            self.active = False
            return ScreenStartResult(running=False, detail=f"the primary monitor did not answer: {exc!r}")
        self.active = result.running
        return result

    async def feed(self) -> AsyncIterator[bytes]:
        """The newest phone screen frames of the primary, each one time (the scene watcher's frame feed)."""
        seq = 0
        source: tuple[str, str | None] | None = None
        while True:
            target = await self._target()
            if target is None:
                await asyncio.sleep(ingest.REMOTE_RETRY.total_seconds())
                continue
            url, headers = target
            # Another primary (a restart has a new token, or another port): its frames count from 0 again.
            if (url, headers.get(ingest.TOKEN_HEADER)) != source:
                source, seq = (url, headers.get(ingest.TOKEN_HEADER)), 0
            try:
                response = await self._http.get(
                    f"{url}{ingest.FRAME_PATH}", params={ingest.AFTER_PARAM: str(seq)}, headers=headers
                )
            except httpx.HTTPError as exc:
                logger.info("no phone screen frame from the primary monitor: %r", exc)
                await asyncio.sleep(ingest.REMOTE_RETRY.total_seconds())
                continue
            if response.status_code == httpx.codes.OK:
                seq = int(response.headers.get(ingest.FRAME_SEQ_HEADER, seq + 1))
                yield response.content
            await asyncio.sleep(ingest.FRAME_POLL.total_seconds())
