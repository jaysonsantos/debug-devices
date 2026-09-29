"""The journal of unsafe readings that the bench state file could not take (N80).

When the lock of `bench-state.json` is busy, an unsafe reading goes at once into `bench-state.journal.jsonl` next to
it: one JSON line per reading, appended with `O_APPEND`, then `fsync`. The append does not wait for the state lock;
it takes only a short lock on the journal file. A write of the state takes that lock only to read the lines and to
cut them after its save (never for the whole write, N81), and the cut removes only the lines whose ids the saved
state has, so a line appended in between stays. So a reading survives any exit (a signal, a crash, SIGKILL): the
next load in any server counts it, and the next write merges it into the state and cuts the merged lines.

A bad line (for example a line that a crash cut) is logged and kept for a later merge, never dropped in silence. An
append starts on a new line when the file does not end with one.
"""

import contextlib
import fcntl
import logging
import os
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import AwareDatetime, BaseModel, ValidationError

from debug_devices_mcp.multimeter import MeterResult

logger = logging.getLogger(__name__)

JOURNAL_SUFFIX = ".journal.jsonl"
JOURNAL_FILE_MODE = 0o600
# The journal lock is held only for an append or a merge (milliseconds).
JOURNAL_LOCK_TIMEOUT = timedelta(seconds=2)
JOURNAL_LOCK_RETRY = timedelta(milliseconds=5)
NEWLINE = b"\n"


class JournalLockedError(OSError):
    """The journal lock stayed busy for JOURNAL_LOCK_TIMEOUT (another server appends or cuts)."""


class JournalEntry(BaseModel):
    """One unsafe reading. The id makes a merge idempotent (the state keeps the merged ids)."""

    id: str
    written_at: AwareDatetime
    result: MeterResult


class JournalLines(BaseModel):
    entries: list[JournalEntry]
    # Lines that do not parse (a cut last line): they stay in the file.
    bad: list[str]
    # Every line as it is in the file, with its entry id (None: a bad line), for the cut.
    raw: list[tuple[str, str | None]] = []


def read_all(fd: int) -> bytes:
    data = b""
    while chunk := os.pread(fd, 1 << 16, len(data)):
        data += chunk
    return data


def journal_path(state_path: Path) -> Path:
    """`bench-state.json` -> `bench-state.journal.jsonl`."""
    return state_path.with_name(f"{state_path.stem}{JOURNAL_SUFFIX}")


def parse(data: bytes) -> JournalLines:
    entries, bad, raw_lines = [], [], []
    for raw in data.decode(errors="replace").splitlines():
        if not raw.strip():
            continue
        try:
            entry = JournalEntry.model_validate_json(raw)
        except ValidationError:
            bad.append(raw)
            raw_lines.append((raw, None))
            continue
        entries.append(entry)
        raw_lines.append((raw, entry.id))
    return JournalLines(entries=entries, bad=bad, raw=raw_lines)


class BenchJournal:
    def __init__(self, path: Path) -> None:
        self.path = path

    def append(self, result: MeterResult) -> JournalEntry:
        """Write one reading and `fsync` it. Raises OSError (a full disk, a read-only folder, a busy journal lock)."""
        entry = JournalEntry(id=str(uuid.uuid7()), written_at=datetime.now(UTC), result=result)
        created = not self.path.exists()
        fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT, JOURNAL_FILE_MODE)
        try:
            with self._locked(fd):
                size = os.fstat(fd).st_size
                if size and os.pread(fd, 1, size - 1) != NEWLINE:
                    os.write(fd, NEWLINE)
                os.write(fd, entry.model_dump_json().encode() + NEWLINE)
                os.fsync(fd)
        finally:
            os.close(fd)
        if created:
            self._sync_folder()
        return entry

    def read(self) -> JournalLines:
        """The lines now, without the lock (for the gate: an append in progress is at most one bad last line)."""
        try:
            return parse(self.path.read_bytes())
        except FileNotFoundError:
            return JournalLines(entries=[], bad=[])

    def read_locked(self) -> JournalLines:
        """The lines under the journal lock (a whole last line). Raises JournalLockedError when the lock stays busy."""
        try:
            fd = os.open(self.path, os.O_RDONLY)
        except FileNotFoundError:
            return JournalLines(entries=[], bad=[])
        try:
            with self._locked(fd):
                return parse(read_all(fd))
        finally:
            os.close(fd)

    def cut(self, merged_ids: set[str]) -> None:
        """After a state save: remove the lines whose ids the saved state has. Lines with other ids (appended after
        the read of this write) and bad lines stay. The same inode stays, so a waiting append goes after them. Raises
        JournalLockedError when the lock stays busy (the lines then stay for a later cut; the ids make that safe)."""
        try:
            fd = os.open(self.path, os.O_RDWR)
        except FileNotFoundError:
            return
        try:
            with self._locked(fd):
                lines = parse(read_all(fd))
                for line in lines.bad:
                    logger.warning("a bad line in the bench journal %s stays for later: %.120s", self.path, line)
                kept = [raw for raw, entry_id in lines.raw if entry_id is None or entry_id not in merged_ids]
                if len(kept) == len(lines.raw):
                    return
                os.ftruncate(fd, 0)
                if kept:
                    os.pwrite(fd, b"".join(raw.encode() + NEWLINE for raw in kept), 0)
                os.fsync(fd)
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def _locked(self, fd: int) -> Iterator[None]:
        deadline = time.monotonic() + JOURNAL_LOCK_TIMEOUT.total_seconds()
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise JournalLockedError(f"another server holds the bench journal lock: {self.path}") from None
                time.sleep(JOURNAL_LOCK_RETRY.total_seconds())
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)

    def _sync_folder(self) -> None:
        with contextlib.suppress(OSError):
            folder = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(folder)
            finally:
                os.close(folder)
