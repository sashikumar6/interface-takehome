import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from computer_use.discovery.llm import (
    DecisionAction,
    LLMDecision,
    OpenAIResponsesClient,
    ScriptedLLMClient,
    _validate_provider_decision,
)
from computer_use.domain.models import Target


def click_decision() -> LLMDecision:
    return LLMDecision(
        action=DecisionAction.CLICK,
        target=Target(role="button", accessible_name="Search"),
        rationale="Submit the member lookup.",
    )


def test_invalid_provider_decision_shape_is_rejected() -> None:
    with pytest.raises(ValidationError):
        LLMDecision(action=DecisionAction.TYPE, rationale="missing target and input")


def test_provider_type_prefers_named_input_reference_over_duplicate_literal() -> None:
    payload = {
        "action": "type",
        "target": {
            "role": "textbox",
            "accessible_name": "Member ID",
            "text": None,
            "near_label": None,
            "frame_name": None,
            "frame_title": None,
        },
        "value": "[AVAILABLE_BY_REFERENCE]",
        "input_reference": "member_id",
        "reads_into": None,
        "rationale": "Use the supplied member identifier by reference.",
        "expected_postcondition": {
            "kind": "element_present",
            "target": {
                "role": "button",
                "accessible_name": "Search",
                "text": None,
                "near_label": None,
                "frame_name": None,
                "frame_title": None,
            },
            "pattern": "present",
        },
        "output_bindings": None,
        "success_condition": None,
        "escalation_reason": None,
    }
    decision = _validate_provider_decision(payload)
    assert decision.input_reference == "member_id"
    assert decision.value is None
    assert decision.expected_postcondition is not None
    assert decision.expected_postcondition.pattern is None


@pytest.mark.asyncio
async def test_openai_adapter_parses_stored_structured_fixture_without_secret_logging() -> None:
    decision = click_decision()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer unit-test-key"
        body = json.loads(request.content)
        assert body["store"] is False
        output_format = body["text"]["format"]
        assert output_format["strict"] is True
        schema = output_format["schema"]
        assert schema["required"] == list(schema["properties"])
        assert schema["additionalProperties"] is False
        for definition in schema["$defs"].values():
            if "properties" in definition:
                assert definition["required"] == list(definition["properties"])
                assert definition["additionalProperties"] is False
        target_properties = schema["$defs"]["Target"]["properties"]
        assert "structural_fallback" not in target_properties
        assert "structural_surface" not in target_properties
        return httpx.Response(
            200,
            json={
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": decision.model_dump_json()}],
                    }
                ]
            },
        )

    client = OpenAIResponsesClient(
        api_key=SecretStr("unit-test-key"),
        model="fixture-model",
        transport=httpx.MockTransport(handler),
    )
    parsed = await client.decide(system_prompt="system", user_prompt="user")
    assert parsed == decision


@pytest.mark.asyncio
async def test_scripted_client_is_explicitly_labeled_and_bounded() -> None:
    client = ScriptedLLMClient([click_decision()])
    assert (await client.decide(system_prompt="x", user_prompt="y")).action is DecisionAction.CLICK
    with pytest.raises(RuntimeError, match="exhausted"):
        await client.decide(system_prompt="x", user_prompt="y")
