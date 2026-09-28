"""When may a sync send a stored setting to the phone again? (docs/phone-api.md, `app_start_id`)

Only in two cases:

- the user or an agent changes the setting through this server (the tools send it themselves), or
- the app started again: `CameraStatus.app_start_id` is new since this server last saw it (and one time after
  phone_connect).

A status that differs from the stored value is never a reason to send it again: another MCP server or client may
have changed it. Two servers that each "correct" the phone to their own value would fight forever (the camera rebound
every ~7 s on 2026-09-27).
"""

import logging

from debug_devices_mcp.phone_api import CameraStatus

logger = logging.getLogger(__name__)

OTHER_CLIENT_LOG = "the phone %s is %s (changed by another client); not sending ours again"


class AppStartWatch:
    def __init__(self, setting: str) -> None:
        self._setting = setting
        self._seen: str | None = None
        # True until the first status after phone_connect (and after the server start).
        self._connect = True
        self._logged: tuple[str | None, str] | None = None

    def reset(self) -> None:
        """A new phone_connect: the next status may get the stored setting one time."""
        self._connect = True

    def may_send(self, status: CameraStatus) -> bool:
        """Call this for every status read. True when the stored setting may be sent now."""
        connect, self._connect = self._connect, False
        start_id = status.app_start_id
        if start_id is None:
            # An app from before app_start_id: only at phone_connect, never on the status polls.
            return connect
        restarted = start_id != self._seen
        self._seen = start_id
        return restarted or connect

    def other_client(self, status: CameraStatus, phone_value: str) -> None:
        """The phone shows another value in the same app run: someone else changed it. Log it one time."""
        key = (status.app_start_id, phone_value)
        if key != self._logged:
            self._logged = key
            logger.info(
                "the phone %s is %s (changed by another client); not sending ours again", self._setting, phone_value
            )
