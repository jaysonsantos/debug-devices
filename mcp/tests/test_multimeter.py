import base64
import json
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr

from debug_devices_mcp.multimeter import (
    MeterMode,
    MissingApiKeyError,
    MultimeterReading,
    OutputTruncatedError,
    VisionClient,
    VisionError,
    build_request,
    parse_reading,
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
