"""Run external commands (adb, ffmpeg). Tests replace the runner with a fake."""

import asyncio
from collections.abc import Sequence
from datetime import timedelta
from typing import Protocol

from pydantic import BaseModel


class CommandResult(BaseModel):
    returncode: int
    stdout: bytes
    stderr: bytes

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandError(Exception):
    """A command did not start, timed out, or exited with an error."""


class CommandRunner(Protocol):
    async def run(self, args: Sequence[str], timeout: timedelta) -> CommandResult: ...


class SubprocessRunner:
    async def run(self, args: Sequence[str], timeout: timedelta) -> CommandResult:
        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise CommandError(f"command not found: {args[0]}") from exc
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout.total_seconds())
        except TimeoutError as exc:
            process.kill()
            await process.wait()
            raise CommandError(f"command timed out after {timeout}: {' '.join(args)}") from exc
        return CommandResult(returncode=process.returncode or 0, stdout=stdout, stderr=stderr)
