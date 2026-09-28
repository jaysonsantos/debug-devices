"""The user's in-sensor zoom choice: one state for the tool, the monitor page, and the settings file, and its sync.

POST /v1/camera binds the camera again (the preview stops for about 1 s). So the sync sends the choice only when the
status clearly differs (choice on and status off after an app restart, or choice off and status on). It never
sends for `unsupported` or `fallback`: the phone cannot do it, and a new request would only stop the preview again.
"""

import logging
from collections.abc import Callable
from http import HTTPStatus

from debug_devices_mcp.app_start import AppStartWatch
from debug_devices_mcp.phone_api import (
    AfMode,
    AfModeName,
    CameraSettingsNotSupportedError,
    CameraSettingsRequest,
    CameraStatus,
    InSensorZoom,
    PhoneApiError,
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
        self._value = False
        self._listeners: list[ChoiceListener] = []
        self._seen = self.enabled

    @property
    def enabled(self) -> bool:
        """The choice in the settings file now (shared by all MCP servers), or in memory without a store."""
        if self._store is None:
            return self._value
        return bool(self._store.current().in_sensor_zoom)

    def add_listener(self, listener: ChoiceListener) -> None:
        self._listeners.append(listener)

    def refresh(self) -> None:
        """Tell the listeners when another MCP server changed the file."""
        enabled = self.enabled
        if enabled != self._seen:
            self._seen = enabled
            for listener in self._listeners:
                listener(enabled)

    def set(self, enabled: bool) -> None:
        self._value = enabled
        self._seen = enabled
        if self._store is not None:
            try:
                self._store.update(lambda saved: saved.model_copy(update={"in_sensor_zoom": enabled}))
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
        self._watch = AppStartWatch("in-sensor zoom")

    def reset(self) -> None:
        self._unsupported = False
        self._watch.reset()

    def needs_send(self, status: CameraStatus) -> bool:
        current = status.in_sensor_zoom or InSensorZoom.OFF
        if self._choice.enabled:
            return current is InSensorZoom.OFF
        return current is InSensorZoom.ON

    async def send(self) -> CameraStatus:
        """Send the choice now. Raise `CameraSettingsNotSupportedError` for an old app (the tool reports it)."""
        return await self._phone.camera(CameraSettingsRequest(in_sensor_zoom=self._choice.enabled))

    async def ensure(self, status: CameraStatus) -> CameraStatus:
        """Send the choice after an app restart (or phone_connect) when the phone differs; see app_start.py."""
        may_send = self._watch.may_send(status)
        if self._unsupported or not self.needs_send(status):
            return status
        if not may_send:
            self._watch.other_client(status, str(status.in_sensor_zoom))
            return status
        try:
            return await self.send()
        except CameraSettingsNotSupportedError as exc:
            self._unsupported = True
            logger.warning("%s", exc)
        except PhoneError as exc:
            self._watch.failed()
            logger.info("in-sensor zoom choice not sent: %s", exc)
        return status


# region: autofocus mode

type AfModeListener = Callable[[AfModeName], None]
DEFAULT_AF_MODE: AfModeName = "continuous"
AF_MODE_UNKNOWN_TO_APP = (
    "the phone app does not know af_mode (POST /v1/camera answered 400); update the phone app for the macro focus"
)
AF_MODE_NOT_ON_PHONE = "this phone has no macro autofocus mode: the camera stays in continuous autofocus"


class AfModeChoice:
    """The autofocus mode choice (default continuous), saved in the settings file like the in-sensor zoom."""

    def __init__(self, store: SettingsStore | None = None) -> None:
        self._store = store
        self._value: AfModeName = DEFAULT_AF_MODE
        self._listeners: list[AfModeListener] = []
        self._seen = self.mode

    @property
    def mode(self) -> AfModeName:
        """The choice in the settings file now (shared by all MCP servers), or in memory without a store."""
        if self._store is None:
            return self._value
        return self._store.current().af_mode or DEFAULT_AF_MODE

    def add_listener(self, listener: AfModeListener) -> None:
        self._listeners.append(listener)

    def refresh(self) -> None:
        """Tell the listeners when another MCP server changed the file."""
        mode = self.mode
        if mode != self._seen:
            self._seen = mode
            for listener in self._listeners:
                listener(mode)

    def set(self, mode: AfModeName) -> None:
        self._value = mode
        self._seen = mode
        if self._store is not None:
            try:
                self._store.update(lambda saved: saved.model_copy(update={"af_mode": mode}))
            except OSError as exc:
                logger.warning("cannot save the autofocus mode choice: %s", exc)
        for listener in self._listeners:
            listener(mode)


class AfModeUnknownToAppError(PhoneError):
    """The app answered 400 to `af_mode`: it is from before the autofocus mode."""


class AfModeSync:
    """Send the choice after phone_connect and after an app restart (the status shows `continuous` again).

    A phone without the macro mode answers 200 and stays `continuous`: then the sync stops until the next
    phone_connect, so it does not send again on each status poll. An app without `af_mode` is left alone.
    """

    def __init__(self, phone: PhoneClient, choice: AfModeChoice) -> None:
        self._phone = phone
        self._choice = choice
        self._stopped = False
        self._watch = AppStartWatch("autofocus mode")

    def reset(self) -> None:
        self._stopped = False
        self._watch.reset()

    def needs_send(self, status: CameraStatus) -> bool:
        if status.af_mode is None or status.af_mode is AfMode.UNKNOWN:
            return False
        return status.af_mode.value != self._choice.mode

    async def send(self) -> CameraStatus:
        """Send the choice now. An app without `af_mode` raises PhoneError with a clear message."""
        try:
            status = await self._phone.camera(CameraSettingsRequest(af_mode=self._choice.mode))
        except PhoneApiError as exc:
            if exc.status == HTTPStatus.BAD_REQUEST:
                raise AfModeUnknownToAppError(AF_MODE_UNKNOWN_TO_APP) from exc
            raise
        self._stopped = self.needs_send(status)
        return status

    async def ensure(self, status: CameraStatus) -> CameraStatus:
        """Send the choice after an app restart (or phone_connect) when the phone differs; see app_start.py."""
        may_send = self._watch.may_send(status)
        if self._stopped or not self.needs_send(status):
            return status
        if not may_send:
            self._watch.other_client(status, str(status.af_mode))
            return status
        try:
            return await self.send()
        except (CameraSettingsNotSupportedError, AfModeUnknownToAppError) as exc:
            # The app cannot do it: stop until the next phone_connect.
            self._stopped = True
            logger.warning("%s", exc)
        except PhoneError as exc:
            # Only this send failed (for example the camera was not ready): the next status reads try again.
            self._watch.failed()
            logger.info("autofocus mode choice not sent: %s", exc)
        return status


# endregion: autofocus mode


# region: markings


type MarkingsListener = Callable[[bool], None]


class MarkingsChoice:
    """Show or hide the markings (default shown), saved in the settings file like the other choices."""

    def __init__(self, store: SettingsStore | None = None) -> None:
        self._store = store
        self._value = True
        self._listeners: list[MarkingsListener] = []
        self._seen = self.visible

    @property
    def visible(self) -> bool:
        if self._store is None:
            return self._value
        saved = self._store.current().markings_visible
        return True if saved is None else saved

    def add_listener(self, listener: MarkingsListener) -> None:
        self._listeners.append(listener)

    def refresh(self) -> None:
        """Tell the listeners when another MCP server changed the file."""
        visible = self.visible
        if visible != self._seen:
            self._seen = visible
            for listener in self._listeners:
                listener(visible)

    def set(self, visible: bool) -> None:
        self._value = visible
        self._seen = visible
        if self._store is not None:
            try:
                self._store.update(lambda saved: saved.model_copy(update={"markings_visible": visible}))
            except OSError as exc:
                logger.warning("cannot save the markings choice: %s", exc)
        for listener in self._listeners:
            listener(visible)


class MarkingsSync:
    """Send the choice to the phone at phone_connect and after an app restart (the app starts with them shown).

    The rule of app_start.py: never send again only because the status differs. An old app (no `overlay_visible`)
    gets nothing here: while the markings are hidden, Services keeps the boxes off the phone.
    """

    def __init__(self, phone: PhoneClient, choice: MarkingsChoice) -> None:
        self._phone = phone
        self._choice = choice
        self._watch = AppStartWatch("markings visibility")

    def reset(self) -> None:
        self._watch.reset()

    async def send(self) -> CameraStatus:
        """Send the choice now. An old app raises OverlayNotSupportedError (it cannot hide its boxes)."""
        return await self._phone.overlay_visibility(self._choice.visible)

    async def ensure(self, status: CameraStatus) -> CameraStatus:
        may_send = self._watch.may_send(status)
        if status.overlay_visible is None or status.overlay_visible == self._choice.visible:
            return status
        if not may_send:
            self._watch.other_client(status, f"overlay_visible={status.overlay_visible}")
            return status
        try:
            return await self.send()
        except PhoneError as exc:
            self._watch.failed()
            logger.info("markings visibility not sent: %s", exc)
        return status


# endregion: markings
