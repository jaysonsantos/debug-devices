"""One staged capture: the key press on the monitor page (Space, C, or the Capture button) takes the phone photo and
the multimeter reading of that moment, and the queue (staged.py) keeps them for the agent's next `multimeter_read`.

The photo and the meter part run at the same time, like bench_measure:

- The photo is the still of phone_snapshot (the same turn and flips), scaled like its default result. It does not
  replace the agent's last snapshot, and it leaves the camera view of the agent's photos alone (`record_view=False`,
  N88): the pixel tools keep referring to the photo that the agent saw.
- The meter part is the path of `multimeter_read` (`server.read_meter`: the vision call, the frame checks, the
  bench limits, and the bench gate), so an unsafe voltage closes the bench gate at capture time, before any pop.
  Without a crop box there is no meter part and no vision call: only the crop goes to the vision model. The crop is
  checked again at each frame (`require_crop`, N89): a crop box cleared during the capture stops the meter part.
"""

import asyncio
import logging
from collections.abc import Callable
from uuid import uuid7

from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel

from debug_devices_mcp.constants import images
from debug_devices_mcp.images import downscale_jpeg
from debug_devices_mcp.meter_frames import DEFAULT_FRAMES
from debug_devices_mcp.multimeter import MeterResult, MeterSource
from debug_devices_mcp.phone_api import PhoneError
from debug_devices_mcp.server import Services, read_meter
from debug_devices_mcp.staged import StagedCapture, StagedPhoto, StagedState, StagedStore, utc_now
from debug_devices_mcp.webcam import WebcamError

logger = logging.getLogger(__name__)

NO_CROP = "no meter reading: no crop box is set (only the crop box goes to the vision model; set it on the page)"
NO_PHOTO = "no phone photo: {reason}"
NO_METER = "no meter reading: {reason}"


class PhotoPart(BaseModel):
    jpeg: bytes | None = None
    photo: StagedPhoto | None = None
    note: str | None = None


class MeterPart(BaseModel):
    result: MeterResult | None = None
    frames: list[bytes] = []
    note: str | None = None


class StagedCapturer:
    """Takes captures for the page and writes them to the store. `origin`: this server's origin label."""

    def __init__(self, services: Services, store: StagedStore, origin: Callable[[], str]) -> None:
        self._services = services
        self.store = store
        self._origin = origin
        self._tasks: set[asyncio.Task[None]] = set()

    async def capture(self) -> StagedCapture:
        """Stage one capture now (pending) and fill it in the background. Raises QueueFullError."""
        capture = StagedCapture(capture_id=str(uuid7()), captured_at=utc_now(), origin=self._origin())
        await self.store.add(capture)
        task = asyncio.create_task(self._fill(capture), name="staged-capture")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return capture

    async def wait(self) -> None:
        """Wait for the running captures (tests, and a clean stop)."""
        while self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def _fill(self, capture: StagedCapture) -> None:
        try:
            photo, meter = await asyncio.gather(self._photo(), self._meter())
        except Exception as exc:  # the capture must end with a result in the store, whatever failed
            logger.exception("a staged capture failed")
            photo, meter = PhotoPart(), MeterPart(note=NO_METER.format(reason=exc))
        notes = [note for note in (photo.note, meter.note) if note]
        state = StagedState.READY if meter.result is not None or photo.photo is not None else StagedState.FAILED
        finished = capture.model_copy(
            update={"state": state, "photo": photo.photo, "meter": meter.result, "notes": notes}
        )
        if not await self.store.finish(finished, photo.jpeg, meter.frames):
            logger.info("staged capture %s was removed before it finished", capture.capture_id)

    async def _photo(self) -> PhotoPart:
        try:
            # Not the agent's photo: it must not set the camera view of the agent's photos (N88).
            jpeg, transform = await self._services.phone_snapshot(record_view=False)
        except (PhoneError, ToolError, OSError) as exc:
            return PhotoPart(note=NO_PHOTO.format(reason=exc))
        scaled = await asyncio.to_thread(downscale_jpeg, jpeg, images.DEFAULT_MAX_SIDE)
        photo = StagedPhoto(
            width=scaled.width,
            height=scaled.height,
            turn_degrees=transform.turn_degrees,
            flip_horizontal=transform.flip_horizontal,
            flip_vertical=transform.flip_vertical,
        )
        return PhotoPart(jpeg=scaled.data, photo=photo)

    async def _meter(self) -> MeterPart:
        services = self._services
        if services.webcam.crop is None:
            return MeterPart(note=NO_CROP)
        try:
            result, captured = await read_meter(services, MeterSource.WEBCAM, None, DEFAULT_FRAMES, require_crop=True)
        except (ToolError, WebcamError) as exc:
            return MeterPart(note=NO_METER.format(reason=exc))
        return MeterPart(result=result, frames=[jpeg for jpeg, _ in captured])
