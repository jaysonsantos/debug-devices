"""The bench state: one small local session record (bench-state.json next to instructions.md, git-ignored).

It holds the power state, the meter mode, the probe contact, the confirmed measurements, the part candidates, the
photo ids, and the steps. Only a "confirmed" multimeter_read result can enter the measurements. A safety gate
guards resistance, continuity, and diode steps: power isolated, confirmed by the user, and a safe DC residual voltage
measured after that confirmation. A probe short sends the record back to the power check.

The record holds only what the agent passes (a few part names), never data copied from a board file.
"""

import contextlib
import json
import os
import tempfile
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import AwareDatetime, BaseModel, Field, ValidationError

from debug_devices_mcp.bench_points import PointName, PointNameError, point_name, text_key
from debug_devices_mcp.board.model import Board
from debug_devices_mcp.evidence import CaptureLog
from debug_devices_mcp.meter_frames import PREFIX_FACTORS
from debug_devices_mcp.multimeter import MeterMode, MeterResult, MeterStatus, UnitFamily, unit_parts

STATE_VERSION = 1
# A residual voltage at or below this is safe for a resistance, continuity, or diode test (a board without power).
SAFE_RESIDUAL_VOLTS = 0.5
MAX_PHOTO_IDS = 1000
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
    """A user action: the user cleared one point (for example after a discharge with a resistor)."""

    point: str
    label: str
    reason: str
    cleared_at: AwareDatetime


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


def unsafe_text(reading: str, label: str) -> str:
    return (
        f"the residual voltage {reading} at {label!r} is not safe (limit {SAFE_RESIDUAL_VOLTS} V): stop, let the board "
        f"discharge, and measure {label!r} again in DC V, or the user clears this point with a reason "
        "(bench_state_update clear_residual_point); a safe reading at another point or a new confirmation does not "
        "clear it"
    )


def note_unsafe(state: BenchState, at: datetime) -> None:
    state.last_unsafe_at = at if state.last_unsafe_at is None else max(state.last_unsafe_at, at)


def set_point(state: BenchState, point: ResidualPoint) -> None:
    """The newest reading or clearance of a point replaces only the earlier one of the same point."""
    state.residual_points = [*(item for item in state.residual_points if item.point != point.point), point]


def gate(state: BenchState) -> GateReport:
    """What a resistance, continuity, or diode step needs, and what is missing.

    The gate opens only when the isolation confirmation is newer than the last unsafe residual reading, every point
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
    missing.extend(unsafe_text(point.reading, point.label) for point in state.residual_points if not point.safe)
    measured_after = confirmed_at is not None and any(
        point.safe and point.source_id is not None and point.at >= confirmed_at for point in state.residual_points
    )
    if not measured_after:
        missing.append(
            "no safe residual-voltage measurement (multimeter_read in DC voltage mode) after the confirmation"
        )
    return GateReport(unpowered_tests_allowed=not missing, missing=missing)


class BenchStateStore:
    """Reads the file on each call (another MCP server can write it). Without a path: memory only (tests)."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path
        self._memory = BenchState()

    def load(self) -> BenchState:
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


def power_check_problem(state: BenchState, measurement: Measurement, residual: bool) -> str | None:
    """A power check completes only with a safe DC residual reading after the user's isolation confirmation."""
    if measurement.mode is not RESIDUAL_MODE:
        return AC_RESIDUAL_NOTE
    if not residual:
        return (
            "a power check needs a DC voltage reading after the user confirmed the isolation (bench_state_update "
            "power isolated, user_confirmed_isolation true)"
        )
    if not is_safe(measurement):
        return unsafe_text(reading_text(measurement), measurement.label)
    missing = gate(state).missing
    return "; ".join(missing) if missing else None


class ResidualKeptError(ToolError):
    """The step was refused, but the reading changed the safety gate: the tool saves the record, then refuses."""


def step_refusal(
    state: BenchState, result: MeterResult, step_id: str | None, step: Step | None, done_before_gate: str | None
) -> str | None:
    """Why the measurement cannot complete this step, or None."""
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


def checked_point(state: BenchState, result: MeterResult, label: str, board: Board | None) -> PointName:
    """The point name of a residual reading. A name that does not name one point is refused; an unsafe reading still
    closes the gate (the last unsafe time needs a newer isolation confirmation), and the tool saves that."""
    try:
        return point_name(label, board)
    except PointNameError as exc:
        if result.captured_at is not None and not is_safe(result):
            note_unsafe(state, result.captured_at)
            raise ResidualKeptError(
                f"{exc}. This unsafe reading closed the safety gate until the user confirms the isolation again: "
                "record it again with the point name."
            ) from exc
        raise ToolError(str(exc)) from exc


def record_measurement(
    state: BenchState,
    result: MeterResult,
    label: str,
    step_id: str | None,
    done_before_gate: str | None = None,
    board: Board | None = None,
) -> tuple[Measurement, str | None]:
    """Add a confirmed meter result. Other results are refused: they are not measurements.

    `done_before_gate` (the user's reason) records a resistance, continuity, or diode measurement that was done
    before the gate was complete (for example with the power off, before the residual-voltage check). The flag stays
    on the measurement; bench_begin_step stays strict for new steps.

    A residual reading (DC V after the isolation confirmation, or an unsafe AC reading) needs a point name that names
    one point (bench_points.py, checked against `board` when one is open). It sets the latest reading of that point.

    The second value is the notice: why a step stays open (a power-check step completes only with a safe DC residual
    reading after the user's confirmation), an AC reading that does not count, or a point name without a board.
    A voltage reading after the confirmation is never lost: when the step is refused, it enters the record without the
    step, and ResidualKeptError tells the tool to save the record before it refuses. The same capture id enters once.
    """
    if result.status is not MeterStatus.CONFIRMED or result.capture_id is None or result.captured_at is None:
        raise ToolError(
            f"this meter result is {result.status}, not confirmed: it cannot enter the confirmed measurements. "
            f"{result.request or ''}".strip()
        )
    step = next((item for item in state.steps if item.step_id == step_id), None) if step_id else None
    power = state.power
    after_confirmation = power.confirmed_at is not None and result.captured_at >= power.confirmed_at
    voltage = result.mode in VOLTAGE_MODES and result.unit_family is UnitFamily.VOLTAGE and after_confirmation
    residual = voltage and result.mode is RESIDUAL_MODE
    # Only DC V is the residual check. An unsafe AC reading still blocks the gate: the board is not without power.
    for_gate = residual or (voltage and not is_safe(result))
    name = checked_point(state, result, label, board) if for_gate else None
    measurement, new = add_measurement(state, result, label, step_id, done_before_gate)
    # A reading does not replace a recent mode that the user confirmed on the dial: multimeter_read uses it.
    if not is_recent_user_mode(state.meter_mode):
        state.meter_mode = MeterModeRecord(mode=result.mode, source=READING_SOURCE, recorded_at=result.captured_at)
    if name is not None:
        point = ResidualPoint(
            point=name.key,
            label=label,
            at=result.captured_at,
            safe=is_safe(result),
            reading=reading_text(result),
            source_id=result.capture_id,
        )
        set_point(state, point)
        if not point.safe:
            note_unsafe(state, result.captured_at)
    refusal = step_refusal(state, result, step_id, step, done_before_gate)
    if refusal is not None:
        if for_gate:
            measurement.step_id = None if new else measurement.step_id
            raise ResidualKeptError(
                f"{refusal}. The voltage reading is recorded without the step, because it counts for the safety gate: "
                "do not record it again."
            )
        if new:
            state.measurements.remove(measurement)
        raise ToolError(refusal)
    notes = [AC_RESIDUAL_NOTE] if voltage and not residual else []
    if step is not None:
        problem = power_check_problem(state, measurement, residual) if step.kind is StepKind.POWER_CHECK else None
        if problem is None:
            step.done, step.done_at, step.evidence_id = True, now(), measurement.source_id
            step.completed_by = EVIDENCE
            step.reason = done_before_gate
        else:
            notes = [f"step {step.step_id} stays open: {problem}"]
    if name is not None and name.warning is not None:
        notes.append(name.warning)
    # A safe DC residual voltage (and no unsafe point) completes the open power checks.
    if residual and gate(state).unpowered_tests_allowed:
        complete_power_checks(state, measurement.source_id)
    return measurement, "; ".join(notes) or None


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
        names = ", ".join(repr(item.label) for item in state.residual_points) or "none"
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


def complete_power_checks(state: BenchState, evidence_id: str) -> None:
    for check in state.steps:
        if check.kind is StepKind.POWER_CHECK and not check.done:
            check.done, check.done_at, check.evidence_id = True, now(), evidence_id
            check.completed_by = EVIDENCE


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
    """Every phone_snapshot goes into the record. A record that cannot be read is left as it is."""
    try:
        state = store.load()
    except ToolError:
        return
    if capture_id not in state.photo_ids:
        state.photo_ids = [*state.photo_ids, capture_id][-MAX_PHOTO_IDS:]
        store.save(state)


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
    ) -> BenchStateView:
        """Update the bench record. Every field is optional.

        `power` "isolated" needs `user_confirmed_isolation: true` (the user said that the charger and the battery or
        bench supply are off); "powered" or "unknown" clears the isolation. Neither clears an unsafe residual point.
        `clear_residual_point` (a point name) with `clear_residual_reason` records that the user made that one point
        safe (for example "capacitor discharged with a resistor"): it counts as a safe reading for that point. Use it
        only when the user says so.
        `probe_contact` (where the probes touch) and `complete_step` (a visual step) need `photo_id`: a current
        phone_snapshot capture id that shows it. Measurement steps complete through bench_record_measurement.
        `part_candidates` are boardview estimates, kept apart from the measurements.
        `skip_step` (with `step_reason`) skips a step. `complete_step` with `step_reason` and no `photo_id` completes
        a step from the user's report (for example a test that they did themselves), so the next step moves on.
        Every phone_snapshot goes into `photo_ids` by itself.
        """
        state = store.load()
        if photo_id is not None:
            captures.require_current_photo(photo_id)
            if photo_id not in state.photo_ids:
                state.photo_ids = [*state.photo_ids, photo_id][-MAX_PHOTO_IDS:]
        change_power(state, power, user_confirmed_isolation)
        if meter_mode_confirmed_by_user is not None:
            state.meter_mode = MeterModeRecord(mode=meter_mode_confirmed_by_user, source=USER_SOURCE, recorded_at=now())
        if probe_contact is not None:
            if photo_id is None:
                raise ToolError("a probe contact needs `photo_id`: a current phone_snapshot that shows the probes")
            state.probe_contact = ProbeContact(description=probe_contact, photo_id=photo_id, recorded_at=now())
        if part_candidates is not None:
            state.part_candidates = list(dict.fromkeys(part_candidates))[:MAX_CANDIDATES]
        for new in add_steps or []:
            state.steps.append(Step(step_id=str(uuid.uuid7()), text=new.text, kind=new.kind))
        change_step(state, complete_step, skip_step, step_reason, photo_id)
        if clear_residual_point is not None:
            clear_point(state, clear_residual_point, clear_residual_reason, await open_board())
        return store.view(store.save(state))

    @server.tool()
    async def bench_record_measurement(
        capture_id: str, label: str, step_id: str | None = None, done_before_gate: str | None = None
    ) -> BenchStateView:
        """Put a multimeter_read result (by its capture_id) into the confirmed measurements.

        Only a "confirmed" result can enter; an uncertain, disputed, or unreadable one is refused. With `step_id`,
        the measurement completes that step (its kind must fit the meter mode; resistance, continuity, and diode
        steps need the safety gate). A confirmed DC voltage reading after the user confirmed the isolation is the
        residual-voltage check of the gate. An AC reading is recorded but does not count ("residual check needs DC
        V"); an unsafe AC reading still blocks the gate. Each measurement keeps the power state and the user's
        confirmation. For a residual reading, `label` names one point: a part pin ("C12.1", "C12 pin 1") or a net
        ("PP3V3_S5"), checked against the open board; a generic name ("residual", "test", "point") is refused. Each
        point keeps its latest residual reading. An unsafe point blocks the gate until a newer safe DC reading at the
        same point, or until the user clears it (bench_state_update clear_residual_point); a new isolation
        confirmation does not clear it, and after an unsafe reading the gate also needs a newer confirmation. A
        power-check step completes only with a safe DC residual reading after the user's confirmation; otherwise
        `notice` tells why it stays open. When the step is refused, a voltage reading that counts for the gate is
        still recorded (without the step): do not record it again. A second call with the same capture_id does not
        add it again.
        `done_before_gate`: the user's reason when a resistance, continuity, or diode test was already done before the
        gate was complete (for example with the power off, before the residual check); the measurement keeps it.
        """
        result = captures.meter_results.get(capture_id)
        if result is None:
            raise ToolError(f"no multimeter_read result with capture id {capture_id} in this server session")
        state = store.load()
        board = await open_board()
        try:
            _, notice = record_measurement(state, result, label, step_id, done_before_gate, board)
        except ResidualKeptError:
            store.save(state)
            raise
        return store.view(store.save(state), notice)

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
        state = store.load()
        probe_short(state)
        return store.view(store.save(state))


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
