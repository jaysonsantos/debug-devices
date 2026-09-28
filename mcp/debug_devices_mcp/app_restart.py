"""The phone app restarted (a new `CameraStatus.app_start_id`): the agent and the user must know.

After a restart the app is back at its defaults: the zoom is 1x, the still can have another turn, and the phone has
no boxes. The old photo registrations and highlight boxes are for the old camera view. Every phone tool result carries
the notice until the agent takes a new phone_snapshot, and the monitor page shows it.
"""

from datetime import UTC, datetime

from pydantic import AwareDatetime, BaseModel

from debug_devices_mcp.phone_api import CameraStatus

RESTART_NOTICE = (
    "the phone app restarted: the snapshot turn is now {turn} degrees and the zoom is back to {zoom:g}x; "
    "old boxes and registrations were cleared"
)
RESTART_STALE_REASON = (
    "registration {id!r} is from before the phone app restarted (the zoom and the snapshot turn went back to the app "
    "defaults): take a fresh phone_snapshot and call board_register_photo again"
)
# The tools whose results carry the notice.
PHONE_TOOL_PREFIX = "phone_"


class RestartNotice(BaseModel):
    message: str
    app_start_id: str
    turn_degrees: int
    zoom_ratio: float
    at: AwareDatetime


class AppRestartWatch:
    """Sees each phone status. The first app_start_id is the app run that this server met: no restart."""

    def __init__(self) -> None:
        self._seen: str | None = None

    def restarted(self, status: CameraStatus) -> bool:
        start_id = status.app_start_id
        if start_id is None:
            return False
        restarted = self._seen is not None and start_id != self._seen
        self._seen = start_id
        return restarted


def restart_notice(status: CameraStatus, turn_degrees: int) -> RestartNotice:
    return RestartNotice(
        message=RESTART_NOTICE.format(turn=turn_degrees, zoom=status.zoom_ratio),
        app_start_id=status.app_start_id or "",
        turn_degrees=turn_degrees,
        zoom_ratio=status.zoom_ratio,
        at=datetime.now(UTC),
    )
