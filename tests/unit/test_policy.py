from computer_use.domain.models import ActionType, RiskLevel
from computer_use.safety.policy import PolicyDisposition, PolicyEngine


def test_off_origin_navigation_is_blocked_for_both_run_types() -> None:
    policy = PolicyEngine.development("http://127.0.0.1:8765")
    for run_type in ("discovery", "replay"):
        decision = policy.evaluate_action(
            ActionType.NAVIGATE,
            run_type=run_type,
            url="https://example.com/escape",
            capability_approved=True,
        )
        assert decision.disposition is PolicyDisposition.DENY
        assert decision.code == "ORIGIN_NOT_ALLOWED"


def test_irreversible_action_always_requires_human() -> None:
    policy = PolicyEngine.development("http://127.0.0.1:8765")
    decision = policy.evaluate_action(
        ActionType.CLICK,
        run_type="replay",
        current_url="http://127.0.0.1:8765/members/12345/sub-accounts/review",
        risk=RiskLevel.IRREVERSIBLE,
        capability_approved=True,
    )
    assert decision.disposition is PolicyDisposition.REQUIRE_HUMAN
    assert decision.code == "HUMAN_CONTROL_REQUIRED"


def test_draft_replay_is_denied_without_explicit_development_override() -> None:
    policy = PolicyEngine.development("http://127.0.0.1:8765")
    decision = policy.evaluate_action(
        ActionType.CLICK,
        run_type="replay",
        current_url="http://127.0.0.1:8765/members/search",
        capability_approved=False,
    )
    assert decision.code == "CAPABILITY_NOT_APPROVED"
