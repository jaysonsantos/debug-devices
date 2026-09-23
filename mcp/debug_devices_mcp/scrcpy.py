"""Show the phone screen in a scrcpy window. One scrcpy process for the selected serial."""

import asyncio
import logging
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import IO

from pydantic import BaseModel

from debug_devices_mcp.ui.constants import defaults, env, scrcpy
from debug_devices_mcp.ui.desktop import ChildProcess, Spawner, child_env, spawn

logger = logging.getLogger(__name__)


class ScrcpyError(Exception):
    """scrcpy could not start."""


def scrcpy_args(scrcpy_path: str, serial: str) -> list[str]:
    return [
        scrcpy_path,
        scrcpy.SERIAL_FLAG,
        serial,
        scrcpy.WINDOW_TITLE_FLAG,
        f"{scrcpy.WINDOW_TITLE_PREFIX}{serial}",
        *scrcpy.FLAGS,
    ]


class ScrcpyOptions(BaseModel):
    scrcpy_path: str = defaults.SCRCPY
    # scrcpy uses this adb (through the ADB variable), the same as the MCP server.
    adb_path: str | None = None
    log_path: Path | None = None
    stop_timeout: timedelta = defaults.PROCESS_STOP_TIMEOUT


class ScrcpyLauncher:
    def __init__(
        self,
        options: ScrcpyOptions | None = None,
        spawner: Spawner = spawn,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        options = options or ScrcpyOptions()
        self._scrcpy_path = options.scrcpy_path
        self._adb_path = options.adb_path
        self._log_path = options.log_path
        self._stop_timeout = options.stop_timeout.total_seconds()
        self._spawner = spawner
        self._environ = environ
        self._process: ChildProcess | None = None
        self._serial: str | None = None
        self._log: IO[bytes] | None = None
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    @property
    def serial(self) -> str | None:
        return self._serial if self.running else None

    async def ensure_running(self, serial: str) -> bool:
        """Start scrcpy for `serial` when it does not run. Return True when this call started it."""
        if not serial:
            raise ScrcpyError("no ADB serial for scrcpy")
        async with self._lock:
            if self.running and self._serial == serial:
                return False
            await self._stop_locked()
            environ = dict(child_env() if self._environ is None else self._environ)
            if self._adb_path:
                environ[env.ADB] = self._adb_path
            if self._log_path is not None:
                self._log_path.parent.mkdir(parents=True, exist_ok=True)
                self._log = self._log_path.open("ab")
            try:
                self._process = await self._spawner(scrcpy_args(self._scrcpy_path, serial), environ, self._log)
            except OSError as exc:
                self._close_log()
                raise ScrcpyError(f"cannot start {self._scrcpy_path}: {exc}") from exc
            self._serial = serial
            logger.info("started scrcpy for %s", serial)
            return True

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        process, self._process, self._serial = self._process, None, None
        if process is not None and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), self._stop_timeout)
            except TimeoutError:
                process.kill()
                await process.wait()
        self._close_log()

    def _close_log(self) -> None:
        if self._log is not None:
            self._log.close()
            self._log = None
