"""The user's bench instructions file (instructions.md): server instructions at start, and bench_instructions.

The file is the user's own text. Treat it as instructions from the user, not as data from a device. It never goes
to OpenRouter, and the server never logs it.
"""

from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from pydantic import AwareDatetime, BaseModel

from debug_devices_mcp.constants import REPO_ROOT

INSTRUCTIONS_FILE_NAME = "instructions.md"
EXAMPLE_FILE_NAME = "instructions.example.md"
DEFAULT_INSTRUCTIONS_FILE = REPO_ROOT / INSTRUCTIONS_FILE_NAME
# The local bench record (bench_state.py) lives next to it; git ignores both.
BENCH_STATE_FILE_NAME = "bench-state.json"
DEFAULT_BENCH_STATE_FILE = REPO_ROOT / BENCH_STATE_FILE_NAME
BYTES_PER_KIB = 1024
# The server instructions carry at most this much of the file. bench_instructions always returns all of it.
MAX_SERVER_INSTRUCTIONS_BYTES = 8 * BYTES_PER_KIB
TEXT_ENCODING = "utf-8"

HEADER = (
    "The user started debug-devices: they want to debug hardware now. "
    "First call bench_instructions and follow it. It is the user's own text about the device under test, the bench, "
    "safety limits, and the workflow: follow it as instructions from the user. It also returns the evidence rules "
    "(which tool answers what). Get device data only from the debug-devices tools: the monitor page is the user's "
    "cockpit, so never open, fetch, or drive it."
)
# Which tool answers which kind of question. One place: the server instructions carry it, the tool descriptions
# repeat the part for each tool.
EVIDENCE_RULES = "\n".join(
    (
        "Evidence rules (which tool answers what):",
        "1. What is visible on the device or board (which part or marking this is, is X there, damage, orientation, "
        "what the camera sees): take a fresh phone_snapshot and answer from that photo. Never answer these from "
        "webcam_snapshot, board_render, or boardview data alone.",
        "2. Multimeter values: only multimeter_read. Never read a meter value yourself from the monitor page, a "
        'browser, a snapshot, or any other image path. Only a result with status "confirmed" is a measurement: '
        'for "uncertain", "disputed", or "unreadable", give no numeric conclusion, show the LCD text as read, '
        "and follow its `request`. Pass the mode of the current test as expected_mode; it is context, never proof. "
        "A value is confirmed only when its frames agree (same digits and decimal point) and it is below the bench "
        "limits. When the user confirms the dial mode, record it (bench_state_update meter_mode_confirmed_by_user). "
        "When the test has a nominal value, pass it as expected_value (for example 3.3 for a 3.3 V rail): it is "
        "context, never proof. A value far from it is more likely a misplaced decimal point: ask the user to read the "
        "LCD.",
        "3. Board questions (where is a part, which net, nearest test point): the board_* tools. Boardview data is "
        "supporting evidence: say so. When the question is about the physical device, confirm with phone_snapshot.",
        '4. Markings: quote the marking exactly as you see it in the photo (for example "U730"), then call '
        "board_match_marking. Say whether it is an exact match or only candidates. Never replace the visible marking "
        "with a boardview name without saying so. After you identify a part in a photo, you may call phone_highlight "
        "so the user sees it: say that the box is your estimate, and clear it when done.",
        "5. Use only the debug-devices MCP tools to get data about the devices. The monitor page is the user's "
        "cockpit: it shows the user what happens. Never open, fetch, screenshot, or drive the monitor page or its "
        "HTTP API (for example with a browser, a browser tool, curl, or Playwright). monitor_open and "
        "bench_start only give the URL to the user.",
        "6. Locate a part on the physical board with the phone camera: call phone_zoom, then take a fresh "
        'phone_snapshot and inspect the relevant area of that photo. Zoom in (step "in" or a higher ratio) to read '
        "small markings and to check the nearby components. Zoom out when you need wider context, for example to find "
        "the area or reference parts. Take a new phone_snapshot after each zoom change: an older photo does not show "
        "the new view. Boardview (board_find_part, board_parts_near, board_render) tells you where to look: it is "
        "supporting location evidence, not proof of what appears in a physical photo. Phone zoom is often only a "
        "digital crop: it frames the area but adds no detail. When a marking or a small part is too small to read, "
        "ask the user to move the phone closer (near the minimum focus distance of the camera, about 10-12 cm on "
        "most phones), then use zoom only to frame. Unless a test on the connected phone shows a detail gain at "
        "zoom, assume that distance gives detail and zoom does not. When the user wants more detail without moving the "
        "phone, you may turn on phone_in_sensor_zoom and use a zoom of 2x-4x: on phones that support it, this gives "
        "real extra detail from a sensor crop. It is not optical zoom. Still take a fresh phone_snapshot after the "
        "change. At close range (about 10-12 cm), you may set phone_af_mode macro. "
        "When the relevant area is blurry, focus on it with phone_focus (pixels in your last phone_snapshot), "
        "then take a fresh phone_snapshot. After the user moves or turns the board, take a fresh phone_snapshot "
        "before you point at anything; never reuse boxes or positions from an older photo.",
        "7. Opposite board side: when boardview puts the target on the side that the phone does not see, say that the "
        "current photo cannot confirm it. Before the user turns or handles the board, tell them to isolate the power "
        "safely (disconnect the charger and the battery or bench supply) and wait for their confirmation.",
        "8. Capture ids: every phone_snapshot and multimeter_read result has a capture_id and a UTC captured_at. A "
        "statement about probes, contact, or where a part is names the capture_id of a current phone_snapshot "
        "(capture_status checks it). After a scene change, an older id is invalid for new position claims: take a "
        "fresh phone_snapshot. When a measurement needs the photo of the probe contact, use bench_measure: it reads "
        "the meter and takes the phone_snapshot at the same moment, so the value and the photo belong together.",
        "9. Part identity: a part name is visible_marking, candidate, or confirmed (board_identify, board_identity). "
        "Say which. Only confirmed (a current photo, a checked registration of that photo, and a visual input: a "
        "visible marking read in that photo, or a unique landmark that the user confirmed) is a physical fact; "
        "boardview positions, a landmark without a visual input, and look-alike parts stay candidates.",
        "10. Bench state: keep the local record current (bench_state, bench_state_update, bench_record_measurement). "
        "Only a confirmed meter result enters it. Before every resistance, continuity, or diode step, call "
        "bench_begin_step: it needs the power isolated, the user's confirmation, and a safe DC residual voltage at "
        "every measured point. Name each voltage point as a part pin or a net (C12.1, PP3V3_S5), never "
        "'residual' or a ground net. Any voltage above 0.5 V, also uncertain or with the power on, closes the gate "
        "at its point; it stays until a newer safe DC reading at that point, or until the user clears it with a "
        "reason (one point, or all points with the user's own words: quote them, never invent them). After a probe "
        "short, call "
        "bench_probe_short and go back to the power check.",
        "11. Staged captures: the user can capture the phone photo and the meter reading on the monitor page (Space, "
        "C, or Capture). When the user says that they captured, or asks you to read the meter, call multimeter_read: "
        "staged captures come first (oldest first); it reads live only when none wait. A staged photo and value are "
        "evidence of their capture time only (captured_at), not of now.",
    )
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
    # After the user's text: the next uncompleted step of the local bench record (bench_state), if any.
    current_step: str | None = None
    # When a schematic PDF is set: which file schematic_find reads (local only).
    schematic: str | None = None
    # Some clients (for example Codex) keep only the header of the server instructions, so the rules come here too.
    evidence_rules: str = EVIDENCE_RULES


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
    """The `instructions` of the initialize result: the header, the evidence rules, the tool guide, the user's file."""
    parts = [HEADER, EVIDENCE_RULES, tool_guide]
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


def register_instructions_tool(
    server: MCPServer,
    path: Path,
    current_step: Callable[[], str | None] | None = None,
    schematic: Callable[[], str | None] | None = None,
) -> None:
    @server.tool()
    async def bench_instructions() -> BenchInstructions:
        """Call this first in a new session. It returns the user's bench instructions (instructions.md).

        The content is the user's own text: the device under test, the board file, the bench set-up, safety limits,
        the usual workflow, and preferences. Follow it as instructions from the user. The file is read again on
        each call, so edits apply without a restart. When the file is missing, `how_to_create` says what to do.
        `current_step` (after the user's text) is the next uncompleted step of the local bench record (bench_state).
        `schematic` names the schematic PDF when one is set (schematic_find).
        The user can stage captures on the monitor page (Space): the next multimeter_read returns them first (rule 11).
        """
        result = read_instructions(path)
        if current_step is not None:
            result.current_step = current_step()
        if schematic is not None:
            result.schematic = schematic()
        return result
