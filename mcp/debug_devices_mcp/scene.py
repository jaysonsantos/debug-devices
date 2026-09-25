"""Scene-change detection: did the board or the phone move since the agent's last phone_snapshot?

A highlight box or a photo registration is only valid for the scene that the agent saw. The watcher takes small
grayscale frames from the phone screen stream (about 2 per second), crops them to the camera preview, blurs them, and
compares them with a reference: the first frame after the agent's last phone_snapshot. A large mean difference, or a
clear shift of the whole picture, for CHANGE_FRAMES frames in a row marks the scene as changed. Our own camera
commands (zoom, torch, flips, sensor zoom, focus, highlight boxes) take a new reference after they settle.
"""

import asyncio
import contextlib
import io
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import numpy as np
from mcp.server.mcpserver.exceptions import ToolError
from PIL import Image as PilImage
from PIL import ImageFilter

from debug_devices_mcp.screen_mjpeg import ScreenTranscoder, TranscodeOptions

logger = logging.getLogger(__name__)

SCENE_CHANGED_MESSAGE = (
    "the board or the phone moved since your last phone_snapshot: take a fresh phone_snapshot and look again "
    "(the board may be rotated)"
)

type SceneListener = Callable[[datetime], Awaitable[None]]
type FrameListener = Callable[[bytes], Awaitable[None]]
type Clock = Callable[[], float]
type FrameFeed = Callable[[], AsyncIterator[bytes]]


@dataclass(frozen=True)
class SceneOptions:
    # One compared frame per interval: about 2 per second.
    interval: timedelta = timedelta(milliseconds=500)
    # The frame listeners (live tracking) get about 4 frames per second.
    frame_interval: timedelta = timedelta(milliseconds=250)
    # The camera preview part of the phone screen (fractions of the width and height): the app status text at the
    # top left and the system bars stay out.
    crop_left: float = 0.05
    crop_top: float = 0.2
    crop_right: float = 0.95
    crop_bottom: float = 0.95
    # The compared frames are this wide (grayscale); the height follows the crop.
    frame_width: int = 64
    blur_radius: float = 1.5
    # The mean absolute difference (0-255, after each frame's mean brightness is removed) that means "changed".
    diff_threshold: float = 12.0
    # The whole picture moved by this many pixels (of frame_width) or more...
    shift_threshold_px: int = 2
    # ...and that shift explains the difference: the shifted difference is below this part of the plain one.
    shift_gain: float = 0.6
    # The largest shift that the check tries, in pixels.
    max_shift_px: int = 4
    # The change must last this many compared frames in a row (a hand that passes does not count).
    change_frames: int = 2
    # After our own camera command, wait this long before the next reference (the zoom, the focus, and the
    # sensor zoom rebind need time).
    settle: timedelta = timedelta(milliseconds=1500)
    # How often the frame feed checks if it must switch between the page fallback decoder and its own one.
    switch_check: timedelta = timedelta(seconds=1)


DEFAULT_OPTIONS = SceneOptions()
# Our own decoder when the page fallback decoder does not run: 4 frames per second, 720 px wide (live tracking
# needs the detail; the scene check reads 2 of them per second).
SCENE_TRANSCODE = TranscodeOptions(fps=4, max_width=720, quality=6)


# region: state


class SceneState:
    """Shared by the tools (they check and reset it) and the watcher (it marks changes)."""

    def __init__(self, clock: Clock = time.monotonic, options: SceneOptions = DEFAULT_OPTIONS) -> None:
        self._clock = clock
        self._options = options
        self.changed_at: datetime | None = None
        # True: the watcher takes the next frame after `settle_until` as its new reference.
        self.reference_wanted = False
        self.settle_until = 0.0
        # A generation number: the watcher drops its reference when it changes.
        self.generation = 0
        self._listeners: list[SceneListener] = []
        # The newest phone screen frame (JPEG), and the code that follows each frame (live tracking).
        self.latest_frame: bytes | None = None
        self._frame_listeners: list[FrameListener] = []

    @property
    def changed(self) -> bool:
        return self.changed_at is not None

    def add_listener(self, listener: SceneListener) -> None:
        self._listeners.append(listener)

    def add_frame_listener(self, listener: FrameListener) -> None:
        self._frame_listeners.append(listener)

    async def frame(self, jpeg: bytes) -> None:
        self.latest_frame = jpeg
        for listener in self._frame_listeners:
            try:
                await listener(jpeg)
            except Exception:
                logger.exception("a frame listener failed")

    def snapshot_taken(self) -> None:
        """A new phone_snapshot: the scene is the one in this photo. The next frame is the new reference."""
        self.changed_at = None
        self.reference_wanted = True
        self.settle_until = 0.0
        self.generation += 1

    def own_command(self) -> None:
        """Our own camera command changes the picture: take a new reference after it settles."""
        if self.generation == 0 or self.changed:
            return
        self.reference_wanted = True
        self.settle_until = self._clock() + self._options.settle.total_seconds()
        self.generation += 1

    def guard(self) -> None:
        if self.changed:
            raise ToolError(SCENE_CHANGED_MESSAGE)

    async def mark_changed(self) -> None:
        self.changed_at = datetime.now(UTC)
        self.reference_wanted = False
        for listener in self._listeners:
            try:
                await listener(self.changed_at)
            except Exception:
                logger.exception("a scene change listener failed")


# endregion: state

# region: compare


@dataclass(frozen=True)
class SceneDiff:
    mean_diff: float
    shift_x: int
    shift_y: int
    shifted_diff: float


def prepare(jpeg: bytes, options: SceneOptions = DEFAULT_OPTIONS) -> np.ndarray:
    """A small, blurred grayscale frame of the camera preview area, with its mean brightness removed."""
    with PilImage.open(io.BytesIO(jpeg)) as opened:
        opened.draft("L", (options.frame_width * 4, options.frame_width * 8))
        image = opened.convert("L")
    box = (
        round(image.width * options.crop_left),
        round(image.height * options.crop_top),
        round(image.width * options.crop_right),
        round(image.height * options.crop_bottom),
    )
    cropped = image.crop(box)
    height = max(1, round(cropped.height * options.frame_width / max(cropped.width, 1)))
    small = cropped.resize((options.frame_width, height)).filter(ImageFilter.GaussianBlur(options.blur_radius))
    frame = np.asarray(small, dtype=np.float32)
    return frame - frame.mean()


def _overlap_diff(reference: np.ndarray, frame: np.ndarray, dx: int, dy: int) -> float:
    """The mean difference when the frame is moved by (dx, dy) against the reference (only the overlap)."""
    height, width = reference.shape
    ref = reference[max(dy, 0) : height + min(dy, 0), max(dx, 0) : width + min(dx, 0)]
    moved = frame[max(-dy, 0) : height + min(-dy, 0), max(-dx, 0) : width + min(-dx, 0)]
    return float(np.abs(ref - moved).mean())


def compare(reference: np.ndarray, frame: np.ndarray, options: SceneOptions = DEFAULT_OPTIONS) -> SceneDiff:
    if reference.shape != frame.shape:
        # Another screen size (for example the phone turned): a new scene.
        return SceneDiff(mean_diff=float("inf"), shift_x=0, shift_y=0, shifted_diff=float("inf"))
    plain = _overlap_diff(reference, frame, 0, 0)
    best = (plain, 0, 0)
    limit = options.max_shift_px
    for dy in range(-limit, limit + 1):
        for dx in range(-limit, limit + 1):
            if dx or dy:
                diff = _overlap_diff(reference, frame, dx, dy)
                if diff < best[0]:
                    best = (diff, dx, dy)
    return SceneDiff(mean_diff=plain, shift_x=best[1], shift_y=best[2], shifted_diff=best[0])


def is_changed(diff: SceneDiff, options: SceneOptions = DEFAULT_OPTIONS) -> bool:
    if diff.mean_diff > options.diff_threshold:
        return True
    shifted = max(abs(diff.shift_x), abs(diff.shift_y)) >= options.shift_threshold_px
    return shifted and diff.shifted_diff < diff.mean_diff * options.shift_gain


# endregion: compare

# region: watcher


class SceneWatcher:
    """Compares the phone screen frames with the reference and marks a scene change in the shared state."""

    def __init__(
        self,
        state: SceneState,
        feed: FrameFeed,
        options: SceneOptions = DEFAULT_OPTIONS,
        clock: Clock = time.monotonic,
    ) -> None:
        self.state = state
        self._feed = feed
        self._options = options
        self._clock = clock
        self._reference: np.ndarray | None = None
        self._generation = -1
        self._streak = 0
        self._next_at = 0.0
        self._next_frame_at = 0.0
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if not self.running:
            self._task = asyncio.create_task(self._run(), name="scene-watcher")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _run(self) -> None:
        async for jpeg in self._feed():
            now = self._clock()
            if now >= self._next_frame_at:
                self._next_frame_at = now + self._options.frame_interval.total_seconds()
                await self.state.frame(jpeg)
            if now < self._next_at:
                continue
            self._next_at = now + self._options.interval.total_seconds()
            try:
                await self.check(jpeg)
            except (OSError, ValueError) as exc:
                logger.warning("scene watcher: cannot read a frame: %s", exc)

    async def check(self, jpeg: bytes) -> None:
        """Compare one frame (the loop calls this about twice per second)."""
        state = self.state
        if state.generation != self._generation:
            self._generation = state.generation
            self._reference = None
            self._streak = 0
        if state.changed:
            return
        if state.reference_wanted:
            if self._clock() < state.settle_until:
                return
            self._reference = await asyncio.to_thread(prepare, jpeg, self._options)
            state.reference_wanted = False
            return
        if self._reference is None:
            return
        frame = await asyncio.to_thread(prepare, jpeg, self._options)
        diff = compare(self._reference, frame, self._options)
        self._streak = self._streak + 1 if is_changed(diff, self._options) else 0
        if self._streak >= self._options.change_frames:
            logger.info("scene changed: %s", diff)
            self._reference = None
            self._streak = 0
            await state.mark_changed()


async def frames_while(frames: AsyncIterator[bytes], keep_going: Callable[[], bool], check: timedelta):
    """Yield from `frames` while `keep_going()` is true; check it at least every `check` without a frame."""
    pending: asyncio.Future[bytes] | None = None
    try:
        while keep_going():
            if pending is None:
                pending = asyncio.ensure_future(anext(frames))
            done, _ = await asyncio.wait({pending}, timeout=check.total_seconds())
            if pending in done:
                result, pending = pending.result(), None
                yield result
    except StopAsyncIteration:
        return
    finally:
        if pending is not None:
            pending.cancel()
            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration):
                await pending
        with contextlib.suppress(RuntimeError):
            await frames.aclose()  # type: ignore[attr-defined]


def screen_feed(fallback: ScreenTranscoder | None, own: ScreenTranscoder, options: SceneOptions = DEFAULT_OPTIONS):
    """The phone screen as JPEG frames: the page's fallback decoder while it runs, else a small one of our own."""

    async def feed() -> AsyncIterator[bytes]:
        def fallback_runs() -> bool:
            return fallback is not None and fallback.running

        while True:
            if fallback is not None and fallback_runs():
                async for jpeg in frames_while(fallback.frames(), fallback_runs, options.switch_check):
                    yield jpeg
                continue
            async with own.viewer():
                async for jpeg in frames_while(own.frames(), lambda: not fallback_runs(), options.switch_check):
                    yield jpeg

    return feed


# endregion: watcher
