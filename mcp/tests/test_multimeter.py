import base64
import json
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr

from debug_devices_mcp.multimeter import (
    MeterMode,
    MeterStatus,
    MissingApiKeyError,
    MultimeterReading,
    OutputTruncatedError,
    UnitFamily,
    VisionClient,
    VisionError,
    build_request,
    check_reading,
    parse_reading,
    unit_family,
)

from .conftest import JPEG

READING = {
    "readable": True,
    "value": 4.98,
    "unit": "V",
    "display_text": "4.98",
    "mode": "dc_voltage",
    "range": "20V",
    "flags": ["AUTO"],
    "confidence": 0.93,
    "notes": "",
}


def completion(content: str) -> httpx.Response:
    return httpx.Response(
        200, json={"id": "gen-1", "choices": [{"message": {"role": "assistant", "content": content}}]}
    )


def vision(handler: httpx.MockTransport, key: str | None = "sk-test") -> VisionClient:
    return VisionClient(
        SecretStr(key) if key is not None else None,
        "openai/gpt-6-luna",
        "https://openrouter.test/api/v1",
        timedelta(seconds=1),
        transport=handler,
    )


async def test_request_shape_and_reading() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return completion(json.dumps(READING))

    reading = await vision(httpx.MockTransport(handler)).read_multimeter(JPEG)

    assert reading.mode == MeterMode.DC_VOLTAGE
    assert reading.value == 4.98
    request = seen[0]
    assert request.url == "https://openrouter.test/api/v1/chat/completions"
    assert request.headers["authorization"] == "Bearer sk-test"
    body = json.loads(request.content)
    assert body["model"] == "openai/gpt-6-luna"
    assert body["response_format"]["type"] == "json_schema"
    assert body["provider"] == {"require_parameters": True}
    assert body["reasoning"] == {"effort": "low"}
    assert body["max_tokens"] == 4000
    assert not {"temperature", "top_p", "stop"} & set(body)
    schema_format = body["response_format"]["json_schema"]
    assert schema_format["strict"] is True
    assert set(schema_format["schema"]["required"]) == set(MultimeterReading.model_fields)
    assert schema_format["schema"]["additionalProperties"] is False
    image = body["messages"][1]["content"][1]
    assert image["type"] == "image_url"
    assert image["image_url"]["detail"] == "high"
    assert body["messages"][1]["content"][0]["text"] == "Read this multimeter."
    assert image["image_url"]["url"] == "data:image/jpeg;base64," + base64.b64encode(JPEG).decode()


async def test_retries_once_on_invalid_json() -> None:
    answers = iter(["not json", json.dumps(READING)])
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return completion(next(answers))

    reading = await vision(httpx.MockTransport(handler)).read_multimeter(JPEG)

    assert reading.display_text == "4.98"
    assert len(calls) == 2


async def test_gives_up_after_two_invalid_answers() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return completion('{"readable": true}')

    with pytest.raises(VisionError, match="after 2 attempts"):
        await vision(httpx.MockTransport(handler)).read_multimeter(JPEG)
    assert len(calls) == 2


async def test_missing_key_is_clear() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("no request without a key")

    with pytest.raises(MissingApiKeyError, match="OPENROUTER_API_KEY"):
        await vision(httpx.MockTransport(handler), key=None).read_multimeter(JPEG)


async def test_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"error": {"message": "Insufficient credits"}})

    with pytest.raises(VisionError, match="402"):
        await vision(httpx.MockTransport(handler)).read_multimeter(JPEG)


def test_parse_reading_accepts_code_fence() -> None:
    reading = parse_reading("```json\n" + json.dumps({**READING, "value": None, "display_text": "OL"}) + "\n```")
    assert reading.value is None
    assert reading.display_text == "OL"


def assert_strict(node: object, path: str = "$") -> None:
    """Fail on anything that OpenAI strict structured outputs refuses."""
    if isinstance(node, list):
        for index, item in enumerate(node):
            assert_strict(item, f"{path}[{index}]")
        return
    if not isinstance(node, dict):
        return
    assert "$ref" not in node, f"$ref at {path}"
    assert "$defs" not in node, f"$defs at {path}"
    assert "anyOf" not in node, f"anyOf at {path}; use type: [x, null]"
    if node.get("type") == "object":
        assert node.get("additionalProperties") is False, f"additionalProperties at {path}"
        assert set(node.get("required", [])) == set(node.get("properties", {})), f"required keys at {path}"
    for key, value in node.items():
        assert_strict(value, f"{path}.{key}")


def sent_schema() -> dict[str, object]:
    body = json.loads(build_request("m", JPEG).model_dump_json(by_alias=True, exclude_none=True))
    return body["response_format"]["json_schema"]["schema"]


def test_sent_schema_is_strict() -> None:
    schema = sent_schema()

    assert_strict(schema)
    assert set(schema["required"]) == set(MultimeterReading.model_fields)
    assert schema["properties"]["mode"]["enum"] == [member.value for member in MeterMode]
    assert schema["properties"]["value"]["type"] == ["number", "null"]
    assert schema["properties"]["range"]["type"] == ["string", "null"]


def test_strict_walker_catches_violations() -> None:
    bad_schemas = [
        {
            "type": "object",
            "properties": {"a": {"$ref": "#/$defs/A"}},
            "required": ["a"],
            "additionalProperties": False,
        },
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": [], "additionalProperties": False},
        {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a"]},
        {"$defs": {}, "type": "string"},
    ]
    for bad in bad_schemas:
        with pytest.raises(AssertionError):
            assert_strict(bad)


async def test_length_stop_is_clear_and_not_retried() -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        choice = {"finish_reason": "length", "message": {"role": "assistant", "content": None}}
        return httpx.Response(200, text="\n\n   \n" + json.dumps({"choices": [choice]}))

    with pytest.raises(OutputTruncatedError, match=r"token limit \(max_tokens=4000, reasoning effort low\)"):
        await vision(httpx.MockTransport(handler)).read_multimeter(JPEG)
    assert len(calls) == 1


async def test_error_preview_skips_leading_white_space() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, text=" " * 600 + '{"error": {"message": "upstream"}}')

    with pytest.raises(VisionError, match="upstream"):
        await vision(httpx.MockTransport(handler)).read_multimeter(JPEG)


def test_prompt_puts_the_lcd_before_the_dial() -> None:
    body = json.loads(build_request("m", JPEG).model_dump_json(by_alias=True, exclude_none=True))
    system = body["messages"][0]["content"]
    assert "The unit symbol and the annunciators on the LCD decide the mode" in system
    assert "Do not take the mode from the dial alone" in system
    assert "confidence to 0.5 or lower" in system


async def test_meter_model_goes_into_the_prompt() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return completion(json.dumps(READING))

    client = vision(httpx.MockTransport(handler))
    client.meter_model = "PROSTER T21D"
    await client.read_multimeter(JPEG)

    text = json.loads(seen[0].content)["messages"][1]["content"][0]["text"]
    assert text == "Read this multimeter. The meter is a PROSTER T21D. Use what you know about its display and dial."


# region: meter check (the report's acceptance: clear Ω, kΩ, MΩ, V, OL; obscured symbols; 443 V in a resistance test)


def reading(**changes: object) -> MultimeterReading:
    return MultimeterReading.model_validate({**READING, **changes})


@pytest.mark.parametrize(
    ("unit", "family"),
    [
        ("Ω", UnitFamily.RESISTANCE),
        ("kΩ", UnitFamily.RESISTANCE),
        ("MΩ", UnitFamily.RESISTANCE),
        ("\u2126", UnitFamily.RESISTANCE),  # OHM SIGN
        ("k ohm", UnitFamily.RESISTANCE),
        ("mV", UnitFamily.VOLTAGE),
        ("V", UnitFamily.VOLTAGE),
        ("µA", UnitFamily.CURRENT),
        ("uA", UnitFamily.CURRENT),
        ("nF", UnitFamily.CAPACITANCE),
        ("kHz", UnitFamily.FREQUENCY),
        ("°C", UnitFamily.TEMPERATURE),
        ("%", UnitFamily.PERCENT),
        ("unknown", UnitFamily.UNKNOWN),
        ("", UnitFamily.UNKNOWN),
        ("kxV", UnitFamily.UNKNOWN),
    ],
)
def test_unit_family(unit: str, family: UnitFamily) -> None:
    assert unit_family(unit) is family


@pytest.mark.parametrize(
    ("unit", "display_text", "value"),
    [("Ω", "12.3", 12.3), ("kΩ", "443.0", 443.0), ("MΩ", "1.205", 1.205)],
)
def test_clear_resistance_is_confirmed(unit: str, display_text: str, value: float) -> None:
    result = check_reading(
        reading(mode="resistance", unit=unit, display_text=display_text, value=value, confidence=0.9),
        MeterMode.RESISTANCE,
    )
    assert result.status is MeterStatus.CONFIRMED
    assert result.value == value
    assert result.unit_family is UnitFamily.RESISTANCE
    assert result.display_text == display_text
    assert result.request is None


def test_clear_voltage_is_confirmed() -> None:
    result = check_reading(reading(), MeterMode.DC_VOLTAGE)
    assert result.status is MeterStatus.CONFIRMED
    assert result.value == 4.98


def test_overload_in_resistance_mode() -> None:
    result = check_reading(
        reading(mode="resistance", unit="MΩ", display_text="O.L", value=None, confidence=0.9), MeterMode.RESISTANCE
    )
    assert result.status is MeterStatus.CONFIRMED
    assert result.overload is True
    assert result.value is None
    assert result.display_text == "O.L"


def test_obscured_unit_symbol_gives_no_number() -> None:
    result = check_reading(
        reading(mode="resistance", unit="unknown", display_text="443", value=443.0, confidence=0.4),
        MeterMode.RESISTANCE,
    )
    assert result.status is MeterStatus.UNCERTAIN
    assert result.unit_family is UnitFamily.UNKNOWN
    assert result.value is None
    assert result.display_text == "443"
    assert result.request is not None
    assert "LCD symbols and the dial together" in result.request


def test_443_volts_during_a_resistance_test_is_disputed() -> None:
    # The model read volts (a consistent pair on its own), but the current test is a resistance test.
    result = check_reading(
        reading(mode="dc_voltage", unit="V", display_text="443", value=443.0, confidence=0.45), MeterMode.RESISTANCE
    )
    assert result.status is MeterStatus.DISPUTED
    assert result.value is None
    assert any("expects resistance" in problem for problem in result.problems)


def test_unit_that_does_not_fit_the_mode_is_disputed() -> None:
    result = check_reading(reading(mode="resistance", unit="V", display_text="443", value=443.0, confidence=0.95))
    assert result.status is MeterStatus.DISPUTED
    assert result.value is None


def test_expected_mode_never_confirms() -> None:
    # A low-confidence reading stays uncertain, also when it matches the expected mode.
    low = check_reading(reading(confidence=0.3), MeterMode.DC_VOLTAGE)
    assert low.status is MeterStatus.UNCERTAIN
    assert low.value is None
    # The same family with another mode (AC instead of DC) is not a confirmation either.
    other = check_reading(reading(confidence=0.95), MeterMode.AC_VOLTAGE)
    assert other.status is MeterStatus.UNCERTAIN


def test_unreadable_display() -> None:
    result = check_reading(reading(readable=False, value=None, confidence=0.1))
    assert result.status is MeterStatus.UNREADABLE
    assert result.value is None


# endregion
