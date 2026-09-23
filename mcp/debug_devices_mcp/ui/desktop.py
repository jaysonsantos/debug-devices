"""Start desktop programs (Firefox, scrcpy) from the MCP process."""

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from pathlib import Path
from typing import IO, Protocol

from debug_devices_mcp.ui.constants import browser, defaults, env

logger = logging.getLogger(__name__)


def child_env(environ: Mapping[str, str] = os.environ) -> dict[str, str]:
    """The environment for GUI children.

    The MCP client can start the server without WAYLAND_DISPLAY and DISPLAY. Then point the children at the
    default Wayland socket, when it exists.
    """
    result = dict(environ)
    if result.get(env.WAYLAND_DISPLAY) or result.get(env.DISPLAY):
        return result
    runtime_dir = result.get(env.XDG_RUNTIME_DIR)
    if runtime_dir and (Path(runtime_dir) / defaults.WAYLAND_SOCKET).exists():
        result[env.WAYLAND_DISPLAY] = defaults.WAYLAND_SOCKET
    return result


class ChildProcess(Protocol):
    @property
    def returncode(self) -> int | None: ...

    def terminate(self) -> None: ...

    def kill(self) -> None: ...

    async def wait(self) -> int: ...


type Spawner = Callable[[Sequence[str], Mapping[str, str], IO[bytes] | None], Awaitable[ChildProcess]]


async def spawn(args: Sequence[str], environ: Mapping[str, str], log: IO[bytes] | None) -> ChildProcess:
    """Start a GUI child. Its output never goes to stdout: stdout is the MCP stdio channel."""
    output = log if log is not None else asyncio.subprocess.DEVNULL
    return await asyncio.create_subprocess_exec(
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=output,
        stderr=output,
        env=dict(environ),
    )


class BrowserOpener:
    """Open the monitor page once in a new Firefox window. Fall back to xdg-open."""

    def __init__(self, spawner: Spawner = spawn, environ: Mapping[str, str] | None = None) -> None:
        self._spawner = spawner
        self._environ = environ
        self._waiters: set[asyncio.Task[int]] = set()

    async def open(self, url: str) -> str | None:
        """Return the program that opened the page, or None when no program could start."""
        environ = child_env() if self._environ is None else self._environ
        for command in (browser.FIREFOX, browser.FALLBACK):
            try:
                process = await self._spawner([*command, url], environ, None)
            except OSError as exc:
                logger.info("cannot start %s: %s", command[0], exc)
                continue
            # Reap the child in the background. A running Firefox takes the URL and exits at once.
            waiter = asyncio.create_task(process.wait())
            self._waiters.add(waiter)
            waiter.add_done_callback(self._waiters.discard)
            return command[0]
        logger.warning("cannot open the monitor page; open %s by hand", url)
        return None
