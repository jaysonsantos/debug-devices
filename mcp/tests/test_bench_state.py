"""The local bench record (bench_state.py). Report: "P2: Keep a compact bench state"."""

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from mcp import Client
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import TextContent

from debug_devices_mcp.bench_state import (
    SAFE_RESIDUAL_VOLTS,
    USER_MODE_MAX_AGE,
    BenchState,
    BenchStateStore,
    Measurement,
    StepKind,
    gate,
    is_safe,
    recent_user_mode,
    residual_volts,
)
from debug_devices_mcp.config import Settings
from debug_devices_mcp.constants import REPO_ROOT
from debug_devices_mcp.multimeter import MeterMode, MeterResult, MeterStatus, MultimeterReading, check_reading
from debug_devices_mcp.server import Services, build_server

from .test_meter_frames import DIODE_VOLTS, answers, confirm_mode
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
        await call(client, "bench_record_measurement", capture_id=residual, label="residual voltage")
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
    assert [item["label"] for item in done["state"]["measurements"]] == ["residual voltage", "rail"]
    assert done["state"]["measurements"][1]["confidence_state"] == "confirmed"


async def test_unsafe_residual_voltage_blocks(bench: Bench) -> None:
    async with Client(bench.server) as client:
        state = await call(client, "bench_state_update", add_steps=[{"text": "Diode test", "kind": "diode"}])
        await call(client, "bench_state_update", power="isolated", user_confirmed_isolation=True)
        high = bench.add_meter(meter("dc_voltage", "V", 5.1, "5.10"))
        after = await call(client, "bench_record_measurement", capture_id=high, label="residual voltage")
        message = await refused(client, "bench_begin_step", step_id=state["next_step"]["step_id"])

    assert after["gate"]["unpowered_tests_allowed"] is False
    assert "not safe" in message


async def test_residual_before_the_confirmation_does_not_count(bench: Bench) -> None:
    early = bench.add_meter(meter("dc_voltage", "V", 0.01, "0.01"))
    async with Client(bench.server) as client:
        await call(client, "bench_state_update", power="isolated", user_confirmed_isolation=True)
        state = await call(client, "bench_record_measurement", capture_id=early, label="residual voltage")

    assert state["state"]["power"]["residual"] is None
    assert state["gate"]["unpowered_tests_allowed"] is False


async def test_probe_short_goes_back_to_the_power_check(bench: Bench) -> None:
    async with Client(bench.server) as client:
        await call(client, "bench_state_update", add_steps=[{"text": "Continuity", "kind": "continuity"}])
        await call(client, "bench_state_update", power="isolated", user_confirmed_isolation=True)
        residual = bench.add_meter(meter("dc_voltage", "V", 0.0, "0.00"))
        await call(client, "bench_record_measurement", capture_id=residual, label="residual voltage")
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
        state = await call(client, "bench_record_measurement", capture_id=residual, label="residual voltage")

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
        state = await call(client, "bench_record_measurement", capture_id=residual, label="residual voltage")

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
        await measure(client, bench, meter("dc_voltage", "V", 5.1, "5.10"), "C12 positive side")
        other = await measure(client, bench, meter("dc_voltage", "V", 0.01, "0.01"), "VBUS")
        again = await measure(client, bench, meter("dc_voltage", "mV", 8.0, "8.0"), "  c12 POSITIVE  side")

    assert other["gate"]["unpowered_tests_allowed"] is False
    assert any("5.10 V at 'C12 positive side' is not safe" in item for item in other["gate"]["missing"])
    assert other["state"]["power"]["residual"]["label"] == "C12 positive side"
    assert other["next_step"]["kind"] == "power_check"
    # A new reading at the same point (the same label, any case and spaces) replaces the unsafe one.
    assert again["gate"]["unpowered_tests_allowed"] is True
    assert again["next_step"] is None
    assert [point["label"] for point in again["state"]["power"]["residual_points"]] == [
        "VBUS",
        "  c12 POSITIVE  side",
    ]


async def test_the_highest_unsafe_point_decides_and_a_new_confirmation_clears_the_points(bench: Bench) -> None:
    async with Client(bench.server) as client:
        await isolate(client)
        await measure(client, bench, meter("dc_voltage", "V", 5.1, "5.10"), "A")
        both = await measure(client, bench, meter("dc_voltage", "V", 12.0, "12.00"), "B")
        one = await measure(client, bench, meter("dc_voltage", "V", 0.0, "0.00"), "B")
        await isolate(client)
        cleared = await call(client, "bench_state")

    assert both["state"]["power"]["residual"]["label"] == "B"
    assert len([item for item in both["gate"]["missing"] if "not safe" in item]) == 2
    assert one["state"]["power"]["residual"]["label"] == "A"
    assert cleared["state"]["power"]["residual_points"] == []
    assert any("no residual-voltage measurement" in item for item in cleared["gate"]["missing"])


async def test_a_power_check_step_needs_a_safe_residual_after_the_confirmation(bench: Bench) -> None:
    async with Client(bench.server) as client:
        state = await call(client, "bench_probe_short")
        check_id = state["next_step"]["step_id"]
        before = await measure(client, bench, meter("dc_voltage", "V", 0.01, "0.01"), "VBUS", step_id=check_id)
        await isolate(client)
        unsafe = await measure(client, bench, meter("dc_voltage", "V", 5.1, "5.10"), "VBUS", step_id=check_id)
        safe = await measure(client, bench, meter("dc_voltage", "V", 0.02, "0.02"), "VBUS", step_id=check_id)

    assert before["next_step"]["step_id"] == check_id
    assert before["notice"].startswith(f"step {check_id} stays open: a power check needs a voltage reading after")
    assert unsafe["next_step"]["step_id"] == check_id
    assert "5.10 V at 'VBUS' is not safe" in unsafe["notice"]
    # The unsafe reading is in the record and blocks the gate.
    assert unsafe["state"]["measurements"][-1]["display_text"] == "5.10"
    assert unsafe["gate"]["unpowered_tests_allowed"] is False
    assert safe["notice"] is None
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
