"""The bench state: one small local session record (bench-state.json next to instructions.md, git-ignored).

It holds the power state, the meter mode, the probe contact, the confirmed measurements, the part candidates, the
photo ids, and the steps. Only a "confirmed" multimeter_read result can enter the measurements. A safety gate
guards resistance, continuity, and diode steps: power isolated, confirmed by the user, and a safe residual voltage
measured after that confirmation. A probe short sends the record back to the power check.

The record holds only what the agent passes (a few part names), never data copied from a board file.
"""

import contextlib
import json
import math
import os
import tempfile
import uuid
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import AwareDatetime, BaseModel, Field, ValidationError

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
POWER_CHECK_TEXT = (
    "Power check: the user disconnects the charger and the battery or bench supply and confirms it; then measure the "
    "residual voltage with the meter in voltage mode."
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
    # The residual reading that decides the gate: the highest unsafe point, else the latest safe reading.
    residual: Measurement | None = None
    # The latest confirmed voltage reading at each point (the measurement label) after the confirmation. An unsafe
    # point stays until a new reading at the same point, or a new isolation confirmation, replaces it.
    residual_points: list[Measurement] = Field(default_factory=list)


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
    # Why a step of this call stays open (for example a power check with an unsafe residual voltage).
    notice: str | None = None


# endregion: record


def now() -> datetime:
    return datetime.now(UTC)


def residual_volts(measurement: Measurement) -> float | None:
    """The reading in volts with its unit prefix (u, m, k, M), or None: an overload, not volts, or an unknown prefix."""
    family, prefix = unit_parts(measurement.unit)
    factor = PREFIX_FACTORS.get(prefix)
    if measurement.value is None or family is not UnitFamily.VOLTAGE or factor is None:
        return None
    return measurement.value * factor


def is_safe(measurement: Measurement) -> bool:
    volts = residual_volts(measurement)
    return volts is not None and abs(volts) <= SAFE_RESIDUAL_VOLTS


def danger(measurement: Measurement) -> float:
    """For the order of unsafe readings: the volts, and an overload or unknown prefix above every number."""
    volts = residual_volts(measurement)
    return math.inf if volts is None else abs(volts)


def point_key(label: str) -> str:
    return " ".join(label.casefold().split())


def residual_readings(power: PowerRecord) -> list[Measurement]:
    # A record from before residual_points has only `residual`.
    return power.residual_points or ([power.residual] if power.residual is not None else [])


def add_residual(power: PowerRecord, measurement: Measurement) -> None:
    """A voltage reading after the confirmation replaces only an earlier reading at the same point.

    So a safe reading at another point does not hide an unsafe one: the board can still hold a charge there.
    """
    key = point_key(measurement.label)
    others = [reading for reading in residual_readings(power) if point_key(reading.label) != key]
    power.residual_points = [*others, measurement]
    unsafe = [reading for reading in power.residual_points if not is_safe(reading)]
    power.residual = max(unsafe, key=danger) if unsafe else measurement


def unsafe_text(reading: Measurement) -> str:
    return (
        f"the residual voltage {reading.display_text} {reading.unit} at {reading.label!r} is not safe (limit "
        f"{SAFE_RESIDUAL_VOLTS} V): stop, let the board discharge, and measure again at the same point with the same "
        "label (a safe reading at another point does not clear it)"
    )


def gate(state: BenchState) -> GateReport:
    """What a resistance, continuity, or diode step needs, and what is missing."""
    missing = []
    power = state.power
    if power.state is not PowerState.ISOLATED:
        missing.append("the record does not show the power as isolated")
    if not power.user_confirmed_isolation or power.confirmed_at is None:
        missing.append("the user has not confirmed that the charger and the battery or bench supply are off")
    readings = residual_readings(power)
    if not readings:
        missing.append("no residual-voltage measurement (multimeter_read in voltage mode) after the confirmation")
    missing.extend(unsafe_text(reading) for reading in readings if not is_safe(reading))
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
    """A power check completes only with a safe residual reading after the user's isolation confirmation."""
    if not residual:
        return (
            "a power check needs a voltage reading after the user confirmed the isolation (bench_state_update power "
            "isolated, user_confirmed_isolation true)"
        )
    if not is_safe(measurement):
        return unsafe_text(measurement)
    missing = gate(state).missing
    return "; ".join(missing) if missing else None


def record_measurement(
    state: BenchState, result: MeterResult, label: str, step_id: str | None, done_before_gate: str | None = None
) -> tuple[Measurement, str | None]:
    """Add a confirmed meter result. Other results are refused: they are not measurements.

    `done_before_gate` (the user's reason) records a resistance, continuity, or diode measurement that was done
    before the gate was complete (for example with the power off, before the residual-voltage check). The flag stays
    on the measurement; bench_begin_step stays strict for new steps.

    The second value tells why the step stays open: a power-check step completes only with a safe residual reading
    after the user's confirmation. The measurement enters the record in any case, so an unsafe reading blocks the gate.
    """
    if result.status is not MeterStatus.CONFIRMED or result.capture_id is None or result.captured_at is None:
        raise ToolError(
            f"this meter result is {result.status}, not confirmed: it cannot enter the confirmed measurements. "
            f"{result.request or ''}".strip()
        )
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
    # A reading does not replace a recent mode that the user confirmed on the dial: multimeter_read uses it.
    if not is_recent_user_mode(state.meter_mode):
        state.meter_mode = MeterModeRecord(mode=result.mode, source=READING_SOURCE, recorded_at=result.captured_at)
    power = state.power
    after_confirmation = power.confirmed_at is not None and result.captured_at >= power.confirmed_at
    residual = result.mode in VOLTAGE_MODES and result.unit_family is UnitFamily.VOLTAGE and after_confirmation
    if residual:
        add_residual(power, measurement)
    notice = None
    step = find_step(state, step_id) if step_id else None
    if step is not None:
        if result.mode not in STEP_MODES.get(step.kind, frozenset()):
            raise ToolError(f"step {step.step_id} is a {step.kind} step, but this measurement is {result.mode}")
        if step.kind in UNPOWERED_KINDS and not gate(state).unpowered_tests_allowed and not done_before_gate:
            raise ToolError(
                "this step needs the safety gate first: "
                + "; ".join(gate(state).missing)
                + ". If the user already did this test safely (power off), pass done_before_gate with their reason."
            )
        problem = power_check_problem(state, measurement, residual) if step.kind is StepKind.POWER_CHECK else None
        if problem is None:
            step.done, step.done_at, step.evidence_id = True, now(), measurement.source_id
            step.completed_by = EVIDENCE
            step.reason = done_before_gate
        else:
            notice = f"step {step.step_id} stays open: {problem}"
    # A safe residual voltage (and no unsafe point) completes the open power checks.
    if residual and gate(state).unpowered_tests_allowed:
        for check in state.steps:
            if check.kind is StepKind.POWER_CHECK and not check.done:
                check.done, check.done_at, check.evidence_id = True, now(), measurement.source_id
                check.completed_by = EVIDENCE
    return measurement, notice


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


def register_bench_state_tools(server: MCPServer, store: BenchStateStore, captures: CaptureLog) -> None:
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
    ) -> BenchStateView:
        """Update the bench record. Every field is optional.

        `power` "isolated" needs `user_confirmed_isolation: true` (the user said that the charger and the battery or
        bench supply are off); "powered" or "unknown" clears the isolation and the residual-voltage check.
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
        if power is PowerState.ISOLATED:
            if not user_confirmed_isolation:
                raise ToolError(
                    "tell the user to disconnect the charger and the battery or bench supply, wait for their "
                    "confirmation, then set power isolated with user_confirmed_isolation true"
                )
            state.power = PowerRecord(state=PowerState.ISOLATED, user_confirmed_isolation=True, confirmed_at=now())
        elif power is not None:
            state.power = PowerRecord(state=power)
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
        return store.view(store.save(state))

    @server.tool()
    async def bench_record_measurement(
        capture_id: str, label: str, step_id: str | None = None, done_before_gate: str | None = None
    ) -> BenchStateView:
        """Put a multimeter_read result (by its capture_id) into the confirmed measurements.

        Only a "confirmed" result can enter; an uncertain, disputed, or unreadable one is refused. With `step_id`,
        the measurement completes that step (its kind must fit the meter mode; resistance, continuity, and diode
        steps need the safety gate). A confirmed voltage reading after the user confirmed the isolation is the
        residual-voltage check of the gate. Each measurement keeps the power state and the user's confirmation.
        Each point (the `label`) keeps its latest residual reading: an unsafe point blocks the gate until a new
        reading with the same label is safe, or the user confirms the isolation again. A power-check step completes
        only with a safe residual reading after the user's confirmation; otherwise `notice` tells why it stays open.
        `done_before_gate`: the user's reason when a resistance, continuity, or diode test was already done before the
        gate was complete (for example with the power off, before the residual check); the measurement keeps it.
        """
        result = captures.meter_results.get(capture_id)
        if result is None:
            raise ToolError(f"no multimeter_read result with capture id {capture_id} in this server session")
        state = store.load()
        _, notice = record_measurement(state, result, label, step_id, done_before_gate)
        return store.view(store.save(state), notice)

    @server.tool()
    async def bench_begin_step(step_id: str) -> BenchStateView:
        """Check the safety gate before a step. Call it before every resistance, continuity, or diode step.

        Those steps need: the power isolated, the user's confirmation, and a safe residual voltage measured after
        the confirmation (multimeter_read in voltage mode, then bench_record_measurement). Otherwise this tool
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
