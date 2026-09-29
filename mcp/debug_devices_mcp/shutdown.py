"""Make sure the process exits after the server stops.

The MCP SDK reads stdin in a worker thread. In a terminal, that read still waits for input after Ctrl-C, so the
process stays alive after the cleanup. When the MCP client closes stdin (the normal case), the process exits
before the watchdog fires.
"""

import contextlib
import logging
import os
import sys
import threading
from collections.abc import Callable
from datetime import timedelta

logger = logging.getLogger(__name__)

# The time for a normal exit after the lifespan cleanup. After it, the process exits by force.
EXIT_GRACE = timedelta(seconds=2)
EXIT_CODE = 0
WATCHDOG_THREAD_NAME = "exit-watchdog"

type ExitFunction = Callable[[int], object]


def _force_exit(exit_function: ExitFunction) -> None:
    logger.warning("the server stopped, but a thread still waits for stdin; exiting now")
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(OSError, ValueError):
            stream.flush()
    exit_function(EXIT_CODE)


def arm_exit_watchdog(grace: timedelta = EXIT_GRACE, exit_function: ExitFunction = os._exit) -> threading.Timer:
    """Exit by force `grace` after this call. The timer is a daemon thread: a normal exit does not wait for it."""
    timer = threading.Timer(grace.total_seconds(), _force_exit, args=(exit_function,))
    timer.name = WATCHDOG_THREAD_NAME
    timer.daemon = True
    timer.start()
    return timer
