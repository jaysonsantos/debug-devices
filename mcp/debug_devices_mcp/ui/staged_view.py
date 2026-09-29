"""The staged captures as the page shows them: the list route and the `staged` SSE event send the same view."""

from datetime import datetime

from pydantic import AwareDatetime, BaseModel

from debug_devices_mcp.staged import MAX_STAGED, STAGED_TTL, Capturer, StagedCapture, StagedState, utc_now

SECONDS_PER_MINUTE = 60


class StagedView(BaseModel):
    """One capture as the page shows it."""

    capture_id: str
    captured_at: AwareDatetime
    age_s: float
    state: StagedState
    origin: str
    has_photo: bool
    # The meter crop image of the reading (the crop only).
    has_meter_image: bool = False
    # For example "4.98 V" and "confirmed"; None while the reading runs or without a meter part.
    meter_text: str | None = None
    meter_status: str | None = None
    bench_notice: str | None = None
    notes: list[str] = []

    @classmethod
    def of(cls, capture: StagedCapture, now: datetime) -> StagedView:
        meter = capture.meter
        return cls(
            capture_id=capture.capture_id,
            captured_at=capture.captured_at,
            age_s=capture.age_seconds(now),
            state=capture.state,
            origin=capture.origin,
            has_photo=capture.photo is not None,
            has_meter_image=capture.meter_image_index() is not None,
            meter_text=f"{meter.display_text} {meter.unit}".strip() if meter is not None else None,
            meter_status=str(meter.status) if meter is not None else None,
            bench_notice=meter.bench_notice if meter is not None else None,
            notes=capture.notes,
        )

    def page_key(self) -> tuple[object, ...]:
        """What the page shows of this capture (not its age): a push only when one of these changes."""
        return (self.capture_id, self.state, self.has_photo, self.has_meter_image, self.meter_text, *self.notes)


class StagedList(BaseModel):
    captures: list[StagedView]
    limit: int = MAX_STAGED
    ttl_minutes: int = int(STAGED_TTL.total_seconds() // SECONDS_PER_MINUTE)

    def page_key(self) -> tuple[object, ...]:
        return tuple(capture.page_key() for capture in self.captures)


async def staged_list(capturer: Capturer | None) -> StagedList:
    if capturer is None:
        return StagedList(captures=[])
    now = utc_now()
    return StagedList(captures=[StagedView.of(item, now) for item in await capturer.store.list()])
