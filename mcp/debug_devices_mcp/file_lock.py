"""A cross-process lock on a lock file (`fcntl.flock`), with a time limit. The settings file and the staged captures
use it: every MCP server of the user writes these files."""

import errno
import fcntl
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

# A writer holds the lock only for a short read and write. After this time, the caller gets an OSError.
LOCK_TIMEOUT = timedelta(seconds=2)
LOCK_RETRY = timedelta(milliseconds=10)


@contextmanager
def locked(lock_path: Path, what: str, timeout: timedelta = LOCK_TIMEOUT) -> Iterator[None]:
    """Hold the lock on `lock_path` (created when missing). This blocks the calling thread while it waits: on the
    event loop, run the locked work with `asyncio.to_thread`."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as handle:
        deadline = time.monotonic() + timeout.total_seconds()
        while True:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError(errno.EWOULDBLOCK, f"{what} is locked: {lock_path}") from None
                time.sleep(LOCK_RETRY.total_seconds())
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
