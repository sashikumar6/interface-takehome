"""Deterministic compiler with no provider dependency or inference step."""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from computer_use.domain.errors import CompilationError
from computer_use.domain.models import (
    ActionType,
    AppMetadata,
    ApprovalState,
    BusinessOutcome,
    CapabilityArtifact,
    Condition,
    ConditionKind,
    DiscoveryAction,
    DiscoveryStatus,
    DiscoveryTrace,
    FailureOutcome,
    GoalSpec,
    Provenance,
    RetryPolicy,
    RiskLevel,
    Step,
    Target,
    ValueType,
)


@dataclass(frozen=True, slots=True)
class CompilerConfig:
    """Explicit inputs to compilation; no defaults are inferred from UI behavior."""

    capability_id: str
    description: str
    target_app_id: str = "legacy_demo_bank"
    vendor_id: str = "interface-ai-demo"
    supported_version_range: str = ">=1.0,<2.0"
    semantic_version: str = "1.0.0"
    compiler_version: str = "1.0.0"
    default_retry_policy: RetryPolicy = field(
        default_factory=lambda: RetryPolicy(
            maximum_attempts=2,
            backoff_seconds=0.25,
            retryable_error_codes=(
                "CHECKPOINT_MISMATCH",
                "TARGET_NOT_FOUND",
                "NAVIGATION_FAILED",
                "SURFACE_EXECUTION_FAILED",
            ),
        )
    )
    default_step_timeout_ms: int = 3_000
    risk: RiskLevel = RiskLevel.SAFE
    known_business_outcomes: tuple[BusinessOutcome, ...] = ()
    known_failure_outcomes: tuple[FailureOutcome, ...] = ()


def _normalized(value: Any, value_type: ValueType) -> tuple[str, Any]:
    if value_type is ValueType.STRING:
        return ("string", " ".join(str(value).split()))
    if value_type is ValueType.INTEGER:
        return ("number", Decimal(int(value)))
    if value_type in {ValueType.NUMBER, ValueType.DECIMAL}:
        return ("number", Decimal(str(value)).normalize())
    if value_type is ValueType.BOOLEAN:
        if isinstance(value, bool):
            parsed = value
        elif str(value).casefold() in {"true", "1", "yes"}:
            parsed = True
        elif str(value).casefold() in {"false", "0", "no"}:
            parsed = False
        else:
            raise ValueError("not a boolean")
        return ("boolean", parsed)
    raise ValueError(f"unsupported input type: {value_type}")


class CapabilityCompiler:
    """Compile successful observed actions into a reviewable draft artifact."""

    def __init__(self, config: CompilerConfig) -> None:
        self.config = config

    def _bind_value(self, recorded: str, goal: GoalSpec) -> str:
        matches: list[str] = []
        for spec in goal.input_specs:
            if spec.name not in goal.inputs:
                continue
            try:
                if (
                    isinstance(recorded, str)
                    and recorded.startswith("***")
                    and recorded == goal.inputs[spec.name]
                ):
                    matches.append(spec.name)
                    continue
                goal_value = spec.parse(goal.inputs[spec.name])
                recorded_value = spec.parse(recorded)
                if _normalized(goal_value, spec.type) == _normalized(recorded_value, spec.type):
                    matches.append(spec.name)
            except (TypeError, ValueError, ArithmeticError):
                continue
        if len(matches) > 1:
            raise CompilationError(
                "AMBIGUOUS_PARAMETER_BINDING",
                f"recorded value matches multiple typed inputs: {sorted(matches)}",
            )
        return f"{{{{{matches[0]}}}}}" if matches else recorded

    def _include_action(self, action: DiscoveryAction) -> bool:
        if not action.incidental:
            return True
        if action.action is not ActionType.WAIT_FOR:
            raise CompilationError(
                "UNSAFE_INCIDENTAL_DROP",
                "only explicitly marked wait_for observations may be dropped as incidental",
            )
        return False

    @staticmethod
    def _canonical_target(target: Target | None) -> Target | None:
        if target is None:
            return None
        data = target.model_dump(mode="json")
        if target.role and target.accessible_name:
            # Accessible role/name is the portable identity. Observed text and
            # neighboring labels often contain fixture-specific values.
            data["text"] = None
            data["near_label"] = None
        return Target.model_validate(data)

    @classmethod
    def _canonical_condition(cls, condition: Condition | None) -> Condition | None:
        if condition is None or condition.target is None:
            return condition
        data = condition.model_dump(mode="json")
        canonical_target = cls._canonical_target(condition.target)
        assert canonical_target is not None
        data["target"] = canonical_target.model_dump(mode="json")
        return Condition.model_validate(data)

    def compile(self, trace: DiscoveryTrace, goal: GoalSpec | None = None) -> CapabilityArtifact:
        """Return byte-stable output for identical trace, goal, and compiler config."""

        selected_goal = goal or trace.goal
        if trace.final_status is not DiscoveryStatus.SUCCESS:
            raise CompilationError("UNSUCCESSFUL_TRACE", "only successful traces can be compiled")
        if selected_goal != trace.goal:
            raise CompilationError("GOAL_MISMATCH", "compiler goal must equal the trace goal")
        if not trace.actions:
            raise CompilationError("EMPTY_TRACE", "successful trace contains no actions")
        if trace.success_condition is None:
            raise CompilationError(
                "MISSING_SUCCESS_CONDITION", "successful trace lacks a condition"
            )

        steps: list[Step] = []
        included_actions = [action for action in trace.actions if self._include_action(action)]
        for action_index, action in enumerate(included_actions):
            value = action.value
            if value is not None and action.action is ActionType.TYPE:
                value = self._bind_value(value, selected_goal)
            target = self._canonical_target(action.target)
            checkpoint = self._canonical_condition(action.expected_postcondition)
            if action.action is ActionType.CLICK and action_index + 1 < len(included_actions):
                following = included_actions[action_index + 1]
                following_target = self._canonical_target(following.target)
                if following.action is ActionType.READ and following_target is not None:
                    checkpoint = Condition(
                        kind=ConditionKind.ELEMENT_PRESENT,
                        target=following_target,
                    )
            steps.append(
                Step(
                    id=f"step_{len(steps) + 1:02d}_{action.action.value}",
                    action=action.action,
                    target=target,
                    value=value,
                    retry_policy=self.config.default_retry_policy,
                    timeout_ms=self.config.default_step_timeout_ms,
                    checkpoint=checkpoint,
                    reads_into=action.reads_into,
                    risk=action.risk,
                )
            )
        if not steps:
            raise CompilationError("EMPTY_CAPABILITY", "incidental filtering removed every action")

        requested_sources = {output.source for output in selected_goal.requested_outputs}
        read_sources = {step.reads_into for step in steps if step.reads_into}
        if requested_sources != set(trace.declared_outputs):
            raise CompilationError(
                "OUTPUT_BINDING_MISMATCH",
                "trace declared outputs do not exactly match the goal output bindings",
            )
        if not requested_sources.issubset(read_sources):
            raise CompilationError("MISSING_OUTPUT_READ", "an output lacks an observed read action")

        entry = urlsplit(selected_goal.target_entry_point)
        if entry.scheme not in {"http", "https"} or not entry.hostname:
            raise CompilationError("INVALID_ENTRY_POINT", "goal entry point must be an HTTP(S) URL")
        origin = f"{entry.scheme}://{entry.netloc}"
        route = entry.path or "/"
        artifact = CapabilityArtifact(
            capability_id=self.config.capability_id,
            semantic_version=self.config.semantic_version,
            description=self.config.description,
            risk=self.config.risk,
            target_app=AppMetadata(
                app_id=self.config.target_app_id,
                vendor_id=self.config.vendor_id,
                supported_version_range=self.config.supported_version_range,
                base_origin=origin,
                entry_route=route,
            ),
            inputs=selected_goal.input_specs,
            steps=tuple(steps),
            known_business_outcomes=self.config.known_business_outcomes,
            known_failure_outcomes=self.config.known_failure_outcomes,
            outputs=selected_goal.requested_outputs,
            final_success_condition=self._canonical_condition(trace.success_condition),
            provenance=Provenance(
                source_discovery_run_id=trace.run_id,
                compiler_version=self.config.compiler_version,
                created_at=trace.completed_at,
                provider=trace.provider,
                model=trace.model,
                approval_state=ApprovalState.DRAFT,
            ),
        )
        return artifact
