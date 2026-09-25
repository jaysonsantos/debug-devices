"""The phone snapshot orientation (flips): one state for the tools, the monitor page, and the settings file."""

import logging
from collections.abc import Callable

from debug_devices_mcp.images import SnapshotOrientation
from debug_devices_mcp.phone_api import (
    CameraStatus,
    PhoneClient,
    PhoneError,
    PreviewFlipRequest,
    PreviewNotSupportedError,
)
from debug_devices_mcp.ui.settings import SettingsStore

logger = logging.getLogger(__name__)

type OrientationListener = Callable[[SnapshotOrientation], None]


class OrientationState:
    """The current flips. It reads the settings file at start and saves each change there (also without the UI)."""

    def __init__(self, store: SettingsStore | None = None) -> None:
        self._store = store
        saved = store.load().snapshot_orientation if store is not None else None
        self.current = saved or SnapshotOrientation()
        self._listeners: list[OrientationListener] = []

    def add_listener(self, listener: OrientationListener) -> None:
        self._listeners.append(listener)

    def update(self, flip_horizontal: bool | None = None, flip_vertical: bool | None = None) -> SnapshotOrientation:
        """Change the given flips (None keeps a flip), save, and tell the listeners."""
        self.current = SnapshotOrientation(
            flip_horizontal=self.current.flip_horizontal if flip_horizontal is None else flip_horizontal,
            flip_vertical=self.current.flip_vertical if flip_vertical is None else flip_vertical,
        )
        if self._store is not None:
            try:
                saved = self._store.load()
                self._store.save(saved.model_copy(update={"snapshot_orientation": self.current}))
            except OSError as exc:
                logger.warning("cannot save the snapshot orientation: %s", exc)
        for listener in self._listeners:
            listener(self.current)
        return self.current


class PreviewSync:
    """Send the snapshot flips to the phone preview (POST /v1/preview), so the phone screen matches the snapshots.

    The app mirrors only its camera preview; its status text stays readable. The snapshot stays unflipped on the
    phone: the server flips it (`orient_jpeg`), so nothing is flipped twice.
    """

    def __init__(self, phone: PhoneClient, orientation: OrientationState) -> None:
        self._phone = phone
        self._orientation = orientation
        # An app from before POST /v1/preview answers 404: warn one time, then stop sending until phone_connect.
        self._unsupported = False

    def matches(self, status: CameraStatus) -> bool:
        current = self._orientation.current
        return (status.preview_flip_horizontal, status.preview_flip_vertical) == (
            current.flip_horizontal,
            current.flip_vertical,
        )

    def reset(self) -> None:
        """A new phone_connect: try the endpoint again (the app can have an update now)."""
        self._unsupported = False

    async def push(self) -> CameraStatus | None:
        """Send the current flips. Return the new status, or None when the phone does not take them now."""
        if self._unsupported:
            return None
        current = self._orientation.current
        request = PreviewFlipRequest(flip_horizontal=current.flip_horizontal, flip_vertical=current.flip_vertical)
        try:
            return await self._phone.preview(request)
        except PreviewNotSupportedError as exc:
            self._unsupported = True
            logger.warning("%s: the phone preview is not flipped; the snapshots still are", exc)
        except PhoneError as exc:
            # Not connected yet, or the camera is not ready: phone_connect and the status reads send it again.
            logger.info("phone preview flips not sent: %s", exc)
        return None

    async def ensure(self, status: CameraStatus) -> CameraStatus:
        """Send the flips when the phone shows other ones (for example after an app restart)."""
        if self.matches(status):
            return status
        return await self.push() or status
