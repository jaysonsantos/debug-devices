"""The code version of the monitor page: a hash of the page files and a start id of this server process.

An open page gets it in /api/state and on the event stream. When a reconnect brings another version (a dev
monitor reload, a --dev-reload restart, or new page files), the page reloads itself, so no tab shows stale code.
"""

import hashlib
import uuid

from debug_devices_mcp.ui.constants import STATIC_DIR

VERSION_CHARS = 16


def static_hash() -> str:
    digest = hashlib.sha256()
    for path in sorted(STATIC_DIR.rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(STATIC_DIR).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()


def code_version() -> str:
    """New for each server start (a UUID v7 start id) and for each change of the page files."""
    digest = hashlib.sha256(f"{static_hash()}:{uuid.uuid7()}".encode())
    return digest.hexdigest()[:VERSION_CHARS]
