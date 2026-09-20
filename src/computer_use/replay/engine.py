"""Deterministic capability replay with bounded recovery and four terminal statuses."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any

from computer_use.domain.errors import ComputerUseError
from computer_use.domain.models import (
    ActionType,
    BusinessOutcome,
    CapabilityArtifact,
    FailureDetail,
    InputSpec,
    OutputSpec,
    ReplayResult,
    ReplayStatus,
    Step,
    ValueType,
    interpolate_template,
)
from computer_use.handoff.manager import HandoffManager
from computer_use.observability.evidence import EvidenceRecorder
from computer_use.replay.conditions import ConditionEvaluator
from computer_use.safety.policy import PolicyDisposition, PolicyEngine
from computer_use.safety.redaction import redact_exception
from computer_use.surfaces.base import SurfaceDriver


class ReplayEngine:
    """Execute an approved capability without importing or calling any LLM provider."""

    def __init__(
        self,
        *,
        policy: PolicyEngine,
        recorder: EvidenceRecorder,
        condition_evaluator: ConditionEvaluator | None = None,
        handoff_manager: HandoffManager | None = None,
        step_timeout_ms: int = 2_000,
    ) -> None:
        self.policy = policy
        self.recorder = recorder
        self.conditions = condition_evaluator or ConditionEvaluator()
        self.handoff_manager = handoff_manager
        self.step_timeout_ms = step_timeout_ms

    @staticmethod
    def _parse_inputs(specs: tuple[InputSpec, ...], supplied: dict[str, Any]) -> dict[str, Any]:
        by_name = {spec.name: spec for spec in specs}
        unknown = set(supplied) - set(by_name)
        if unknown:
            raise ValueError(f"unknown inputs: {sorted(unknown)}")
        missing = [spec.name for spec in specs if spec.required and spec.name not in supplied]
        if missing:
            raise ValueError(f"missing required inputs: {missing}")
        return {name: by_name[name].parse(value) for name, value in supplied.items()}

    @staticmethod
    def _parse_output(spec: OutputSpec, raw: str) -> Any:
        parser = spec.parser
        if parser in {None, "text"} and spec.type is ValueType.STRING:
            return raw
        if parser == "currency_decimal":
            match = re.search(r"[-+]?\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)", raw)
            if not match:
                raise ValueError(f"output {spec.name} does not contain a currency decimal")
            return Decimal(match.group(1).replace(",", ""))
        if parser == "integer" or spec.type is ValueType.INTEGER:
            return int(raw.strip())
        if parser == "number" or spec.type is ValueType.NUMBER:
            return float(raw.strip())
        if parser == "decimal" or spec.type is ValueType.DECIMAL:
            try:
                return Decimal(raw.strip())
            except InvalidOperation as exc:
                raise ValueError(f"output {spec.name} is not a decimal") from exc
        if parser == "boolean" or spec.type is ValueType.BOOLEAN:
            normalized = raw.strip().casefold()
            if normalized in {"true", "yes", "1"}:
                return True
            if normalized in {"false", "no", "0"}:
                return False
            raise ValueError(f"output {spec.name} is not a boolean")
        return raw

    async def _capture_terminal(
        self, driver: SurfaceDriver, result: ReplayResult, observation: Any | None = None
    ) -> ReplayResult:
        try:
            await self.recorder.capture_screenshot(
                driver, "final.png", description=f"replay terminal status: {result.status.value}"
            )
        except Exception as error:
            self.recorder.record_event(
                "screenshot_failed",
                error=redact_exception(error),
                final_classification=result.status,
            )
        if observation is not None:
            self.recorder.record_failure_snapshot(observation)
        self.recorder.write_json("result.json", result)
        self.recorder.record_event("run_completed", final_classification=result.status)
        return result

    async def _business_outcome(
        self,
        driver: SurfaceDriver,
        outcomes: tuple[BusinessOutcome, ...],
        parameters: dict[str, Any],
    ) -> str | None:
        for outcome in outcomes:
            condition = outcome.detection_condition.interpolate(parameters)
            result = await self.conditions.evaluate(driver, condition, timeout_ms=150)
            if result.matched:
                return outcome.name
        return None

    async def _failure(
        self,
        *,
        driver: SurfaceDriver,
        step: Step | None,
        step_index: int | None,
        code: str,
        message: str,
        expected: Any = None,
        retry_count: int = 0,
    ) -> ReplayResult:
        observation = None
        evidence = []
        try:
            observation = await driver.observe()
            shot = await self.recorder.capture_screenshot(
                driver, "failure.png", description="hard replay failure", record_event=False
            )
            evidence.append(shot)
        except Exception as error:
            self.recorder.record_event("failure_capture_failed", error=redact_exception(error))
        failure = FailureDetail(
            error_code=code,
            step_id=step.id if step else None,
            step_index=step_index,
            message=message,
            expected=expected,
            observed=observation.model_dump(mode="json") if observation else None,
            evidence_references=tuple(evidence),
            retry_count=retry_count,
        )
        return await self._capture_terminal(
            driver,
            ReplayResult(
                run_id=self.recorder.run_id, status=ReplayStatus.HARD_FAILURE, failure=failure
            ),
            observation,
        )

    async def _execute_step(
        self, driver: SurfaceDriver, step: Step, parameters: dict[str, Any]
    ) -> tuple[str | None, str | None]:
        target = step.target.interpolate(parameters) if step.target else None
        if step.action is ActionType.NAVIGATE:
            assert step.value is not None
            value = interpolate_template(step.value, parameters, regex_escape=False)
            result = await driver.navigate(value)
        elif step.action is ActionType.CLICK:
            assert target is not None
            result = await driver.click(target)
        elif step.action is ActionType.TYPE:
            assert target is not None and step.value is not None
            value = interpolate_template(step.value, parameters, regex_escape=False)
            result = await driver.type(target, value)
        elif step.action is ActionType.READ:
            assert target is not None
            result = await driver.read(target)
            return result.locator_strategy, result.value
        else:
            assert step.checkpoint is not None
            condition_result = await driver.wait_for(
                step.checkpoint.interpolate(parameters), self.step_timeout_ms
            )
            if not condition_result.matched:
                raise ComputerUseError(
                    "CHECKPOINT_MISMATCH", "wait_for condition did not become true"
                )
            return "condition", None
        return result.locator_strategy, None

    async def run(
        self,
        capability: CapabilityArtifact,
        inputs: dict[str, Any],
        driver: SurfaceDriver,
        *,
        development_override: bool = False,
        goal: str | None = None,
    ) -> ReplayResult:
        started = monotonic()
        started_at = datetime.now(UTC)
        self.recorder.write_json(
            "invocation.json",
            {"capability_id": capability.capability_id, "inputs": inputs},
        )
        self.recorder.record_event("run_started", capability_id=capability.capability_id)
        try:
            parameters = self._parse_inputs(capability.inputs, inputs)
        except (TypeError, ValueError, ArithmeticError) as error:
            return await self._failure(
                driver=driver,
                step=None,
                step_index=None,
                code="INPUT_VALIDATION_FAILED",
                message=str(error),
            )

        reads: dict[str, str] = {}
        for index, step in enumerate(capability.steps):
            last_code = "HARD_FAILURE"
            last_message = "step failed"
            attempts = step.retry_policy.maximum_attempts
            for attempt in range(1, attempts + 1):
                if monotonic() - started >= self.policy.config.run_timeout_seconds:
                    return await self._failure(
                        driver=driver,
                        step=step,
                        step_index=index,
                        code="RUN_TIMEOUT",
                        message="replay exceeded its configured timeout",
                        retry_count=attempt - 1,
                    )
                observation = await driver.observe()
                value = (
                    interpolate_template(step.value, parameters, regex_escape=False)
                    if step.value is not None
                    else None
                )
                decision = self.policy.evaluate_action(
                    step,
                    run_type="replay",
                    url=value if step.action is ActionType.NAVIGATE else None,
                    current_url=observation.url,
                    base_url=capability.target_app.base_origin,
                    capability=capability,
                    development_override=development_override,
                    step_index=index,
                    started_at=started_at,
                    control_owner=(
                        self.handoff_manager.owner
                        if self.handoff_manager is not None
                        else "automation"
                    ),
                )
                self.recorder.record_event(
                    "policy_decision",
                    step_id=step.id,
                    step_index=index,
                    policy_decision=decision.model_dump(),
                    action=step.action,
                    target=step.target,
                )
                if decision.disposition is PolicyDisposition.REQUIRE_HUMAN:
                    manager = self.handoff_manager or HandoffManager(
                        run_id=self.recorder.run_id, recorder=self.recorder
                    )
                    self.handoff_manager = manager
                    request = await manager.request_intervention(
                        driver=driver,
                        capability_id=capability.capability_id,
                        goal=goal or capability.description,
                        step=step,
                        step_index=index,
                        reason=decision.reason,
                        observation=observation,
                    )
                    result = ReplayResult(
                        run_id=self.recorder.run_id,
                        status=ReplayStatus.ESCALATED,
                        intervention_request_id=request.request_id,
                    )
                    self.recorder.write_json("result.json", result)
                    return result
                if not decision.allowed:
                    return await self._failure(
                        driver=driver,
                        step=step,
                        step_index=index,
                        code=("RUN_TIMEOUT" if decision.code == "RUN_TIMEOUT" else "POLICY_DENIED"),
                        message=decision.reason,
                        retry_count=attempt - 1,
                    )
                try:
                    strategy, read_value = await self._execute_step(driver, step, parameters)
                    checkpoint_result = None
                    if step.checkpoint is not None and step.action is not ActionType.WAIT_FOR:
                        checkpoint_result = await self.conditions.evaluate(
                            driver,
                            step.checkpoint.interpolate(parameters),
                            timeout_ms=self.step_timeout_ms,
                        )
                        if not checkpoint_result.matched:
                            raise ComputerUseError(
                                "CHECKPOINT_MISMATCH", "step checkpoint did not become true"
                            )
                    if step.reads_into and read_value is not None:
                        reads[step.reads_into] = read_value
                    self.recorder.record_event(
                        "step_succeeded",
                        step_id=step.id,
                        step_index=index,
                        action=step.action,
                        locator_strategy=strategy,
                        condition_result=checkpoint_result,
                        retry_count=attempt - 1,
                    )
                    break
                except (ComputerUseError, ValueError) as error:
                    last_code = getattr(error, "code", "HARD_FAILURE")
                    last_message = str(error)
                    outcome = await self._business_outcome(
                        driver, capability.known_business_outcomes, parameters
                    )
                    if outcome:
                        result = ReplayResult(
                            run_id=self.recorder.run_id,
                            status=ReplayStatus.BUSINESS_OUTCOME,
                            business_outcome=outcome,
                            safe_details="known outcome detected before hard-failure classification",
                        )
                        return await self._capture_terminal(driver, result)
                    current = await driver.observe()
                    combined = " ".join((*current.alerts, current.visible_text)).casefold()
                    if "permission denied" in combined:
                        return await self._failure(
                            driver=driver,
                            step=step,
                            step_index=index,
                            code="PERMISSION_DENIED",
                            message="the demo application denied access",
                            expected=(
                                step.checkpoint.model_dump(mode="json") if step.checkpoint else None
                            ),
                            retry_count=attempt - 1,
                        )
                    retryable = last_code in step.retry_policy.retryable_error_codes
                    if attempt < attempts and retryable:
                        self.recorder.record_event(
                            "step_retry",
                            step_id=step.id,
                            step_index=index,
                            error_code=last_code,
                            retry_count=attempt,
                        )
                        if step.retry_policy.backoff_seconds:
                            await asyncio.sleep(step.retry_policy.backoff_seconds)
                        continue
                    final_code = "RETRY_EXHAUSTED" if attempts > 1 and retryable else last_code
                    return await self._failure(
                        driver=driver,
                        step=step,
                        step_index=index,
                        code=final_code,
                        message=last_message,
                        expected=(
                            step.checkpoint.model_dump(mode="json") if step.checkpoint else None
                        ),
                        retry_count=attempt - 1,
                    )
            else:  # pragma: no cover - loop always returns on final failure
                return await self._failure(
                    driver=driver,
                    step=step,
                    step_index=index,
                    code="RETRY_EXHAUSTED",
                    message=last_message,
                    retry_count=attempts - 1,
                )

        final = await self.conditions.evaluate(
            driver,
            capability.final_success_condition.interpolate(parameters),
            timeout_ms=self.step_timeout_ms,
        )
        if not final.matched:
            outcome = await self._business_outcome(
                driver, capability.known_business_outcomes, parameters
            )
            if outcome:
                return await self._capture_terminal(
                    driver,
                    ReplayResult(
                        run_id=self.recorder.run_id,
                        status=ReplayStatus.BUSINESS_OUTCOME,
                        business_outcome=outcome,
                        safe_details="known outcome detected before final checkpoint failure",
                    ),
                )
            return await self._failure(
                driver=driver,
                step=capability.steps[-1],
                step_index=len(capability.steps) - 1,
                code="CHECKPOINT_MISMATCH",
                message="final capability success condition did not match",
                expected=capability.final_success_condition.model_dump(mode="json"),
            )
        try:
            outputs = {
                spec.name: self._parse_output(spec, reads[spec.source])
                for spec in capability.outputs
            }
        except (KeyError, TypeError, ValueError, ArithmeticError) as error:
            return await self._failure(
                driver=driver,
                step=capability.steps[-1],
                step_index=len(capability.steps) - 1,
                code="OUTPUT_PARSE_FAILED",
                message=str(error),
            )
        return await self._capture_terminal(
            driver,
            ReplayResult(run_id=self.recorder.run_id, status=ReplayStatus.SUCCESS, outputs=outputs),
        )
