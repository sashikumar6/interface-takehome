"""Deterministic capability replay with bounded recovery and four terminal statuses."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from time import monotonic
from typing import Any

from computer_use.domain.errors import ComputerUseError
from computer_use.domain.models import (
    ActionType,
    BusinessOutcome,
    CapabilityArtifact,
    Condition,
    FailureDetail,
    FailureOutcome,
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


@dataclass(slots=True)
class _ReplayState:
    capability: CapabilityArtifact
    parameters: dict[str, Any]
    development_override: bool
    goal: str | None
    started: float
    started_at: datetime
    reads: dict[str, str] = field(default_factory=dict)
    completed_idempotency_keys: set[str] = field(default_factory=set)
    next_step_index: int = 0


class ReplayEngine:
    """Execute an approved capability without importing or calling any LLM provider."""

    def __init__(
        self,
        *,
        policy: PolicyEngine,
        recorder: EvidenceRecorder,
        condition_evaluator: ConditionEvaluator | None = None,
        handoff_manager: HandoffManager | None = None,
        step_timeout_ms: int | None = None,
    ) -> None:
        self.policy = policy
        self.recorder = recorder
        self.conditions = condition_evaluator or ConditionEvaluator()
        self.handoff_manager = handoff_manager
        self.step_timeout_ms = step_timeout_ms
        self._state: _ReplayState | None = None

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
    def _parse_output(spec: OutputSpec, raw: str) -> str | int | float | Decimal | bool:
        parser = spec.parser
        if spec.type is ValueType.STRING:
            return raw
        if spec.type is ValueType.INTEGER:
            return int(raw.strip())
        if spec.type is ValueType.NUMBER:
            return float(raw.strip())
        if spec.type is ValueType.DECIMAL:
            value = raw.strip()
            if parser == "currency_decimal":
                match = re.search(r"[-+]?\$?\s*([0-9][0-9,]*(?:\.[0-9]+)?)", raw)
                if not match:
                    raise ValueError(f"output {spec.name} does not contain a currency decimal")
                value = match.group(1).replace(",", "")
            try:
                return Decimal(value)
            except InvalidOperation as exc:
                raise ValueError(f"output {spec.name} is not a decimal") from exc
        normalized = raw.strip().casefold()
        if normalized in {"true", "yes", "1"}:
            return True
        if normalized in {"false", "no", "0"}:
            return False
        raise ValueError(f"output {spec.name} is not a boolean")

    def _timeout_for(self, step: Step) -> int:
        if self.step_timeout_ms is None:
            return step.timeout_ms
        return min(step.timeout_ms, self.step_timeout_ms)

    def _outcome_timeout_for(self, step: Step) -> int:
        return min(step.outcome_probe_timeout_ms, self._timeout_for(step))

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
        if result.status is not ReplayStatus.ESCALATED:
            self._state = None
        return result

    async def _declared_outcomes(
        self,
        driver: SurfaceDriver,
        business_outcomes: tuple[BusinessOutcome, ...],
        failure_outcomes: tuple[FailureOutcome, ...],
        parameters: dict[str, Any],
        *,
        outcome_timeout_ms: int,
        success_condition: Condition | None = None,
        success_timeout_ms: int | None = None,
    ) -> tuple[str | None, FailureOutcome | None, bool | None]:
        declared: list[tuple[str, BusinessOutcome | FailureOutcome]] = [
            *(("business", item) for item in business_outcomes),
            *(("failure", item) for item in failure_outcomes),
        ]
        task_metadata: dict[
            asyncio.Task[Any], tuple[str, BusinessOutcome | FailureOutcome | None]
        ] = {}
        for kind, item in declared:
            task = asyncio.create_task(
                self.conditions.evaluate(
                    driver,
                    item.detection_condition.interpolate(parameters),
                    timeout_ms=outcome_timeout_ms,
                )
            )
            task_metadata[task] = (kind, item)
        success_task: asyncio.Task[Any] | None = None
        if success_condition is not None:
            success_task = asyncio.create_task(
                self.conditions.evaluate(
                    driver,
                    success_condition.interpolate(parameters),
                    timeout_ms=success_timeout_ms or outcome_timeout_ms,
                )
            )
            task_metadata[success_task] = ("success", None)
        if not task_metadata:
            return None, None, None

        pending = set(task_metadata)
        completed: set[asyncio.Task[Any]] = set()

        def classification() -> tuple[str | None, FailureOutcome | None, bool | None]:
            success_matched = (
                success_task.result().matched
                if success_task is not None and success_task in completed
                else None
            )
            for task, (kind, item) in task_metadata.items():
                if task in completed and kind == "business" and task.result().matched:
                    assert isinstance(item, BusinessOutcome)
                    return item.name, None, success_matched
            for task, (kind, item) in task_metadata.items():
                if task in completed and kind == "failure" and task.result().matched:
                    assert isinstance(item, FailureOutcome)
                    return None, item, success_matched
            return None, None, success_matched

        try:
            while pending:
                done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                completed.update(done)
                outcome, failure, success_matched = classification()
                terminal_seen = (
                    outcome is not None or failure is not None or success_matched is True
                )
                if terminal_seen:
                    pending_outcomes = {
                        task
                        for task in pending
                        if task_metadata[task][0] in {"business", "failure"}
                    }
                    if outcome is None and pending_outcomes:
                        grace_done, _ = await asyncio.wait(pending_outcomes)
                        completed.update(grace_done)
                        pending.difference_update(grace_done)
                        outcome, failure, success_matched = classification()
                    return outcome, failure, success_matched
            return classification()
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)

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
        if step.precondition is not None:
            precondition = await self.conditions.evaluate(
                driver,
                step.precondition.interpolate(parameters),
                timeout_ms=self._timeout_for(step),
            )
            if not precondition.matched:
                raise ComputerUseError(
                    "PRECONDITION_FAILED", "step precondition did not become true"
                )
        if step.action is ActionType.NAVIGATE:
            assert step.value is not None
            value = interpolate_template(step.value, parameters, regex_escape=False)
            result = await driver.navigate(value, self._timeout_for(step))
        elif step.action is ActionType.CLICK:
            assert target is not None
            result = await driver.click(target, self._timeout_for(step))
        elif step.action is ActionType.TYPE:
            assert target is not None and step.value is not None
            value = interpolate_template(step.value, parameters, regex_escape=False)
            result = await driver.type(target, value, self._timeout_for(step))
        elif step.action is ActionType.READ:
            assert target is not None
            result = await driver.read(target, self._timeout_for(step))
            return result.locator_strategy, result.value
        else:
            assert step.checkpoint is not None
            condition_result = await driver.wait_for(
                step.checkpoint.interpolate(parameters), self._timeout_for(step)
            )
            if not condition_result.matched:
                raise ComputerUseError(
                    "CHECKPOINT_MISMATCH", "wait_for condition did not become true"
                )
            return "condition", None
        return result.locator_strategy, None

    async def _classify_error(
        self,
        driver: SurfaceDriver,
        state: _ReplayState,
        step: Step,
        index: int,
        attempt: int,
    ) -> ReplayResult | None:
        timeout_ms = self._outcome_timeout_for(step)
        outcome, declared_failure, _ = await self._declared_outcomes(
            driver,
            state.capability.known_business_outcomes,
            state.capability.known_failure_outcomes,
            state.parameters,
            outcome_timeout_ms=timeout_ms,
        )
        if outcome:
            return await self._capture_terminal(
                driver,
                ReplayResult(
                    run_id=self.recorder.run_id,
                    status=ReplayStatus.BUSINESS_OUTCOME,
                    business_outcome=outcome,
                    safe_details="known outcome detected before hard-failure classification",
                ),
            )
        if declared_failure is not None:
            return await self._failure(
                driver=driver,
                step=step,
                step_index=index,
                code=declared_failure.error_code,
                message=declared_failure.description,
                expected=declared_failure.detection_condition.model_dump(mode="json"),
                retry_count=attempt - 1,
            )
        return None

    async def _continue(self, driver: SurfaceDriver) -> ReplayResult:
        state = self._state
        assert state is not None
        capability = state.capability
        for index in range(state.next_step_index, len(capability.steps)):
            step = capability.steps[index]
            state.next_step_index = index
            last_code = "HARD_FAILURE"
            last_message = "step failed"
            attempts = step.retry_policy.maximum_attempts
            for attempt in range(1, attempts + 1):
                current_url = await driver.current_url()
                value = (
                    interpolate_template(step.value, state.parameters, regex_escape=False)
                    if step.value is not None
                    else None
                )
                decision = self.policy.evaluate_action(
                    step,
                    run_type="replay",
                    url=value if step.action is ActionType.NAVIGATE else None,
                    current_url=current_url,
                    base_url=capability.target_app.base_origin,
                    capability=capability,
                    development_override=state.development_override,
                    step_index=index,
                    elapsed_seconds=monotonic() - state.started,
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
                    observation = await driver.observe()
                    request = await manager.request_intervention(
                        driver=driver,
                        capability_id=capability.capability_id,
                        goal=state.goal or capability.description,
                        step=step,
                        step_index=index,
                        reason=decision.reason,
                        observation=observation,
                    )
                    return await self._capture_terminal(
                        driver,
                        ReplayResult(
                            run_id=self.recorder.run_id,
                            status=ReplayStatus.ESCALATED,
                            intervention_request_id=request.request_id,
                        ),
                    )
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
                    idempotency_key = (
                        interpolate_template(
                            step.idempotency_key,
                            state.parameters,
                            regex_escape=False,
                        )
                        if step.idempotency_key is not None
                        else None
                    )
                    strategy: str | None
                    read_value: str | None
                    if (
                        idempotency_key is not None
                        and idempotency_key in state.completed_idempotency_keys
                    ):
                        strategy, read_value = "idempotency_guard", None
                    else:
                        strategy, read_value = await self._execute_step(
                            driver, step, state.parameters
                        )
                        if idempotency_key is not None:
                            state.completed_idempotency_keys.add(idempotency_key)
                    checkpoint_result = None
                    if step.checkpoint is not None and step.action is not ActionType.WAIT_FOR:
                        checkpoint_result = await self.conditions.evaluate(
                            driver,
                            step.checkpoint.interpolate(state.parameters),
                            timeout_ms=self._timeout_for(step),
                        )
                        if not checkpoint_result.matched:
                            raise ComputerUseError(
                                "CHECKPOINT_MISMATCH", "step checkpoint did not become true"
                            )
                    if step.reads_into and read_value is not None:
                        state.reads[step.reads_into] = read_value
                    self.recorder.record_event(
                        "step_succeeded",
                        step_id=step.id,
                        step_index=index,
                        action=step.action,
                        locator_strategy=strategy,
                        condition_result=checkpoint_result,
                        retry_count=attempt - 1,
                    )
                    state.next_step_index = index + 1
                    break
                except Exception as error:
                    last_code = str(getattr(error, "code", "UNEXPECTED_EXECUTION_ERROR"))
                    last_message = redact_exception(error)
                    classified = await self._classify_error(driver, state, step, index, attempt)
                    if classified is not None:
                        return classified
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

        final_step = capability.steps[-1]
        final_timeout = self._timeout_for(final_step)
        outcome, declared_failure, final_matched = await self._declared_outcomes(
            driver,
            capability.known_business_outcomes,
            capability.known_failure_outcomes,
            state.parameters,
            outcome_timeout_ms=self._outcome_timeout_for(final_step),
            success_condition=capability.final_success_condition,
            success_timeout_ms=final_timeout,
        )
        if outcome:
            return await self._capture_terminal(
                driver,
                ReplayResult(
                    run_id=self.recorder.run_id,
                    status=ReplayStatus.BUSINESS_OUTCOME,
                    business_outcome=outcome,
                    safe_details="known outcome detected before final success classification",
                ),
            )
        if declared_failure is not None:
            return await self._failure(
                driver=driver,
                step=capability.steps[-1],
                step_index=len(capability.steps) - 1,
                code=declared_failure.error_code,
                message=declared_failure.description,
                expected=declared_failure.detection_condition.model_dump(mode="json"),
            )
        if not final_matched:
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
                spec.name: self._parse_output(spec, state.reads[spec.source])
                for spec in capability.outputs
            }
        except (KeyError, TypeError, ValueError, ArithmeticError) as error:
            return await self._failure(
                driver=driver,
                step=capability.steps[-1],
                step_index=len(capability.steps) - 1,
                code="OUTPUT_PARSE_FAILED",
                message=redact_exception(error),
            )
        return await self._capture_terminal(
            driver,
            ReplayResult(run_id=self.recorder.run_id, status=ReplayStatus.SUCCESS, outputs=outputs),
        )

    async def _run_bounded(self, driver: SurfaceDriver) -> ReplayResult:
        state = self._state
        assert state is not None
        remaining = self.policy.config.run_timeout_seconds - (monotonic() - state.started)
        if remaining <= 0:
            return await self._failure(
                driver=driver,
                step=None,
                step_index=None,
                code="RUN_TIMEOUT",
                message="replay exceeded its configured timeout",
            )
        try:
            async with asyncio.timeout(remaining):
                return await self._continue(driver)
        except TimeoutError:
            return await self._failure(
                driver=driver,
                step=None,
                step_index=None,
                code="RUN_TIMEOUT",
                message="replay exceeded its configured timeout during an in-flight operation",
            )
        except Exception as error:
            return await self._failure(
                driver=driver,
                step=None,
                step_index=None,
                code=str(getattr(error, "code", "UNEXPECTED_EXECUTION_ERROR")),
                message=redact_exception(error),
            )

    async def run(
        self,
        capability: CapabilityArtifact,
        inputs: dict[str, Any],
        driver: SurfaceDriver,
        *,
        development_override: bool = False,
        goal: str | None = None,
    ) -> ReplayResult:
        if self._state is not None:
            return await self._failure(
                driver=driver,
                step=None,
                step_index=None,
                code="RUN_ALREADY_ACTIVE",
                message="this replay engine already has a paused or active run",
            )
        # Handoff managers are run-scoped. A completed engine can safely be
        # reused without inheriting released ownership from a prior run.
        self.handoff_manager = None
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
                message=redact_exception(error),
            )
        self._state = _ReplayState(
            capability=capability,
            parameters=parameters,
            development_override=development_override,
            goal=goal,
            started=monotonic(),
            started_at=datetime.now(UTC),
        )
        return await self._run_bounded(driver)

    @property
    def resume_condition(self) -> Condition | None:
        """Return the pending step checkpoint used to validate human work."""

        if self._state is None:
            return None
        index = self._state.next_step_index
        if index >= len(self._state.capability.steps):
            return self._state.capability.final_success_condition
        return self._state.capability.steps[index].checkpoint

    @property
    def pending_step_timeout_ms(self) -> int:
        """Return the declared timeout for the step currently owned by a human."""

        if self._state is None:
            raise RuntimeError("there is no active replay step")
        index = self._state.next_step_index
        return self._timeout_for(self._state.capability.steps[index])

    async def resume(self, driver: SurfaceDriver) -> ReplayResult:
        """Continue a paused run after human control returns on the same driver."""

        state = self._state
        manager = self.handoff_manager
        if state is None or manager is None:
            return await self._failure(
                driver=driver,
                step=None,
                step_index=None,
                code="NO_PAUSED_RUN",
                message="there is no paused replay to resume",
            )
        if manager.owner.value != "automation" or manager.status.value != "resumed":
            return await self._failure(
                driver=driver,
                step=None,
                step_index=None,
                code="CONTROL_NOT_OWNED",
                message="automation cannot resume until human control is released and validated",
            )
        index = state.next_step_index
        step = state.capability.steps[index]
        if step.checkpoint is None:
            return await self._failure(
                driver=driver,
                step=step,
                step_index=index,
                code="RESUME_VALIDATION_UNAVAILABLE",
                message="the human-completed step has no checkpoint for safe resume",
            )
        validated = await self.conditions.evaluate(
            driver,
            step.checkpoint.interpolate(state.parameters),
            timeout_ms=self._timeout_for(step),
        )
        if not validated.matched:
            return await self._failure(
                driver=driver,
                step=step,
                step_index=index,
                code="RESUME_VALIDATION_FAILED",
                message="human-completed step did not satisfy its declared checkpoint",
                expected=step.checkpoint.model_dump(mode="json"),
            )
        self.recorder.record_event(
            "human_step_validated",
            step_id=step.id,
            step_index=index,
            condition_result=validated,
        )
        if step.idempotency_key is not None:
            state.completed_idempotency_keys.add(
                interpolate_template(
                    step.idempotency_key,
                    state.parameters,
                    regex_escape=False,
                )
            )
        state.next_step_index = index + 1
        return await self._run_bounded(driver)
