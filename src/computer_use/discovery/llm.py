"""Provider-independent, structured one-decision-at-a-time LLM adapters."""

from __future__ import annotations

import json
from collections import deque
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from computer_use.domain.models import Condition, Target


class DecisionAction(StrEnum):
    """The complete action vocabulary exposed to a discovery model."""

    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    READ = "read"
    WAIT_FOR = "wait_for"
    DONE = "done"
    ESCALATE = "escalate"


class LLMDecision(BaseModel):
    """A validated decision; impossible action/field combinations are rejected."""

    model_config = ConfigDict(extra="forbid", strict=True)

    action: DecisionAction
    target: Target | None = None
    value: str | None = None
    input_reference: str | None = None
    reads_into: str | None = None
    rationale: str = Field(min_length=1, max_length=300)
    expected_postcondition: Condition | None = None
    output_bindings: dict[str, str] | None = None
    success_condition: Condition | None = None
    escalation_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_action_shape(self) -> LLMDecision:
        if (
            self.action in {DecisionAction.CLICK, DecisionAction.TYPE, DecisionAction.READ}
            and self.target is None
        ):
            raise ValueError(f"{self.action.value} requires target")
        if self.action is DecisionAction.TYPE:
            if (self.value is None) == (self.input_reference is None):
                raise ValueError("type requires exactly one of value or input_reference")
        elif (
            self.value is not None or self.input_reference is not None
        ) and self.action is not DecisionAction.NAVIGATE:
            raise ValueError("value/input_reference are only legal for navigate or type")
        if self.action is DecisionAction.NAVIGATE and self.value is None:
            raise ValueError("navigate requires value")
        if self.action is DecisionAction.READ and not self.reads_into:
            raise ValueError("read requires reads_into")
        if self.action is DecisionAction.WAIT_FOR and self.expected_postcondition is None:
            raise ValueError("wait_for requires expected_postcondition")
        if self.action is DecisionAction.DONE:
            if self.output_bindings is None or self.success_condition is None:
                raise ValueError("done requires output_bindings and success_condition")
        elif self.output_bindings is not None or self.success_condition is not None:
            raise ValueError("output_bindings/success_condition are legal only for done")
        if self.action is DecisionAction.ESCALATE and not self.escalation_reason:
            raise ValueError("escalate requires escalation_reason")
        if self.action is not DecisionAction.ESCALATE and self.escalation_reason is not None:
            raise ValueError("escalation_reason is legal only for escalate")
        return self


class LLMClient(Protocol):
    """One-decision provider boundary used only by discovery."""

    provider: str
    model: str

    async def decide(self, *, system_prompt: str, user_prompt: str) -> LLMDecision: ...


def _output_text(payload: Mapping[str, Any]) -> str:
    """Extract Responses API text without assuming output array ordering."""

    direct = payload.get("output_text")
    if isinstance(direct, str) and direct:
        return direct
    for item in payload.get("output", []):
        if not isinstance(item, Mapping):
            continue
        for content in item.get("content", []):
            if isinstance(content, Mapping) and content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str):
                    return text
    raise ValueError("provider response contained no output text")


class OpenAIResponsesClient:
    """Minimal OpenAI Responses adapter using JSON-schema formatted output."""

    provider = "openai"

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model: str,
        timeout_seconds: float = 45.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._timeout = timeout_seconds
        self._transport = transport

    async def decide(self, *, system_prompt: str, user_prompt: str) -> LLMDecision:
        schema = LLMDecision.model_json_schema()
        request = {
            "model": self.model,
            "instructions": system_prompt,
            "input": user_prompt,
            "temperature": 0,
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "ui_discovery_decision",
                    "description": "Exactly one constrained UI discovery action",
                    "strict": False,
                    "schema": schema,
                }
            },
        }
        headers = {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(
            timeout=self._timeout,
            transport=self._transport,
        ) as client:
            response = await client.post(
                "https://api.openai.com/v1/responses", json=request, headers=headers
            )
            response.raise_for_status()
            return LLMDecision.model_validate_json(_output_text(response.json()))


class AnthropicMessagesClient:
    """Minimal Anthropic Messages adapter forcing one typed decision tool call."""

    provider = "anthropic"

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model: str,
        timeout_seconds: float = 45.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self.model = model
        self._timeout = timeout_seconds
        self._transport = transport

    async def decide(self, *, system_prompt: str, user_prompt: str) -> LLMDecision:
        request = {
            "model": self.model,
            "max_tokens": 1_024,
            "temperature": 0,
            "system": system_prompt,
            "messages": [{"role": "user", "content": user_prompt}],
            "tools": [
                {
                    "name": "choose_ui_action",
                    "description": "Return exactly one constrained UI action",
                    "input_schema": LLMDecision.model_json_schema(),
                }
            ],
            "tool_choice": {"type": "tool", "name": "choose_ui_action"},
        }
        headers = {
            "x-api-key": self._api_key.get_secret_value(),
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(
            timeout=self._timeout,
            transport=self._transport,
        ) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages", json=request, headers=headers
            )
            response.raise_for_status()
            payload = response.json()
        for item in payload.get("content", []):
            if isinstance(item, Mapping) and item.get("type") == "tool_use":
                return LLMDecision.model_validate_json(json.dumps(item.get("input")))
        raise ValueError("provider response contained no decision tool call")


class ScriptedLLMClient:
    """Deterministic client for tests; never qualifies as genuine discovery evidence."""

    provider = "scripted-test-fixture"
    model = "scripted-test-fixture-v1"

    def __init__(self, decisions: Sequence[LLMDecision | Mapping[str, Any]]) -> None:
        self._decisions: deque[LLMDecision] = deque(
            decision if isinstance(decision, LLMDecision) else LLMDecision.model_validate(decision)
            for decision in decisions
        )
        self.calls = 0

    async def decide(self, *, system_prompt: str, user_prompt: str) -> LLMDecision:
        del system_prompt, user_prompt
        self.calls += 1
        if not self._decisions:
            raise RuntimeError("scripted discovery exhausted its decisions")
        return self._decisions.popleft()


class LLMClientTrap:
    """Test double that proves replay never reaches the provider boundary."""

    provider = "trap"
    model = "trap"

    async def decide(self, *, system_prompt: str, user_prompt: str) -> LLMDecision:
        del system_prompt, user_prompt
        raise AssertionError("replay attempted to call an LLM client")


def decision_fixture_json(decision: LLMDecision) -> str:
    """Return stable JSON used by stored non-secret provider parsing fixtures."""

    return json.dumps(decision.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
