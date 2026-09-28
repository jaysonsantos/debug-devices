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

    def differences(self, other: CameraView) -> list[str]:
        return [name for name in CameraView.model_fields if getattr(self, name) != getattr(other, name)]

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
        # The checked result of each multimeter_read, by the capture id of its image (bench_state uses it).
        self.meter_results: dict[str, MeterResult] = {}

    def attach(self, scene: SceneState) -> None:
        """Follow the scene: a change starts a new epoch."""
        self._scene = scene
        scene.add_listener(self._on_scene_change)

    async def _on_scene_change(self, _: datetime) -> None:
        self.scene_changed()

    def scene_changed(self) -> None:
        self.epoch += 1

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
            reason = "the board or the phone moved after this photo: take a fresh phone_snapshot"
        else:
            latest = self.latest_photo
            newer = latest is not None and latest.capture_id != capture.capture_id
            reason = "current scene" + (" (a newer phone_snapshot exists)" if newer else "")
            return CaptureStatus(capture_id=capture_id, capture=capture, valid_for_position_claims=True, reason=reason)
        return CaptureStatus(capture_id=capture_id, capture=capture, valid_for_position_claims=False, reason=reason)

    def reuse_problem(self, registered_photo_id: str | None) -> str | None:
        """Can pixel positions of the latest phone_snapshot use a registration of an older photo?

        Yes (None) when it is the same photo, or when both are phone snapshots of the current scene with the same
        camera view. Otherwise the reason.
        """
        latest = self.latest_photo
        # A registration of a photo that is not a phone_snapshot of this session cannot be checked here.
        if registered_photo_id is None or latest is None or registered_photo_id == latest.capture_id:
            return None
        registered = self._captures.get(registered_photo_id)
        problem = None
        if registered is None:
            problem = "the registered photo is not known any more: register your last phone_snapshot"
        elif registered.scene_epoch != self.epoch or latest.scene_epoch != self.epoch:
            problem = "the board or the phone moved after the registered photo: register your last phone_snapshot"
        elif registered.view is None or latest.view is None:
            problem = "the camera view of a photo is not known: register your last phone_snapshot"
        elif changed := registered.view.differences(latest.view):
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
