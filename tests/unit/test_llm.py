import json

import httpx
import pytest
from pydantic import SecretStr, ValidationError

from computer_use.discovery.llm import (
    DecisionAction,
    LLMDecision,
    OpenAIResponsesClient,
    ScriptedLLMClient,
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


@pytest.mark.asyncio
async def test_openai_adapter_parses_stored_structured_fixture_without_secret_logging() -> None:
    decision = click_decision()

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer unit-test-key"
        body = json.loads(request.content)
        assert body["store"] is False
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
