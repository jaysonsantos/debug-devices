"""Several decoded frames give one stable reading, "unstable", or "unreadable".

A value is stable when the last `min_agree` readable frames show the same digits, decimal point, sign, unit, and mode.
With fewer readable frames than `min_agree`, the reading is "unreadable": there is not enough to tell.
"""

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import timedelta

from pydantic import BaseModel, Field

from debug_devices_mcp.meter_mode import MeterMode
from debug_devices_mcp.sevenseg.constants import stability
from debug_devices_mcp.sevenseg.decode import DecodedFrame, decode_jpeg
from debug_devices_mcp.sevenseg.profile import MeterProfile
from debug_devices_mcp.sevenseg.reading import LocalReading, LocalStatus

type ReadingKey = tuple[str, int | None, bool, str, MeterMode]
NO_FRAMES = "no frame was decoded"


def reading_key(reading: LocalReading) -> ReadingKey | None:
    """What must stay the same between frames (an AC/DC change is a change). None for an unreadable frame."""
    if not reading.readable:
        return None
    digits = reading.display_text if reading.overload else (reading.digits or "")
    return digits, reading.digits_before_point, reading.negative, reading.unit, reading.mode


def shown(readings: Sequence[LocalReading]) -> str:
    return ", ".join(
        f"{reading.display_text} {reading.unit} {reading.mode}" if reading.readable else "unreadable"
        for reading in readings
    )


def combine_readings(readings: Sequence[LocalReading], min_agree: int) -> LocalReading:
    """The latest readable reading when the last `min_agree` readable frames agree; "unstable" when they do not;
    "unreadable" when fewer than `min_agree` frames are readable."""
    if not readings:
        raise ValueError(NO_FRAMES)
    readable = [reading for reading in readings if reading.readable]
    latest = readable[-1] if readable else readings[-1]
    key = reading_key(latest)
    agreeing = sum(1 for reading in readable if reading_key(reading) == key)
    update: dict[str, object] = {"frames": len(readings), "agreeing_frames": agreeing}
    if len(readable) < min_agree:
        update |= {
            "status": LocalStatus.UNREADABLE,
            "readable": False,
            "value": None,
            "problems": [
                *latest.problems,
                f"{len(readable)} of {len(readings)} frames are readable, {min_agree} must agree ({shown(readings)})",
            ],
        }
        return latest.model_copy(update=update)
    tail = readable[-min_agree:]
    if not all(reading_key(reading) == key for reading in tail):
        update |= {
            "status": LocalStatus.UNSTABLE,
            "problems": [
                *latest.problems,
                f"{agreeing} of {len(readings)} frames agree, {min_agree} must agree ({shown(readings)})",
            ],
        }
    elif any(reading.status is LocalStatus.UNCERTAIN for reading in tail):
        update |= {"status": LocalStatus.UNCERTAIN, "confidence": min(reading.confidence for reading in tail)}
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
