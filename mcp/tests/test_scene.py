"""The scene-change detector with synthetic phone screen frames: still, noise, light, shift, rotation."""

import asyncio
import io
from collections.abc import AsyncIterator
from datetime import datetime, timedelta

import numpy as np
import pytest
from PIL import Image

from debug_devices_mcp.scene import (
    SCENE_CHANGED_MESSAGE,
    SceneState,
    SceneWatcher,
    compare,
    frames_while,
    is_changed,
    prepare,
)

# A phone screen: portrait, like the fallback decoder output.
SCREEN_WIDTH, SCREEN_HEIGHT = 360, 800
SEED = 7


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def board_texture() -> np.ndarray:
    """A board-like picture: blocks of random gray levels (parts), larger than the screen so it can move."""
    rng = np.random.default_rng(SEED)
    blocks = rng.integers(30, 225, size=(60, 30)).astype(np.uint8)
    return np.asarray(Image.fromarray(blocks).resize((SCREEN_WIDTH * 2, SCREEN_HEIGHT * 2), Image.Resampling.NEAREST))


TEXTURE = board_texture()


def screen(dx: int = 0, dy: int = 0, angle: float = 0.0, noise: float = 0.0, light: int = 0) -> bytes:
    """One phone screen frame (JPEG): a view of the board texture, moved, turned, with noise or brighter."""
    image = Image.fromarray(TEXTURE)
    if angle:
        image = image.rotate(angle, resample=Image.Resampling.BILINEAR)
    left, top = SCREEN_WIDTH // 2 + dx, SCREEN_HEIGHT // 2 + dy
    frame = np.asarray(image.crop((left, top, left + SCREEN_WIDTH, top + SCREEN_HEIGHT)), dtype=np.float32)
    if noise:
        frame = frame + np.random.default_rng(SEED).normal(0, noise, frame.shape)
    frame = np.clip(frame + light, 0, 255).astype(np.uint8)
    output = io.BytesIO()
    Image.fromarray(frame).convert("RGB").save(output, format="JPEG", quality=90)
    return output.getvalue()


def changed(frame: bytes) -> bool:
    return is_changed(compare(prepare(screen()), prepare(frame)))


# region: compare


@pytest.mark.parametrize(
    ("name", "frame", "expected"),
    [
        ("still", screen(), False),
        ("sensor noise", screen(noise=6), False),
        ("brighter (auto exposure)", screen(light=25), False),
        ("moved 3% sideways", screen(dx=12), True),
        ("moved 5% down", screen(dy=40), True),
        ("turned 10 degrees", screen(angle=10), True),
        ("turned 180 degrees", screen(angle=180), True),
    ],
)
def test_detector(name: str, frame: bytes, expected: bool) -> None:
    assert changed(frame) is expected, name


def test_a_small_shift_is_found_by_the_shift_check() -> None:
    diff = compare(prepare(screen()), prepare(screen(dx=12)))
    assert abs(diff.shift_x) >= 2
    assert diff.shifted_diff < diff.mean_diff


# endregion: compare

# region: watcher


class Recorder:
    def __init__(self) -> None:
        self.changes: list[datetime] = []

    async def __call__(self, changed_at: datetime) -> None:
        self.changes.append(changed_at)


def watcher() -> tuple[SceneWatcher, SceneState, Clock, Recorder]:
    clock = Clock()
    state = SceneState(clock=clock)
    recorder = Recorder()
    state.add_listener(recorder)

    async def no_feed() -> AsyncIterator[bytes]:
        return
        yield b""

    return SceneWatcher(state, no_feed, clock=clock), state, clock, recorder


async def test_no_reference_before_the_first_snapshot() -> None:
    scene, state, _, recorder = watcher()
    for _ in range(3):
        await scene.check(screen(angle=30))
    assert recorder.changes == []
    state.own_command()  # nothing to reset yet
    assert not state.reference_wanted


async def test_a_move_after_the_snapshot_is_marked_once() -> None:
    scene, state, _, recorder = watcher()
    state.snapshot_taken()
    await scene.check(screen())  # the reference
    await scene.check(screen(noise=5))
    await scene.check(screen(dx=20))  # one frame: a hand that passes
    await scene.check(screen())
    assert recorder.changes == []
    await scene.check(screen(dx=20))
    await scene.check(screen(dx=20))
    assert len(recorder.changes) == 1
    assert state.changed
    with pytest.raises(Exception, match="moved since your last phone_snapshot"):
        state.guard()
    await scene.check(screen(dx=40))
    await scene.check(screen(dx=40))
    assert len(recorder.changes) == 1  # stays changed until a new snapshot
    state.snapshot_taken()
    assert not state.changed
    await scene.check(screen(dx=40))  # the new reference
    await scene.check(screen(dx=40))
    assert len(recorder.changes) == 1
    assert SCENE_CHANGED_MESSAGE.startswith("the board or the phone moved")


async def test_own_commands_take_a_new_reference_after_they_settle() -> None:
    scene, state, clock, recorder = watcher()
    state.snapshot_taken()
    await scene.check(screen())
    # Our zoom changes the picture: the frames before the settle time do not count.
    state.own_command()
    for _ in range(3):
        await scene.check(screen(dx=30))
    clock.now += 2.0
    await scene.check(screen(dx=30))  # the new reference, after the zoom
    for _ in range(3):
        await scene.check(screen(dx=30, noise=4))
    assert recorder.changes == []
    await scene.check(screen(angle=20))
    await scene.check(screen(angle=20))
    assert len(recorder.changes) == 1


async def test_frames_while_stops_when_asked() -> None:
    keep = {"go": True}

    async def endless() -> AsyncIterator[bytes]:
        count = 0
        while True:
            count += 1
            yield bytes([count])
            await asyncio.sleep(0.01)

    got = []
    async for frame in frames_while(endless(), lambda: keep["go"], timedelta(milliseconds=20)):
        got.append(frame)
        if len(got) == 3:
            keep["go"] = False
    assert got == [b"\x01", b"\x02", b"\x03"]


# endregion: watcher
