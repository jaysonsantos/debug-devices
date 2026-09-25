"""Pointing: the green boxes and arrows for searched parts, kept on the board while the phone moves.

After board_register_photo, a live tracker follows the board in the phone screen frames (tracking.py). Each
phone_point_to (the agent, or the Board panel of the page) sets the target parts. On each tracked frame the boxes
and arrows are computed again and sent to the phone, rate-limited and only when they change by a step.
"""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

import numpy as np
from mcp.server.mcpserver.exceptions import ToolError

from debug_devices_mcp.board.tools import BoardSession, Registration
from debug_devices_mcp.focus import SnapshotGeometry
from debug_devices_mcp.highlight import overlay_box
from debug_devices_mcp.phone_api import CameraStatus, OverlayArrow, OverlayBox
from debug_devices_mcp.pointer import FULL_TURN, HALF_TURN, PointPlan, PointResult, plan
from debug_devices_mcp.scene import SceneState
from debug_devices_mcp.tracking import LiveTracker, Matrix, TrackingState

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
NO_FRAMES = "no live tracking: the phone screen stream is not running (the monitor page shows it)"
ESTIMATE_NOTE = (
    "Boardview positions mapped through the photo registration: an estimate. Tell the user to follow the arrow "
    "and the distance, not left or right words. Clear with phone_highlight clear."
)

type TrackingListener = Callable[[TrackingState], Awaitable[None]]


class PointingHost(Protocol):
    """What the pointing needs from the MCP server (`Services`)."""

    board: BoardSession
    scene: SceneState
    last_snapshot: SnapshotGeometry | None
    last_snapshot_image: bytes | None

    async def send_overlay(self, boxes: list[OverlayBox], arrows: list[OverlayArrow]) -> CameraStatus: ...


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
        self._sent: tuple[list[OverlayBox], list[OverlayArrow]] | None = None
        self._last_post = 0.0
        self._listeners: list[TrackingListener] = []
        self._lock = asyncio.Lock()

    @property
    def state(self) -> TrackingState:
        return self.tracker.state if self.tracker is not None else TrackingState.OFF

    def add_listener(self, listener: TrackingListener) -> None:
        self._listeners.append(listener)

    async def _tell(self) -> None:
        for listener in self._listeners:
            await listener(self.state)

    def tracked(self, registration_id: str | None) -> bool:
        tracker = self.tracker
        return tracker is not None and tracker.following and tracker.registration_id == registration_id

    def stop(self) -> None:
        """Another overlay (the agent's own boxes, or clear) replaces the pointing."""
        self.target = None
        self._sent = None

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

    async def registered(self, registration: Registration) -> str:
        """A new registration: start the live tracker on it. Returns the tracking note."""
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
        """A new phone screen frame (about 4 per second): follow the board and move the boxes and arrows."""
        tracker = self.tracker
        if tracker is None or not tracker.following:
            return
        lost = await asyncio.to_thread(tracker.update, jpeg)
        if lost:
            logger.info("live tracking lost for registration %s", tracker.registration_id)
            registration = self._host.board.registrations.get(tracker.registration_id)
            if registration is not None:
                registration.stale = True
            if self.target is not None and self.target.registration_id == tracker.registration_id:
                self.stop()
                await self._host.send_overlay([], [])
            await self._tell()
            return
        if self.target is not None and self.target.registration_id == tracker.registration_id:
            try:
                await self._refresh(force=False)
            except ToolError as exc:
                logger.warning("cannot move the pointing boxes: %s", exc)

    async def scene_changed(self) -> bool:
        """The scene watcher saw a move. True when the tracker follows it (the registration stays valid)."""
        return self.tracker is not None and self.tracker.following

    # endregion: registration and frames

    # region: point to

    def _registration(self, registration_id: str | None) -> Registration:
        session = self._host.board
        registration_id = registration_id or session.last_registration_id
        if registration_id is None:
            raise ToolError(NO_REGISTRATION)
        if not self.tracked(registration_id):
            self._host.scene.guard()
        return session.registration(registration_id)

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
        size = (geometry.width, geometry.height)
        return plan(parts, registration.side, matrix, size, geometry.orientation), tracking

    async def _refresh(self, *, force: bool) -> tuple[PointPlan, bool, CameraStatus | None]:
        async with self._lock:
            target = self.target
            if target is None:
                raise ToolError(NO_REGISTRATION)
            point_plan, tracking = self._plan(target)
            geometry = self._host.last_snapshot
            assert geometry is not None
            boxes = [overlay_box(box, geometry) for box in point_plan.boxes]
            arrows = point_plan.arrows
            now = self._clock()
            due = now - self._last_post >= MIN_POST_INTERVAL_S
            status = None
            if force or (due and moved_enough(self._sent, boxes, arrows)):
                status = await self._host.send_overlay(boxes, arrows)
                self._sent = (boxes, arrows)
                self._last_post = now
            return point_plan, tracking, status

    async def point_to(self, refdes: list[str], registration_id: str | None) -> PointResult:
        registration = self._registration(registration_id)
        for name in refdes:
            self._host.board.part(name)
        self.target = Target(registration_id=registration.registration_id, refdes=refdes)
        self._sent = None
        point_plan, tracking, status = await self._refresh(force=True)
        geometry = self._host.last_snapshot
        assert geometry is not None
        boxes = [overlay_box(box, geometry) for box in point_plan.boxes]
        return PointResult(
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
