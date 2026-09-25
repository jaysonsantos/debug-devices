"""The user's in-sensor zoom choice: one state for the tool, the monitor page, and the settings file, and its sync.

POST /v1/camera binds the camera again (the preview stops for about 1 s). So the sync sends the choice only when the
status clearly differs (choice on and status off after an app restart, or choice off and status on). It never
sends for `unsupported` or `fallback`: the phone cannot do it, and a new request would only stop the preview again.
"""

import logging
from collections.abc import Callable

from debug_devices_mcp.phone_api import (
    CameraSettingsNotSupportedError,
    CameraSettingsRequest,
    CameraStatus,
    InSensorZoom,
    PhoneClient,
    PhoneError,
)
from debug_devices_mcp.ui.settings import SettingsStore

logger = logging.getLogger(__name__)

type ChoiceListener = Callable[[bool], None]


class InSensorZoomChoice:
    """The choice (default off). It reads the settings file at start and saves each change there (also without UI)."""

    def __init__(self, store: SettingsStore | None = None) -> None:
        self._store = store
        saved = store.load().in_sensor_zoom if store is not None else None
        self.enabled = bool(saved)
        self._listeners: list[ChoiceListener] = []

    def add_listener(self, listener: ChoiceListener) -> None:
        self._listeners.append(listener)

    def set(self, enabled: bool) -> None:
        self.enabled = enabled
        if self._store is not None:
            try:
                saved = self._store.load()
                self._store.save(saved.model_copy(update={"in_sensor_zoom": enabled}))
            except OSError as exc:
                logger.warning("cannot save the in-sensor zoom choice: %s", exc)
        for listener in self._listeners:
            listener(enabled)


class InSensorZoomSync:
    """Send the choice to the app after phone_connect and after an app restart (the status shows the other value)."""

    def __init__(self, phone: PhoneClient, choice: InSensorZoomChoice) -> None:
        self._phone = phone
        self._choice = choice
        # An app from before POST /v1/camera answers 404: warn one time, then stop until the next phone_connect.
        self._unsupported = False

    def reset(self) -> None:
        self._unsupported = False

    def needs_send(self, status: CameraStatus) -> bool:
        current = status.in_sensor_zoom or InSensorZoom.OFF
        if self._choice.enabled:
            return current is InSensorZoom.OFF
        return current is InSensorZoom.ON

    async def send(self) -> CameraStatus:
        """Send the choice now. Raise `CameraSettingsNotSupportedError` for an old app (the tool reports it)."""
        return await self._phone.camera(CameraSettingsRequest(in_sensor_zoom=self._choice.enabled))

    async def ensure(self, status: CameraStatus) -> CameraStatus:
        if self._unsupported or not self.needs_send(status):
            return status
        try:
            return await self.send()
        except CameraSettingsNotSupportedError as exc:
            self._unsupported = True
            logger.warning("%s", exc)
        except PhoneError as exc:
            logger.info("in-sensor zoom choice not sent: %s", exc)
        return status
