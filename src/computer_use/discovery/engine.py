"""Live observe-decide-act discovery constrained by policy and typed decisions."""

from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic
from typing import Any

from computer_use.discovery.llm import DecisionAction, LLMClient
from computer_use.discovery.prompts import SYSTEM_PROMPT, build_decision_input
from computer_use.domain.errors import ComputerUseError
from computer_use.domain.models import (
    ActionResult,
    ActionType,
    ConditionResult,
    DiscoveryAction,
    DiscoveryStatus,
    DiscoveryTrace,
    GoalSpec,
    ReadResult,
    RiskLevel,
)
from computer_use.observability.evidence import EvidenceRecorder
from computer_use.safety.policy import PolicyDisposition, PolicyEngine
from computer_use.safety.redaction import redact, redact_exception
from computer_use.surfaces.base import SurfaceDriver

_ACTION_MAP = {
    DecisionAction.NAVIGATE: ActionType.NAVIGATE,
    DecisionAction.CLICK: ActionType.CLICK,
    DecisionAction.TYPE: ActionType.TYPE,
    DecisionAction.READ: ActionType.READ,
    DecisionAction.WAIT_FOR: ActionType.WAIT_FOR,
}


class DiscoveryEngine:
    """Run a bounded genuine or scripted model loop; never used by replay."""

    def __init__(
        self,
        *,
        client: LLMClient,
        policy: PolicyEngine,
        recorder: EvidenceRecorder,
        condition_timeout_ms: int = 2_500,
    ) -> None:
        self.client = client
        self.policy = policy
        self.recorder = recorder
        self.condition_timeout_ms = condition_timeout_ms

    def _finish(
        self,
        *,
        goal: GoalSpec,
        started_at: datetime,
        actions: list[DiscoveryAction],
        status: DiscoveryStatus,
        outputs: dict[str, Any],
        success_condition: Any = None,
        failure_message: str | None = None,
    ) -> DiscoveryTrace:
        trace = DiscoveryTrace(
            run_id=self.recorder.run_id,
            goal=goal,
            provider=self.client.provider,
            model=self.client.model,
            started_at=started_at,
            completed_at=datetime.now(UTC),
            actions=tuple(actions),
            final_status=status,
            declared_outputs=outputs,
            success_condition=success_condition,
            evidence_references=(),
            failure_message=failure_message,
        )
        self.recorder.write_json("discovery-trace.json", trace)
        self.recorder.record_event("discovery_completed", final_classification=status)
        return trace

    async def run(self, goal: GoalSpec, driver: SurfaceDriver) -> DiscoveryTrace:
        started_at = datetime.now(UTC)
        started = monotonic()
        history: list[dict[str, Any]] = []
        actions: list[DiscoveryAction] = []
        reads: dict[str, str] = {}
        signatures: list[str] = []
        self.recorder.record_event(
            "discovery_started", provider=self.client.provider, model=self.client.model
        )

        while len(actions) < min(goal.maximum_steps, self.policy.config.max_steps):
            if monotonic() - started >= min(
                goal.timeout_seconds, self.policy.config.run_timeout_seconds
            ):
                return self._finish(
                    goal=goal,
                    started_at=started_at,
                    actions=actions,
                    status=DiscoveryStatus.HARD_FAILURE,
                    outputs={},
                    failure_message="RUN_TIMEOUT",
                )
            observation = await driver.observe()
            safe_goal = goal.model_dump(mode="json")
            safe_goal["inputs"] = {name: "[AVAILABLE_BY_REFERENCE]" for name in goal.inputs}
            decision = await self.client.decide(
                system_prompt=SYSTEM_PROMPT,
                user_prompt=build_decision_input(
                    goal=safe_goal,
                    observation=observation.model_dump(mode="json"),
                    history=history,
                    policy_summary=self.policy.summary(),
                ),
            )
            signature = decision.model_dump_json(exclude={"rationale"})
            signatures.append(signature)
            if len(signatures) >= 3 and len(set(signatures[-3:])) == 1:
                return self._finish(
                    goal=goal,
                    started_at=started_at,
                    actions=actions,
                    status=DiscoveryStatus.HARD_FAILURE,
                    outputs={},
                    failure_message="REPETITION_DEAD_END",
                )

            if decision.action is DecisionAction.DONE:
                assert decision.success_condition is not None
                condition = decision.success_condition.interpolate(goal.inputs)
                done_result = await driver.wait_for(condition, self.condition_timeout_ms)
                if not done_result.matched:
                    return self._finish(
                        goal=goal,
                        started_at=started_at,
                        actions=actions,
                        status=DiscoveryStatus.HARD_FAILURE,
                        outputs={},
                        failure_message="DONE_SUCCESS_CONDITION_MISMATCH",
                    )
                assert decision.output_bindings is not None
                declared: dict[str, str] = {}
                requested_by_name = {item.name: item for item in goal.requested_outputs}
                for output_name, binding in decision.output_bindings.items():
                    if output_name not in requested_by_name or binding not in reads:
                        return self._finish(
                            goal=goal,
                            started_at=started_at,
                            actions=actions,
                            status=DiscoveryStatus.HARD_FAILURE,
                            outputs={},
                            failure_message="INVALID_OUTPUT_BINDING",
                        )
                    declared[binding] = reads[binding]
                await self.recorder.capture_screenshot(
                    driver, "final.png", description="successful discovery final state"
                )
                return self._finish(
                    goal=goal,
                    started_at=started_at,
                    actions=actions,
                    status=DiscoveryStatus.SUCCESS,
                    outputs=declared,
                    success_condition=condition,
                )
            if decision.action is DecisionAction.ESCALATE:
                return self._finish(
                    goal=goal,
                    started_at=started_at,
                    actions=actions,
                    status=DiscoveryStatus.ESCALATED,
                    outputs={},
                    failure_message=decision.escalation_reason,
                )

            action_type = _ACTION_MAP[decision.action]
            value = decision.value
            if decision.input_reference is not None:
                if decision.input_reference not in goal.inputs:
                    return self._finish(
                        goal=goal,
                        started_at=started_at,
                        actions=actions,
                        status=DiscoveryStatus.HARD_FAILURE,
                        outputs={},
                        failure_message="UNKNOWN_INPUT_REFERENCE",
                    )
                value = str(goal.inputs[decision.input_reference])
            policy_decision = self.policy.evaluate_action(
                action_type,
                run_type="discovery",
                url=value if action_type is ActionType.NAVIGATE else None,
                current_url=observation.url,
                base_url=goal.target_entry_point,
                risk=RiskLevel.SAFE,
                step_count=len(actions),
                started_at=started_at,
            )
            self.recorder.record_event(
                "policy_decision",
                step_index=len(actions),
                action=action_type,
                policy_decision=policy_decision.model_dump(),
            )
            if policy_decision.disposition is not PolicyDisposition.ALLOW:
                return self._finish(
                    goal=goal,
                    started_at=started_at,
                    actions=actions,
                    status=(
                        DiscoveryStatus.ESCALATED
                        if policy_decision.disposition is PolicyDisposition.REQUIRE_HUMAN
                        else DiscoveryStatus.HARD_FAILURE
                    ),
                    outputs={},
                    failure_message=policy_decision.reason,
                )
            result: ActionResult | ReadResult | ConditionResult
            try:
                if action_type is ActionType.NAVIGATE:
                    assert value is not None
                    result = await driver.navigate(value)
                    read_value = None
                elif action_type is ActionType.CLICK:
                    assert decision.target is not None
                    result = await driver.click(decision.target)
                    read_value = None
                elif action_type is ActionType.TYPE:
                    assert decision.target is not None and value is not None
                    result = await driver.type(decision.target, value)
                    read_value = None
                elif action_type is ActionType.READ:
                    assert decision.target is not None and decision.reads_into is not None
                    result = await driver.read(decision.target)
                    read_value = result.value
                    assert read_value is not None
                    reads[decision.reads_into] = read_value
                else:
                    assert decision.expected_postcondition is not None
                    result = await driver.wait_for(
                        decision.expected_postcondition.interpolate(goal.inputs),
                        self.condition_timeout_ms,
                    )
                    read_value = None
                    if not result.matched:
                        raise ComputerUseError(
                            "CHECKPOINT_MISMATCH", "model-requested wait condition did not match"
                        )
                if (
                    decision.expected_postcondition is not None
                    and action_type is not ActionType.WAIT_FOR
                ):
                    postcondition = await driver.wait_for(
                        decision.expected_postcondition.interpolate(goal.inputs),
                        self.condition_timeout_ms,
                    )
                    if not postcondition.matched:
                        raise ComputerUseError(
                            "CHECKPOINT_MISMATCH", "proposed postcondition did not match"
                        )
                action = DiscoveryAction(
                    index=len(actions),
                    action=action_type,
                    target=decision.target,
                    value=value,
                    expected_postcondition=(
                        decision.expected_postcondition.interpolate(goal.inputs)
                        if decision.expected_postcondition
                        else None
                    ),
                    rationale=decision.rationale,
                    observed_result=redact(result.model_dump(mode="json")),
                    evidence_reference=observation.screenshot,
                    reads_into=decision.reads_into,
                    read_value=read_value,
                )
                actions.append(action)
                history.append(
                    {
                        "action": action_type.value,
                        "target": redact(decision.target),
                        "result": redact(result),
                    }
                )
                self.recorder.record_event(
                    "discovery_action",
                    step_index=action.index,
                    action=action_type,
                    target=decision.target,
                    rationale=decision.rationale,
                    result=result,
                )
            except (ComputerUseError, ValueError) as error:
                return self._finish(
                    goal=goal,
                    started_at=started_at,
                    actions=actions,
                    status=DiscoveryStatus.HARD_FAILURE,
                    outputs={},
                    failure_message=redact_exception(error),
                )

        return self._finish(
            goal=goal,
            started_at=started_at,
            actions=actions,
            status=DiscoveryStatus.HARD_FAILURE,
            outputs={},
            failure_message="MAX_STEPS_EXCEEDED",
        )
