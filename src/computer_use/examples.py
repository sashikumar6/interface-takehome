"""Deterministic synthetic goals, decisions, and capability fixtures used by demos/tests."""

from __future__ import annotations

from datetime import UTC, datetime

from computer_use.compiler.capability import CapabilityCompiler, CompilerConfig
from computer_use.discovery.llm import DecisionAction, LLMDecision
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
    InputSpec,
    OutputSpec,
    Provenance,
    RetryPolicy,
    RiskLevel,
    Step,
    Target,
    ValueType,
)


def lookup_goal(origin: str, member_id: str = "12345") -> GoalSpec:
    return GoalSpec(
        natural_language_goal="Look up the current savings balance for the supplied member ID.",
        target_entry_point=f"{origin.rstrip('/')}/members/search",
        input_specs=(
            InputSpec(
                name="member_id",
                type=ValueType.STRING,
                description="Synthetic five-digit demo member identifier",
                validation_pattern=r"^[0-9]{5}$",
            ),
        ),
        inputs={"member_id": member_id},
        requested_outputs=(
            OutputSpec(
                name="balance",
                type=ValueType.DECIMAL,
                description="Current synthetic savings balance",
                source="balance_text",
                parser="currency_decimal",
            ),
        ),
        maximum_steps=12,
        timeout_seconds=60,
    )


def lookup_scripted_decisions(origin: str) -> list[LLMDecision]:
    balance_target = Target(
        role="status",
        accessible_name="Savings account balance",
        frame_name="account-detail",
        frame_title="Account details",
    )
    return [
        LLMDecision(
            action=DecisionAction.NAVIGATE,
            value=f"{origin.rstrip('/')}/members/search",
            rationale="Open the member-search entry point.",
            expected_postcondition=Condition(
                kind=ConditionKind.URL_MATCHES, pattern=r"/members/search$"
            ),
        ),
        LLMDecision(
            action=DecisionAction.TYPE,
            target=Target(role="textbox", accessible_name="Member ID"),
            input_reference="member_id",
            rationale="Enter the supplied member reference.",
        ),
        LLMDecision(
            action=DecisionAction.CLICK,
            target=Target(role="button", accessible_name="Search"),
            rationale="Submit the member lookup.",
            expected_postcondition=Condition(
                kind=ConditionKind.ELEMENT_PRESENT,
                target=Target(role="link", accessible_name="View savings account"),
            ),
        ),
        LLMDecision(
            action=DecisionAction.CLICK,
            target=Target(role="link", accessible_name="View savings account"),
            rationale="Open the savings panel in the account frame.",
            expected_postcondition=Condition(
                kind=ConditionKind.ELEMENT_PRESENT, target=balance_target
            ),
        ),
        LLMDecision(
            action=DecisionAction.READ,
            target=balance_target,
            reads_into="balance_text",
            rationale="Read the observable current-balance status.",
        ),
        LLMDecision(
            action=DecisionAction.DONE,
            rationale="The requested balance has been read and the success state is present.",
            output_bindings={"balance": "balance_text"},
            success_condition=Condition(kind=ConditionKind.ELEMENT_PRESENT, target=balance_target),
        ),
    ]


def lookup_business_outcomes() -> tuple[BusinessOutcome, ...]:
    return (
        BusinessOutcome(
            name="member_not_found",
            description="The synthetic member fixture does not exist.",
            detection_condition=Condition(
                kind=ConditionKind.TEXT_MATCHES, pattern=r"Member not found"
            ),
        ),
        BusinessOutcome(
            name="invalid_member_id",
            description="The supplied member ID does not have the expected format.",
            detection_condition=Condition(
                kind=ConditionKind.TEXT_MATCHES, pattern=r"Invalid member ID format"
            ),
        ),
    )


def lookup_failure_outcomes() -> tuple[FailureOutcome, ...]:
    return (
        FailureOutcome(
            name="permission_denied",
            error_code="PERMISSION_DENIED",
            description="The operator is not authorized to view this synthetic member.",
            detection_condition=Condition(
                kind=ConditionKind.TEXT_MATCHES, pattern=r"Permission denied"
            ),
        ),
    )


def lookup_compiler() -> CapabilityCompiler:
    return CapabilityCompiler(
        CompilerConfig(
            capability_id="lookup_member_balance",
            description="Look up a synthetic member and return the current savings balance.",
            known_business_outcomes=lookup_business_outcomes(),
            known_failure_outcomes=lookup_failure_outcomes(),
        )
    )


def open_sub_account_artifact(origin: str, *, approved: bool = False) -> CapabilityArtifact:
    """Return the deliberately thin risky flow; the commit step always hands off."""

    member_link = Target(role="link", accessible_name="Open sub-account")
    commit_target = Target(role="button", accessible_name="Commit sub-account")
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return CapabilityArtifact(
        capability_id="open_sub_account",
        description="Prepare a synthetic sub-account and transfer the irreversible commit to a human.",
        risk=RiskLevel.IRREVERSIBLE,
        target_app=AppMetadata(
            app_id="legacy_demo_bank",
            vendor_id="interface-ai-demo",
            supported_version_range=">=1.0,<2.0",
            base_origin=origin.rstrip("/"),
            entry_route="/members/search",
        ),
        inputs=(
            InputSpec(
                name="member_id",
                type=ValueType.STRING,
                description="Synthetic five-digit member ID",
                validation_pattern=r"^[0-9]{5}$",
            ),
            InputSpec(
                name="account_type",
                type=ValueType.STRING,
                description="Demo sub-account type option",
                validation_pattern=r"^(holiday_savings|emergency_savings)$",
            ),
            InputSpec(
                name="nickname",
                type=ValueType.STRING,
                description="Synthetic account nickname",
                validation_pattern=r"^.{1,40}$",
            ),
        ),
        steps=(
            Step(
                id="step_01_navigate",
                action=ActionType.NAVIGATE,
                value=f"{origin.rstrip('/')}/members/search",
                checkpoint=Condition(kind=ConditionKind.URL_MATCHES, pattern=r"/members/search$"),
            ),
            Step(
                id="step_02_type_member",
                action=ActionType.TYPE,
                target=Target(role="textbox", accessible_name="Member ID"),
                value="{{member_id}}",
            ),
            Step(
                id="step_03_search",
                action=ActionType.CLICK,
                target=Target(role="button", accessible_name="Search"),
                checkpoint=Condition(kind=ConditionKind.ELEMENT_PRESENT, target=member_link),
            ),
            Step(
                id="step_04_open_form",
                action=ActionType.CLICK,
                target=member_link,
                checkpoint=Condition(
                    kind=ConditionKind.ELEMENT_PRESENT,
                    target=Target(role="combobox", accessible_name="Sub-account type"),
                ),
            ),
            Step(
                id="step_05_type_account_type",
                action=ActionType.TYPE,
                target=Target(role="combobox", accessible_name="Sub-account type"),
                value="{{account_type}}",
            ),
            Step(
                id="step_06_type_nickname",
                action=ActionType.TYPE,
                target=Target(role="textbox", accessible_name="Account nickname"),
                value="{{nickname}}",
            ),
            Step(
                id="step_07_review",
                action=ActionType.CLICK,
                target=Target(role="button", accessible_name="Review sub-account"),
                checkpoint=Condition(
                    kind=ConditionKind.ELEMENT_PRESENT,
                    target=Target(role="button", accessible_name="Commit sub-account"),
                ),
            ),
            Step(
                id="step_08_commit",
                action=ActionType.CLICK,
                target=commit_target,
                risk=RiskLevel.IRREVERSIBLE,
                retry_policy=RetryPolicy(maximum_attempts=1),
                idempotency_key="open-sub-account:{{member_id}}:{{nickname}}",
                precondition=Condition(
                    kind=ConditionKind.ELEMENT_PRESENT,
                    target=commit_target,
                ),
                checkpoint=Condition(
                    kind=ConditionKind.ELEMENT_PRESENT,
                    target=Target(role="status", accessible_name="Sub-account opened"),
                ),
            ),
        ),
        outputs=(),
        final_success_condition=Condition(
            kind=ConditionKind.ELEMENT_PRESENT,
            target=Target(role="status", accessible_name="Sub-account opened"),
        ),
        provenance=Provenance(
            source_discovery_run_id="documented-risk-flow-fixture",
            compiler_version="1.0.0",
            created_at=now,
            provider="documented-fixture",
            model="reviewed-static-flow",
            approval_state=ApprovalState.APPROVED if approved else ApprovalState.DRAFT,
            approved_at=now if approved else None,
            approved_by="local-demo-reviewer" if approved else None,
        ),
    )


def trace_from_actions(goal: GoalSpec, actions: tuple[DiscoveryAction, ...]) -> DiscoveryTrace:
    """Small helper for compiler unit tests."""

    now = datetime(2026, 1, 1, tzinfo=UTC)
    final = actions[-1].expected_postcondition
    return DiscoveryTrace(
        run_id="fixture-discovery-run",
        goal=goal,
        provider="scripted-test-fixture",
        model="scripted-test-fixture-v1",
        started_at=now,
        completed_at=now,
        actions=actions,
        final_status=DiscoveryStatus.SUCCESS,
        declared_outputs={"balance_text": "Current balance: $1234.56"},
        success_condition=final,
    )
