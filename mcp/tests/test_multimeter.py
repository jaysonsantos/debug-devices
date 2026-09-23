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
    VisionClient,
    VisionError,
    parse_reading,
    reading_json_schema,
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
    schema_format = body["response_format"]["json_schema"]
    assert schema_format["strict"] is True
    assert set(schema_format["schema"]["required"]) == set(MultimeterReading.model_fields)
    assert schema_format["schema"]["additionalProperties"] is False
    image = body["messages"][1]["content"][1]
    assert image["type"] == "image_url"
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


def test_schema_has_no_refs_for_strict_mode() -> None:
    schema = reading_json_schema()

    assert "$ref" not in json.dumps(schema)
    assert "$defs" not in schema
    mode = schema["properties"]["mode"]
    assert mode["type"] == "string"
    assert mode["enum"] == [member.value for member in MeterMode]
    assert "description" in mode
