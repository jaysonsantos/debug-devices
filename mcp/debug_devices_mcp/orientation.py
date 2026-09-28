"""The phone image orientation: one transform for the monitor preview and phone_snapshot, and the flips state.

The geometry (the app has a portrait-locked activity and CameraX; docs/phone-api.md):

- The phone screen stream shows the camera image N in the natural portrait orientation of the phone.
- The page shows the screen turned clockwise by V: the Screen view choice (0/90/180/270), or in auto
  V = (360 - rotation_degrees) % 360.
- The app returns its still S = N turned clockwise by (360 - rotation_degrees): the rotation of the still.
- So the page shows S turned clockwise by T = (V + rotation_degrees) % 360: the remaining turn. It is 0 in auto.

phone_snapshot turns the still by T, then applies the user's flips (in the frame that the user sees). The same
`ImageTransform` maps every position back to the still ("true orientation") for the phone: focus points, overlay
boxes, and arrows. The phone preview flips are in the natural frame: with V = 90 or 270, they swap axes.
"""

import logging
from collections.abc import Callable

from debug_devices_mcp.app_start import AppStartWatch
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

QUARTER_TURN = 90
HALF_TURN = 180
FULL_TURN = 360
SCREEN_ROTATION_AUTO = "auto"

# region: transform


class ImageTransform(SnapshotOrientation):
    """The final transform of a phone still: a clockwise quarter turn, then the flips (in the turned frame)."""

    turn_degrees: int = 0

    @property
    def unchanged(self) -> bool:
        return not (self.turn_degrees % FULL_TURN or self.flip_horizontal or self.flip_vertical)

    @property
    def sideways(self) -> bool:
        return self.turn_degrees % HALF_TURN == QUARTER_TURN

    def describe(self) -> str:
        flips = super().describe()
        turn = self.turn_degrees % FULL_TURN
        if not turn:
            return flips
        return f"turned {turn}° clockwise to match the monitor view, {flips}"

    # Normalized points (0 to 1, origin top left). "from true": the still of the app -> the shown image.

    def point_from_true(self, x: float, y: float) -> tuple[float, float]:
        x, y = turn_point(x, y, self.turn_degrees)
        return (1 - x if self.flip_horizontal else x), (1 - y if self.flip_vertical else y)

    def point_to_true(self, x: float, y: float) -> tuple[float, float]:
        x, y = (1 - x if self.flip_horizontal else x), (1 - y if self.flip_vertical else y)
        return turn_point(x, y, FULL_TURN - self.turn_degrees % FULL_TURN)

    def box_to_true(self, x: float, y: float, width: float, height: float) -> tuple[float, float, float, float]:
        return _box(self.point_to_true(x, y), self.point_to_true(x + width, y + height))

    def box_from_true(self, x: float, y: float, width: float, height: float) -> tuple[float, float, float, float]:
        return _box(self.point_from_true(x, y), self.point_from_true(x + width, y + height))

    # Angles: 0 = right, 90 = down (clockwise, y down).

    def angle_from_true(self, angle: float) -> float:
        angle += self.turn_degrees
        if self.flip_horizontal:
            angle = HALF_TURN - angle
        if self.flip_vertical:
            angle = -angle
        return angle % FULL_TURN

    def angle_to_true(self, angle: float) -> float:
        if self.flip_vertical:
            angle = -angle
        if self.flip_horizontal:
            angle = HALF_TURN - angle
        return (angle - self.turn_degrees) % FULL_TURN

    def size_from_true(self, width: int, height: int) -> tuple[int, int]:
        return (height, width) if self.sideways else (width, height)


def _box(a: tuple[float, float], b: tuple[float, float]) -> tuple[float, float, float, float]:
    left, top = min(a[0], b[0]), min(a[1], b[1])
    return left, top, max(a[0], b[0]) - left, max(a[1], b[1]) - top


def turn_point(x: float, y: float, turn_degrees: int) -> tuple[float, float]:
    """A normalized point after a clockwise turn of the image by 0/90/180/270 degrees."""
    match turn_degrees % FULL_TURN:
        case 90:
            return 1 - y, x
        case 180:
            return 1 - x, 1 - y
        case 270:
            return y, 1 - x
        case _:
            return x, y


def as_transform(orientation: SnapshotOrientation) -> ImageTransform:
    if isinstance(orientation, ImageTransform):
        return orientation
    return ImageTransform(flip_horizontal=orientation.flip_horizontal, flip_vertical=orientation.flip_vertical)


def view_rotation(screen_rotation: str, rotation_degrees: int) -> int:
    """The clockwise turn of the phone screen on the page (the same rule as app.js viewRotation)."""
    if screen_rotation == SCREEN_ROTATION_AUTO:
        return (FULL_TURN - rotation_degrees) % FULL_TURN
    return int(screen_rotation) % FULL_TURN


def remaining_turn(view_degrees: int, still_rotation: int) -> int:
    """The clockwise turn from the app's still to the page view: T = (V + rotation_degrees) % 360."""
    return (view_degrees + still_rotation) % FULL_TURN


def still_transform(screen_rotation: str, rotation_degrees: int, flips: SnapshotOrientation) -> ImageTransform:
    turn = remaining_turn(view_rotation(screen_rotation, rotation_degrees), rotation_degrees)
    return ImageTransform(turn_degrees=turn, flip_horizontal=flips.flip_horizontal, flip_vertical=flips.flip_vertical)


def phone_preview_flips(flips: SnapshotOrientation, view_degrees: int) -> SnapshotOrientation:
    """The user's flips (in the page frame) as preview flips of the phone (in its natural frame)."""
    if view_degrees % HALF_TURN == QUARTER_TURN:
        return SnapshotOrientation(flip_horizontal=flips.flip_vertical, flip_vertical=flips.flip_horizontal)
    return flips


# endregion: transform

type OrientationListener = Callable[[SnapshotOrientation], None]


class OrientationState:
    """The flips. With a store, the settings file holds them (shared by all MCP servers); each read sees the file
    as it is now. Without a store (tests), they stay in memory."""

    def __init__(self, store: SettingsStore | None = None) -> None:
        self._store = store
        self._value = SnapshotOrientation()
        self._screen_rotation = SCREEN_ROTATION_AUTO
        self._listeners: list[OrientationListener] = []
        self._seen = self.current

    @property
    def current(self) -> SnapshotOrientation:
        if self._store is None:
            return self._value
        return self._store.current().snapshot_orientation or SnapshotOrientation()

    @property
    def screen_rotation(self) -> str:
        """The page's Screen view choice (auto, 0, 90, 180, 270), from the settings file (shared by all servers)."""
        if self._store is None:
            return self._screen_rotation
        saved = self._store.current().screen_rotation
        return str(saved.value) if saved is not None else SCREEN_ROTATION_AUTO

    def set_screen_rotation(self, value: str) -> None:
        """Tests without a store. The page saves the choice in the settings file."""
        self._screen_rotation = value

    def transform(self, rotation_degrees: int) -> ImageTransform:
        """The transform of a still that the app takes now with this `rotation_degrees`."""
        return still_transform(self.screen_rotation, rotation_degrees, self.current)

    def refresh(self) -> None:
        """Tell the listeners when another MCP server changed the file."""
        current = self.current
        if current != self._seen:
            self._seen = current
            for listener in self._listeners:
                listener(current)

    def add_listener(self, listener: OrientationListener) -> None:
        self._listeners.append(listener)

    def update(self, flip_horizontal: bool | None = None, flip_vertical: bool | None = None) -> SnapshotOrientation:
        """Change the given flips (None keeps a flip), save, and tell the listeners."""
        before = self.current
        updated = SnapshotOrientation(
            flip_horizontal=before.flip_horizontal if flip_horizontal is None else flip_horizontal,
            flip_vertical=before.flip_vertical if flip_vertical is None else flip_vertical,
        )
        self._value = updated
        if self._store is not None:
            try:
                saved = self._store.load()
                self._store.save(saved.model_copy(update={"snapshot_orientation": updated}))
            except OSError as exc:
                logger.warning("cannot save the snapshot orientation: %s", exc)
        self._seen = updated
        for listener in self._listeners:
            listener(updated)
        return updated


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
        self._watch = AppStartWatch("preview flips")
        # The still rotation of the last status: in auto, the page view turn follows it.
        self._rotation_degrees = 0
        # The preview flips that this server sent last (in the phone's natural frame).
        self._sent: SnapshotOrientation | None = None

    def desired(self) -> SnapshotOrientation:
        """The preview flips that make the page view show the user's flips (see the module docstring)."""
        view = view_rotation(self._orientation.screen_rotation, self._rotation_degrees)
        return phone_preview_flips(self._orientation.current, view)

    def matches(self, status: CameraStatus) -> bool:
        desired = self.desired()
        return (status.preview_flip_horizontal, status.preview_flip_vertical) == (
            desired.flip_horizontal,
            desired.flip_vertical,
        )

    def reset(self) -> None:
        """A new phone_connect: try the endpoint again (the app can have an update now)."""
        self._unsupported = False
        self._watch.reset()

    async def push(self) -> CameraStatus | None:
        """Send the current flips. Return the new status, or None when the phone does not take them now."""
        if self._unsupported:
            return None
        desired = self.desired()
        request = PreviewFlipRequest(flip_horizontal=desired.flip_horizontal, flip_vertical=desired.flip_vertical)
        try:
            status = await self._phone.preview(request)
            self._sent = desired
            return status
        except PreviewNotSupportedError as exc:
            self._unsupported = True
            logger.warning("%s: the phone preview is not flipped; the snapshots still are", exc)
        except PhoneError as exc:
            # Not connected yet, or the camera is not ready: phone_connect and the status reads send it again.
            logger.info("phone preview flips not sent: %s", exc)
        return None

    async def ensure(self, status: CameraStatus) -> CameraStatus:
        """Send the flips after an app restart (or phone_connect) when the phone shows other ones. Never only because
        they differ: another client may have changed them."""
        self._rotation_degrees = status.rotation_degrees
        may_send = self._watch.may_send(status)
        if self.matches(status):
            self._sent = self.desired()
            return status
        # The wanted preview flips changed: the user changed the flips or the Screen view (maybe through another
        # server: all servers compute the same value), or the page view turned with the phone (auto).
        if self._sent is not None and self.desired() != self._sent:
            return await self.push() or status
        if not may_send:
            flips = f"flip_horizontal={status.preview_flip_horizontal}, flip_vertical={status.preview_flip_vertical}"
            self._watch.other_client(status, flips)
            return status
        return await self.push() or status
