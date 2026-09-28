"""Several decoded frames give one stable reading, or "unstable".

A value is stable when the last `min_agree` readable frames show the same digits, decimal point, sign, and unit.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta

from pydantic import BaseModel, Field

from debug_devices_mcp.sevenseg.constants import stability
from debug_devices_mcp.sevenseg.decode import DecodedFrame, decode_jpeg
from debug_devices_mcp.sevenseg.profile import MeterProfile
from debug_devices_mcp.sevenseg.reading import LocalReading, LocalStatus

type ReadingKey = tuple[str, int | None, bool, str]
NO_FRAMES = "no frame was decoded"


def reading_key(reading: LocalReading) -> ReadingKey | None:
    """What must stay the same between frames. None for an unreadable frame."""
    if not reading.readable:
        return None
    digits = reading.display_text if reading.overload else (reading.digits or "")
    return digits, reading.digits_before_point, reading.negative, reading.unit


def combine_readings(readings: Sequence[LocalReading], min_agree: int) -> LocalReading:
    """The latest reading when the last `min_agree` readable frames agree; otherwise it is "unstable"."""
    if not readings:
        raise ValueError(NO_FRAMES)
    readable = [reading for reading in readings if reading.readable]
    latest = readable[-1] if readable else readings[-1]
    counts = {"frames": len(readings)}
    if not readable:
        return latest.model_copy(update={**counts, "agreeing_frames": 0})
    key = reading_key(latest)
    agreeing = sum(1 for reading in readable if reading_key(reading) == key)
    tail = readable[-min_agree:]
    stable = len(tail) >= min_agree and all(reading_key(reading) == key for reading in tail)
    update: dict[str, object] = {**counts, "agreeing_frames": agreeing}
    if not stable:
        shown = ", ".join(reading.display_text or "?" for reading in readings)
        update["status"] = LocalStatus.UNSTABLE
        update["problems"] = [
            *latest.problems,
            f"{agreeing} of {len(readings)} frames agree, {min_agree} must agree ({shown})",
        ]
    elif any(reading.status is LocalStatus.UNCERTAIN for reading in tail):
        update["status"] = LocalStatus.UNCERTAIN
        update["confidence"] = min(reading.confidence for reading in tail)
    return latest.model_copy(update=update)


type Grab = Callable[[], Awaitable[bytes]]
type Sleep = Callable[[float], Awaitable[None]]


class SamplePlan(BaseModel):
    """Standalone sampling: `rate_hz` frames per second for `window`, and the frames that must agree."""

    rate_hz: float = Field(default=stability.RATE_HZ, gt=0)
    window: timedelta = stability.WINDOW
    min_agree: int = Field(default=stability.MIN_AGREE, ge=1)

    @property
    def count(self) -> int:
        return max(self.min_agree, round(self.rate_hz * self.window.total_seconds()))


async def sample(
    grab: Grab, profile: MeterProfile, plan: SamplePlan, sleep: Sleep = asyncio.sleep
) -> tuple[LocalReading, list[DecodedFrame]]:
    """Decode the frames of the plan (at least `min_agree`), then combine them."""
    frames = []
    for index in range(plan.count):
        if index:
            await sleep(1.0 / plan.rate_hz)
        frames.append(await asyncio.to_thread(decode_jpeg, await grab(), profile))
    return combine_readings([frame.reading for frame in frames], plan.min_agree), frames
