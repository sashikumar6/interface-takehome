from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from computer_use.discovery.engine import DiscoveryEngine
from computer_use.discovery.llm import ScriptedLLMClient
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

    async def navigate(self, url: str) -> ActionResult:
        self.actions.append(f"navigate:{url}")
        return ActionResult(success=True, locator_strategy="url")

    async def click(self, target: Target) -> ActionResult:
        self.actions.append(f"click:{target.accessible_name}")
        return ActionResult(success=True, locator_strategy="role+accessible_name")

    async def type(self, target: Target, text: str) -> ActionResult:
        self.actions.append(f"type:{text}")
        return ActionResult(success=True, locator_strategy="role+accessible_name")

    async def read(self, target: Target) -> ReadResult:
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
