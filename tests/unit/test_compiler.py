from datetime import UTC, datetime

import pytest

from computer_use.compiler.capability import CapabilityCompiler, CompilerConfig
from computer_use.domain.errors import CompilationError
from computer_use.domain.models import (
    ActionType,
    Condition,
    ConditionKind,
    DiscoveryAction,
    DiscoveryStatus,
    DiscoveryTrace,
    GoalSpec,
    InputSpec,
    OutputSpec,
    Target,
    ValueType,
)


def make_goal(**inputs: object) -> GoalSpec:
    specs = tuple(
        InputSpec(name=name, type=ValueType.STRING, description=f"input {name}") for name in inputs
    )
    return GoalSpec(
        natural_language_goal="Compile a fixture trace",
        target_entry_point="http://127.0.0.1:8765/members/search",
        input_specs=specs,
        inputs=inputs,
        requested_outputs=(
            OutputSpec(
                name="balance",
                type=ValueType.DECIMAL,
                description="balance",
                source="balance_text",
                parser="currency_decimal",
            ),
        ),
    )


def make_trace(
    goal: GoalSpec, recorded: str, *, status: DiscoveryStatus = DiscoveryStatus.SUCCESS
) -> DiscoveryTrace:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    success = Condition(
        kind=ConditionKind.ELEMENT_PRESENT,
        target=Target(role="status", accessible_name="Savings account balance"),
    )
    actions = (
        DiscoveryAction(
            index=0,
            action=ActionType.TYPE,
            target=Target(role="textbox", accessible_name="Member ID"),
            value=recorded,
            rationale="enter value",
            observed_result={"success": True},
        ),
        DiscoveryAction(
            index=1,
            action=ActionType.READ,
            target=Target(role="status", accessible_name="Savings account balance"),
            reads_into="balance_text",
            read_value="Current balance: $1.00",
            expected_postcondition=success,
            rationale="read value",
            observed_result={"success": True},
        ),
    )
    return DiscoveryTrace(
        run_id="trace-1",
        goal=goal,
        provider="fixture",
        model="fixture-v1",
        started_at=now,
        completed_at=now,
        actions=actions,
        final_status=status,
        declared_outputs={"balance_text": "Current balance: $1.00"}
        if status is DiscoveryStatus.SUCCESS
        else {},
        success_condition=success if status is DiscoveryStatus.SUCCESS else None,
    )


def compiler() -> CapabilityCompiler:
    return CapabilityCompiler(CompilerConfig(capability_id="lookup_fixture", description="fixture"))


def test_exact_string_binding_and_literal_preservation() -> None:
    bound = compiler().compile(make_trace(make_goal(member_id="12345"), "12345"))
    assert bound.steps[0].value == "{{member_id}}"
    literal = compiler().compile(make_trace(make_goal(member_id="12345"), "literal"))
    assert literal.steps[0].value == "literal"


def test_type_aware_integer_normalization() -> None:
    goal = GoalSpec(
        natural_language_goal="integer binding",
        target_entry_point="http://127.0.0.1:8765/members/search",
        input_specs=(InputSpec(name="member_id", type=ValueType.INTEGER, description="id"),),
        inputs={"member_id": 12345},
        requested_outputs=make_goal(x="y").requested_outputs,
    )
    artifact = compiler().compile(make_trace(goal, "012345"))
    assert artifact.steps[0].value == "{{member_id}}"


def test_ambiguous_binding_and_unsuccessful_trace_are_rejected() -> None:
    with pytest.raises(CompilationError, match="multiple typed inputs"):
        compiler().compile(make_trace(make_goal(first="same", second="same"), "same"))
    goal = make_goal(member_id="12345")
    trace = make_trace(goal, "12345", status=DiscoveryStatus.HARD_FAILURE)
    with pytest.raises(CompilationError, match="successful traces"):
        compiler().compile(trace)


def test_compiler_is_byte_deterministic_and_draft() -> None:
    trace = make_trace(make_goal(member_id="12345"), "12345")
    first = compiler().compile(trace)
    second = compiler().compile(trace)
    assert first.stable_json() == second.stable_json()
    assert first.provenance.approval_state.value == "draft"
