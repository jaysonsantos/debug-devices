"""Shared fakes. Run the tests with `uv run pytest` from the repo root."""

import io
from collections.abc import Callable, Sequence
from datetime import timedelta
from pathlib import Path

import pytest
from PIL import Image

from debug_devices_mcp.config import Settings
from debug_devices_mcp.constants import env
from debug_devices_mcp.process import CommandResult


def make_jpeg(width: int, height: int) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(output, format="JPEG")
    return output.getvalue()


JPEG = make_jpeg(64, 48)

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
def settings(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Settings:
    """Defaults only: no .env file and no variables from the shell.

    The instructions file points into tmp_path, so no test reads the user's real instructions.md.
    """
    for name in (
        env.OPENROUTER_API_KEY,
        env.VISION_MODEL,
        env.WEBCAM,
        env.ADB_SERIAL,
        env.METER_MODEL,
        env.INSTRUCTIONS,
    ):
        monkeypatch.delenv(name, raising=False)
    return Settings(
        _env_file=None,
        poll_interval=timedelta(milliseconds=1),
        app_start_timeout=timedelta(seconds=1),
        instructions_file=tmp_path / "instructions.md",
    )
