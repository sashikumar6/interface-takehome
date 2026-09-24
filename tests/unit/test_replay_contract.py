from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from computer_use.domain.models import (
    ActionResult,
    ActionType,
    AppMetadata,
    ApprovalState,
    BusinessOutcome,
    CapabilityArtifact,
    Condition,
    ConditionKind,
    ConditionResult,
    EvidenceRef,
    FailureOutcome,
    Observation,
    OutputSpec,
    Provenance,
    ReadResult,
    ReplayStatus,
    RetryPolicy,
    RiskLevel,
    Step,
    Target,
    ValueType,
)
from computer_use.observability.evidence import EvidenceRecorder
from computer_use.replay.engine import ReplayEngine
from computer_use.safety.policy import PolicyEngine

ORIGIN = "http://127.0.0.1:8765"


class ContractSurface:
    def __init__(
        self,
        *,
        click_error: Exception | None = None,
        read_value: str = "ok",
        wait_results: list[bool] | None = None,
    ) -> None:
        self.click_error = click_error
        self.read_value = read_value
        self.wait_results = list(wait_results or [])
        self.clicks = 0
        self.action_timeouts: list[int] = []

    async def current_url(self) -> str:
        return f"{ORIGIN}/fixture"

    async def observe(self) -> Observation:
        text = "safe fixture state"
        return Observation(
            url=f"{ORIGIN}/fixture",
            title="Fixture",
            visible_text=text,
            digest=hashlib.sha256(text.encode()).hexdigest(),
        )

    async def navigate(self, url: str, timeout_ms: int) -> ActionResult:
        self.action_timeouts.append(timeout_ms)
        return ActionResult(success=True, locator_strategy="url", observed=url)

    async def click(self, target: Target, timeout_ms: int) -> ActionResult:
        self.action_timeouts.append(timeout_ms)
        self.clicks += 1
        if self.click_error is not None:
            raise self.click_error
        return ActionResult(success=True, locator_strategy="role+accessible_name")

    async def type(self, target: Target, text: str, timeout_ms: int) -> ActionResult:
        self.action_timeouts.append(timeout_ms)
        return ActionResult(success=True, locator_strategy="role+accessible_name")

    async def read(self, target: Target, timeout_ms: int) -> ReadResult:
        self.action_timeouts.append(timeout_ms)
        return ReadResult(
            success=True,
            value=self.read_value,
            locator_strategy="role+accessible_name",
        )

    async def wait_for(self, condition: Condition, timeout_ms: int) -> ConditionResult:
        matched = self.wait_results.pop(0) if self.wait_results else True
        return ConditionResult(matched=matched, observed=f"checked within {timeout_ms}ms")

    async def screenshot(self, destination: Path) -> EvidenceRef:
        destination.parent.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(destination.write_bytes, b"fixture-png")
        return EvidenceRef(path=str(destination), kind="screenshot")

    async def close(self) -> None:
        return None


def artifact(
    step: Step,
    *,
    outputs: tuple[OutputSpec, ...] = (),
    business_outcomes: tuple[BusinessOutcome, ...] = (),
    failure_outcomes: tuple[FailureOutcome, ...] = (),
) -> CapabilityArtifact:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return CapabilityArtifact(
        capability_id="contract_fixture",
        description="Exercise the replay result contract.",
        risk=RiskLevel.SAFE,
        target_app=AppMetadata(
            app_id="fixture",
            vendor_id="fixture",
            supported_version_range=">=1.0,<2.0",
            base_origin=ORIGIN,
            entry_route="/fixture",
        ),
        inputs=(),
        steps=(step,),
        known_business_outcomes=business_outcomes,
        known_failure_outcomes=failure_outcomes,
        outputs=outputs,
        final_success_condition=Condition(
            kind=ConditionKind.ELEMENT_PRESENT,
            target=Target(role="status", accessible_name="Done"),
        ),
        provenance=Provenance(
            source_discovery_run_id="fixture-run",
            compiler_version="1.0.0",
            created_at=now,
            provider="fixture",
            model="fixture",
            approval_state=ApprovalState.APPROVED,
            approved_at=now,
            approved_by="reviewer",
        ),
    )


@pytest.mark.asyncio
async def test_unexpected_surface_exception_is_a_persisted_typed_result(tmp_path: Path) -> None:
    recorder = EvidenceRecorder(tmp_path, "replay", run_directory=tmp_path / "run")
    engine = ReplayEngine(policy=PolicyEngine.development(ORIGIN), recorder=recorder)
    capability = artifact(
        Step(
            id="click",
            action=ActionType.CLICK,
            target=Target(role="button", accessible_name="Continue"),
            retry_policy=RetryPolicy(maximum_attempts=1),
            timeout_ms=200,
        )
    )

    result = await engine.run(
        capability,
        {},
        ContractSurface(click_error=RuntimeError("Element is not attached to the DOM")),
    )

    assert result.status is ReplayStatus.HARD_FAILURE
    assert result.failure is not None
    assert result.failure.error_code == "UNEXPECTED_EXECUTION_ERROR"
    assert (tmp_path / "run" / "result.json").exists()
    assert (tmp_path / "run" / "final.png").exists()
    assert recorder.read_events()[-1]["event_type"] == "run_completed"


@pytest.mark.asyncio
async def test_output_parse_failure_is_typed_and_persisted(tmp_path: Path) -> None:
    recorder = EvidenceRecorder(tmp_path, "replay", run_directory=tmp_path / "run")
    engine = ReplayEngine(policy=PolicyEngine.development(ORIGIN), recorder=recorder)
    capability = artifact(
        Step(
            id="read",
            action=ActionType.READ,
            target=Target(role="status", accessible_name="Balance"),
            reads_into="balance_text",
            timeout_ms=200,
        ),
        outputs=(
            OutputSpec(
                name="balance",
                type=ValueType.DECIMAL,
                description="Balance",
                source="balance_text",
                parser="currency_decimal",
            ),
        ),
    )

    result = await engine.run(capability, {}, ContractSurface(read_value="not a balance"))

    assert result.status is ReplayStatus.HARD_FAILURE
    assert result.failure is not None
    assert result.failure.error_code == "OUTPUT_PARSE_FAILED"


@pytest.mark.parametrize(
    ("value_type", "parser", "raw", "expected"),
    [
        (ValueType.STRING, "text", " hello ", " hello "),
        (ValueType.INTEGER, "integer", "42", 42),
        (ValueType.NUMBER, "number", "3.5", 3.5),
        (ValueType.DECIMAL, "decimal", "3.50", Decimal("3.50")),
        (ValueType.BOOLEAN, "boolean", "yes", True),
    ],
)
def test_all_output_parser_families(
    value_type: ValueType, parser: str, raw: str, expected: object
) -> None:
    spec = OutputSpec.model_validate(
        {
            "name": "value",
            "type": value_type,
            "description": "Typed value",
            "source": "value_text",
            "parser": parser,
        }
    )
    assert ReplayEngine._parse_output(spec, raw) == expected


@pytest.mark.asyncio
async def test_business_outcome_precedes_coexisting_final_success(tmp_path: Path) -> None:
    recorder = EvidenceRecorder(tmp_path, "replay", run_directory=tmp_path / "run")
    engine = ReplayEngine(policy=PolicyEngine.development(ORIGIN), recorder=recorder)
    capability = artifact(
        Step(
            id="click",
            action=ActionType.CLICK,
            target=Target(role="button", accessible_name="Continue"),
            timeout_ms=200,
        ),
        business_outcomes=(
            BusinessOutcome(
                name="already_complete",
                description="The fixture was already complete.",
                detection_condition=Condition(
                    kind=ConditionKind.TEXT_MATCHES,
                    pattern="Already complete",
                ),
            ),
        ),
        failure_outcomes=(
            FailureOutcome(
                name="permission_denied",
                error_code="PERMISSION_DENIED",
                description="Access denied.",
                detection_condition=Condition(
                    kind=ConditionKind.TEXT_MATCHES,
                    pattern="Permission denied",
                ),
            ),
        ),
    )

    result = await engine.run(capability, {}, ContractSurface())

    assert result.status is ReplayStatus.BUSINESS_OUTCOME
    assert result.business_outcome == "already_complete"


@pytest.mark.asyncio
async def test_declared_failure_precedes_coexisting_final_success(tmp_path: Path) -> None:
    recorder = EvidenceRecorder(tmp_path, "replay", run_directory=tmp_path / "run")
    engine = ReplayEngine(policy=PolicyEngine.development(ORIGIN), recorder=recorder)
    capability = artifact(
        Step(
            id="click",
            action=ActionType.CLICK,
            target=Target(role="button", accessible_name="Continue"),
            timeout_ms=200,
        ),
        failure_outcomes=(
            FailureOutcome(
                name="permission_denied",
                error_code="PERMISSION_DENIED",
                description="Access denied.",
                detection_condition=Condition(
                    kind=ConditionKind.TEXT_MATCHES,
                    pattern="Permission denied",
                ),
            ),
        ),
    )

    result = await engine.run(capability, {}, ContractSurface())

    assert result.status is ReplayStatus.HARD_FAILURE
    assert result.failure is not None
    assert result.failure.error_code == "PERMISSION_DENIED"


@pytest.mark.asyncio
async def test_idempotency_key_prevents_duplicate_action_during_checkpoint_retry(
    tmp_path: Path,
) -> None:
    recorder = EvidenceRecorder(tmp_path, "replay", run_directory=tmp_path / "run")
    engine = ReplayEngine(policy=PolicyEngine.development(ORIGIN), recorder=recorder)
    capability = artifact(
        Step(
            id="commit",
            action=ActionType.CLICK,
            target=Target(role="button", accessible_name="Commit"),
            idempotency_key="commit:fixture",
            retry_policy=RetryPolicy(
                maximum_attempts=2,
                retryable_error_codes=("CHECKPOINT_MISMATCH",),
            ),
            checkpoint=Condition(
                kind=ConditionKind.ELEMENT_PRESENT,
                target=Target(role="status", accessible_name="Done"),
            ),
            timeout_ms=200,
        )
    )
    surface = ContractSurface(wait_results=[False, True, True])

    result = await engine.run(capability, {}, surface)

    assert result.status is ReplayStatus.SUCCESS
    assert surface.clicks == 1
    assert surface.action_timeouts == [200]
