"""Share the webcam with the monitor of another debug-devices MCP process.

Only one process can read a V4L2 device. When the webcam is busy and a debug-devices monitor answers on
127.0.0.1, this process takes its frames from that monitor, already cropped with the crop of that monitor.

The monitor is looked for on this process's own page port first, then on `other_ports` (for the webcam: the default
page port and the ports of the running pages in the runtime dir), so a server started with another `--ui-port` (a
test, a second client) still finds the running monitor (QA round 11).
"""

import logging
import os
from collections.abc import AsyncIterator, Callable, Iterable
from datetime import timedelta
from pathlib import Path

import httpx
from pydantic import BaseModel, ValidationError

from debug_devices_mcp.ui.constants import APP_NAME, defaults, http, remote
from debug_devices_mcp.webcam import JPEG_MAGIC, Crop, WebcamError
from debug_devices_mcp.webcam_stream import FrameSource

logger = logging.getLogger(__name__)

OK = 200


class MonitorIdentity(BaseModel):
    """The answer of `/api/whoami`. `app` tells a debug-devices monitor from other local services."""

    app: str
    pid: int
    url: str
    webcam: str | None
    webcam_running: bool
    webcam_crop: Crop | None


class RemoteUnavailableError(WebcamError):
    """No other debug-devices monitor answers, or it cannot give a frame."""


class RemoteCropMissingError(WebcamError):
    """The monitor that owns the webcam has no crop box: its frames are whole webcam frames (N51 of QA round 12).
    Not a RemoteUnavailableError: this process must not read the webcam itself instead."""


REMOTE_CROP_MISSING = (
    "the webcam belongs to the debug-devices monitor at {url}, and it has no crop box: set the crop box on the page "
    "that owns the webcam. No frame was sent anywhere (a whole webcam frame can show people)."
)


def is_busy(error: WebcamError) -> bool:
    return remote.BUSY_MARKER in str(error).lower()


class RemoteMonitor:
    """A client for the monitor of another MCP process on the same PC."""

    def __init__(
        self,
        port: int,
        timeout: timedelta,
        host: str = defaults.HOST,
        transport: httpx.AsyncBaseTransport | None = None,
        other_ports: Callable[[], Iterable[int]] | None = None,
    ) -> None:
        self.port = port
        self._host = host
        self._other_ports = other_ports
        self.base_url = self._url(port)
        self._http = httpx.AsyncClient(base_url=self.base_url, timeout=timeout.total_seconds(), transport=transport)

    def _url(self, port: int) -> str:
        return f"{http.SCHEME}://{self._host}:{port}"

    async def aclose(self) -> None:
        await self._http.aclose()

    def _candidates(self) -> list[int]:
        """The own port first, then the other ports, each one time."""
        others = list(self._other_ports()) if self._other_ports is not None else []
        return list(dict.fromkeys([self.port, *others]))

    async def _whoami(self, port: int) -> MonitorIdentity | None:
        try:
            response = await self._http.get(
                f"{self._url(port)}{remote.WHOAMI_PATH}", timeout=remote.PROBE_TIMEOUT.total_seconds()
            )
            identity = MonitorIdentity.model_validate_json(response.content) if response.status_code == OK else None
        except httpx.HTTPError, ValidationError:
            return None
        if identity is None or identity.app != APP_NAME or identity.pid == os.getpid():
            return None
        return identity

    async def _find(self, accept: Callable[[MonitorIdentity], bool]) -> MonitorIdentity | None:
        """The first candidate port with a debug-devices monitor of another process that `accept`s. The next
        requests (info, stream, frames) go to that port."""
        for port in self._candidates():
            identity = await self._whoami(port)
            if identity is not None and accept(identity):
                if self._url(port) != self.base_url:
                    logger.info("a debug-devices monitor answers on port %s", port)
                self.base_url = self._url(port)
                self._http.base_url = self.base_url
                return identity
        # None found: the next request is for the own port again (not the port of a monitor that is gone).
        self.base_url = self._url(self.port)
        self._http.base_url = self.base_url
        return None

    async def find_monitor(self) -> MonitorIdentity | None:
        """The debug-devices monitor of another process, with or without a webcam stream."""
        return await self._find(lambda _: True)

    async def identify(self, device: Path) -> MonitorIdentity | None:
        """The other monitor, when it is a debug-devices monitor of another process that streams `device`."""
        return await self._find(lambda identity: identity.webcam == str(device) and identity.webcam_running)

    async def info(self) -> bytes:
        """The raw `/api/webcam/info` JSON of the other monitor."""
        try:
            response = await self._http.get(remote.INFO_PATH)
        except httpx.HTTPError as exc:
            raise RemoteUnavailableError(f"the monitor at {self.base_url} does not answer: {exc!r}") from exc
        if response.status_code != OK:
            raise RemoteUnavailableError(f"the monitor at {self.base_url} gave no webcam info ({response.status_code})")
        return response.content

    async def stream(self) -> AsyncIterator[bytes]:
        """The MJPEG bytes of the live view of the other monitor, for the page of this process."""
        async with self._http.stream("GET", remote.STREAM_PATH, timeout=None) as response:
            if response.status_code != OK:
                raise RemoteUnavailableError(f"the monitor at {self.base_url} gave no stream ({response.status_code})")
            async for chunk in response.aiter_bytes():
                yield chunk

    async def capture_jpeg(self) -> bytes:
        """The next frame of the other monitor, cropped with its crop."""
        try:
            response = await self._http.get(remote.FRAME_PATH, params={remote.CROPPED_PARAM: remote.TRUE})
        except httpx.HTTPError as exc:
            raise RemoteUnavailableError(f"the monitor at {self.base_url} does not answer: {exc!r}") from exc
        if response.status_code != OK or not response.content.startswith(JPEG_MAGIC):
            raise RemoteUnavailableError(
                f"the monitor at {self.base_url} gave no frame ({response.status_code}): "
                f"{response.text[: remote.ERROR_PREVIEW_CHARS]}"
            )
        return response.content


class SharedWebcam:
    """A `FrameSource` that reads the local webcam, or the monitor of another process when the webcam is busy."""

    def __init__(
        self,
        local: FrameSource,
        remote_monitor: RemoteMonitor,
        start_local: Callable[[], None] | None = None,
    ) -> None:
        self.local = local
        self.remote = remote_monitor
        self._start_local = start_local
        self.remote_identity: MonitorIdentity | None = None

    @property
    def device(self) -> Path:
        return self.local.device

    @property
    def crop(self) -> Crop | None:
        return self.remote_identity.webcam_crop if self.remote_identity is not None else self.local.crop

    def set_start_local(self, start_local: Callable[[], None]) -> None:
        """Called when the other monitor stops answering, before this process reads the local webcam again."""
        self._start_local = start_local

    async def use_remote_if_present(self) -> bool:
        """Switch to the other monitor when one answers. Return True when this process now uses it."""
        identity = await self.remote.identify(self.device)
        if identity is not None and self.remote_identity is None:
            logger.warning("webcam %s: using the frames of the monitor at %s", self.device, identity.url)
        self.remote_identity = identity
        return identity is not None

    async def capture_jpeg(self) -> bytes:
        if self.remote_identity is not None:
            try:
                return await self._remote_capture()
            except RemoteUnavailableError as exc:
                logger.warning("webcam %s: %s. Using the local webcam again.", self.device, exc)
                self.remote_identity = None
                if self._start_local is not None:
                    self._start_local()
        try:
            return await self.local.capture_jpeg()
        except WebcamError as exc:
            if not is_busy(exc):
                raise
            if await self.use_remote_if_present():
                return await self._remote_capture()
            raise WebcamError(
                f"{exc}. Another program uses {self.device}, and no debug-devices monitor answers on "
                f"{self.remote.base_url}. Close the other program, or start one MCP server with the monitor."
            ) from exc

    async def _remote_capture(self) -> bytes:
        # Ask for the identity again: the crop can change, and the port can belong to a new process now.
        identity = await self.remote.identify(self.device)
        if identity is None:
            raise RemoteUnavailableError(f"no debug-devices monitor answers on {self.remote.base_url} anymore")
        self.remote_identity = identity
        if identity.webcam_crop is None:
            # Frames of another monitor only with its crop box (N51 of QA round 12): no whole frame leaves.
            raise RemoteCropMissingError(REMOTE_CROP_MISSING.format(url=identity.url))
        return await self.remote.capture_jpeg()
