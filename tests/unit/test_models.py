from decimal import Decimal

import pytest
from pydantic import ValidationError

from computer_use.domain.models import (
    ActionType,
    Condition,
    ConditionKind,
    FailureDetail,
    OutputSpec,
    ReplayResult,
    ReplayStatus,
    RiskLevel,
    Step,
    Target,
    TargetScope,
    ValueType,
)


def test_condition_rejects_impossible_field_combinations() -> None:
    with pytest.raises(ValidationError):
        Condition(kind=ConditionKind.ELEMENT_PRESENT)
    with pytest.raises(ValidationError):
        Condition(
            kind=ConditionKind.URL_MATCHES,
            pattern="members",
            target=Target(text="Member search"),
        )
    with pytest.raises(ValidationError):
        Condition(kind=ConditionKind.TEXT_MATCHES, pattern="[")


def test_condition_interpolation_escapes_parameter_as_regex_data() -> None:
    condition = Condition(
        kind=ConditionKind.TEXT_MATCHES,
        pattern=r"Member {{member_id}} ready",
    )
    interpolated = condition.interpolate({"member_id": "12.3+"})
    assert interpolated.pattern == r"Member 12\.3\+ ready"


def test_target_requires_portable_strategy_and_normalizes_text() -> None:
    target = Target(role="button", accessible_name="  Review   sub-account ")
    assert target.accessible_name == "Review sub-account"
    with pytest.raises(ValidationError):
        Target(role="button")
    with pytest.raises(ValidationError):
        Target(structural_fallback="#submit")


def test_target_supports_explicit_relational_scope_and_ordinal() -> None:
    target = Target(
        role="button",
        accessible_name="Open",
        within=TargetScope(role="row", text="Member {{member_id}}"),
        ordinal=1,
    ).interpolate({"member_id": "12345"})
    assert target.within is not None
    assert target.within.text == "Member 12345"
    assert target.ordinal == 1


def test_output_parser_must_match_declared_type() -> None:
    with pytest.raises(ValidationError, match="incompatible"):
        OutputSpec(
            name="balance",
            type=ValueType.DECIMAL,
            description="Balance",
            source="balance_text",
            parser="boolean",
        )


def test_step_action_invariants() -> None:
    with pytest.raises(ValidationError):
        Step(id="type_member", action=ActionType.TYPE, target=Target(text="Member ID"))
    with pytest.raises(ValidationError):
        Step(
            id="read_balance",
            action=ActionType.READ,
            target=Target(role="status", accessible_name="Savings account balance"),
        )
    with pytest.raises(ValidationError, match="irreversible step requires"):
        Step(
            id="commit",
            action=ActionType.CLICK,
            target=Target(role="button", accessible_name="Commit"),
            risk=RiskLevel.IRREVERSIBLE,
        )


def test_replay_result_statuses_are_mutually_exclusive() -> None:
    success = ReplayResult(
        run_id="run-1", status=ReplayStatus.SUCCESS, outputs={"balance": Decimal("12.34")}
    )
    assert success.outputs == {"balance": Decimal("12.34")}
    with pytest.raises(ValidationError):
        ReplayResult(
            run_id="run-2",
            status=ReplayStatus.BUSINESS_OUTCOME,
            business_outcome="member_not_found",
            outputs={},
        )
    with pytest.raises(ValidationError):
        ReplayResult(run_id="run-3", status=ReplayStatus.HARD_FAILURE)
    failure = ReplayResult(
        run_id="run-4",
        status=ReplayStatus.HARD_FAILURE,
        failure=FailureDetail(error_code="TARGET_NOT_FOUND", message="missing"),
    )
    assert failure.failure is not None
