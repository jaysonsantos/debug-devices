"""Shared fakes. Run the tests with `uv run pytest` from the repo root."""

from collections.abc import Callable, Sequence
from datetime import timedelta

import pytest

from debug_devices_mcp.config import Settings
from debug_devices_mcp.constants import env
from debug_devices_mcp.process import CommandResult

JPEG = b"\xff\xd8\xff\xe0fake-jpeg\xff\xd9"

type Responder = Callable[[list[str]], CommandResult]


class FakeRunner:
    """Records every command and answers with `responder`."""

    def __init__(self, responder: Responder) -> None:
        self.calls: list[list[str]] = []
        self._responder = responder

    async def run(self, args: Sequence[str], timeout: timedelta) -> CommandResult:
        command = list(args)
        self.calls.append(command)
        return self._responder(command)


def ok(stdout: bytes = b"", stderr: bytes = b"") -> CommandResult:
    return CommandResult(returncode=0, stdout=stdout, stderr=stderr)


def failed(stderr: bytes, returncode: int = 1) -> CommandResult:
    return CommandResult(returncode=returncode, stdout=b"", stderr=stderr)


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Defaults only: no .env file and no variables from the shell."""
    for name in (env.OPENROUTER_API_KEY, env.VISION_MODEL, env.WEBCAM, env.ADB_SERIAL):
        monkeypatch.delenv(name, raising=False)
    return Settings(_env_file=None, poll_interval=timedelta(milliseconds=1), app_start_timeout=timedelta(seconds=1))
