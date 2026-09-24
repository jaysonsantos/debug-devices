"""Run `obv-dump` and build a `Board`. Boards are cached in memory by the SHA-256 of the file."""

import asyncio
import hashlib
import time
from datetime import timedelta
from pathlib import Path

from pydantic import BaseModel, SecretStr, ValidationError

from debug_devices_mcp.board.constants import HASH_CHUNK_BYTES, dump
from debug_devices_mcp.board.dump import BoardDump, DumpError
from debug_devices_mcp.board.model import Board
from debug_devices_mcp.constants import ERROR_BODY_PREVIEW_CHARS
from debug_devices_mcp.process import CommandError, CommandRunner


class BoardviewKeys(BaseModel):
    """Keys for encrypted formats. They go only to the obv-dump command line, never into a message or a log."""

    fz: SecretStr | None = None
    cae: SecretStr | None = None
    xzz: SecretStr | None = None


class LoaderOptions(BaseModel):
    dump_bin: str
    timeout: timedelta
    keys: BoardviewKeys = BoardviewKeys()


class BoardLoadError(Exception):
    """The board could not be loaded. The message never contains a key."""


class BoardDumpFailed(BoardLoadError):
    """obv-dump returned a `DumpError`."""

    def __init__(self, error: DumpError) -> None:
        detail = f" ({error.format})" if error.format else ""
        super().__init__(f"obv-dump could not read the board{detail}: {error.error}: {error.message}")
        self.error = error


class LoadedBoard(BaseModel):
    """What `load` tells the caller besides the board."""

    sha256: str
    cached: bool
    load_seconds: float


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(HASH_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def key_args(keys: BoardviewKeys) -> list[str]:
    args: list[str] = []
    for flag, key in ((dump.FZ_KEY_FLAG, keys.fz), (dump.CAE_KEY_FLAG, keys.cae), (dump.XZZ_KEY_FLAG, keys.xzz)):
        if key is not None and key.get_secret_value():
            args += [flag, key.get_secret_value()]
    return args


def parse_dump_output(stdout: bytes, returncode: int) -> BoardDump:
    """Exit 0: a `BoardDump`. Exit 1: a `DumpError`. Anything else is a crash."""
    preview = stdout.decode(errors="replace").strip()[:ERROR_BODY_PREVIEW_CHARS]
    if returncode == dump.EXIT_PARSE_FAILED:
        try:
            error = DumpError.model_validate_json(stdout)
        except ValidationError as exc:
            raise BoardLoadError(f"obv-dump failed (exit 1) without a DumpError: {preview!r}") from exc
        raise BoardDumpFailed(error)
    if returncode != dump.EXIT_OK:
        raise BoardLoadError(f"obv-dump crashed (exit {returncode}). The file is probably damaged or not supported.")
    try:
        return BoardDump.model_validate_json(stdout)
    except ValidationError as exc:
        raise BoardLoadError(f"obv-dump output does not match docs/boardview-json.md: {exc}") from exc


class BoardviewLoader:
    def __init__(self, runner: CommandRunner, options: LoaderOptions) -> None:
        self._runner = runner
        self._options = options
        self._cache: dict[str, Board] = {}

    @property
    def options(self) -> LoaderOptions:
        return self._options

    async def load(self, path: Path) -> tuple[Board, LoadedBoard]:
        start = time.monotonic()
        path = await asyncio.to_thread(path.expanduser)
        try:
            sha256 = await asyncio.to_thread(file_sha256, path)
        except OSError as exc:
            raise BoardLoadError(f"cannot read {path}: {exc.strerror or exc}") from exc
        if (board := self._cache.get(sha256)) is not None:
            return board, LoadedBoard(sha256=sha256, cached=True, load_seconds=time.monotonic() - start)
        command = [self._options.dump_bin, str(path), *key_args(self._options.keys)]
        try:
            result = await self._runner.run(command, self._options.timeout)
        except CommandError:
            # The command error text has the full command line, keys included. Do not chain or repeat it.
            raise BoardLoadError(
                f"obv-dump did not finish for {path.name} (not found, or no result within {self._options.timeout}). "
                f"Check --obv-dump-path ({self._options.dump_bin})."
            ) from None
        parsed = await asyncio.to_thread(parse_dump_output, result.stdout, result.returncode)
        board = await asyncio.to_thread(Board, parsed, sha256)
        self._cache[sha256] = board
        return board, LoadedBoard(sha256=sha256, cached=False, load_seconds=time.monotonic() - start)
