"""The local bench record (bench_state.py). Report: "P2: Keep a compact bench state"."""

import asyncio
import contextlib
import fcntl
import json
import re
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import TextContent

from debug_devices_mcp import bench_state
from debug_devices_mcp.bench_points import NAME_THE_POINT, PointNameError, point_name
from debug_devices_mcp.bench_state import (
    AC_RESIDUAL_NOTE,
    SAFE_RESIDUAL_VOLTS,
    UNSAFE_AFTER_CONFIRMATION,
    USER_MODE_MAX_AGE,
    BenchState,
    BenchStateStore,
    DiodeContext,
    Measurement,
    PowerRecord,
    PowerState,
    ResidualPoint,
    StepKind,
    add_photo,
    clear_all_points,
    diode_voltage,
    gate,
    is_safe,
    lcd_voltage,
    note_meter_reading,
    recent_user_mode,
    residual_volts,
    result_voltage,
)
from debug_devices_mcp.board.dump import BoardDump
from debug_devices_mcp.board.model import Board
from debug_devices_mcp.config import Settings
from debug_devices_mcp.constants import REPO_ROOT
from debug_devices_mcp.meter_frames import combine
from debug_devices_mcp.multimeter import MeterMode, MeterResult, MeterStatus, MultimeterReading, check_reading
from debug_devices_mcp.server import Services, build_server

from .test_board import fixture
from .test_meter_frames import DIODE_VOLTS, LIMITS, answers, confirm_mode, frame
from .test_multimeter import READING
from .test_server import FakePhone, make_services, no_vision


def meter(mode: str, unit: str, value: float | None, display_text: str, confidence: float = 0.95) -> MeterResult:
    """A checked multimeter_read result with a fresh capture id and time, as the tool makes it."""
    reading = MultimeterReading.model_validate(
        {**READING, "mode": mode, "unit": unit, "value": value, "display_text": display_text, "confidence": confidence}
    )
    return check_reading(reading).model_copy(update={"capture_id": str(uuid.uuid7()), "captured_at": datetime.now(UTC)})


class Bench:
    def __init__(self, settings: Settings) -> None:
        self.services: Services = make_services(settings, FakePhone(), no_vision())
        self.server = build_server(self.services)

    def add_meter(self, result: MeterResult) -> str:
        assert result.capture_id is not None
        self.services.captures.meter_results[result.capture_id] = result
        return result.capture_id


@pytest.fixture
def bench(settings: Settings) -> Bench:
    return Bench(settings)


async def call(client: Client, tool: str, **arguments: object) -> dict[str, Any]:
    result = await client.call_tool(tool, arguments)
    assert not result.is_error, result.content
    assert result.structured_content is not None
    return result.structured_content


async def refused(client: Client, tool: str, **arguments: object) -> str:
    result = await client.call_tool(tool, arguments)
    assert result.is_error
    return next(block for block in result.content if isinstance(block, TextContent)).text


async def test_low_confidence_and_disputed_results_cannot_enter(bench: Bench) -> None:
    uncertain = bench.add_meter(meter("resistance", "kΩ", 443.0, "443", confidence=0.4))
    disputed = bench.add_meter(meter("resistance", "V", 443.0, "443"))
    async with Client(bench.server) as client:
        low = await refused(client, "bench_record_measurement", capture_id=uncertain, label="R1")
        conflict = await refused(client, "bench_record_measurement", capture_id=disputed, label="R1")
        state = await call(client, "bench_state")

    assert "uncertain, not confirmed" in low
    assert "disputed, not confirmed" in conflict
    assert state["state"]["measurements"] == []


async def test_safety_gate_for_a_resistance_step(bench: Bench) -> None:
    async with Client(bench.server) as client:
        state = await call(
            client,
            "bench_state_update",
            add_steps=[{"text": "Resistance of the rail to GND", "kind": "resistance"}],
        )
        step_id = state["next_step"]["step_id"]
        before = await refused(client, "bench_begin_step", step_id=step_id)
        without_user = await refused(client, "bench_state_update", power="isolated")
        await call(client, "bench_state_update", power="isolated", user_confirmed_isolation=True)
        no_residual = await refused(client, "bench_begin_step", step_id=step_id)
        residual = bench.add_meter(meter("dc_voltage", "mV", 12.0, "12.0"))
        await call(client, "bench_record_measurement", capture_id=residual, label="C12.1")
        allowed = await call(client, "bench_begin_step", step_id=step_id)
        resistance = bench.add_meter(meter("resistance", "kΩ", 443.0, "443.0"))
        done = await call(client, "bench_record_measurement", capture_id=resistance, label="rail", step_id=step_id)

    assert "power as isolated" in before
    assert "user has not confirmed" in before
    assert "user_confirmed_isolation" in without_user
    assert "residual-voltage" in no_residual
    assert allowed["gate"]["unpowered_tests_allowed"] is True
    # The completed measurement is no longer the next step.
    assert done["next_step"] is None
    assert done["state"]["steps"][0]["done"] is True
    assert done["state"]["steps"][0]["evidence_id"] == resistance
    assert [item["label"] for item in done["state"]["measurements"]] == ["C12.1", "rail"]
    assert done["state"]["measurements"][1]["confidence_state"] == "confirmed"


async def test_unsafe_residual_voltage_blocks(bench: Bench) -> None:
    async with Client(bench.server) as client:
        state = await call(client, "bench_state_update", add_steps=[{"text": "Diode test", "kind": "diode"}])
        await call(client, "bench_state_update", power="isolated", user_confirmed_isolation=True)
        high = bench.add_meter(meter("dc_voltage", "V", 5.1, "5.10"))
        after = await call(client, "bench_record_measurement", capture_id=high, label="C12.1")
        message = await refused(client, "bench_begin_step", step_id=state["next_step"]["step_id"])

    assert after["gate"]["unpowered_tests_allowed"] is False
    assert "not safe" in message


async def test_residual_before_the_confirmation_does_not_count(bench: Bench) -> None:
    early = bench.add_meter(meter("dc_voltage", "V", 0.01, "0.01"))
    async with Client(bench.server) as client:
        await call(client, "bench_state_update", power="isolated", user_confirmed_isolation=True)
        state = await call(client, "bench_record_measurement", capture_id=early, label="C12.1")

    # The point keeps the reading, but the gate needs a safe reading after the confirmation.
    assert [point["safe"] for point in state["state"]["residual_points"]] == [True]
    assert state["gate"]["unpowered_tests_allowed"] is False


async def test_probe_short_goes_back_to_the_power_check(bench: Bench) -> None:
    async with Client(bench.server) as client:
        await call(client, "bench_state_update", add_steps=[{"text": "Continuity", "kind": "continuity"}])
        await call(client, "bench_state_update", power="isolated", user_confirmed_isolation=True)
        residual = bench.add_meter(meter("dc_voltage", "V", 0.0, "0.00"))
        await call(client, "bench_record_measurement", capture_id=residual, label="C12.1")
        state = await call(client, "bench_probe_short")
        continuity_id = state["state"]["steps"][1]["step_id"]
        blocked = await refused(client, "bench_begin_step", step_id=continuity_id)
        instructions = await call(client, "bench_instructions")

    assert state["state"]["power"]["state"] == "unknown"
    assert state["state"]["power"]["user_confirmed_isolation"] is False
    assert state["next_step"]["kind"] == "power_check"
    assert state["gate"]["unpowered_tests_allowed"] is False
    assert "do not start" in blocked
    # bench_instructions shows the current step after the user's text.
    assert instructions["current_step"].startswith("Power check")


async def test_power_check_step_completes_with_a_safe_residual(bench: Bench) -> None:
    async with Client(bench.server) as client:
        await call(client, "bench_probe_short")
        await call(client, "bench_state_update", power="isolated", user_confirmed_isolation=True)
        residual = bench.add_meter(meter("dc_voltage", "V", 0.02, "0.02"))
        state = await call(client, "bench_record_measurement", capture_id=residual, label="C12.1")

    assert state["state"]["steps"][0]["done"] is True
    assert state["next_step"] is None


async def test_probe_contact_needs_a_current_photo(bench: Bench) -> None:
    async with Client(bench.server) as client:
        photo = await client.call_tool("phone_snapshot", {})
        photo_id = json.loads(next(block for block in photo.content if isinstance(block, TextContent)).text)[
            "capture_id"
        ]
        no_photo = await refused(client, "bench_state_update", probe_contact="red probe on the coil pad")
        state = await call(client, "bench_state_update", probe_contact="red probe on the coil pad", photo_id=photo_id)
        await bench.services.scene.mark_changed()
        stale = await refused(client, "bench_state_update", probe_contact="black probe on GND", photo_id=photo_id)

    assert "photo_id" in no_photo
    assert state["state"]["probe_contact"]["photo_id"] == photo_id
    assert state["state"]["photo_ids"] == [photo_id]
    assert "moved" in stale


async def test_measurement_steps_do_not_complete_by_hand(bench: Bench) -> None:
    async with Client(bench.server) as client:
        photo = await client.call_tool("phone_snapshot", {})
        photo_id = json.loads(next(block for block in photo.content if isinstance(block, TextContent)).text)[
            "capture_id"
        ]
        state = await call(client, "bench_state_update", add_steps=[{"text": "Voltage of the rail", "kind": "voltage"}])
        message = await refused(
            client, "bench_state_update", complete_step=state["next_step"]["step_id"], photo_id=photo_id
        )
        visual = await call(client, "bench_state_update", add_steps=[{"text": "Look for burn marks", "kind": "visual"}])
        visual_id = visual["state"]["steps"][1]["step_id"]
        completed = await call(client, "bench_state_update", complete_step=visual_id, photo_id=photo_id)

    assert "completes with bench_record_measurement" in message
    assert completed["state"]["steps"][1]["done"] is True
    assert completed["state"]["steps"][1]["evidence_id"] == photo_id


def test_file_store_round_trip_and_bad_file(tmp_path: Path) -> None:
    path = tmp_path / "bench-state.json"
    store = BenchStateStore(path)
    assert store.load() == BenchState()
    state = store.load()
    state.part_candidates = ["L501", "L502"]
    store.save(state)
    assert json.loads(path.read_text())["part_candidates"] == ["L501", "L502"]
    assert BenchStateStore(path).load().part_candidates == ["L501", "L502"]
    assert not list(tmp_path.glob(".bench-state.json.*.tmp"))
    path.write_text("{not json")
    with pytest.raises(ToolError, match="cannot read the bench state"):
        store.load()


def test_gate_lists_everything_that_is_missing() -> None:
    report = gate(BenchState())
    assert report.unpowered_tests_allowed is False
    assert len(report.missing) == 3


def test_bench_state_file_is_git_ignored() -> None:
    lines = (REPO_ROOT / ".gitignore").read_text().splitlines()
    assert "bench-state.json" in lines


def test_step_kinds_cover_the_report() -> None:
    assert {StepKind.RESISTANCE, StepKind.CONTINUITY, StepKind.DIODE} <= set(StepKind)
    assert MeterMode.RESISTANCE in MeterMode


def test_bench_state_setting(settings: Settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert settings.bench_state_file.parent == tmp_path
    monkeypatch.setenv("DEBUG_DEVICES_BENCH_STATE_FILE", str(tmp_path / "other.json"))
    assert Settings.from_cli([]).bench_state_file == tmp_path / "other.json"
    # An empty variable, as in .env.example, means the default file next to instructions.md.
    monkeypatch.setenv("DEBUG_DEVICES_BENCH_STATE_FILE", "")
    assert Settings.from_cli([]).bench_state_file == REPO_ROOT / "bench-state.json"


# region: bench feedback 2, item 9


async def test_measurement_keeps_the_power_state(bench: Bench) -> None:
    async with Client(bench.server) as client:
        await call(client, "bench_state_update", power="isolated", user_confirmed_isolation=True)
        residual = bench.add_meter(meter("dc_voltage", "V", 0.01, "0.01"))
        state = await call(client, "bench_record_measurement", capture_id=residual, label="C12.1")

    measurement = state["state"]["measurements"][0]
    assert measurement["power_state"] == "isolated"
    assert measurement["power_confirmed_by_user"] is True
    assert measurement["done_before_gate"] is None


async def test_test_done_before_the_gate_can_be_attached(bench: Bench) -> None:
    # The bench case: the user did the resistance test with the power off before the residual-voltage check.
    async with Client(bench.server) as client:
        state = await call(client, "bench_state_update", add_steps=[{"text": "Rail to GND", "kind": "resistance"}])
        step_id = state["next_step"]["step_id"]
        resistance = bench.add_meter(meter("resistance", "kΩ", 443.0, "443.0"))
        refused_without = await refused(
            client, "bench_record_measurement", capture_id=resistance, label="rail", step_id=step_id
        )
        attached = await call(
            client,
            "bench_record_measurement",
            capture_id=resistance,
            label="rail",
            step_id=step_id,
            done_before_gate="the user measured with the charger and the battery removed, before the residual check",
        )
        still_strict = await refused(client, "bench_begin_step", step_id=step_id)

    assert "done_before_gate" in refused_without
    assert attached["next_step"] is None
    assert attached["state"]["steps"][0]["completed_by"] == "evidence"
    assert "battery removed" in attached["state"]["measurements"][0]["done_before_gate"]
    assert "already complete" in still_strict


async def test_skip_or_complete_a_step_with_a_reason(bench: Bench) -> None:
    async with Client(bench.server) as client:
        state = await call(
            client,
            "bench_state_update",
            add_steps=[{"text": "Voltage of PP3V3", "kind": "voltage"}, {"text": "Look at C8850", "kind": "visual"}],
        )
        first, second = (step["step_id"] for step in state["state"]["steps"])
        no_reason = await refused(client, "bench_state_update", skip_step=first)
        reported = await call(
            client, "bench_state_update", complete_step=first, step_reason="the user measured it: 3.31 V"
        )
        skipped = await call(client, "bench_state_update", skip_step=second, step_reason="not needed any more")

    assert "step_reason" in no_reason
    assert reported["state"]["steps"][0]["completed_by"] == "user_report"
    assert reported["next_step"]["step_id"] == second
    assert skipped["state"]["steps"][1]["completed_by"] == "skipped"
    assert skipped["next_step"] is None


async def test_every_phone_snapshot_goes_into_the_record(bench: Bench) -> None:
    async with Client(bench.server) as client:
        ids = []
        for _ in range(3):
            photo = await client.call_tool("phone_snapshot", {})
            ids.append(
                json.loads(next(block for block in photo.content if isinstance(block, TextContent)).text)["capture_id"]
            )
        measured = await client.call_tool("bench_measure", {"frames": 1})
        state = await call(client, "bench_state")

    assert measured.structured_content is not None
    assert state["state"]["photo_ids"] == [*ids, measured.structured_content["photo"]["capture_id"]]


# endregion


# region: QA round 4, the safety gate (B-E3, B-E4, B-E8)


async def isolate(client: Client) -> None:
    await call(client, "bench_state_update", power="isolated", user_confirmed_isolation=True)


async def measure(client: Client, bench: Bench, result: MeterResult, label: str, **arguments: object) -> dict[str, Any]:
    return await call(client, "bench_record_measurement", capture_id=bench.add_meter(result), label=label, **arguments)


async def test_a_safe_reading_at_another_point_does_not_hide_an_unsafe_one(bench: Bench) -> None:
    async with Client(bench.server) as client:
        await call(client, "bench_probe_short")
        await isolate(client)
        await measure(client, bench, meter("dc_voltage", "V", 5.1, "5.10"), "C12.1")
        other = await measure(client, bench, meter("dc_voltage", "V", 0.01, "0.01"), "VBUS")
        # The same point in another spelling: a newer safe reading, but the confirmation is older than the unsafe one.
        same_point = await measure(client, bench, meter("dc_voltage", "mV", 8.0, "8.0"), "c12 pin 1")
        await isolate(client)
        after_new_confirmation = await measure(client, bench, meter("dc_voltage", "mV", 6.0, "6.0"), "C12.1")

    assert other["gate"]["unpowered_tests_allowed"] is False
    assert any("5.10 V at 'C12.1' is not safe" in item for item in other["gate"]["missing"])
    assert other["next_step"]["kind"] == "power_check"
    assert same_point["gate"]["unpowered_tests_allowed"] is False
    assert same_point["gate"]["missing"] == [UNSAFE_AFTER_CONFIRMATION]
    assert [point["point"] for point in same_point["state"]["residual_points"]] == ["vbus", "c12.1"]
    assert after_new_confirmation["gate"]["unpowered_tests_allowed"] is True
    assert after_new_confirmation["next_step"] is None


async def test_a_new_confirmation_does_not_clear_an_unsafe_point(bench: Bench) -> None:
    async with Client(bench.server) as client:
        await isolate(client)
        await measure(client, bench, meter("dc_voltage", "V", 5.1, "5.10"), "C12.1")
        await measure(client, bench, meter("dc_voltage", "V", 12.0, "12.00"), "PP12V")
        await isolate(client)
        elsewhere = await measure(client, bench, meter("dc_voltage", "V", 0.0, "0.00"), "VBUS")
        one_left = await measure(client, bench, meter("dc_voltage", "V", 0.0, "0.00"), "PP12V")
        after_short = await call(client, "bench_probe_short")

    # The QA case: a safe reading at another point after a new confirmation does not open the gate.
    assert elsewhere["gate"]["unpowered_tests_allowed"] is False
    assert len([item for item in elsewhere["gate"]["missing"] if "is not safe" in item]) == 2
    assert [item for item in one_left["gate"]["missing"] if "is not safe" in item] == [
        item for item in elsewhere["gate"]["missing"] if "'C12.1'" in item
    ]
    # A probe short does not clear the points either.
    assert [point["safe"] for point in after_short["state"]["residual_points"]] == [False, True, True]


async def test_the_user_can_clear_one_point_with_a_reason(bench: Bench) -> None:
    async with Client(bench.server) as client:
        await isolate(client)
        await measure(client, bench, meter("dc_voltage", "V", 5.1, "5.10"), "C12.1")
        no_reason = await refused(client, "bench_state_update", clear_residual_point="C12.1")
        unknown = await refused(client, "bench_state_update", clear_residual_point="C99.1", clear_residual_reason="x")
        cleared = await call(
            client,
            "bench_state_update",
            clear_residual_point="C12 pin 1",
            clear_residual_reason="capacitor discharged with a resistor",
        )
        await isolate(client)
        opened = await measure(client, bench, meter("dc_voltage", "V", 0.01, "0.01"), "VBUS")

    assert "clear_residual_reason" in no_reason
    assert "no residual point 'C99.1'" in unknown
    point = cleared["state"]["residual_points"][0]
    assert (point["safe"], point["source_id"], point["user_reason"]) == (
        True,
        None,
        "capacitor discharged with a resistor",
    )
    assert cleared["state"]["residual_clearances"][0]["reason"] == "capacitor discharged with a resistor"
    # The clearance counts for its point; the gate still needs a new confirmation and a safe DC reading after it.
    assert cleared["gate"]["unpowered_tests_allowed"] is False
    assert opened["gate"]["unpowered_tests_allowed"] is True


async def test_a_power_check_step_needs_a_safe_residual_after_the_confirmation(bench: Bench) -> None:
    async with Client(bench.server) as client:
        state = await call(client, "bench_probe_short")
        check_id = state["next_step"]["step_id"]
        before = await measure(client, bench, meter("dc_voltage", "V", 0.01, "0.01"), "VBUS", step_id=check_id)
        await isolate(client)
        unsafe = await measure(client, bench, meter("dc_voltage", "V", 5.1, "5.10"), "VBUS", step_id=check_id)
        too_early = await measure(client, bench, meter("dc_voltage", "V", 0.02, "0.02"), "VBUS", step_id=check_id)
        await isolate(client)
        safe = await measure(client, bench, meter("dc_voltage", "V", 0.01, "0.01"), "VBUS", step_id=check_id)

    assert before["next_step"]["step_id"] == check_id
    assert before["notice"].startswith(f"step {check_id} stays open: a power check needs a DC voltage reading after")
    assert unsafe["next_step"]["step_id"] == check_id
    assert "5.10 V at 'VBUS' is not safe" in unsafe["notice"]
    # The unsafe reading is in the record and blocks the gate.
    assert unsafe["state"]["measurements"][-1]["display_text"] == "5.10"
    assert unsafe["gate"]["unpowered_tests_allowed"] is False
    # A newer safe reading at the point, but the confirmation is older than the unsafe reading.
    assert too_early["notice"].startswith(f"step {check_id} stays open: {UNSAFE_AFTER_CONFIRMATION}")
    # Without a board, only the warning about the point name stays.
    assert safe["notice"].startswith("no board is open, so 'VBUS' is not checked")
    assert safe["next_step"] is None
    assert safe["state"]["steps"][0]["evidence_id"] == safe["state"]["measurements"][-1]["source_id"]


@pytest.mark.parametrize(
    ("unit", "value", "volts"),
    [
        ("\u00b5V", 450.0, 450e-6),  # MICRO SIGN
        ("\u03bcV", 450.0, 450e-6),  # GREEK SMALL LETTER MU
        ("uV", 450.0, 450e-6),
        ("mV", 12.0, 0.012),
        ("V", 0.3, 0.3),
        ("kV", 0.4, 400.0),
        ("MV", 0.4, 400_000.0),
        ("\u2126", 0.4, None),  # not volts
        ("xV", 0.4, None),  # unknown prefix
    ],
)
def test_residual_volts_use_the_unit_prefix(unit: str, value: float, volts: float | None) -> None:
    measurement = Measurement(
        source_id="id",
        measured_at=datetime.now(UTC),
        label="VBUS",
        mode=MeterMode.DC_VOLTAGE,
        value=value,
        unit=unit,
        display_text=str(value),
        overload=False,
        confidence_state=MeterStatus.CONFIRMED,
    )
    assert residual_volts(measurement) == (pytest.approx(volts) if volts is not None else None)
    assert is_safe(measurement) is (volts is not None and abs(volts) <= SAFE_RESIDUAL_VOLTS)


async def test_450_microvolts_opens_the_gate_and_0_4_kilovolts_blocks_it(bench: Bench) -> None:
    async with Client(bench.server) as client:
        await isolate(client)
        micro = await measure(client, bench, meter("dc_voltage", "\u00b5V", 450.0, "450.0"), "VBUS")
        kilo = await measure(client, bench, meter("dc_voltage", "kV", 0.4, "0.400"), "VBUS")

    assert micro["gate"]["unpowered_tests_allowed"] is True
    assert kilo["gate"]["unpowered_tests_allowed"] is False


# endregion


# region: QA round 4, a measurement keeps the user's meter mode (B-F3)


async def test_recording_a_measurement_keeps_the_users_meter_mode(settings: Settings) -> None:
    services = make_services(settings, FakePhone(), answers(DIODE_VOLTS))
    async with Client(build_server(services)) as client:
        await confirm_mode(client, "dc_voltage")
        first = await call(client, "multimeter_read")
        recorded = await call(client, "bench_record_measurement", capture_id=first["capture_id"], label="VBUS")
        second = await call(client, "multimeter_read")

    assert first["status"] == "confirmed"
    assert recorded["state"]["meter_mode"]["source"] == "user"
    assert recorded["state"]["meter_mode"]["mode"] == "dc_voltage"
    assert recent_user_mode(services.bench) is MeterMode.DC_VOLTAGE
    # The next read still uses the user's mode.
    assert second["mode_source"] == "user"
    assert second["status"] == "confirmed"


async def test_a_reading_replaces_an_expired_user_mode(bench: Bench) -> None:
    async with Client(bench.server) as client:
        await call(client, "bench_state_update", meter_mode_confirmed_by_user="dc_voltage")
        state = bench.services.bench.load()
        assert state.meter_mode is not None
        state.meter_mode.recorded_at -= USER_MODE_MAX_AGE + timedelta(minutes=1)
        bench.services.bench.save(state)
        ohms = bench.add_meter(meter("resistance", "k\u03a9", 443.0, "443.0"))
        recorded = await call(client, "bench_record_measurement", capture_id=ohms, label="R12")

    assert (recorded["state"]["meter_mode"]["mode"], recorded["state"]["meter_mode"]["source"]) == (
        "resistance",
        "reading",
    )


# endregion


# region: QA round 6, only DC V is the residual check (N5), and a refused step keeps the reading (N6)


async def test_an_ac_reading_is_recorded_but_does_not_open_the_gate(bench: Bench) -> None:
    async with Client(bench.server) as client:
        state = await call(client, "bench_probe_short")
        check_id = state["next_step"]["step_id"]
        await isolate(client)
        with_step = await measure(client, bench, meter("ac_voltage", "V", 0.01, "0.01"), "C12", step_id=check_id)
        without_step = await measure(client, bench, meter("ac_voltage", "mV", 3.0, "3.0"), "VBUS")

    assert with_step["notice"] == f"step {check_id} stays open: {AC_RESIDUAL_NOTE}"
    assert with_step["next_step"]["step_id"] == check_id
    assert with_step["state"]["measurements"][-1]["mode"] == "ac_voltage"
    assert without_step["notice"] == AC_RESIDUAL_NOTE
    assert without_step["gate"]["unpowered_tests_allowed"] is False
    assert without_step["state"]["residual_points"] == []
    assert AC_RESIDUAL_NOTE.startswith("residual check needs DC V")


async def test_an_unsafe_ac_reading_still_blocks_the_gate(bench: Bench) -> None:
    async with Client(bench.server) as client:
        await isolate(client)
        await measure(client, bench, meter("dc_voltage", "V", 0.01, "0.01"), "C12.1")
        mains = await measure(client, bench, meter("ac_voltage", "V", 230.0, "230.0"), "VBUS")
        await isolate(client)
        dc_again = await measure(client, bench, meter("dc_voltage", "V", 0.02, "0.02"), "VBUS")

    assert mains["gate"]["unpowered_tests_allowed"] is False
    assert any("230.0 V at 'VBUS' is not safe" in item for item in mains["gate"]["missing"])
    # A new confirmation and a safe DC reading at the same point clear it.
    assert dc_again["gate"]["unpowered_tests_allowed"] is True


@pytest.mark.parametrize("wrong_step", ["resistance step", "unknown step id"])
async def test_a_refused_step_keeps_the_unsafe_reading(bench: Bench, wrong_step: str) -> None:
    async with Client(bench.server) as client:
        state = await call(client, "bench_state_update", add_steps=[{"text": "Rail to GND", "kind": "resistance"}])
        resistance_id = state["next_step"]["step_id"]
        await isolate(client)
        opened = await measure(client, bench, meter("dc_voltage", "V", 0.01, "0.01"), "C12")
        unsafe = bench.add_meter(meter("dc_voltage", "V", 5.1, "5.10"))
        step_id = resistance_id if wrong_step == "resistance step" else "no-such-step"
        refusal = await refused(client, "bench_record_measurement", capture_id=unsafe, label="VBUS", step_id=step_id)
        blocked = await refused(client, "bench_begin_step", step_id=resistance_id)
        again = await call(client, "bench_record_measurement", capture_id=unsafe, label="VBUS")

    assert opened["gate"]["unpowered_tests_allowed"] is True
    assert "recorded without the step" in refusal
    assert "not safe" in blocked
    # The retry of the same capture id does not add it again.
    assert [item["display_text"] for item in again["state"]["measurements"]] == ["0.01", "5.10"]
    assert again["state"]["measurements"][-1]["step_id"] is None
    assert again["gate"]["unpowered_tests_allowed"] is False


async def test_a_refused_unsafe_reading_first_then_a_safe_one_elsewhere_keeps_the_gate_closed(bench: Bench) -> None:
    async with Client(bench.server) as client:
        state = await call(client, "bench_state_update", add_steps=[{"text": "Rail to GND", "kind": "resistance"}])
        resistance_id = state["next_step"]["step_id"]
        await isolate(client)
        unsafe = bench.add_meter(meter("dc_voltage", "V", 5.1, "5.10"))
        await refused(client, "bench_record_measurement", capture_id=unsafe, label="VBUS", step_id=resistance_id)
        safe = await measure(client, bench, meter("dc_voltage", "V", 0.01, "0.01"), "C12")

    assert safe["gate"]["unpowered_tests_allowed"] is False
    assert any("5.10 V at 'VBUS' is not safe" in item for item in safe["gate"]["missing"])


async def test_a_refused_reading_that_is_not_a_residual_changes_nothing(bench: Bench) -> None:
    async with Client(bench.server) as client:
        state = await call(client, "bench_state_update", add_steps=[{"text": "Rail voltage", "kind": "voltage"}])
        voltage_id = state["next_step"]["step_id"]
        ohms = bench.add_meter(meter("resistance", "k\u03a9", 443.0, "443.0"))
        message = await refused(client, "bench_record_measurement", capture_id=ohms, label="R12", step_id=voltage_id)
        after = await call(client, "bench_state")

    assert "is a voltage step" in message
    assert "recorded without the step" not in message
    assert after["state"]["measurements"] == []


# endregion


# region: QA round 6, the B-E3 decision: a residual label names one point (rule 1)


def identity_board() -> Board:
    """Synthetic open fixture (mcp/tests/fixtures/boardview/identity.json): C8850 pins 1-2, net PP_SYN_1V0."""
    return Board(BoardDump.model_validate_json(fixture("identity.json")), "sha")


@pytest.mark.parametrize(
    ("label", "key"),
    [("C8850.1", "c8850.1"), ("l501 pin 2", "l501.2"), ("C8850:1", "c8850.1"), ("pp_syn_1v0", "pp_syn_1v0")],
)
def test_a_board_point_name_is_a_part_pin_or_a_net(label: str, key: str) -> None:
    name = point_name(label, identity_board())
    assert (name.key, name.warning) == (key, None)


@pytest.mark.parametrize(
    ("label", "problem"),
    [
        ("C8850", "is a part; name its pin (C8850.1, C8850.2)"),
        ("C8850.3", "C8850 has no pin '3'"),
        ("C9999.1", "is not a part pin or a net of the open board"),
        ("PP_*", "is a pattern, not one point"),
        ("C12 positive side", "is not a part pin or a net of the open board"),
    ],
)
def test_a_name_that_is_not_one_board_point_is_refused(label: str, problem: str) -> None:
    with pytest.raises(PointNameError, match=re.escape(problem)) as error:
        point_name(label, identity_board())
    assert str(error.value).startswith(NAME_THE_POINT)


@pytest.mark.parametrize("label", ["residual", "Residual voltage", "test", "point", "test point 3", "", "   "])
@pytest.mark.parametrize("with_board", [True, False])
def test_a_generic_name_is_refused_with_and_without_a_board(label: str, with_board: bool) -> None:
    with pytest.raises(PointNameError, match=re.escape(NAME_THE_POINT)):
        point_name(label, identity_board() if with_board else None)


def test_without_a_board_a_free_name_has_a_warning() -> None:
    name = point_name("C12 pin 1", None)
    assert name.key == "c12.1"
    assert name.warning is not None
    assert "use this name only for this one point" in name.warning


async def test_the_same_generic_label_at_two_points_cannot_open_the_gate(bench: Bench) -> None:
    bench.services.board.board = identity_board()
    async with Client(bench.server) as client:
        await isolate(client)
        unsafe = bench.add_meter(meter("dc_voltage", "V", 5.1, "5.10"))
        refusal = await refused(client, "bench_record_measurement", capture_id=unsafe, label="residual")
        safe = bench.add_meter(meter("dc_voltage", "V", 0.01, "0.01"))
        generic_safe = await refused(client, "bench_record_measurement", capture_id=safe, label="residual")
        await isolate(client)
        named = await call(client, "bench_record_measurement", capture_id=safe, label="C8850 pin 1")
        named_unsafe = await call(client, "bench_record_measurement", capture_id=unsafe, label="L501.2")

    assert "name the point: part.pin or net" in refusal
    assert "the bench safety gate is closed at an unknown point" in refusal
    assert "safety gate" not in generic_safe
    # The refused unsafe reading stays as an unknown point: a safe reading elsewhere does not open the gate.
    assert named["gate"]["unpowered_tests_allowed"] is False
    assert any(f"capture {unsafe} is not safe" in item for item in named["gate"]["missing"])
    # Recorded with its point name, the unknown point moves to that point.
    assert [(point["point"], point["safe"]) for point in named_unsafe["state"]["residual_points"]] == [
        ("c8850.1", True),
        ("l501.2", False),
    ]


# endregion


# region: QA round 8 (N13 to N17, and the three B-E3 rule 2 cases)


async def open_gate(client: Client, bench: Bench) -> dict[str, Any]:
    await isolate(client)
    opened = await measure(client, bench, meter("dc_voltage", "V", 0.01, "0.01"), "C12.1")
    assert opened["gate"]["unpowered_tests_allowed"] is True
    return opened


async def test_an_unconfirmed_unsafe_reading_closes_the_gate(bench: Bench) -> None:
    async with Client(bench.server) as client:
        await open_gate(client, bench)
        uncertain = bench.add_meter(meter("dc_voltage", "V", 5.1, "5.10", confidence=0.3))
        refusal = await refused(client, "bench_record_measurement", capture_id=uncertain, label="VBUS")
        state = await call(client, "bench_state")

    assert "uncertain, not confirmed" in refusal
    assert "the bench safety gate is closed at 'VBUS'" in refusal
    assert state["gate"]["unpowered_tests_allowed"] is False
    assert state["state"]["measurements"][-1]["label"] == "C12.1"
    assert [(point["point"], point["safe"]) for point in state["state"]["residual_points"]][-1] == ("vbus", False)


async def test_an_unconfirmed_safe_reading_never_opens_the_gate(bench: Bench) -> None:
    async with Client(bench.server) as client:
        await isolate(client)
        uncertain = bench.add_meter(meter("dc_voltage", "V", 0.01, "0.01", confidence=0.3))
        refusal = await refused(client, "bench_record_measurement", capture_id=uncertain, label="VBUS")
        state = await call(client, "bench_state")

    assert "safety gate" not in refusal
    assert state["state"]["residual_points"] == []
    assert state["gate"]["unpowered_tests_allowed"] is False


@pytest.mark.parametrize("tool", ["multimeter_read", "bench_measure"])
async def test_a_disputed_unsafe_read_closes_the_gate_at_once(settings: Settings, tool: str) -> None:
    good = {**READING, "display_text": "5.10", "value": 5.10}
    moved = {**READING, "display_text": "51.0", "value": 51.0}
    services = make_services(settings, FakePhone(), answers(good, moved))
    safe = meter("dc_voltage", "V", 0.01, "0.01")
    assert safe.capture_id is not None
    services.captures.meter_results[safe.capture_id] = safe
    async with Client(build_server(services)) as client:
        await isolate(client)
        await call(client, "bench_record_measurement", capture_id=safe.capture_id, label="C12.1")
        read = await call(client, tool)
        meter_result = read if tool == "multimeter_read" else read["meter"]
        state = await call(client, "bench_state")
        named = await refused(client, "bench_record_measurement", capture_id=meter_result["capture_id"], label="VBUS")
        after = await call(client, "bench_state")

    assert meter_result["status"] == "disputed"
    assert "51.0 V is above the safe residual limit" in meter_result["bench_notice"]
    assert state["gate"]["unpowered_tests_allowed"] is False
    unknown = f"unknown point {meter_result['capture_id']}"
    assert unknown in [point["point"] for point in state["state"]["residual_points"]]
    # Recorded with its point name, the unknown point moves to "VBUS" (the result is still refused).
    assert "disputed, not confirmed" in named
    assert [point["point"] for point in after["state"]["residual_points"]] == ["c12.1", "vbus"]


async def test_a_reading_before_the_newest_confirmation_is_still_a_point(bench: Bench) -> None:
    # Case (a): the unsafe capture comes after the first confirmation; the user confirms again before it is recorded.
    async with Client(bench.server) as client:
        await isolate(client)
        unsafe = meter("dc_voltage", "V", 5.1, "5.10")
        await isolate(client)
        late = await measure(client, bench, unsafe, "C12.1")
        other = await measure(client, bench, meter("dc_voltage", "V", 0.01, "0.01"), "VBUS")

    assert late["state"]["residual_points"][0]["safe"] is False
    assert other["gate"]["unpowered_tests_allowed"] is False


async def test_an_older_reading_never_replaces_a_newer_one(bench: Bench) -> None:
    # Case (c): 0.01 V at t1 and 5.10 V at t2 at one point, recorded in the order t2, t1.
    older_safe = meter("dc_voltage", "V", 0.01, "0.01")
    newer_unsafe = meter("dc_voltage", "V", 5.1, "5.10")
    async with Client(bench.server) as client:
        await isolate(client)
        await measure(client, bench, newer_unsafe, "C12.1")
        state = await measure(client, bench, older_safe, "C12.1")
        await isolate(client)
        other = await measure(client, bench, meter("dc_voltage", "V", 0.01, "0.01"), "VBUS")

    assert [(point["point"], point["safe"]) for point in state["state"]["residual_points"]] == [("c12.1", False)]
    assert other["gate"]["unpowered_tests_allowed"] is False


async def test_an_unsafe_reading_with_the_power_on_is_a_point(bench: Bench) -> None:
    # N15: power unknown, 12.00 V at C12.1, then isolation, a confirmation, and a safe reading elsewhere.
    async with Client(bench.server) as client:
        powered = await measure(client, bench, meter("dc_voltage", "V", 12.0, "12.00"), "C12.1")
        await isolate(client)
        elsewhere = await measure(client, bench, meter("dc_voltage", "V", 0.01, "0.01"), "VBUS")
        same_point = await measure(client, bench, meter("dc_voltage", "V", 0.01, "0.01"), "C12.1")

    assert powered["state"]["power"]["state"] == "unknown"
    assert powered["state"]["residual_points"][0]["safe"] is False
    assert elsewhere["gate"]["unpowered_tests_allowed"] is False
    assert same_point["gate"]["unpowered_tests_allowed"] is True


async def test_one_capture_belongs_to_one_point(bench: Bench) -> None:
    # N14: a safe capture recorded for VBUS cannot clear C12.1.
    async with Client(bench.server) as client:
        await isolate(client)
        await measure(client, bench, meter("dc_voltage", "V", 5.1, "5.10"), "C12.1")
        await isolate(client)
        safe = bench.add_meter(meter("dc_voltage", "V", 0.01, "0.01"))
        await call(client, "bench_record_measurement", capture_id=safe, label="VBUS")
        refusal = await refused(client, "bench_record_measurement", capture_id=safe, label="C12.1")
        same = await call(client, "bench_record_measurement", capture_id=safe, label=" vbus ")

    assert f"capture {safe} is already recorded for 'VBUS'" in refusal
    assert same["gate"]["unpowered_tests_allowed"] is False
    assert [item["label"] for item in same["state"]["measurements"]] == ["C12.1", "VBUS"]


@pytest.mark.parametrize("label", ["GND", "agnd", "PGND", "VSS_IO", "dvss"])
def test_a_ground_net_is_not_a_residual_point(label: str) -> None:
    with pytest.raises(PointNameError, match="ground net"):
        point_name(label, None)


@pytest.mark.parametrize("label", ["C8850.2", "GND"])
def test_a_pin_on_a_ground_net_is_not_a_residual_point(label: str) -> None:
    with pytest.raises(PointNameError, match="on the ground net GND"):
        point_name(label, identity_board())


async def test_a_ground_reading_does_not_open_the_gate(bench: Bench) -> None:
    async with Client(bench.server) as client:
        await isolate(client)
        ground = bench.add_meter(meter("dc_voltage", "V", 0.0, "0.00"))
        refusal = await refused(client, "bench_record_measurement", capture_id=ground, label="GND")
        state = await call(client, "bench_state")

    assert "ground net" in refusal
    assert state["state"]["residual_points"] == []
    assert state["gate"]["unpowered_tests_allowed"] is False


async def test_a_refused_step_leaves_the_power_check_consistent(bench: Bench) -> None:
    # N17: a safe residual reading with a wrong step opens the gate; the open power check follows the gate.
    async with Client(bench.server) as client:
        state = await call(client, "bench_probe_short")
        check_id = state["next_step"]["step_id"]
        state = await call(client, "bench_state_update", add_steps=[{"text": "Rail voltage", "kind": "voltage"}])
        voltage_id = state["state"]["steps"][1]["step_id"]
        await isolate(client)
        safe = bench.add_meter(meter("dc_voltage", "V", 0.01, "0.01"))
        refusal = await refused(client, "bench_record_measurement", capture_id=safe, label="C12.1", step_id="nope")
        after_refusal = await call(client, "bench_state")
        attached = await call(client, "bench_record_measurement", capture_id=safe, label="C12.1", step_id=voltage_id)

    assert "recorded without the step" in refusal
    assert "do not record it again" not in refusal
    assert "record the same capture_id again with that step_id" in refusal
    assert after_refusal["gate"]["unpowered_tests_allowed"] is True
    power_check = next(step for step in after_refusal["state"]["steps"] if step["step_id"] == check_id)
    assert power_check["done"] is True
    assert attached["state"]["measurements"][-1]["step_id"] == voltage_id
    assert attached["next_step"] is None


# endregion


# region: bulk clear of all residual points (the default of the user's decision D7)

USER_WORDS = "  I discharged every capacitor with the 1 kOhm resistor, it is safe now. "


async def bulk_clear(client: Client, words: str | None = USER_WORDS) -> dict[str, Any]:
    return await call(client, "bench_state_update", clear_all_residual_points=True, user_words=words)


async def refused_bulk_clear(client: Client, words: str | None = USER_WORDS) -> str:
    return await refused(client, "bench_state_update", clear_all_residual_points=True, user_words=words)


@pytest.mark.parametrize("words", [None, "", "   "])
async def test_a_bulk_clear_needs_the_users_words(bench: Bench, words: str | None) -> None:
    async with Client(bench.server) as client:
        message = await refused_bulk_clear(client, words)

    assert "needs `user_words`: the user's own sentence, quoted exactly" in message


async def test_a_bulk_clear_needs_a_safe_reading_after_the_latest_confirmation(bench: Bench) -> None:
    async with Client(bench.server) as client:
        await measure(client, bench, meter("dc_voltage", "V", 12.0, "12.00"), "PP12V")
        not_confirmed = await refused_bulk_clear(client)
        await measure(client, bench, meter("dc_voltage", "V", 0.01, "0.01"), "VBUS")
        await isolate(client)
        before_the_confirmation = await refused_bulk_clear(client)
        state = await call(client, "bench_state")

    assert "the user has not confirmed the isolation" in not_confirmed
    # The safe VBUS reading is older than the confirmation.
    assert "no safe DC reading after the latest isolation confirmation" in before_the_confirmation
    assert state["state"]["residual_clearances"] == []
    assert [point["safe"] for point in state["state"]["residual_points"]] == [False, True]


async def test_a_bulk_clear_clears_the_older_points_and_keeps_a_newer_unsafe_one(bench: Bench) -> None:
    async with Client(bench.server) as client:
        await measure(client, bench, meter("dc_voltage", "V", 12.0, "12.00"), "PP12V")
        await measure(client, bench, meter("dc_voltage", "V", 5.1, "5.10"), "C12.1")
        await isolate(client)
        await measure(client, bench, meter("dc_voltage", "V", 0.01, "0.01"), "VBUS")
        await measure(client, bench, meter("dc_voltage", "V", 5.1, "5.10"), "L1.1")
        cleared = await bulk_clear(client)

    points = {point["point"]: point for point in cleared["state"]["residual_points"]}
    assert (points["pp12v"]["safe"], points["c12.1"]["safe"], points["l1.1"]["safe"]) == (True, True, False)
    assert (points["pp12v"]["source_id"], points["pp12v"]["user_reason"]) == (None, USER_WORDS)
    record = cleared["state"]["residual_clearances"][-1]
    # The user's words are stored as given, with the time.
    assert (record["point"], record["reason"], record["cleared_points"]) == ("all", USER_WORDS, ["pp12v", "c12.1"])
    assert record["cleared_at"]
    assert cleared["notice"] == (
        "cleared 2 residual points (older than the safe reading at 'VBUS'); kept 1 with a newer unsafe reading: 'L1.1'"
    )
    assert cleared["gate"]["unpowered_tests_allowed"] is False


async def test_a_bulk_clear_opens_the_gate_after_a_safe_reading(bench: Bench) -> None:
    async with Client(bench.server) as client:
        await measure(client, bench, meter("dc_voltage", "V", 12.0, "12.00"), "PP12V")
        await measure(client, bench, meter("dc_voltage", "V", 19.5, "19.50"), "VBUS")
        await isolate(client)
        closed = await measure(client, bench, meter("dc_voltage", "V", 0.02, "0.02"), "C12.1")
        opened = await bulk_clear(client)
        await isolate(client)
        new_confirmation = await call(client, "bench_state")

    assert closed["gate"]["unpowered_tests_allowed"] is False
    assert opened["gate"]["unpowered_tests_allowed"] is True
    # A clearance is no reading: after a new confirmation, the gate needs a new safe reading.
    assert new_confirmation["gate"]["missing"] == [
        "no safe residual-voltage measurement (multimeter_read in DC voltage mode) after the confirmation"
    ]


async def test_a_bulk_clear_refuses_when_every_unsafe_point_is_newer(bench: Bench) -> None:
    async with Client(bench.server) as client:
        await isolate(client)
        await measure(client, bench, meter("dc_voltage", "V", 0.01, "0.01"), "VBUS")
        await measure(client, bench, meter("dc_voltage", "V", 5.1, "5.10"), "C12.1")
        message = await refused_bulk_clear(client)

    assert "every unsafe point ('C12.1') is newer than the safe reading at 'VBUS'" in message


async def test_a_bulk_clear_also_clears_an_unknown_point(settings: Settings) -> None:
    services = make_services(settings, FakePhone(), answers({**READING, "display_text": "12.00", "value": 12.0}))
    async with Client(build_server(services)) as client:
        read = await call(client, "multimeter_read")
        await isolate(client)
        safe = meter("dc_voltage", "V", 0.01, "0.01")
        assert safe.capture_id is not None
        services.captures.meter_results[safe.capture_id] = safe
        await call(client, "bench_record_measurement", capture_id=safe.capture_id, label="VBUS")
        cleared = await bulk_clear(client)

    assert cleared["state"]["residual_clearances"][-1]["cleared_points"] == [f"unknown point {read['capture_id']}"]
    assert cleared["gate"]["unpowered_tests_allowed"] is True


def test_a_ground_point_is_never_the_safe_reading_of_a_bulk_clear() -> None:
    # A record from before the ground rule can hold a GND point.
    confirmed = datetime.now(UTC)
    state = BenchState(
        power=PowerRecord(state=PowerState.ISOLATED, user_confirmed_isolation=True, confirmed_at=confirmed),
        residual_points=[
            ResidualPoint(point="c12.1", label="C12.1", at=confirmed, safe=False, reading="5.10 V", source_id="a"),
            ResidualPoint(
                point="gnd",
                label="GND",
                at=confirmed + timedelta(seconds=1),
                safe=True,
                reading="0.00 V",
                source_id="b",
            ),
        ],
    )
    with pytest.raises(ToolError, match="no safe DC reading after the latest isolation confirmation"):
        clear_all_points(state, USER_WORDS)


# endregion


# region: QA round 10 (N13 rest, N24, N25, N26, and the N16 and N17 gaps)


@pytest.mark.parametrize(
    ("display_text", "mode", "volts"),
    [
        ("5.10", MeterMode.DIODE, 5.1),  # above the diode test voltage: the dial is probably on DC V
        ("0.60", MeterMode.DIODE, None),  # a diode drop
        ("2.95", MeterMode.DIODE, None),
        ("OL", MeterMode.DIODE, None),  # an open diode
        ("5.10", MeterMode.OTHER, 5.1),  # the model could not tell the mode
    ],
)
def test_the_gate_voltage_of_diode_and_other_readings(display_text: str, mode: MeterMode, volts: float | None) -> None:
    unit = "" if mode is MeterMode.OTHER else "V"
    voltage = lcd_voltage(display_text, unit, mode)
    assert (voltage.volts if voltage else None) == (pytest.approx(volts) if volts is not None else None)
    assert lcd_voltage("2.50", "V", MeterMode.DIODE, max_diode_volts=2.0) is not None


def test_each_frame_uses_its_own_mode() -> None:
    diode = frame("0.60", 0.60, mode="diode")
    volts = frame("5.10", 5.10, mode="dc_voltage")
    result = combine([diode, volts], LIMITS)
    assert result.mode is MeterMode.DIODE
    voltage = result_voltage(result)
    assert voltage is not None
    assert voltage.text == "5.10 V"


async def test_a_diode_reading_above_the_limit_closes_the_gate(settings: Settings) -> None:
    services = make_services(settings, FakePhone(), answers({**DIODE_VOLTS}))
    async with Client(build_server(services)) as client:
        read = await call(client, "multimeter_read")
        state = await call(client, "bench_state")

    assert read["status"] == "uncertain"
    assert "5.10 V is above the safe residual limit" in read["bench_notice"]
    assert state["state"]["residual_points"][0]["point"] == f"unknown point {read['capture_id']}"


async def test_the_diode_limit_comes_from_the_setting() -> None:
    store = BenchStateStore(max_diode_volts=6.0)
    diode = combine([frame("5.10", 5.10, mode="diode")] * 2, LIMITS.model_copy(update={"max_diode_volts": 6.0}))
    assert await note_meter_reading(store, diode) is None
    assert store.load().residual_points == []


async def test_a_point_kept_by_the_bulk_clear_is_not_lost_when_its_capture_gets_a_name(bench: Bench) -> None:
    # N24: the 5.10 V reading at C12.1 comes after the anchor, so the bulk clear keeps it (as an unknown point).
    async with Client(bench.server) as client:
        await measure(client, bench, meter("dc_voltage", "V", 12.0, "12.00"), "C12.1")
        await isolate(client)
        await measure(client, bench, meter("dc_voltage", "V", 0.01, "0.01"), "VBUS")
        late = meter("dc_voltage", "V", 5.1, "5.10", confidence=0.3)
        await note_meter_reading(bench.services.bench, late)
        cleared = await bulk_clear(client)
        named = await refused(client, "bench_record_measurement", capture_id=bench.add_meter(late), label="C12.1")
        await isolate(client)
        after = await measure(client, bench, meter("dc_voltage", "V", 0.01, "0.01"), "VBUS")

    assert cleared["state"]["residual_clearances"][-1]["cleared_points"] == ["c12.1"]
    assert "the bench safety gate is closed at 'C12.1'" in named
    points = {point["point"]: point for point in after["state"]["residual_points"]}
    assert points["c12.1"]["safe"] is False
    assert not any(key.startswith("unknown point") for key in points)
    assert after["gate"]["unpowered_tests_allowed"] is False


async def test_a_meter_read_during_a_slow_record_is_not_lost(bench: Bench) -> None:
    # N25: bench_record_measurement waits for the board; multimeter_read writes an unsafe point meanwhile.
    started, go = asyncio.Event(), asyncio.Event()

    async def slow_restore() -> None:
        started.set()
        await go.wait()

    bench.services.board.restore = slow_restore  # type: ignore[method-assign]
    async with Client(bench.server) as client:
        await isolate(client)
        safe = bench.add_meter(meter("dc_voltage", "V", 0.01, "0.01"))
        record = asyncio.create_task(call(client, "bench_record_measurement", capture_id=safe, label="VBUS"))
        await started.wait()
        unsafe = meter("dc_voltage", "V", 5.1, "5.10")
        notice = await note_meter_reading(bench.services.bench, unsafe)
        go.set()
        recorded = await record

    assert notice is not None
    assert [point["point"] for point in recorded["state"]["residual_points"]] == [
        f"unknown point {unsafe.capture_id}",
        "vbus",
    ]
    assert recorded["gate"]["unpowered_tests_allowed"] is False


async def test_parallel_writers_on_the_file_lose_nothing(tmp_path: Path) -> None:
    store = BenchStateStore(tmp_path / "bench-state.json")
    ids = [f"photo-{index}" for index in range(40)]
    unsafe = meter("dc_voltage", "V", 5.1, "5.10")
    await asyncio.gather(
        *(asyncio.to_thread(add_photo, store, capture_id) for capture_id in ids),
        note_meter_reading(store, unsafe),
    )
    state = store.load()
    assert sorted(state.photo_ids) == sorted(ids)
    assert [point.point for point in state.residual_points] == [f"unknown point {unsafe.capture_id}"]


async def test_a_later_unsafe_reading_reopens_the_power_check(bench: Bench) -> None:
    # N26
    async with Client(bench.server) as client:
        state = await call(client, "bench_probe_short")
        check_id = state["next_step"]["step_id"]
        await isolate(client)
        done = await measure(client, bench, meter("dc_voltage", "V", 0.01, "0.01"), "VBUS", step_id=check_id)
        reopened = await measure(client, bench, meter("dc_voltage", "V", 5.1, "5.10"), "C12.1")
        await isolate(client)
        await measure(client, bench, meter("dc_voltage", "V", 0.01, "0.01"), "C12.1")
        again = await measure(client, bench, meter("dc_voltage", "V", 0.01, "0.01"), "VBUS")

    assert done["next_step"] is None
    check = reopened["state"]["steps"][0]
    assert (check["done"], check["evidence_id"]) == (False, None)
    assert check["reason"] == "reopened: the unsafe reading 5.10 V at 'C12.1' closed the safety gate"
    assert reopened["next_step"]["step_id"] == check_id
    assert again["state"]["steps"][0]["done"] is True
    assert again["state"]["steps"][0]["reason"] is None


@pytest.mark.parametrize("label", ["GROUND", "Ground", "GRND", "0 V", "chassis_gnd", "SIG_GROUND"])
def test_more_ground_names_are_refused(label: str) -> None:
    with pytest.raises(PointNameError, match="ground net"):
        point_name(label, None)


def test_the_gate_ignores_an_old_ground_point() -> None:
    confirmed = datetime.now(UTC)
    state = BenchState(
        power=PowerRecord(state=PowerState.ISOLATED, user_confirmed_isolation=True, confirmed_at=confirmed),
        residual_points=[
            ResidualPoint(point="gnd", label="GND", at=confirmed, safe=True, reading="0.00 V", source_id="a")
        ],
    )
    assert gate(state).missing == [
        "no safe residual-voltage measurement (multimeter_read in DC voltage mode) after the confirmation"
    ]


async def test_the_refusal_asks_for_a_step_that_fits(bench: Bench) -> None:
    # N17 wording: a DC V reading with a resistance step.
    async with Client(bench.server) as client:
        state = await call(client, "bench_state_update", add_steps=[{"text": "Rail to GND", "kind": "resistance"}])
        await isolate(client)
        volts = bench.add_meter(meter("dc_voltage", "V", 0.01, "0.01"))
        refusal = await refused(
            client, "bench_record_measurement", capture_id=volts, label="VBUS", step_id=state["next_step"]["step_id"]
        )

    assert "is a resistance step, but this measurement is dc_voltage" in refusal
    assert "a step that fits this reading (a voltage or power-check step)" in refusal


# endregion


# region: QA round 11 (N32: a user-confirmed diode mode; the N33 tests are the journal tests of N80)


@pytest.mark.parametrize(
    ("display_text", "user_diode", "model_volt_mode", "note"),
    [
        ("2.50", True, MeterMode.DC_VOLTAGE, "the model read DC V symbols"),
        ("2.50", True, None, "above a typical diode drop (1 V)"),
        ("0.62", True, MeterMode.DC_VOLTAGE, "the model read DC V symbols"),
        ("0.62", True, MeterMode.AC_VOLTAGE, "the model read AC V symbols"),
        ("0.62", True, None, None),  # a diode drop that the user confirmed
        ("2.50", False, None, None),  # an LED test from the LCD
        ("5.10", False, None, "above the diode test voltage of 3 V"),
    ],
)
def test_a_user_confirmed_diode_mode_does_not_hide_a_voltage(
    display_text: str, user_diode: bool, model_volt_mode: MeterMode | None, note: str | None
) -> None:
    context = DiodeContext(user_diode=user_diode, model_volt_mode=model_volt_mode)
    voltage = diode_voltage(display_text, "V", MeterMode.DIODE, context)
    if note is None:
        assert voltage is None
    else:
        assert voltage is not None
        assert voltage.note is not None
        assert note in voltage.note


async def test_the_gate_notice_explains_the_diode_rule(settings: Settings) -> None:
    dc_volts = {**READING, "mode": "dc_voltage", "unit": "V", "display_text": "2.50", "value": 2.5}
    services = make_services(settings, FakePhone(), answers(dc_volts))
    async with Client(build_server(services)) as client:
        await confirm_mode(client, "diode")
        read = await call(client, "multimeter_read")
        state = await call(client, "bench_state")

    assert (read["status"], read["mode"], read["model_mode"]) == ("confirmed", "diode", "dc_voltage")
    assert "2.50 V is above the safe residual limit" in read["bench_notice"]
    assert "the user confirmed diode mode, but the model read DC V symbols" in read["bench_notice"]
    assert state["gate"]["unpowered_tests_allowed"] is False


@pytest.fixture
def short_lock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bench_state, "LOCK_TIMEOUT", timedelta(milliseconds=50))


@contextlib.contextmanager
def other_writer(path: Path) -> Iterator[None]:
    """Another process holds the lock file of the record."""
    with path.with_name(f"{path.name}{bench_state.LOCK_SUFFIX}").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


# endregion


# region: QA round 13 (N32 gaps)


async def read_with_user_diode(settings: Settings, *answers_: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    services = make_services(settings, FakePhone(), answers(*answers_))
    async with Client(build_server(services)) as client:
        await confirm_mode(client, "diode")
        read = await call(client, "multimeter_read")
        state = await call(client, "bench_state")
    return read, state


async def test_the_model_mode_of_every_frame_counts(settings: Settings) -> None:
    # N32 (a): frame 1 is a diode drop for the model, frame 2 shows DC V symbols.
    drop = {**READING, "mode": "diode", "unit": "V", "display_text": "0.62", "value": 0.62}
    read, state = await read_with_user_diode(settings, drop, {**drop, "mode": "dc_voltage"})

    assert [frame["model_mode"] for frame in read["frames"]] == ["diode", "dc_voltage"]
    assert "the model read DC V symbols" in read["bench_notice"]
    assert state["state"]["residual_points"][0]["safe"] is False


async def test_an_unreadable_unit_with_a_user_diode_mode_counts(settings: Settings) -> None:
    # N32 (b): no unit symbol, the model reads DC V "5.10" in both frames.
    no_unit = {**READING, "mode": "dc_voltage", "unit": "unknown", "display_text": "5.10", "value": 5.1}
    read, state = await read_with_user_diode(settings, no_unit)

    assert (read["status"], read["mode"]) == ("uncertain", "diode")
    assert "5.10 (unit not readable, counted as V) is above the safe residual limit" in read["bench_notice"]
    assert state["gate"]["unpowered_tests_allowed"] is False


def test_an_unreadable_unit_in_diode_mode_counts_above_the_diode_limit() -> None:
    # N32 (b) without a user mode: the diode rule with the number as volts.
    assert lcd_voltage("5.10", "", MeterMode.DIODE) is not None
    assert lcd_voltage("0.62", "", MeterMode.DIODE) is None


# endregion
