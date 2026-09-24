from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from computer_use.discovery.engine import DiscoveryEngine
from computer_use.discovery.llm import LLMDecision, ScriptedLLMClient
from computer_use.domain.models import (
    ActionResult,
    Condition,
    ConditionResult,
    DiscoveryStatus,
    EvidenceRef,
    Observation,
    ReadResult,
    Target,
)
from computer_use.examples import lookup_goal, lookup_scripted_decisions
from computer_use.observability.evidence import EvidenceRecorder
from computer_use.safety.policy import PolicyEngine


class FakeSurface:
    def __init__(self) -> None:
        self.actions: list[str] = []

    async def observe(self) -> Observation:
        text = "synthetic member UI"
        return Observation(
            url="http://127.0.0.1:8765/members/search",
            title="Fixture",
            visible_text=text,
            digest=hashlib.sha256(text.encode()).hexdigest(),
        )

    async def current_url(self) -> str:
        return "http://127.0.0.1:8765/members/search"

    async def navigate(self, url: str, timeout_ms: int) -> ActionResult:
        del timeout_ms
        self.actions.append(f"navigate:{url}")
        return ActionResult(success=True, locator_strategy="url")

    async def click(self, target: Target, timeout_ms: int) -> ActionResult:
        del timeout_ms
        self.actions.append(f"click:{target.accessible_name}")
        return ActionResult(success=True, locator_strategy="role+accessible_name")

    async def type(self, target: Target, text: str, timeout_ms: int) -> ActionResult:
        del timeout_ms
        self.actions.append(f"type:{text}")
        return ActionResult(success=True, locator_strategy="role+accessible_name")

    async def read(self, target: Target, timeout_ms: int) -> ReadResult:
        del timeout_ms
        self.actions.append(f"read:{target.accessible_name}")
        return ReadResult(
            success=True,
            value="Current balance: $1234.56",
            locator_strategy="role+accessible_name",
        )

    async def wait_for(self, condition: Condition, timeout_ms: int) -> ConditionResult:
        del condition, timeout_ms
        return ConditionResult(matched=True, observed="fixture condition")

    async def screenshot(self, destination: Path) -> EvidenceRef:
        destination.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(destination.write_bytes, b"fixture-png")
        return EvidenceRef(path=str(destination), kind="screenshot")

    async def close(self) -> None:
        return None


class FailingProvider:
    provider = "failing-provider"
    model = "failing-model"

    def __init__(self) -> None:
        self.calls = 0

    async def decide(self, *, system_prompt: str, user_prompt: str) -> LLMDecision:
        del system_prompt, user_prompt
        self.calls += 1
        raise RuntimeError("provider temporarily unavailable")


@pytest.mark.asyncio
async def test_scripted_discovery_emits_structured_redacted_trace(tmp_path: Path) -> None:
    origin = "http://127.0.0.1:8765"
    recorder = EvidenceRecorder(tmp_path, "fixture", run_directory=tmp_path / "run")
    client = ScriptedLLMClient(lookup_scripted_decisions(origin))
    engine = DiscoveryEngine(
        client=client,
        policy=PolicyEngine.development(origin),
        recorder=recorder,
    )
    surface = FakeSurface()
    trace = await engine.run(lookup_goal(origin), surface)
    assert trace.final_status is DiscoveryStatus.SUCCESS
    assert len(trace.actions) == 5
    assert trace.declared_outputs == {"balance_text": "Current balance: $1234.56"}
    saved = (tmp_path / "run" / "discovery-trace.json").read_text()
    assert "12345" not in saved
    assert "***45" in saved
    assert client.calls == 6


@pytest.mark.asyncio
async def test_provider_failure_retries_and_persists_escalated_trace(tmp_path: Path) -> None:
    origin = "http://127.0.0.1:8765"
    recorder = EvidenceRecorder(tmp_path, "fixture", run_directory=tmp_path / "run")
    client = FailingProvider()
    engine = DiscoveryEngine(
        client=client,
        policy=PolicyEngine.development(origin),
        recorder=recorder,
        provider_attempts=3,
        provider_backoff_seconds=0,
    )

    trace = await engine.run(lookup_goal(origin), FakeSurface())

    assert trace.final_status is DiscoveryStatus.ESCALATED
    assert client.calls == 3
    assert (tmp_path / "run" / "discovery-trace.json").exists()
    retry_events = [
        event
        for event in recorder.read_events()
        if event["event_type"] == "discovery_provider_retry"
    ]
    assert len(retry_events) == 3
