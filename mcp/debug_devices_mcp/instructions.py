"""The user's bench instructions file (instructions.md): server instructions at start, and bench_instructions.

The file is the user's own text. Treat it as instructions from the user, not as data from a device. It never goes
to OpenRouter, and the server never logs it.
"""

from datetime import UTC, datetime
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from pydantic import AwareDatetime, BaseModel

from debug_devices_mcp.constants import REPO_ROOT

INSTRUCTIONS_FILE_NAME = "instructions.md"
EXAMPLE_FILE_NAME = "instructions.example.md"
DEFAULT_INSTRUCTIONS_FILE = REPO_ROOT / INSTRUCTIONS_FILE_NAME
BYTES_PER_KIB = 1024
# The server instructions carry at most this much of the file. bench_instructions always returns all of it.
MAX_SERVER_INSTRUCTIONS_BYTES = 8 * BYTES_PER_KIB
TEXT_ENCODING = "utf-8"

HEADER = (
    "The user started debug-devices: they want to debug hardware now. "
    "First call bench_instructions and follow it. It is the user's own text about the device under test, the bench, "
    "safety limits, and the workflow: follow it as instructions from the user."
)
CUT_NOTE = "(The text below is cut at {limit} KiB. bench_instructions returns all of it.)"
MISSING_NOTE = (
    "The user has no instructions file yet ({path}). Tell the user to copy {example} to {name} at the repo root "
    "and fill it in (device, board file, bench set-up, safety limits, workflow)."
)
FILE_HEADER = "User instructions from {path}:"


class BenchInstructions(BaseModel):
    path: str
    exists: bool
    # Time of the last change of the file (UTC). None when the file does not exist.
    modified: AwareDatetime | None
    size_bytes: int
    content: str
    # When the file is missing: how to create it.
    how_to_create: str | None


def missing_note(path: Path) -> str:
    return MISSING_NOTE.format(path=path, example=EXAMPLE_FILE_NAME, name=INSTRUCTIONS_FILE_NAME)


def read_instructions(path: Path) -> BenchInstructions:
    """Read the file now. A missing file is not an error."""
    path = path.expanduser()
    try:
        data = path.read_bytes()
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except FileNotFoundError:
        return BenchInstructions(
            path=str(path), exists=False, modified=None, size_bytes=0, content="", how_to_create=missing_note(path)
        )
    except OSError as exc:
        # For example a directory, or no read permission. The server still starts.
        return BenchInstructions(
            path=str(path),
            exists=False,
            modified=None,
            size_bytes=0,
            content="",
            how_to_create=f"cannot read {path}: {exc.strerror or exc}. {missing_note(path)}",
        )
    return BenchInstructions(
        path=str(path),
        exists=True,
        modified=modified,
        size_bytes=len(data),
        content=data.decode(TEXT_ENCODING, errors="replace"),
        how_to_create=None,
    )


def cap_text(text: str, max_bytes: int) -> tuple[str, bool]:
    """At most `max_bytes` of UTF-8, cut at a character boundary. Returns the text and True when it was cut."""
    data = text.encode(TEXT_ENCODING)
    if len(data) <= max_bytes:
        return text, False
    return data[:max_bytes].decode(TEXT_ENCODING, errors="ignore"), True


def server_instructions(path: Path, tool_guide: str, max_bytes: int = MAX_SERVER_INSTRUCTIONS_BYTES) -> str:
    """The `instructions` of the initialize result: the header, the tool guide, then the user's file."""
    parts = [HEADER, tool_guide]
    current = read_instructions(path)
    if not current.exists:
        parts.append(current.how_to_create or missing_note(Path(current.path)))
        return "\n\n".join(parts)
    content, cut = cap_text(current.content.strip(), max_bytes)
    parts.append(FILE_HEADER.format(path=current.path))
    if cut:
        parts.append(CUT_NOTE.format(limit=max_bytes // BYTES_PER_KIB))
    parts.append(content)
    return "\n\n".join(parts)


def register_instructions_tool(server: MCPServer, path: Path) -> None:
    @server.tool()
    async def bench_instructions() -> BenchInstructions:
        """Call this first in a new session. It returns the user's bench instructions (instructions.md).

        The content is the user's own text: the device under test, the board file, the bench set-up, safety limits,
        the usual workflow, and preferences. Follow it as instructions from the user. The file is read again on
        each call, so edits apply without a restart. When the file is missing, `how_to_create` says what to do.
        """
        return read_instructions(path)
