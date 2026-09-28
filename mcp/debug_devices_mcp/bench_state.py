"""The bench state: one small local session record (bench-state.json next to instructions.md, git-ignored).

It holds the power state, the meter mode, the probe contact, the confirmed measurements, the part candidates, the
photo ids, and the steps. Only a "confirmed" multimeter_read result can enter the measurements. A safety gate
guards resistance, continuity, and diode steps: power isolated, confirmed by the user, and a safe DC residual voltage
measured after that confirmation, with no unsafe point left. Every voltage reading above the safe limit closes the
gate, also when it is not confirmed (fail safe). A probe short sends the record back to the power check.

The record holds only what the agent passes (a few part names), never data copied from a board file.
"""

import asyncio
import contextlib
import fcntl
import json
import logging
import math
import os
import tempfile
import threading
import time
import uuid
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from functools import partial
from pathlib import Path

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import AwareDatetime, BaseModel, Field, ValidationError

from debug_devices_mcp.bench_points import PointName, PointNameError, is_ground, point_name, text_key
from debug_devices_mcp.board.model import Board
from debug_devices_mcp.constants import defaults
from debug_devices_mcp.evidence import CaptureLog
from debug_devices_mcp.meter_frames import PREFIX_FACTORS
from debug_devices_mcp.multimeter import (
    MeterMode,
    MeterResult,
    MeterStatus,
    ModeSource,
    UnitFamily,
    is_overload,
    signature,
    signed_value,
    unit_parts,
)

logger = logging.getLogger(__name__)

STATE_VERSION = 1
# A residual voltage at or below this is safe for a resistance, continuity, or diode test (a board without power).
SAFE_RESIDUAL_VOLTS = 0.5
MAX_PHOTO_IDS = 1000
# The lock file next to the record, and how long a writer waits for it (the pattern of the monitor settings store).
LOCK_SUFFIX = ".lock"
LOCK_TIMEOUT = timedelta(seconds=5)
LOCK_RETRY = timedelta(milliseconds=10)
# The background retry of an unsafe reading that could not be saved (then the next write saves it).
FLUSH_DELAY = timedelta(seconds=5)
FLUSH_ATTEMPTS = 12
# The last save of kept unsafe readings at exit: the lock wait, and a little more.
EXIT_SAVE_TIMEOUT = LOCK_TIMEOUT + timedelta(seconds=1)
# A mode that the user confirmed on the dial is context for multimeter_read for this long.
USER_MODE_MAX_AGE = timedelta(minutes=10)
USER_SOURCE = "user"
READING_SOURCE = "reading"
# Step.completed_by values.
EVIDENCE = "evidence"
USER_REPORT = "user_report"
SKIPPED = "skipped"
MAX_CANDIDATES = 20
VOLTAGE_MODES = frozenset({MeterMode.DC_VOLTAGE, MeterMode.AC_VOLTAGE})
VOLT_SYMBOLS = {MeterMode.DC_VOLTAGE: "DC V", MeterMode.AC_VOLTAGE: "AC V"}
UNREADABLE_UNIT = "(unit not readable, counted as V)"
# A silicon diode or a MOSFET body diode drops less than this. With a user-confirmed diode mode, a higher value counts
# as a voltage for the gate (fail safe: an LED test then needs a new DC reading or a clear).
TYPICAL_DIODE_DROP_MAX = 1.0
# With an unreadable unit symbol, these modes count as volts for the safety gate (fail safe).
UNKNOWN_UNIT_VOLT_MODES = frozenset({MeterMode.DC_VOLTAGE, MeterMode.AC_VOLTAGE, MeterMode.OTHER})
# Only DC voltage shows the charge that a capacitor holds: an AC reading is not a residual-voltage check.
RESIDUAL_MODE = MeterMode.DC_VOLTAGE
AC_RESIDUAL_NOTE = (
    "residual check needs DC V: this AC reading does not count as the residual-voltage check (a capacitor holds a "
    "DC charge that AC V does not show)"
)
UNSAFE_AFTER_CONFIRMATION = (
    "an unsafe residual reading came after the isolation confirmation: the user checks that the charger and the "
    "battery or bench supply are off, then confirms the isolation again (a confirmation does not discharge a "
    "capacitor: each unsafe point still needs a newer safe DC reading)"
)
# The `point` of a bulk clearance (bench_state_update clear_all_residual_points).
ALL_POINTS = "all"
# The point of an unsafe reading without a point name (multimeter_read, or a refused name). Its key has the capture id.
UNKNOWN_POINT = "unknown point"
POWER_CHECK_TEXT = (
    "Power check: the user disconnects the charger and the battery or bench supply and confirms it; then measure the "
    "residual voltage with the meter in DC voltage mode."
)


class PowerState(StrEnum):
    UNKNOWN = "unknown"
    POWERED = "powered"
    ISOLATED = "isolated"


class StepKind(StrEnum):
    POWER_CHECK = "power_check"
    VOLTAGE = "voltage"
    RESISTANCE = "resistance"
    CONTINUITY = "continuity"
    DIODE = "diode"
    VISUAL = "visual"
    OTHER = "other"


# Steps that need a board without power.
UNPOWERED_KINDS = frozenset({StepKind.RESISTANCE, StepKind.CONTINUITY, StepKind.DIODE})
# The meter modes that complete a measurement step of each kind.
STEP_MODES: dict[StepKind, frozenset[MeterMode]] = {
    StepKind.VOLTAGE: VOLTAGE_MODES,
    StepKind.POWER_CHECK: VOLTAGE_MODES,
    StepKind.RESISTANCE: frozenset({MeterMode.RESISTANCE}),
    StepKind.CONTINUITY: frozenset({MeterMode.CONTINUITY, MeterMode.RESISTANCE}),
    StepKind.DIODE: frozenset({MeterMode.DIODE}),
}


# region: record


class Measurement(BaseModel):
    # The capture id of the multimeter_read image.
    source_id: str
    measured_at: AwareDatetime
    label: str
    mode: MeterMode
    value: float | None
    unit: str
    display_text: str
    overload: bool
    # Always "confirmed" here: other results cannot enter the record.
    confidence_state: MeterStatus
    step_id: str | None = None
    # The power state of the record when the measurement entered it, and the user's confirmation of it.
    power_state: PowerState = PowerState.UNKNOWN
    power_confirmed_by_user: bool = False
    # Set when the measurement was done before the safety gate was complete (the user's reason).
    done_before_gate: str | None = None


class ProbeContact(BaseModel):
    description: str
    # A current phone_snapshot capture id that shows the probes.
    photo_id: str
    recorded_at: AwareDatetime


class Step(BaseModel):
    step_id: str
    text: str
    kind: StepKind
    done: bool = False
    done_at: AwareDatetime | None = None
    # The capture id that completed the step (a measurement or a photo).
    evidence_id: str | None = None
    # How the step ended: "evidence" (a measurement or a photo), "user_report" (the user did it), or "skipped".
    completed_by: str | None = None
    reason: str | None = None


class PowerRecord(BaseModel):
    state: PowerState = PowerState.UNKNOWN
    # The user said that the charger and the battery or bench supply are off.
    user_confirmed_isolation: bool = False
    confirmed_at: AwareDatetime | None = None


class ResidualPoint(BaseModel):
    """The latest residual reading at one point, or the user's clearance of the point (it counts as safe)."""

    # The point key (bench_points.point_name): every spelling of one pin gives the same key.
    point: str
    # The name as the agent or the user wrote it.
    label: str
    at: AwareDatetime
    safe: bool
    # "5.10 V", or "cleared by the user".
    reading: str
    # The capture id of the reading; None when the user cleared the point.
    source_id: str | None = None
    user_reason: str | None = None


class ResidualClearance(BaseModel):
    """A user action: the user cleared one point (for example after a discharge with a resistor), or all points at
    once (`point` ALL_POINTS, the user's own words in `reason`, and the cleared points in `cleared_points`)."""

    point: str
    label: str
    reason: str
    cleared_at: AwareDatetime
    cleared_points: list[str] = Field(default_factory=list)


class MeterModeRecord(BaseModel):
    mode: MeterMode
    # "reading": from a confirmed multimeter_read; "user": the user confirmed the physical dial.
    source: str
    recorded_at: AwareDatetime


class BenchState(BaseModel):
    version: int = STATE_VERSION
    updated_at: AwareDatetime | None = None
    power: PowerRecord = Field(default_factory=PowerRecord)
    meter_mode: MeterModeRecord | None = None
    probe_contact: ProbeContact | None = None
    measurements: list[Measurement] = Field(default_factory=list)
    # Estimated part names (boardview candidates), kept apart from the measurements.
    part_candidates: list[str] = Field(default_factory=list)
    photo_ids: list[str] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    # The last probe short (the record went back to the power check).
    last_short_at: AwareDatetime | None = None
    # The latest residual reading of each point (DC V after an isolation confirmation, and an unsafe AC reading). A
    # new confirmation or a probe short does not clear a point: a confirmation does not discharge a capacitor.
    residual_points: list[ResidualPoint] = Field(default_factory=list)
    # The last unsafe residual reading: the gate needs an isolation confirmation that is newer.
    last_unsafe_at: AwareDatetime | None = None
    residual_clearances: list[ResidualClearance] = Field(default_factory=list)

    @property
    def next_step(self) -> Step | None:
        return next((step for step in self.steps if not step.done), None)


class GateReport(BaseModel):
    # True when a resistance, continuity, or diode step can start now.
    unpowered_tests_allowed: bool
    missing: list[str]


class BenchStateView(BaseModel):
    path: str | None
    state: BenchState
    next_step: Step | None
    gate: GateReport
    # Notes of this call: why a step stays open, an AC reading that does not count, or a point name without a board.
    notice: str | None = None


# endregion: record


def now() -> datetime:
    return datetime.now(UTC)


def residual_volts(reading: Measurement | MeterResult) -> float | None:
    """The reading in volts with its unit prefix (u, m, k, M), or None: an overload, not volts, or an unknown prefix."""
    family, prefix = unit_parts(reading.unit)
    factor = PREFIX_FACTORS.get(prefix)
    if reading.value is None or family is not UnitFamily.VOLTAGE or factor is None:
        return None
    return reading.value * factor


def is_safe(reading: Measurement | MeterResult) -> bool:
    volts = residual_volts(reading)
    return volts is not None and abs(volts) <= SAFE_RESIDUAL_VOLTS


def reading_text(reading: Measurement | MeterResult) -> str:
    return f"{reading.display_text} {reading.unit}".strip()


class LcdVoltage(BaseModel):
    volts: float
    # The LCD text and the unit, for the notes ("5.10 V").
    text: str
    # Why a diode reading counts as a voltage (for the notice), or None.
    note: str | None = None


class DiodeContext(BaseModel):
    """How much to trust a diode mode: from the LCD, or from the user's dial confirmation (which can hide DC V)."""

    max_diode_volts: float = defaults.MAX_DIODE_VOLTAGE
    # The mode is diode because the user confirmed it (multimeter.ModeSource.USER), not from the LCD.
    user_diode: bool = False
    # The voltage mode (DC V or AC V symbols) that the model itself read in the result or in any frame, or None.
    model_volt_mode: MeterMode | None = None


def lcd_voltage(
    display_text: str, unit: str, mode: MeterMode, max_diode_volts: float = defaults.MAX_DIODE_VOLTAGE
) -> LcdVoltage | None:
    """The number on the LCD in volts, for the safety gate (without the user's dial context: see `diode_voltage`)."""
    return diode_voltage(display_text, unit, mode, DiodeContext(max_diode_volts=max_diode_volts))


def diode_rule(volts: float, context: DiodeContext) -> str | None:
    """Why a diode reading counts as a voltage for the gate (fail safe), or None when it is a plain diode test."""
    if abs(volts) > context.max_diode_volts:
        return (
            f"a diode reading above the diode test voltage of {context.max_diode_volts:g} V counts as a voltage: the "
            "dial is probably on DC V"
        )
    if context.user_diode and context.model_volt_mode is not None:
        symbols = VOLT_SYMBOLS[context.model_volt_mode]
        return (
            f"the user confirmed diode mode, but the model read {symbols} symbols: it counts as a voltage (fail safe)"
        )
    if context.user_diode and abs(volts) > TYPICAL_DIODE_DROP_MAX:
        return (
            f"the user confirmed diode mode, but the value is above a typical diode drop ({TYPICAL_DIODE_DROP_MAX:g} "
            "V): it counts as a voltage (fail safe)"
        )
    return None


def diode_voltage(display_text: str, unit: str, mode: MeterMode, context: DiodeContext) -> LcdVoltage | None:
    """The number on the LCD in volts, for the safety gate. It needs no confirmed value (fail safe): the unit prefix
    counts, an unreadable unit in a voltage mode (or a mode that the model could not tell) counts as volts, and an
    overload is above every limit.

    A diode test shows the meter's own test voltage (a diode drop is about 0.6 V), so a diode reading counts only by
    `diode_rule`: above the diode test voltage, or, with a user-confirmed diode mode, when the model read DC V or the
    value is above a typical diode drop. None: not a voltage reading (another unit, a plain diode test, an open diode
    "OL", or no digits).
    """
    family, prefix = unit_parts(unit)
    if family is UnitFamily.VOLTAGE:
        factor = PREFIX_FACTORS.get(prefix, 1.0)
    elif family is UnitFamily.UNKNOWN and (mode in UNKNOWN_UNIT_VOLT_MODES or mode is MeterMode.DIODE):
        # An unreadable unit: the number counts as volts; a diode reading then goes through `diode_rule`.
        factor = 1.0
    else:
        return None
    text = f"{display_text} {unit}".strip() if family is UnitFamily.VOLTAGE else f"{display_text} {UNREADABLE_UNIT}"
    overload = is_overload(display_text)
    digits, point = signature(display_text)
    if mode is MeterMode.DIODE:
        if overload or not digits:
            return None
        volts = signed_value(display_text, digits, point) * factor
        note = diode_rule(volts, context)
        return LcdVoltage(volts=volts, text=text, note=note) if note is not None else None
    if overload:
        return LcdVoltage(volts=math.inf, text=text)
    if not digits:
        return None
    return LcdVoltage(volts=signed_value(display_text, digits, point) * factor, text=text)


def result_voltage(result: MeterResult, max_diode_volts: float = defaults.MAX_DIODE_VOLTAGE) -> LcdVoltage | None:
    """The highest voltage on the LCD of the result and of each frame, each with its own mode (one frame can show a
    misread point, or another mode). A diode mode that the user confirmed must not hide a DC reading."""
    model_modes = [result.model_mode, *(frame.model_mode for frame in result.frames)]
    context = DiodeContext(
        max_diode_volts=max_diode_volts,
        user_diode=result.mode_source is ModeSource.USER and result.mode is MeterMode.DIODE,
        model_volt_mode=next((mode for mode in model_modes if mode in VOLTAGE_MODES), None),
    )
    readings = [diode_voltage(result.display_text, result.unit, result.mode, context)]
    readings += [diode_voltage(frame.display_text, frame.unit, frame.mode, context) for frame in result.frames]
    found = [reading for reading in readings if reading is not None]
    return max(found, key=lambda reading: abs(reading.volts)) if found else None


def unknown_key(capture_id: str) -> str:
    return f"{UNKNOWN_POINT} {capture_id}"


def unsafe_text(reading: str, label: str) -> str:
    return (
        f"the residual voltage {reading} at {label!r} is not safe (limit {SAFE_RESIDUAL_VOLTS} V): stop, let the board "
        f"discharge, and measure {label!r} again in DC V, or the user clears this point with a reason "
        "(bench_state_update clear_residual_point); a safe reading at another point or a new confirmation does not "
        "clear it"
    )


def point_problem(point: ResidualPoint) -> str:
    if point.label != UNKNOWN_POINT:
        return unsafe_text(point.reading, point.label)
    return (
        f"the voltage {point.reading} of capture {point.source_id} is not safe (limit {SAFE_RESIDUAL_VOLTS} V) and has "
        "no point name: record that capture with its point name (bench_record_measurement), then measure that point "
        f"again in DC V; or the user clears it (clear_residual_point {point.point!r})"
    )


def note_unsafe(state: BenchState, at: datetime) -> None:
    state.last_unsafe_at = at if state.last_unsafe_at is None else max(state.last_unsafe_at, at)


def set_point(state: BenchState, point: ResidualPoint) -> bool:
    """The newer reading or clearance of a point replaces the older one; an older one never replaces a newer one (at
    the same time, the unsafe one stays). False when the point keeps its newer entry."""
    existing = next((item for item in state.residual_points if item.point == point.point), None)
    if existing is not None and (existing.at > point.at or (existing.at == point.at and not existing.safe)):
        return False
    state.residual_points = [*(item for item in state.residual_points if item.point != point.point), point]
    return True


def after_confirmation(state: BenchState, at: datetime) -> bool:
    power = state.power
    return power.user_confirmed_isolation and power.confirmed_at is not None and at >= power.confirmed_at


def gate(state: BenchState) -> GateReport:
    """What a resistance, continuity, or diode step needs, and what is missing.

    The gate opens only when the isolation confirmation is newer than the last unsafe voltage reading, every point
    whose latest reading was unsafe has a newer safe DC reading (or the user cleared it), and at least one safe DC
    reading exists after the confirmation.
    """
    missing = []
    power = state.power
    confirmed_at = power.confirmed_at if power.user_confirmed_isolation else None
    if power.state is not PowerState.ISOLATED:
        missing.append("the record does not show the power as isolated")
    if confirmed_at is None:
        missing.append("the user has not confirmed that the charger and the battery or bench supply are off")
    elif state.last_unsafe_at is not None and state.last_unsafe_at >= confirmed_at:
        missing.append(UNSAFE_AFTER_CONFIRMATION)
    missing.extend(point_problem(point) for point in state.residual_points if not point.safe)
    measured_after = confirmed_at is not None and any(
        point.safe and point.source_id is not None and point.at >= confirmed_at and not is_ground(point.point)
        for point in state.residual_points
    )
    if not measured_after:
        missing.append(
            "no safe residual-voltage measurement (multimeter_read in DC voltage mode) after the confirmation"
        )
    return GateReport(unpowered_tests_allowed=not missing, missing=missing)


class BenchStateStore:
    """Reads the file on each call (another MCP server can write it). Without a path: memory only (tests).

    Every writer changes the record through `update` (or `update_async`): one lock from load to save, so two writers
    (two tools, a tool and multimeter_read, or two MCP servers) never lose each other's change.

    An unsafe reading that cannot be saved (the lock wait timed out) is not lost: the store keeps it, every `load` of
    this server applies it (the gate stays closed here), a background retry saves it, and so does the next write.
    """

    def __init__(self, path: Path | None = None, max_diode_volts: float = defaults.MAX_DIODE_VOLTAGE) -> None:
        self.path = path
        # The diode test voltage of the meter (--max-diode-voltage): a diode reading above it counts as a voltage.
        self.max_diode_volts = max_diode_volts
        self._memory = BenchState()
        self._thread_lock = threading.Lock()
        self._unsaved: list[MeterResult] = []
        self._unsaved_lock = threading.Lock()
        self._flush_task: asyncio.Task[None] | None = None

    def update[T](self, change: Callable[[BenchState], T]) -> tuple[BenchState, T]:
        """Load, change, and save under the lock. A ResidualKeptError from `change` saves the record, then goes up
        (the reading changed the safety gate); any other error saves nothing. `change` must only change the record.
        The unsaved unsafe readings go into the file with this save."""
        with self._locked():
            state = self._read()
            flushed = self._apply_unsaved(state)
            try:
                value = change(state)
            except ResidualKeptError:
                self.save(state)
                self._forget_unsaved(flushed)
                raise
            self.save(state)
            self._forget_unsaved(flushed)
        return state, value

    # region: unsaved unsafe readings

    @property
    def unsaved(self) -> int:
        with self._unsaved_lock:
            return len(self._unsaved)

    def keep_unsaved(self, result: MeterResult) -> None:
        with self._unsaved_lock:
            self._unsaved.append(result)

    def _apply_unsaved(self, state: BenchState) -> list[MeterResult]:
        with self._unsaved_lock:
            unsaved = list(self._unsaved)
        # gate_event is idempotent for one result: the same point and time, and the unsafe entry wins a tie.
        for result in unsaved:
            gate_event(state, result, None, None, self.max_diode_volts)
        return unsaved

    def _forget_unsaved(self, saved: list[MeterResult]) -> None:
        with self._unsaved_lock:
            self._unsaved = [result for result in self._unsaved if all(result is not item for item in saved)]

    def flush_later(self) -> None:
        """Start one background retry that saves the unsaved readings (a few tries, then the next write does it)."""
        if self._flush_task is None or self._flush_task.done():
            self._flush_task = asyncio.get_running_loop().create_task(self._flush())

    async def save_unsaved_at_exit(self) -> None:
        """One more try at exit (a stop, Ctrl-C, SIGTERM, or a reload): a kept unsafe reading is in memory only.

        The MCP shutdown cancels the lifespan, so the save runs shielded, with a limit. A busy lock or a file error (a
        full disk, a read-only folder) goes to the log. A shield does not stop a second interrupt (a native cancel,
        or the runner's KeyboardInterrupt): then the log says that the save was cut, and the interrupt goes on (N65).
        """
        if self._flush_task is not None:
            self._flush_task.cancel()
        if not self.unsaved:
            return
        with anyio.move_on_after(EXIT_SAVE_TIMEOUT.total_seconds(), shield=True) as scope:
            try:
                await self.update_async(lambda _state: None)
            except (ToolError, OSError) as exc:
                logger.warning("%s unsafe readings are not in the bench state file at exit: %s", self.unsaved, exc)
            except BaseException as exc:
                logger.warning(
                    "%s unsafe readings may not be in the bench state file: a second interrupt cut the exit save (%s)",
                    self.unsaved,
                    type(exc).__name__,
                )
                raise
        if scope.cancelled_caught:
            logger.warning(
                "%s unsafe readings are not in the bench state file at exit: the save did not end in %g s",
                self.unsaved,
                EXIT_SAVE_TIMEOUT.total_seconds(),
            )

    async def _flush(self) -> None:
        for _ in range(FLUSH_ATTEMPTS):
            await asyncio.sleep(FLUSH_DELAY.total_seconds())
            if not self.unsaved:
                return
            with contextlib.suppress(ToolError):
                await self.update_async(lambda _state: None)
                return

    # endregion: unsaved unsafe readings

    async def update_async[T](self, change: Callable[[BenchState], T]) -> tuple[BenchState, T]:
        """`update` for code on the event loop: the lock wait and the file work run in a worker thread."""
        return await asyncio.to_thread(self.update, change)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with self._thread_lock:
            if self.path is None:
                yield
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            lock_path = self.path.with_name(f"{self.path.name}{LOCK_SUFFIX}")
            with lock_path.open("a") as handle:
                deadline = time.monotonic() + LOCK_TIMEOUT.total_seconds()
                while True:
                    try:
                        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise ToolError(f"the bench state is locked by another writer: {lock_path}") from None
                        time.sleep(LOCK_RETRY.total_seconds())
                try:
                    yield
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def load(self) -> BenchState:
        """The record, with the unsafe readings that this server could not save yet."""
        state = self._read()
        self._apply_unsaved(state)
        return state

    def _read(self) -> BenchState:
        if self.path is None:
            return self._memory.model_copy(deep=True)
        try:
            return BenchState.model_validate_json(self.path.read_bytes())
        except FileNotFoundError:
            return BenchState()
        except (OSError, ValidationError, json.JSONDecodeError) as exc:
            raise ToolError(f"cannot read the bench state {self.path}: {exc}. Fix or delete the file.") from exc

    def save(self, state: BenchState) -> BenchState:
        state.updated_at = now()
        if self.path is None:
            self._memory = state.model_copy(deep=True)
            return state
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.", suffix=".tmp")
        try:
            with os.fdopen(handle, "w") as file:
                file.write(state.model_dump_json(indent=2) + "\n")
            Path(temporary).replace(self.path)
        except BaseException:
            with contextlib.suppress(FileNotFoundError):
                Path(temporary).unlink()
            raise
        return state

    def view(self, state: BenchState | None = None, notice: str | None = None) -> BenchStateView:
        state = state if state is not None else self.load()
        if unsaved := self.unsaved:
            note = (
                "unsafe readings that are not saved in the bench state file yet (it was locked): "
                f"{unsaved}; they count in this server, and the next bench-state write saves them"
            )
            notice = f"{notice}; {note}" if notice else note
        return BenchStateView(
            path=str(self.path) if self.path else None,
            state=state,
            next_step=state.next_step,
            gate=gate(state),
            notice=notice,
        )


# region: changes


def find_step(state: BenchState, step_id: str) -> Step:
    for step in state.steps:
        if step.step_id == step_id:
            return step
    raise ToolError(f"no step {step_id!r} in the bench state")


def power_check_problem(state: BenchState, measurement: Measurement) -> str | None:
    """A power check completes only with a safe DC residual reading after the user's isolation confirmation."""
    if measurement.mode is not RESIDUAL_MODE:
        return AC_RESIDUAL_NOTE
    if not after_confirmation(state, measurement.measured_at):
        return (
            "a power check needs a DC voltage reading after the user confirmed the isolation (bench_state_update "
            "power isolated, user_confirmed_isolation true)"
        )
    if not is_safe(measurement):
        return unsafe_text(reading_text(measurement), measurement.label)
    missing = gate(state).missing
    return "; ".join(missing) if missing else None


class ResidualKeptError(ToolError):
    """The measurement was refused, but the reading changed the safety gate: the tool saves the record, then refuses."""


def step_refusal(
    state: BenchState, result: MeterResult, step_id: str | None, step: Step | None, done_before_gate: str | None
) -> str | None:
    """Why the measurement cannot complete this step, or None. Decided before the reading changes the gate."""
    if step_id and step is None:
        return f"no step {step_id!r} in the bench state"
    if step is None:
        return None
    if result.mode not in STEP_MODES.get(step.kind, frozenset()):
        return f"step {step.step_id} is a {step.kind} step, but this measurement is {result.mode}"
    if step.kind in UNPOWERED_KINDS and not gate(state).unpowered_tests_allowed and not done_before_gate:
        return (
            "this step needs the safety gate first: "
            + "; ".join(gate(state).missing)
            + ". If the user already did this test safely (power off), pass done_before_gate with their reason."
        )
    return None


class GateEvent(BaseModel):
    """What one meter reading did to the safety gate."""

    notes: list[str] = Field(default_factory=list)
    # The reading changed the residual points (the tool saves the record, also when it refuses).
    changed: bool = False
    # A safe, confirmed DC reading at a named point: it can open the gate and complete the power checks.
    counted: bool = False
    # Why the point name was refused (bench_points.py), or None.
    name_error: str | None = None


def gate_event(
    state: BenchState,
    result: MeterResult,
    label: str | None,
    board: Board | None,
    max_diode_volts: float = defaults.MAX_DIODE_VOLTAGE,
) -> GateEvent:
    """Apply one reading to the safety gate.

    Every voltage reading above the safe limit closes the gate: confirmed or not, in any power state (a misread only
    costs a new reading). Without a valid point name it goes to an unknown point of its capture id. A safe reading
    counts only when it is confirmed, DC, and at a named point; an unconfirmed safe reading never opens the gate.
    """
    voltage = result_voltage(result, max_diode_volts)
    if voltage is None or result.capture_id is None or result.captured_at is None:
        return GateEvent()
    name, name_error = None, None
    if label is not None:
        try:
            name = point_name(label, board)
        except PointNameError as exc:
            name_error = str(exc)
    if abs(voltage.volts) > SAFE_RESIDUAL_VOLTS:
        return unsafe_event(state, result, voltage, label if name is not None else None, name, name_error)
    notes = (
        [AC_RESIDUAL_NOTE]
        if result.mode is MeterMode.AC_VOLTAGE and after_confirmation(state, result.captured_at)
        else []
    )
    confirmed_dc = (
        result.status is MeterStatus.CONFIRMED
        and result.mode is RESIDUAL_MODE
        and result.unit_family is UnitFamily.VOLTAGE
    )
    if not confirmed_dc:
        return GateEvent(notes=notes)
    if name is None or label is None:
        return GateEvent(notes=notes, name_error=name_error)
    point = ResidualPoint(
        point=name.key,
        label=label,
        at=result.captured_at,
        safe=True,
        reading=voltage.text,
        source_id=result.capture_id,
    )
    changed = set_point(state, point)
    return GateEvent(notes=[*notes, *filter(None, [name.warning])], changed=changed, counted=changed)


def unsafe_event(
    state: BenchState,
    result: MeterResult,
    voltage: LcdVoltage,
    label: str | None,
    name: PointName | None,
    name_error: str | None,
) -> GateEvent:
    """An unsafe reading: the named point (the unknown point of its capture moves there), or the unknown point."""
    if result.capture_id is None or result.captured_at is None:
        return GateEvent()
    unknown = unknown_key(result.capture_id)
    if name is None or label is None:
        point = ResidualPoint(
            point=unknown, label=UNKNOWN_POINT, at=result.captured_at, safe=False, reading=voltage.text
        )
        where = f"an unknown point ({unknown}) until it has a point name"
    else:
        state.residual_points = [item for item in state.residual_points if item.point != unknown]
        point = ResidualPoint(point=name.key, label=label, at=result.captured_at, safe=False, reading=voltage.text)
        where = repr(label)
    point.source_id = result.capture_id
    if set_point(state, point):
        reopen_power_checks(state, f"{voltage.text} at {where}")
    note_unsafe(state, result.captured_at)
    notes = [
        f"{voltage.text} is above the safe residual limit of {SAFE_RESIDUAL_VOLTS} V (this result is "
        f"{result.status}): the bench safety gate is closed at {where}; it needs a newer safe DC reading there, or "
        "the user clears it",
        *filter(None, [voltage.note]),
    ]
    if name is not None and name.warning is not None:
        notes.append(name.warning)
    return GateEvent(notes=notes, changed=True, name_error=name_error)


def point_key_of(label: str, board: Board | None) -> str:
    try:
        return point_name(label, board).key
    except PointNameError:
        return text_key(label)


def label_conflict(state: BenchState, capture_id: str, label: str, board: Board | None) -> str | None:
    """One reading belongs to one point: a capture id recorded for one label cannot get another label."""
    key = point_key_of(label, board)
    earlier = [item.label for item in state.measurements if item.source_id == capture_id]
    earlier += [
        item.label for item in state.residual_points if item.source_id == capture_id and item.label != UNKNOWN_POINT
    ]
    other = next((name for name in earlier if point_key_of(name, board) != key), None)
    if other is None:
        return None
    return (
        f"capture {capture_id} is already recorded for {other!r}: one reading belongs to one point. Read the meter "
        f"again at {label!r}."
    )


def refuse_unconfirmed(
    state: BenchState, result: MeterResult, label: str, board: Board | None, max_diode_volts: float
) -> None:
    """A result that is not confirmed is no measurement. An unsafe voltage in it still closes the gate (saved)."""
    text = (
        f"this meter result is {result.status}, not confirmed: it cannot enter the confirmed measurements. "
        f"{result.request or ''}"
    ).strip()
    event = gate_event(state, result, label, board, max_diode_volts)
    if event.changed:
        extra = [*event.notes, *filter(None, [event.name_error])]
        raise ResidualKeptError(f"{text} " + " ".join(extra))
    raise ToolError(text)


def record_measurement(
    state: BenchState,
    result: MeterResult,
    label: str,
    step_id: str | None,
    done_before_gate: str | None = None,
    board: Board | None = None,
    max_diode_volts: float = defaults.MAX_DIODE_VOLTAGE,
) -> tuple[Measurement, str | None]:
    """Add a confirmed meter result. Other results are refused: they are not measurements.

    `done_before_gate` (the user's reason) records a resistance, continuity, or diode measurement that was done
    before the gate was complete (for example with the power off, before the residual-voltage check). The flag stays
    on the measurement; bench_begin_step stays strict for new steps.

    Every voltage reading goes to the safety gate (gate_event) and needs a point name that names one point
    (bench_points.py, checked against `board` when one is open). An unsafe voltage closes the gate, also when the
    result is not confirmed or the name is refused (ResidualKeptError: the tool saves the record, then refuses).

    The step result is decided before the reading changes the gate. When the step is refused, a reading that changed
    the gate stays in the record without the step, and the open power checks still follow the gate.
    The second value is the notice: why a step stays open, an AC reading that does not count, an unsafe reading, or a
    point name without a board. The same capture id enters once, and only for one point.
    """
    if result.capture_id is None or result.captured_at is None:
        raise ToolError("this meter result has no capture id: read the meter again")
    conflict = label_conflict(state, result.capture_id, label, board)
    if conflict is not None:
        raise ToolError(conflict)
    if result.status is not MeterStatus.CONFIRMED:
        refuse_unconfirmed(state, result, label, board, max_diode_volts)
    step = next((item for item in state.steps if item.step_id == step_id), None) if step_id else None
    refusal = step_refusal(state, result, step_id, step, done_before_gate)
    event = gate_event(state, result, label, board, max_diode_volts)
    if event.name_error is not None:
        if event.changed:
            raise ResidualKeptError(f"{event.name_error}. " + " ".join(event.notes))
        raise ToolError(event.name_error)
    measurement, new = add_measurement(state, result, label, None if refusal else step_id, done_before_gate)
    # A reading does not replace a recent mode that the user confirmed on the dial: multimeter_read uses it.
    if not is_recent_user_mode(state.meter_mode):
        state.meter_mode = MeterModeRecord(mode=result.mode, source=READING_SOURCE, recorded_at=result.captured_at)
    # The open power checks follow the gate, also when the step is refused.
    if event.counted and gate(state).unpowered_tests_allowed:
        complete_power_checks(state, measurement.source_id)
    if refusal is not None:
        if event.changed:
            raise ResidualKeptError(
                f"{refusal}. The voltage reading is recorded without the step, because it counts for the safety gate. "
                "To attach it to a step that fits this reading (a voltage or power-check step), record the same "
                "capture_id again with that step_id and the same label."
            )
        if new:
            state.measurements.remove(measurement)
        raise ToolError(refusal)
    notes = event.notes if step is None else finish_step(state, step, measurement, done_before_gate, event.notes)
    return measurement, "; ".join(notes) or None


def finish_step(
    state: BenchState, step: Step, measurement: Measurement, done_before_gate: str | None, notes: list[str]
) -> list[str]:
    """Complete the step with the measurement, or leave a power check open. The notes of the call."""
    problem = power_check_problem(state, measurement) if step.kind is StepKind.POWER_CHECK else None
    if problem is not None:
        return [f"step {step.step_id} stays open: {problem}", *(note for note in notes if note not in problem)]
    step.done, step.done_at, step.evidence_id = True, now(), measurement.source_id
    step.completed_by = EVIDENCE
    step.reason = done_before_gate
    measurement.step_id = step.step_id
    return notes


async def note_meter_reading(store: BenchStateStore, result: MeterResult) -> str | None:
    """multimeter_read and bench_measure: an unsafe voltage closes the safety gate at once (an unknown point until the
    agent records the capture with its point name), under the store lock. The notice, or None when the gate did not
    change."""
    voltage = result_voltage(result, store.max_diode_volts)
    if voltage is None or abs(voltage.volts) <= SAFE_RESIDUAL_VOLTS:
        return None
    try:
        _, event = await store.update_async(
            partial(gate_event, result=result, label=None, board=None, max_diode_volts=store.max_diode_volts)
        )
    except ToolError as exc:
        store.keep_unsaved(result)
        store.flush_later()
        return (
            f"{voltage.text} is above the safe residual limit of {SAFE_RESIDUAL_VOLTS} V, but the bench state is NOT "
            f"SAVED YET ({exc}): this server keeps the reading, the gate stays closed here, and a retry or the next "
            "bench-state write saves it"
        )
    return "; ".join(event.notes) or None


def clear_point(state: BenchState, label: str, reason: str | None, board: Board | None) -> None:
    """The user cleared one residual point (for example after a discharge with a resistor): it counts as safe."""
    if not reason or not reason.strip():
        raise ToolError(
            "clearing a residual point needs `clear_residual_reason`: what the user did (for example 'discharged C12 "
            "with a resistor')"
        )
    keys = {text_key(label)}
    with contextlib.suppress(PointNameError):
        keys.add(point_name(label, board).key)
    point = next((item for item in state.residual_points if item.point in keys), None)
    if point is None:
        names = ", ".join(repr(item.point) for item in state.residual_points) or "none"
        raise ToolError(f"no residual point {label!r} in the bench state (points: {names})")
    cleared_at = now()
    state.residual_clearances.append(
        ResidualClearance(point=point.point, label=label, reason=reason.strip(), cleared_at=cleared_at)
    )
    set_point(
        state,
        ResidualPoint(
            point=point.point,
            label=point.label,
            at=cleared_at,
            safe=True,
            reading="cleared by the user",
            user_reason=reason.strip(),
        ),
    )


def clear_all_points(state: BenchState, user_words: str | None) -> str:
    """The user cleared all residual points at once, in their own words (stored as given). The notice.

    Only after a safe DC reading (not a ground net) that comes after the latest isolation confirmation: the newest
    such reading is the anchor. A point whose unsafe reading is newer than the anchor stays unsafe.
    """
    if user_words is None or not user_words.strip():
        raise ToolError(
            "clearing all residual points needs `user_words`: the user's own sentence, quoted exactly (never write it "
            "yourself)"
        )
    power = state.power
    confirmed_at = power.confirmed_at if power.user_confirmed_isolation else None
    if confirmed_at is None:
        raise ToolError(
            "cannot clear all residual points: the user has not confirmed the isolation (bench_state_update power "
            "isolated, user_confirmed_isolation true)"
        )
    safe_after = [
        point
        for point in state.residual_points
        if point.safe and point.source_id is not None and point.at >= confirmed_at and not is_ground(point.point)
    ]
    if not safe_after:
        raise ToolError(
            "cannot clear all residual points: no safe DC reading after the latest isolation confirmation. Measure one "
            "supply point in DC V first (bench_record_measurement with its point name)"
        )
    anchor = max(safe_after, key=lambda point: point.at)
    unsafe = [point for point in state.residual_points if not point.safe]
    cleared = [point for point in unsafe if point.at <= anchor.at]
    kept = [point for point in unsafe if point.at > anchor.at]
    kept_text = ", ".join(repr(point.label) for point in kept)
    if not cleared:
        raise ToolError(
            "cannot clear all residual points: "
            + (f"every unsafe point ({kept_text}) is newer than" if kept else "no unsafe point is older than")
            + f" the safe reading at {anchor.label!r}; measure those points again"
        )
    cleared_at = now()
    # The clearance covers the readings up to the anchor, so it has the anchor's time: a kept unsafe reading (newer
    # than the anchor) still wins when its capture gets this point's name later. A clearance is no reading: it never
    # counts as the safe reading after the confirmation.
    update = {"at": anchor.at, "safe": True, "reading": "cleared by the user", "source_id": None}
    cleared = [
        point for point in cleared if set_point(state, point.model_copy(update={**update, "user_reason": user_words}))
    ]
    state.residual_clearances.append(
        ResidualClearance(
            point=ALL_POINTS,
            label="all residual points",
            reason=user_words,
            cleared_at=cleared_at,
            cleared_points=[point.point for point in cleared],
        )
    )
    notice = f"cleared {len(cleared)} residual points (older than the safe reading at {anchor.label!r})"
    return f"{notice}; kept {len(kept)} with a newer unsafe reading: {kept_text}" if kept else notice


def add_measurement(
    state: BenchState, result: MeterResult, label: str, step_id: str | None, done_before_gate: str | None
) -> tuple[Measurement, bool]:
    """The measurement of this capture id: the one in the record (a second call), or a new one. True when new."""
    existing = next((item for item in state.measurements if item.source_id == result.capture_id), None)
    if existing is not None:
        return existing, False
    if result.capture_id is None or result.captured_at is None:
        raise ToolError("a measurement needs the capture id and the time of its multimeter_read")
    measurement = Measurement(
        source_id=result.capture_id,
        measured_at=result.captured_at,
        label=label,
        mode=result.mode,
        value=result.value,
        unit=result.unit,
        display_text=result.display_text,
        overload=result.overload,
        confidence_state=result.status,
        step_id=step_id,
        power_state=state.power.state,
        power_confirmed_by_user=state.power.user_confirmed_isolation,
        done_before_gate=done_before_gate,
    )
    state.measurements.append(measurement)
    return measurement, True


def reopen_power_checks(state: BenchState, reading: str) -> None:
    """A new unsafe point closes the gate: a completed power check is open again, so the next step is the check."""
    for check in state.steps:
        if check.kind is StepKind.POWER_CHECK and check.done:
            check.done, check.done_at, check.evidence_id, check.completed_by = False, None, None, None
            check.reason = f"reopened: the unsafe reading {reading} closed the safety gate"


def complete_power_checks(state: BenchState, evidence_id: str) -> None:
    for check in state.steps:
        if check.kind is StepKind.POWER_CHECK and not check.done:
            check.done, check.done_at, check.evidence_id = True, now(), evidence_id
            check.completed_by, check.reason = EVIDENCE, None


def change_power(state: BenchState, power: PowerState | None, user_confirmed_isolation: bool | None) -> None:
    """A new isolation needs the user's confirmation. No power change clears a residual point."""
    if power is PowerState.ISOLATED:
        if not user_confirmed_isolation:
            raise ToolError(
                "tell the user to disconnect the charger and the battery or bench supply, wait for their "
                "confirmation, then set power isolated with user_confirmed_isolation true"
            )
        state.power = PowerRecord(state=PowerState.ISOLATED, user_confirmed_isolation=True, confirmed_at=now())
    elif power is not None:
        state.power = PowerRecord(state=power)


def change_step(
    state: BenchState, complete_step: str | None, skip_step: str | None, reason: str | None, photo_id: str | None
) -> None:
    """Complete a step with a photo (visual), or with the user's report (a reason), or skip it (a reason)."""
    if skip_step is not None:
        if not reason:
            raise ToolError("skipping a step needs `step_reason`")
        step = find_step(state, skip_step)
        step.done, step.done_at, step.completed_by, step.reason = True, now(), SKIPPED, reason
    if complete_step is None:
        return
    step = find_step(state, complete_step)
    if reason and photo_id is None:
        # The user did the step (for example a measurement before the gate), and no evidence can be attached.
        step.done, step.done_at, step.completed_by, step.reason = True, now(), USER_REPORT, reason
        return
    if step.kind in STEP_MODES:
        raise ToolError(
            f"a {step.kind} step completes with bench_record_measurement (a confirmed reading), or with `step_reason` "
            "when the user reports that they did it"
        )
    if photo_id is None:
        raise ToolError("a visual step needs `photo_id` (the current phone_snapshot that shows it), or `step_reason`")
    step.done, step.done_at, step.evidence_id, step.completed_by = True, now(), photo_id, EVIDENCE


def add_photo(store: BenchStateStore, capture_id: str) -> None:
    """Every phone_snapshot goes into the record (under the store lock; call it in a worker thread). A record that
    cannot be read or locked is left as it is."""

    def change(state: BenchState) -> None:
        if capture_id not in state.photo_ids:
            state.photo_ids = [*state.photo_ids, capture_id][-MAX_PHOTO_IDS:]

    with contextlib.suppress(ToolError):
        store.update(change)


def probe_short(state: BenchState) -> None:
    """After a probe short: stop, and check the power state again before any other step."""
    state.power = PowerRecord()
    state.probe_contact = None
    state.last_short_at = now()
    check = Step(step_id=str(uuid.uuid7()), text=POWER_CHECK_TEXT, kind=StepKind.POWER_CHECK)
    first_open = next((index for index, step in enumerate(state.steps) if not step.done), len(state.steps))
    state.steps.insert(first_open, check)


# endregion: changes


class NewStep(BaseModel):
    text: str
    kind: StepKind = StepKind.OTHER


@dataclass(frozen=True)
class StateUpdate:
    """The fields of one bench_state_update call."""

    power: PowerState | None = None
    user_confirmed_isolation: bool | None = None
    meter_mode_confirmed_by_user: MeterMode | None = None
    probe_contact: str | None = None
    photo_id: str | None = None
    part_candidates: list[str] | None = None
    add_steps: list[NewStep] | None = None
    complete_step: str | None = None
    skip_step: str | None = None
    step_reason: str | None = None
    clear_residual_point: str | None = None
    clear_residual_reason: str | None = None
    clear_all_residual_points: bool = False
    user_words: str | None = None


def apply_update(state: BenchState, update: StateUpdate, board: Board | None) -> str | None:
    """Apply a bench_state_update call to the record (under the store lock). The notice of a bulk clear, or None."""
    if update.photo_id is not None and update.photo_id not in state.photo_ids:
        state.photo_ids = [*state.photo_ids, update.photo_id][-MAX_PHOTO_IDS:]
    change_power(state, update.power, update.user_confirmed_isolation)
    if update.meter_mode_confirmed_by_user is not None:
        state.meter_mode = MeterModeRecord(
            mode=update.meter_mode_confirmed_by_user, source=USER_SOURCE, recorded_at=now()
        )
    if update.probe_contact is not None:
        if update.photo_id is None:
            raise ToolError("a probe contact needs `photo_id`: a current phone_snapshot that shows the probes")
        state.probe_contact = ProbeContact(
            description=update.probe_contact, photo_id=update.photo_id, recorded_at=now()
        )
    if update.part_candidates is not None:
        state.part_candidates = list(dict.fromkeys(update.part_candidates))[:MAX_CANDIDATES]
    for new in update.add_steps or []:
        state.steps.append(Step(step_id=str(uuid.uuid7()), text=new.text, kind=new.kind))
    change_step(state, update.complete_step, update.skip_step, update.step_reason, update.photo_id)
    if update.clear_residual_point is not None:
        clear_point(state, update.clear_residual_point, update.clear_residual_reason, board)
    return clear_all_points(state, update.user_words) if update.clear_all_residual_points else None


type OpenBoard = Callable[[], Awaitable[Board | None]]


async def no_board() -> Board | None:
    return None


def register_bench_state_tools(
    server: MCPServer, store: BenchStateStore, captures: CaptureLog, open_board: OpenBoard = no_board
) -> None:
    """`open_board` gives the open board (after a server restart, the board of the last session), or None."""

    @server.tool()
    async def bench_state() -> BenchStateView:
        """The local bench record: power state, meter mode, probe contact, confirmed measurements, part candidates,
        photo ids, the steps, the next uncompleted step, and the safety gate for resistance/continuity/diode steps."""
        return store.view()

    @server.tool()
    async def bench_state_update(
        power: PowerState | None = None,
        user_confirmed_isolation: bool | None = None,
        meter_mode_confirmed_by_user: MeterMode | None = None,
        probe_contact: str | None = None,
        photo_id: str | None = None,
        part_candidates: list[str] | None = None,
        add_steps: list[NewStep] | None = None,
        complete_step: str | None = None,
        skip_step: str | None = None,
        step_reason: str | None = None,
        clear_residual_point: str | None = None,
        clear_residual_reason: str | None = None,
        clear_all_residual_points: bool = False,
        user_words: str | None = None,
    ) -> BenchStateView:
        """Update the bench record. Every field is optional.

        `power` "isolated" needs `user_confirmed_isolation: true` (the user said that the charger and the battery or
        bench supply are off); "powered" or "unknown" clears the isolation. Neither clears an unsafe residual point.
        `clear_residual_point` (a point name) with `clear_residual_reason` records that the user made that one point
        safe (for example "capacitor discharged with a resistor"): it counts as a safe reading for that point. Use it
        only when the user says so.
        `clear_all_residual_points: true` with `user_words` clears all residual points at once, when the user says
        that the board is safe. `user_words` must quote the user's own sentence exactly; never invent, shorten, or
        paraphrase it. It is stored as given, with the time. It needs a safe DC reading (not a ground net) after the
        latest isolation confirmation, and it never clears a point whose unsafe reading is newer than that reading;
        otherwise it refuses and says why. `notice` lists what it cleared and what it kept.
        `probe_contact` (where the probes touch) and `complete_step` (a visual step) need `photo_id`: a current
        phone_snapshot capture id that shows it. Measurement steps complete through bench_record_measurement.
        `part_candidates` are boardview estimates, kept apart from the measurements.
        `skip_step` (with `step_reason`) skips a step. `complete_step` with `step_reason` and no `photo_id` completes
        a step from the user's report (for example a test that they did themselves), so the next step moves on.
        Every phone_snapshot goes into `photo_ids` by itself.
        """
        if photo_id is not None:
            captures.require_current_photo(photo_id)
        update = StateUpdate(
            power=power,
            user_confirmed_isolation=user_confirmed_isolation,
            meter_mode_confirmed_by_user=meter_mode_confirmed_by_user,
            probe_contact=probe_contact,
            photo_id=photo_id,
            part_candidates=part_candidates,
            add_steps=add_steps,
            complete_step=complete_step,
            skip_step=skip_step,
            step_reason=step_reason,
            clear_residual_point=clear_residual_point,
            clear_residual_reason=clear_residual_reason,
            clear_all_residual_points=clear_all_residual_points,
            user_words=user_words,
        )
        # The board check runs before the lock: the lock holds only the load, the change, and the save.
        board = await open_board() if clear_residual_point is not None else None
        state, notice = await store.update_async(partial(apply_update, update=update, board=board))
        return store.view(state, notice)

    @server.tool()
    async def bench_record_measurement(
        capture_id: str, label: str, step_id: str | None = None, done_before_gate: str | None = None
    ) -> BenchStateView:
        """Put a multimeter_read result (by its capture_id) into the confirmed measurements.

        Only a "confirmed" result can enter; an uncertain, disputed, or unreadable one is refused. With `step_id`,
        the measurement completes that step (its kind must fit the meter mode; resistance, continuity, and diode
        steps need the safety gate). Each measurement keeps the power state and the user's confirmation.
        Every voltage reading goes to the safety gate, and its `label` names one point: a part pin ("C12.1", "C12 pin
        1") or a net ("PP3V3_S5"), checked against the open board. A generic name ("residual", "test", "point") and a
        ground net (GND, AGND, VSS, or a pin on one) are refused. A voltage above 0.5 V closes the gate at that point:
        confirmed or not, with the power on or off, also when the name is refused (then at an unknown point of the
        capture; record the capture again with its point name). Each point keeps its latest reading (an older reading
        never replaces a newer one). An unsafe point blocks the gate until a newer safe DC reading at the same point,
        or until the user clears it (bench_state_update clear_residual_point); a new isolation confirmation does not
        clear it, and after an unsafe reading the gate also needs a newer confirmation. Only a confirmed DC reading
        can be the safe reading after the confirmation; an AC reading does not count ("residual check needs DC V").
        A power-check step completes only with a safe DC residual reading after the user's confirmation; otherwise
        `notice` tells why it stays open. When the step is refused, a voltage reading that changed the gate stays in
        the record without the step; to attach it, record the same capture_id again with the step and the same label.
        One capture_id enters once and only for one point.
        `done_before_gate`: the user's reason when a resistance, continuity, or diode test was already done before the
        gate was complete (for example with the power off, before the residual check); the measurement keeps it.
        """
        result = captures.meter_results.get(capture_id)
        if result is None:
            raise ToolError(f"no multimeter_read result with capture id {capture_id} in this server session")
        # The board check runs before the lock: the lock holds only the load, the change, and the save.
        board = await open_board()

        def change(state: BenchState) -> str | None:
            _, notice = record_measurement(
                state, result, label, step_id, done_before_gate, board, store.max_diode_volts
            )
            return notice

        state, notice = await store.update_async(change)
        return store.view(state, notice)

    @server.tool()
    async def bench_begin_step(step_id: str) -> BenchStateView:
        """Check the safety gate before a step. Call it before every resistance, continuity, or diode step.

        Those steps need: the power isolated, the user's confirmation, and a safe residual voltage measured after
        the confirmation (multimeter_read in DC voltage mode, then bench_record_measurement). Otherwise this tool
        refuses and lists what is missing: do not start the test.
        """
        state = store.load()
        step = find_step(state, step_id)
        if step.done:
            raise ToolError(f"step {step_id} is already complete")
        report = gate(state)
        if step.kind in UNPOWERED_KINDS and not report.unpowered_tests_allowed:
            raise ToolError(f"do not start this {step.kind} step: " + "; ".join(report.missing))
        return store.view(state)

    @server.tool()
    async def bench_probe_short() -> BenchStateView:
        """Record a probe short (the probes slipped or touched two points). Stop the test.

        The record goes back to the power check: the power state is unknown, the isolation confirmation and the
        residual-voltage check are cleared, and a power check step comes next.
        """
        state, _ = await store.update_async(probe_short)
        return store.view(state)


def is_recent_user_mode(record: MeterModeRecord | None, max_age: timedelta = USER_MODE_MAX_AGE) -> bool:
    return record is not None and record.source == USER_SOURCE and now() - record.recorded_at <= max_age


def recent_user_mode(store: BenchStateStore, max_age: timedelta = USER_MODE_MAX_AGE) -> MeterMode | None:
    """The meter mode that the user confirmed on the dial, if it is recent enough to trust as context."""
    try:
        record = store.load().meter_mode
    except ToolError:
        return None
    return record.mode if record is not None and is_recent_user_mode(record, max_age) else None


def current_step_text(store: BenchStateStore) -> str | None:
    """For bench_instructions: the next uncompleted step, or None."""
    try:
        step = store.load().next_step
    except ToolError:
        return None
    return f"{step.text} ({step.kind}, step_id {step.step_id})" if step else None
