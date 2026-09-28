"""Pointing: the green boxes and arrows for searched parts, kept on the board while the phone moves.

After board_register_photo, a live tracker follows the board in the phone screen frames (tracking.py). Each
phone_point_to (the agent, or the Board panel of the page) sets the target parts. On each tracked frame the boxes
and arrows are computed again and sent to the phone, rate-limited and only when they change by a step.

Plain phone_highlight boxes (no registration) get their own tracker from the snapshot at phone_highlight: they follow
the board the same way, and a box outside the view becomes an arrow. One set of boxes at a time: new boxes, clear, or
phone_point_to replace them. The registration tracker keeps running for the registration and the pointing target.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol
from uuid import uuid7

import numpy as np
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

from debug_devices_mcp.board.tools import BoardSession, CarryQuality, Registration
from debug_devices_mcp.focus import SnapshotGeometry
from debug_devices_mcp.highlight import BoxOutsideError, PixelBox, overlay_box, visibility
from debug_devices_mcp.phone_api import CameraStatus, OverlayArrow, OverlayBox, PreviewRegion
from debug_devices_mcp.pointer import FULL_TURN, HALF_TURN, ImageFrame, PointPlan, PointResult, follow_boxes, plan
from debug_devices_mcp.scene import SceneState
from debug_devices_mcp.tracking import (
    Features,
    LiveTracker,
    Matrix,
    TrackingState,
    apply,
    frame_features,
    match_photos,
)

logger = logging.getLogger(__name__)

# At most one overlay update to the phone per this time while the tracker moves the boxes.
MIN_POST_INTERVAL_S = 0.3
# Send again only when a box edge moves this much (part of the image) or an arrow turns this much.
BOX_STEP = 0.01
ANGLE_STEP_DEG = 8.0
# The registered photo and the last snapshot: the same photo when their shapes differ by at most this part.
SAME_SHAPE_TOLERANCE = 0.02

NO_SNAPSHOT = "take a phone_snapshot first, then register it (board_register_photo)"
NO_REGISTRATION = "no photo registration: call board_register_photo with 4 or more parts of your last phone_snapshot"
OTHER_SHAPE = "the registered photo has another shape than the last phone_snapshot: register the last phone_snapshot"
TRACKING_ON = "live tracking on: the boxes and arrows follow the board while the phone moves"
BOXES_TRACKING_ON = "live tracking on: the boxes follow the board while the phone moves"
NO_FRAMES = "no live tracking: no phone screen stream in this server or in the primary monitor"
NO_TRACKING = "no live tracking: {reason}"
REPLACED = "newer boxes or a phone_point_to replaced these boxes"
# For the next phone tool result after the tracker lost the board and the server cleared the boxes and arrows.
TRACKING_LOST_NOTE = "live tracking lost the board: the server cleared the boxes and arrows"
ESTIMATE_NOTE = (
    "Boardview positions mapped through the photo registration: an estimate. Tell the user to follow the arrow "
    "and the distance, not left or right words. Clear with phone_highlight clear."
)

type TrackingListener = Callable[[TrackingState], Awaitable[None]]

# Carry a registration to a new snapshot only with a clear feature match (in the pixels of the new snapshot).
CARRY_MIN_INLIERS = 40
CARRY_MIN_RATIO = 0.3
CARRY_MAX_ERROR_PX = 6.0
# The registered photos that the server keeps for a carry-over (the newest ones).
KEPT_PHOTOS = 8
NOT_KEPT = "the registered photo is not kept (a registration from before a server restart): register again"
NO_MATCH = "the board or the phone moved, and the new snapshot does not match the registered photo well enough"


class CarryResult(BaseModel):
    """What a new phone_snapshot did to the newest registration after the board or the phone moved."""

    carried: bool
    # The registration to use now: the carried one, or the stale old one.
    registration_id: str
    carried_from: str | None = None
    quality: CarryQuality | None = None
    note: str


@dataclass
class RegisteredPhoto:
    """The agent's snapshot at registration time, in its pixels (the registration refers to it)."""

    image: bytes
    width: int
    height: int


@dataclass
class TrackedBoxes:
    """Plain phone_highlight boxes that follow the board: in pixels of the snapshot at phone_highlight."""

    boxes: list[PixelBox]
    geometry: SnapshotGeometry
    tracker: LiveTracker


class PointingHost(Protocol):
    """What the pointing needs from the MCP server (`Services`)."""

    board: BoardSession
    scene: SceneState
    last_snapshot: SnapshotGeometry | None
    last_snapshot_image: bytes | None
    # The part of the still that the phone screen shows (from the last status), or None.
    preview_region: PreviewRegion | None
    # The boxes on the phone now (the last overlay).
    highlights: list[OverlayBox]

    async def send_overlay(self, boxes: list[OverlayBox], arrows: list[OverlayArrow]) -> CameraStatus: ...

    def overlay_note(self, note: str) -> None:
        """Tell the agent in its next phone tool result (the boxes and arrows changed without a tool call)."""


@dataclass
class Target:
    registration_id: str
    refdes: list[str]


def matrix_of(registration: Registration) -> Matrix:
    return np.asarray(registration.fit.matrix, dtype=float)


def angle_gap(a: float, b: float) -> float:
    return abs((a - b + HALF_TURN) % FULL_TURN - HALF_TURN)


def moved_enough(old: tuple[list[OverlayBox], list[OverlayArrow]] | None, boxes, arrows) -> bool:
    """True when the phone must get the new boxes and arrows."""
    if old is None:
        return True
    old_boxes, old_arrows = old
    if [box.label for box in old_boxes] != [box.label for box in boxes]:
        return True
    if [arrow.label.split(" ")[0] for arrow in old_arrows] != [arrow.label.split(" ")[0] for arrow in arrows]:
        return True
    for before, after in zip(old_boxes, boxes, strict=True):
        edges = (
            (before.snapshot_x, after.snapshot_x),
            (before.snapshot_y, after.snapshot_y),
            (before.width, after.width),
            (before.height, after.height),
        )
        if any(abs(a - b) >= BOX_STEP for a, b in edges):
            return True
    return any(
        angle_gap(before.angle_deg, after.angle_deg) >= ANGLE_STEP_DEG or before.label != after.label
        for before, after in zip(old_arrows, arrows, strict=True)
    )


class Pointing:
    def __init__(self, host: PointingHost, clock: Callable[[], float] = time.monotonic) -> None:
        self._host = host
        self._clock = clock
        self.tracker: LiveTracker | None = None
        self.target: Target | None = None
        # The plain phone_highlight boxes that follow the board, with their own tracker.
        self.boxes: TrackedBoxes | None = None
        self._sent: tuple[list[OverlayBox], list[OverlayArrow]] | None = None
        self._last_post = 0.0
        self._listeners: list[TrackingListener] = []
        self._lock = asyncio.Lock()
        self._photos: dict[str, RegisteredPhoto] = {}

    @property
    def state(self) -> TrackingState:
        """Following while one of the trackers follows (the page then keeps its drawings at a scene change)."""
        if self.boxes is not None and self.boxes.tracker.following:
            return TrackingState.FOLLOWING
        return self.tracker.state if self.tracker is not None else TrackingState.OFF

    def add_listener(self, listener: TrackingListener) -> None:
        self._listeners.append(listener)

    async def _tell(self) -> None:
        for listener in self._listeners:
            await listener(self.state)

    def tracked(self, registration_id: str | None) -> bool:
        tracker = self.tracker
        return tracker is not None and tracker.following and tracker.registration_id == registration_id

    def overlay_tracked(self) -> bool:
        """True while a tracker moves the boxes and arrows on the phone: they stay at a scene change."""
        if self.boxes is not None:
            return self.boxes.tracker.following
        return self.target is not None and self.tracked(self.target.registration_id)

    async def stop(self) -> None:
        """Another overlay (the agent's own boxes, or clear) replaces the pointing and the tracked boxes."""
        self.target = None
        self._sent = None
        await self._drop_boxes()

    async def _drop_boxes(self) -> None:
        if self.boxes is None:
            return
        self.boxes = None
        await self._tell()

    async def stop_tracking(self) -> None:
        """The phone app restarted: the tracker follows a camera view that is gone."""
        await self.stop()
        if self.tracker is not None:
            self.tracker = None
            await self._tell()

    # region: registration and frames

    def _board_to_snapshot(self, registration: Registration) -> Matrix:
        """Board mm -> pixels of the agent's last snapshot (the registered photo can be that photo at another size)."""
        geometry = self._host.last_snapshot
        if geometry is None:
            raise ToolError(NO_SNAPSHOT)
        scale_x = geometry.width / registration.photo_width_px
        scale_y = geometry.height / registration.photo_height_px
        if abs(scale_x - scale_y) > SAME_SHAPE_TOLERANCE * max(scale_x, scale_y):
            raise ToolError(OTHER_SHAPE)
        return np.diag([scale_x, scale_y, 1.0]) @ matrix_of(registration)

    def _keep_photo(self, registration_id: str) -> None:
        geometry, image = self._host.last_snapshot, self._host.last_snapshot_image
        if geometry is None or image is None:
            return
        self._photos[registration_id] = RegisteredPhoto(image=image, width=geometry.width, height=geometry.height)
        while len(self._photos) > KEPT_PHOTOS:
            self._photos.pop(next(iter(self._photos)))

    async def carry(self) -> CarryResult | None:
        """After a new phone_snapshot: when the newest registration is stale (the board or the phone moved), match
        its registered photo with the new snapshot and carry it over on a good match. None: nothing to carry."""
        session = self._host.board
        old = session.registrations.get(session.last_registration_id or "")
        geometry, image = self._host.last_snapshot, self._host.last_snapshot_image
        if old is None or not old.stale or geometry is None or image is None:
            return None
        photo = self._photos.get(old.registration_id)
        if photo is None:
            return CarryResult(carried=False, registration_id=old.registration_id, note=NOT_KEPT)
        found = await asyncio.to_thread(match_photos, photo.image, image)
        ratio = found.inliers / found.matches if found.matches else 0.0
        quality = CarryQuality(
            inliers=found.inliers,
            matches=found.matches,
            inlier_ratio=round(ratio, 3),
            error_px=round(found.error_px, 2) if found.error_px is not None else None,
        )
        good = found.matrix is not None and found.inliers >= CARRY_MIN_INLIERS and ratio >= CARRY_MIN_RATIO
        if not good or found.error_px is None or found.error_px > CARRY_MAX_ERROR_PX:
            return CarryResult(carried=False, registration_id=old.registration_id, quality=quality, note=NO_MATCH)
        assert found.matrix is not None
        to_photo = np.diag([photo.width / old.photo_width_px, photo.height / old.photo_height_px, 1.0])
        motion = found.matrix @ to_photo
        matrix = motion @ matrix_of(old)
        moved = apply(motion, np.array([(pair.photo_x_px, pair.photo_y_px) for pair in old.fit.pairs]))
        pairs = [
            pair.model_copy(update={"photo_x_px": round(float(x), 2), "photo_y_px": round(float(y), 2)})
            for pair, (x, y) in zip(old.fit.pairs, moved, strict=True)
        ]
        carried = old.model_copy(
            update={
                "registration_id": str(uuid7()),
                "photo_width_px": geometry.width,
                "photo_height_px": geometry.height,
                "fit": old.fit.model_copy(update={"matrix": matrix.tolist(), "pairs": pairs}),
                "stale": False,
                "tracking": None,
                "photo_id": session.current_photo_id() if session.current_photo_id is not None else None,
                "carried_from": old.registration_id,
                "carry_quality": quality,
            }
        )
        session.registrations[carried.registration_id] = carried
        session.last_registration_id = carried.registration_id
        carried.tracking = await self.registered(carried)
        note = f"carried over from {old.registration_id} by image features ({found.inliers} matches agree)"
        return CarryResult(
            carried=True,
            registration_id=carried.registration_id,
            carried_from=old.registration_id,
            quality=quality,
            note=note,
        )

    async def registered(self, registration: Registration) -> str:
        """A new registration: keep its photo (for a later carry-over) and start the live tracker on it."""
        self._keep_photo(registration.registration_id)
        snapshot, frame = self._host.last_snapshot_image, self._host.scene.latest_frame
        if snapshot is None or frame is None:
            self.tracker = None
            await self._tell()
            return NO_FRAMES
        try:
            board_to_snapshot = self._board_to_snapshot(registration)
        except ToolError as exc:
            return f"no live tracking: {exc}"
        started = await asyncio.to_thread(
            LiveTracker.start, registration.registration_id, board_to_snapshot, snapshot, frame
        )
        if isinstance(started, str):
            self.tracker = None
            await self._tell()
            return f"no live tracking: {started}"
        self.tracker = started
        await self._tell()
        return TRACKING_ON

    async def on_frame(self, jpeg: bytes) -> None:
        """A new phone screen frame (about 4 per second): follow the board and move the boxes and arrows. The
        trackers share the features of the frame."""
        tracker = self.tracker if self.tracker is not None and self.tracker.following else None
        boxes = self.boxes if self.boxes is not None and self.boxes.tracker.following else None
        if tracker is None and boxes is None:
            return
        current = await asyncio.to_thread(frame_features, jpeg)
        if tracker is not None:
            await self._follow_registration(tracker, current)
        if boxes is not None:
            await self._follow_boxes(boxes, current)

    async def _follow_registration(self, tracker: LiveTracker, current: Features) -> None:
        lost = await asyncio.to_thread(tracker.follow, current)
        if lost:
            logger.info("live tracking lost for registration %s", tracker.registration_id)
            registration = self._host.board.registrations.get(tracker.registration_id)
            if registration is not None:
                registration.stale = True
            if self.target is not None and self.target.registration_id == tracker.registration_id:
                await self.stop()
                await self._clear_lost()
            await self._tell()
            return
        if self.target is not None and self.target.registration_id == tracker.registration_id:
            try:
                await self._refresh(force=False)
            except ToolError as exc:
                logger.warning("cannot move the pointing boxes: %s", exc)

    async def _clear_lost(self) -> None:
        try:
            await self._host.send_overlay([], [])
        except ToolError as exc:
            logger.warning("cannot clear the boxes after the tracking was lost: %s", exc)
        self._host.overlay_note(TRACKING_LOST_NOTE)

    async def scene_changed(self) -> bool:
        """The scene watcher saw a move. True when the tracker follows it (the registration stays valid)."""
        return self.tracker is not None and self.tracker.following

    # endregion: registration and frames

    # region: plain boxes

    async def follow_plain_boxes(self, boxes: list[PixelBox], sent: list[OverlayBox]) -> str:
        """After phone_highlight sent `boxes` (pixels of the last snapshot, with tags) as `sent`: follow them on the
        board. Returns the tracking note for the tool result."""
        await self._drop_boxes()
        geometry, snapshot, frame = (
            self._host.last_snapshot,
            self._host.last_snapshot_image,
            self._host.scene.latest_frame,
        )
        if geometry is None or snapshot is None or frame is None:
            return NO_FRAMES
        started = await asyncio.to_thread(LiveTracker.start, str(uuid7()), np.eye(3), snapshot, frame)
        if isinstance(started, str):
            return NO_TRACKING.format(reason=started)
        # A newer overlay (another phone_highlight, clear, or phone_point_to) came during the start.
        if self.target is not None or self._host.highlights != sent:
            return NO_TRACKING.format(reason=REPLACED)
        self.boxes = TrackedBoxes(boxes=boxes, geometry=geometry, tracker=started)
        self._sent, self._last_post = (sent, []), self._clock()
        await self._tell()
        return BOXES_TRACKING_ON

    async def _follow_boxes(self, boxes: TrackedBoxes, current: Features) -> None:
        lost = await asyncio.to_thread(boxes.tracker.follow, current)
        if boxes is not self.boxes:
            return
        if lost:
            logger.info("live tracking lost for the highlight boxes")
            await self.stop()
            await self._clear_lost()
            return
        geometry = boxes.geometry
        frame = ImageFrame((geometry.width, geometry.height), geometry.orientation, self._view(geometry))
        shown, arrows = follow_boxes(boxes.boxes, boxes.tracker.board_to_current(), frame)
        overlay = []
        for box in shown:
            try:
                overlay.append(overlay_box(box, geometry))
            except BoxOutsideError:
                continue
        async with self._lock:
            if boxes is not self.boxes:
                return
            try:
                await self._send_when_due(overlay, arrows, force=False)
            except ToolError as exc:
                logger.warning("cannot move the highlight boxes: %s", exc)

    # endregion: plain boxes

    # region: point to

    def _registration(self, registration_id: str | None) -> Registration:
        session = self._host.board
        registration_id = registration_id or session.last_registration_id
        if registration_id is None:
            raise ToolError(NO_REGISTRATION)
        if not self.tracked(registration_id):
            self._host.scene.guard()
        return session.registration(registration_id)

    def _view(self, geometry: SnapshotGeometry) -> tuple[float, float, float, float] | None:
        """The preview region in pixels of the agent's image (through its turn and flips), or None."""
        region = self._host.preview_region
        if region is None:
            return None
        x, y, width, height = geometry.orientation.box_from_true(
            region.snapshot_x, region.snapshot_y, region.width, region.height
        )
        return x * geometry.width, y * geometry.height, width * geometry.width, height * geometry.height

    def _plan(self, target: Target) -> tuple[PointPlan, bool]:
        session = self._host.board
        registration = session.registration(target.registration_id)
        geometry = self._host.last_snapshot
        if geometry is None:
            raise ToolError(NO_SNAPSHOT)
        tracking = self.tracked(registration.registration_id)
        assert self.tracker is not None or not tracking
        matrix = self.tracker.board_to_current() if tracking and self.tracker else self._board_to_snapshot(registration)
        parts = [session.part(name) for name in target.refdes]
        frame = ImageFrame((geometry.width, geometry.height), geometry.orientation, self._view(geometry))
        return plan(parts, registration.side, matrix, frame), tracking

    async def _refresh(self, *, force: bool) -> tuple[PointPlan, bool, CameraStatus | None]:
        async with self._lock:
            target = self.target
            if target is None:
                raise ToolError(NO_REGISTRATION)
            point_plan, tracking = self._plan(target)
            geometry = self._host.last_snapshot
            assert geometry is not None
            boxes = [overlay_box(box, geometry) for box in point_plan.boxes]
            status = await self._send_when_due(boxes, point_plan.arrows, force=force)
            return point_plan, tracking, status

    async def _send_when_due(
        self, boxes: list[OverlayBox], arrows: list[OverlayArrow], *, force: bool
    ) -> CameraStatus | None:
        """Send the moved boxes and arrows: rate-limited, and only when they changed by a step. Hold `_lock`."""
        now = self._clock()
        due = now - self._last_post >= MIN_POST_INTERVAL_S
        if not force and not (due and moved_enough(self._sent, boxes, arrows)):
            return None
        status = await self._host.send_overlay(boxes, arrows)
        self._sent = (boxes, arrows)
        self._last_post = now
        return status

    async def point_to(self, refdes: list[str], registration_id: str | None) -> PointResult:
        registration = self._registration(registration_id)
        for name in refdes:
            self._host.board.part(name)
        await self._drop_boxes()
        self.target = Target(registration_id=registration.registration_id, refdes=refdes)
        self._sent = None
        point_plan, tracking, status = await self._refresh(force=True)
        geometry = self._host.last_snapshot
        assert geometry is not None
        boxes = [overlay_box(box, geometry) for box in point_plan.boxes]
        region = status.preview_region if status is not None and status.preview_region else self._host.preview_region
        seen, warning = visibility(boxes, region)
        return PointResult(
            visibility=seen,
            warning=warning,
            count=len(boxes),
            boxes=boxes,
            arrows=point_plan.arrows,
            targets=point_plan.targets,
            tracking=tracking,
            overlay_boxes=status.overlay_boxes if status else None,
            overlay_arrows=status.overlay_arrows if status else None,
            note=ESTIMATE_NOTE,
        )

    # endregion: point to
