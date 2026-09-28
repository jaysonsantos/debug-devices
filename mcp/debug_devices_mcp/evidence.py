"""Capture ids: every phone_snapshot and every multimeter_read image gets a UUID v7 and a UTC capture time.

A statement about what a photo shows (probe position, contact, part position) names the capture id of a current
phone_snapshot. A scene change (the board or the phone moved) starts a new scene epoch: older photos stay as
records, but they are no longer valid for new position claims.
"""

import uuid
from collections import OrderedDict
from datetime import UTC, datetime
from enum import StrEnum

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ImageContent
from pydantic import AwareDatetime, BaseModel

from debug_devices_mcp.multimeter import MeterResult
from debug_devices_mcp.orientation import ImageTransform
from debug_devices_mcp.phone_api import CameraStatus
from debug_devices_mcp.scene import SceneState

# The log keeps this many captures; older ids are "unknown" after that.
MAX_CAPTURES = 500
META_CAPTURE_ID = "capture_id"
META_CAPTURED_AT = "captured_at"
# Zoom ratios equal to this many decimals are the same view.
ZOOM_DECIMALS = 3
# Two photo sizes have the same shape (one is the other scaled) when their width/height ratios differ at most this much.
SAME_SHAPE_TOLERANCE = 0.02
MOVED_REASON = "the board or the phone moved after this photo: take a fresh phone_snapshot"


class CaptureKind(StrEnum):
    PHONE_SNAPSHOT = "phone_snapshot"
    # The exact image that multimeter_read sent to the vision model (webcam frame or phone still of the meter).
    METER_IMAGE = "meter_image"


class CameraView(BaseModel):
    """What decides the pixel geometry of a phone photo: zoom, sensor mode, lens, and the image transform.

    Two photos of the same scene with the same view have the same pixel positions for the same board point.
    """

    zoom_ratio: float
    in_sensor_zoom: str | None
    focal_length_mm: float | None
    turn_degrees: int
    flip_horizontal: bool
    flip_vertical: bool
    # The size of the image that the agent got (after max_side). Not part of the view: another size of the same view
    # is the same photo scaled, and the pixel tools scale the positions (photo_scale).
    width_px: int | None = None
    height_px: int | None = None

    def differences(self, other: CameraView) -> list[str]:
        return [name for name in GEOMETRY_FIELDS if getattr(self, name) != getattr(other, name)]

    def with_size(self, width_px: int, height_px: int) -> CameraView:
        return self.model_copy(update={"width_px": width_px, "height_px": height_px})

    @classmethod
    def of(cls, status: CameraStatus, transform: ImageTransform) -> CameraView:
        return cls(
            zoom_ratio=round(status.zoom_ratio, ZOOM_DECIMALS),
            in_sensor_zoom=str(status.in_sensor_zoom) if status.in_sensor_zoom is not None else None,
            focal_length_mm=status.optics.focal_length_mm if status.optics is not None else None,
            turn_degrees=transform.turn_degrees,
            flip_horizontal=transform.flip_horizontal,
            flip_vertical=transform.flip_vertical,
        )


# The fields that decide the pixel geometry; the image size only scales it.
GEOMETRY_FIELDS = (
    "zoom_ratio",
    "in_sensor_zoom",
    "focal_length_mm",
    "turn_degrees",
    "flip_horizontal",
    "flip_vertical",
)


class PhotoScaleError(ToolError):
    """The pixels come from a photo with another shape than the registered photo: they cannot be scaled."""


def photo_scale(photo_size: tuple[int, int] | None, registered_size: tuple[int, int]) -> tuple[float, float]:
    """Factors from pixels of a photo of `photo_size` to pixels of the registered photo (1, 1 when unknown)."""
    if photo_size is None:
        return 1.0, 1.0
    scale_x, scale_y = registered_size[0] / photo_size[0], registered_size[1] / photo_size[1]
    if abs(scale_x - scale_y) > SAME_SHAPE_TOLERANCE * max(scale_x, scale_y):
        raise PhotoScaleError(
            f"the photo ({photo_size[0]}x{photo_size[1]} px) has another shape than the registered photo "
            f"({registered_size[0]}x{registered_size[1]} px): register this photo"
        )
    return scale_x, scale_y


class Capture(BaseModel):
    capture_id: str
    captured_at: AwareDatetime
    kind: CaptureKind
    source: str
    # The scene epoch at capture time. A scene change starts a new epoch.
    scene_epoch: int
    # Phone snapshots: the camera view (zoom, lens, transform). None when it is not known.
    view: CameraView | None = None


class CaptureStatus(BaseModel):
    capture_id: str
    capture: Capture | None
    # True only for a phone_snapshot of the current scene: a position claim can name it.
    valid_for_position_claims: bool
    reason: str


class StaleCaptureError(ToolError):
    """A tool got a capture id that is unknown, not a phone_snapshot, or from an older scene."""


class CaptureLog:
    def __init__(self, scene: SceneState | None = None) -> None:
        self._scene = scene
        self._captures: OrderedDict[str, Capture] = OrderedDict()
        self.epoch = 0
        # Why each epoch started (the scene change reason): a photo of an older epoch is stale for the reason of the
        # first change after it.
        self._epoch_reasons: dict[int, str] = {}
        # The checked result of each multimeter_read, by the capture id of its image (bench_state uses it).
        self.meter_results: dict[str, MeterResult] = {}

    def attach(self, scene: SceneState) -> None:
        """Follow the scene: a change starts a new epoch."""
        self._scene = scene
        scene.add_listener(self._on_scene_change)

    async def _on_scene_change(self, _: datetime) -> None:
        self.scene_changed(self._scene.change_reason if self._scene is not None else None)

    def scene_changed(self, reason: str | None = None) -> None:
        """A new epoch. `reason`: why (for example no scene watcher and a zoom change); None: the board moved."""
        self.epoch += 1
        self._epoch_reasons[self.epoch] = reason or MOVED_REASON

    def stale_reason(self, capture: Capture) -> str:
        if capture.scene_epoch != self.epoch:
            return self._epoch_reasons.get(capture.scene_epoch + 1, MOVED_REASON)
        if self._scene is not None and self._scene.change_reason is not None:
            return self._scene.change_reason
        return MOVED_REASON

    def record(self, kind: CaptureKind, source: str, view: CameraView | None = None) -> Capture:
        capture = Capture(
            capture_id=str(uuid.uuid7()),
            captured_at=datetime.now(UTC),
            kind=kind,
            source=source,
            scene_epoch=self.epoch,
            view=view,
        )
        self._captures[capture.capture_id] = capture
        while len(self._captures) > MAX_CAPTURES:
            old_id, _ = self._captures.popitem(last=False)
            self.meter_results.pop(old_id, None)
        return capture

    def get(self, capture_id: str) -> Capture | None:
        return self._captures.get(capture_id)

    def photo_size(self, capture_id: str | None) -> tuple[int, int] | None:
        """The size of the image that the agent got for this phone_snapshot (None: the latest), when known."""
        capture = self._captures.get(capture_id) if capture_id is not None else self.latest_photo
        view = capture.view if capture is not None else None
        if view is None or view.width_px is None or view.height_px is None:
            return None
        return view.width_px, view.height_px

    @property
    def latest_photo(self) -> Capture | None:
        photos = [item for item in self._captures.values() if item.kind is CaptureKind.PHONE_SNAPSHOT]
        return photos[-1] if photos else None

    def status(self, capture_id: str) -> CaptureStatus:
        capture = self._captures.get(capture_id)
        if capture is None:
            reason = "unknown capture id (not from this server session, or too old)"
            return CaptureStatus(capture_id=capture_id, capture=None, valid_for_position_claims=False, reason=reason)
        if capture.kind is not CaptureKind.PHONE_SNAPSHOT:
            reason = f"a {capture.kind} is not a phone_snapshot: it cannot support a position claim"
        elif capture.scene_epoch != self.epoch or (self._scene is not None and self._scene.changed):
            reason = self.stale_reason(capture)
        else:
            latest = self.latest_photo
            newer = latest is not None and latest.capture_id != capture.capture_id
            reason = "current scene" + (" (a newer phone_snapshot exists)" if newer else "")
            return CaptureStatus(capture_id=capture_id, capture=capture, valid_for_position_claims=True, reason=reason)
        return CaptureStatus(capture_id=capture_id, capture=capture, valid_for_position_claims=False, reason=reason)

    def reuse_problem(self, registered_photo_id: str | None, photo_id: str | None = None) -> str | None:
        """Can pixel positions of a phone_snapshot (`photo_id`; None: the latest) use a registration of another photo?

        Yes (None) when it is the same photo, or when both are phone snapshots of the current scene with the same
        camera view (another image size is fine: the tools scale the pixels). Otherwise the reason.
        """
        photo = self._captures.get(photo_id) if photo_id is not None else self.latest_photo
        if photo_id is not None and photo is None:
            return "the photo is not known (not from this server session, or too old): take a fresh phone_snapshot"
        # A registration of a photo that is not a phone_snapshot of this session cannot be checked here.
        if registered_photo_id is None or photo is None or registered_photo_id == photo.capture_id:
            return None
        registered = self._captures.get(registered_photo_id)
        problem = None
        if registered is None:
            problem = "the registered photo is not known any more: register your last phone_snapshot"
        elif registered.scene_epoch != self.epoch or photo.scene_epoch != self.epoch:
            problem = "the board or the phone moved after the registered photo: register your last phone_snapshot"
        elif registered.view is None or photo.view is None:
            problem = "the camera view of a photo is not known: register your last phone_snapshot"
        elif changed := registered.view.differences(photo.view):
            problem = (
                f"the camera view changed after the registered photo ({', '.join(changed)}): register your last "
                "phone_snapshot again"
            )
        return problem

    def require_current_photo(self, capture_id: str) -> Capture:
        """The capture for a new position claim. Refuses an unknown, non-photo, or stale id."""
        status = self.status(capture_id)
        if not status.valid_for_position_claims or status.capture is None:
            raise StaleCaptureError(f"capture {capture_id}: {status.reason}")
        return status.capture


def tag_image(image: ImageContent, capture: Capture) -> ImageContent:
    """Put the capture id and time on the image block, so the image and the result name the same capture."""
    meta = {
        **(image.meta or {}),
        META_CAPTURE_ID: capture.capture_id,
        META_CAPTURED_AT: capture.captured_at.isoformat(),
    }
    return image.model_copy(update={"meta": meta})


def register_evidence_tools(server: MCPServer, log: CaptureLog) -> None:
    @server.tool()
    async def capture_status(capture_id: str) -> CaptureStatus:
        """Check a capture id (from phone_snapshot or multimeter_read) before a position claim.

        A statement about probes, contact, or where a part is must name the capture id of a current phone_snapshot.
        `valid_for_position_claims` is false for an unknown id, a meter image, or a photo from before a scene change
        (the board or the phone moved): take a fresh phone_snapshot then.
        """
        return log.status(capture_id)
