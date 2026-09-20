"""Shared policy gate for discovery and deterministic replay."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum, StrEnum
from typing import Any, cast
from urllib.parse import urljoin, urlsplit

from computer_use.domain.models import ActionType, ApprovalState, ControlOwner, RiskLevel
from computer_use.safety.redaction import redact, redact_text


def _enum_value(value: object) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    return str(value)


def _action_name(action: object) -> str:
    candidate = getattr(action, "action", action)
    return _enum_value(candidate).strip().casefold()


def _risk_name(risk: object | None, action: object) -> str:
    candidate = risk if risk is not None else getattr(action, "risk", RiskLevel.SAFE)
    return _enum_value(candidate).strip().casefold()


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing credentials are forbidden")
    host = parsed.hostname.casefold()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    default_port = (parsed.scheme.casefold() == "http" and parsed.port == 80) or (
        parsed.scheme.casefold() == "https" and parsed.port == 443
    )
    port = "" if parsed.port is None or default_port else f":{parsed.port}"
    return f"{parsed.scheme.casefold()}://{host}{port}"


class PolicyDisposition(StrEnum):
    """A policy decision is either executable, denied, or requires a human."""

    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_HUMAN = "require_human"


@dataclass(frozen=True, slots=True)
class PolicyDecision:
    """Serializable result from the single discovery/replay policy API."""

    disposition: PolicyDisposition
    code: str
    reason: str
    action: str
    run_type: str
    risk: str = RiskLevel.SAFE.value

    @property
    def allowed(self) -> bool:
        return self.disposition is PolicyDisposition.ALLOW

    @property
    def requires_human(self) -> bool:
        return self.disposition is PolicyDisposition.REQUIRE_HUMAN

    @property
    def requires_confirmation(self) -> bool:
        """Compatibility alias for callers describing handoff as confirmation."""

        return self.requires_human

    def model_dump(self) -> dict[str, object]:
        """Expose a Pydantic-like dump for evidence recording."""

        return {
            "disposition": self.disposition.value,
            "code": self.code,
            "reason": redact_text(self.reason),
            "action": self.action,
            "run_type": self.run_type,
            "risk": self.risk,
            "allowed": self.allowed,
            "requires_human": self.requires_human,
        }


class PolicyViolation(RuntimeError):
    """Raised by :meth:`PolicyEngine.enforce_action` for non-allow decisions."""

    def __init__(self, decision: PolicyDecision) -> None:
        self.decision = decision
        super().__init__(f"{decision.code}: {redact_text(decision.reason)}")


@dataclass(frozen=True, slots=True)
class PolicyConfig:
    """Configuration for origin, route, action, risk, and run limits."""

    allowed_origins: frozenset[str]
    allowed_route_patterns: tuple[str, ...] = (r"/.*",)
    allowed_actions: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {
                ActionType.NAVIGATE.value,
                ActionType.CLICK.value,
                ActionType.TYPE.value,
                ActionType.READ.value,
                ActionType.WAIT_FOR.value,
                "done",
                "escalate",
            }
        )
    )
    max_steps: int = 25
    run_timeout_seconds: float = 120.0
    require_approved_capability: bool = True
    confirmation_risks: frozenset[str] = field(
        default_factory=lambda: frozenset(
            {RiskLevel.REQUIRES_CONFIRMATION.value, RiskLevel.IRREVERSIBLE.value}
        )
    )
    always_handoff_risks: frozenset[str] = field(
        default_factory=lambda: frozenset({RiskLevel.IRREVERSIBLE.value})
    )

    def __post_init__(self) -> None:
        if not self.allowed_origins:
            raise ValueError("at least one allowed origin is required")
        normalized_origins = frozenset(_origin(item) for item in self.allowed_origins)
        object.__setattr__(self, "allowed_origins", normalized_origins)
        normalized_actions = frozenset(_action_name(item) for item in self.allowed_actions)
        object.__setattr__(self, "allowed_actions", normalized_actions)
        normalized_confirmation = frozenset(_enum_value(item) for item in self.confirmation_risks)
        object.__setattr__(self, "confirmation_risks", normalized_confirmation)
        normalized_handoff = frozenset(_enum_value(item) for item in self.always_handoff_risks)
        object.__setattr__(self, "always_handoff_risks", normalized_handoff)
        if self.max_steps < 1:
            raise ValueError("max_steps must be at least 1")
        if self.run_timeout_seconds <= 0:
            raise ValueError("run_timeout_seconds must be positive")
        if not self.allowed_route_patterns:
            raise ValueError("at least one allowed route pattern is required")
        for pattern in self.allowed_route_patterns:
            re.compile(pattern)

    @classmethod
    def development(cls, base_url: str = "http://127.0.0.1:8000", **overrides: Any) -> PolicyConfig:
        """Return a strict local-demo policy with no off-origin access."""

        return cls(allowed_origins=frozenset({_origin(base_url)}), **overrides)


class PolicyEngine:
    """Authorize every UI action through one discovery/replay decision path."""

    def __init__(self, config: PolicyConfig) -> None:
        self.config = config

    @classmethod
    def development(cls, base_url: str = "http://127.0.0.1:8000", **overrides: Any) -> PolicyEngine:
        return cls(PolicyConfig.development(base_url, **overrides))

    def summary(self) -> dict[str, object]:
        """Return a bounded, secret-free policy summary for discovery prompts."""

        return {
            "allowed_origins": sorted(self.config.allowed_origins),
            "allowed_route_patterns": list(self.config.allowed_route_patterns),
            "allowed_actions": sorted(self.config.allowed_actions),
            "max_steps": self.config.max_steps,
            "run_timeout_seconds": self.config.run_timeout_seconds,
            "approval_required_for_replay": self.config.require_approved_capability,
            "human_confirmation_risks": sorted(self.config.confirmation_risks),
        }

    def _decision(
        self,
        disposition: PolicyDisposition,
        code: str,
        reason: str,
        *,
        action: str,
        run_type: str,
        risk: str,
    ) -> PolicyDecision:
        return PolicyDecision(disposition, code, redact_text(reason), action, run_type, risk)

    def _deny(
        self, code: str, reason: str, *, action: str, run_type: str, risk: str
    ) -> PolicyDecision:
        return self._decision(
            PolicyDisposition.DENY, code, reason, action=action, run_type=run_type, risk=risk
        )

    def _route_allowed(self, url: str) -> bool:
        parsed = urlsplit(url)
        route = parsed.path or "/"
        return any(
            re.fullmatch(pattern, route) is not None
            for pattern in self.config.allowed_route_patterns
        )

    @staticmethod
    def _approval_value(capability: object | None, explicit: object | None) -> str | None:
        if explicit is not None:
            if isinstance(explicit, bool):
                return ApprovalState.APPROVED.value if explicit else ApprovalState.DRAFT.value
            return _enum_value(explicit).casefold()
        if capability is None:
            return None
        provenance = getattr(capability, "provenance", None)
        approval = getattr(provenance, "approval_state", None)
        if approval is None and isinstance(capability, Mapping):
            provenance = capability.get("provenance", {})
            if isinstance(provenance, Mapping):
                approval = provenance.get("approval_state")
        return _enum_value(approval).casefold() if approval is not None else None

    def evaluate_action(
        self,
        action: object,
        *,
        run_type: str = "replay",
        url: str | None = None,
        current_url: str | None = None,
        base_url: str | None = None,
        risk: object | None = None,
        capability: object | None = None,
        capability_approved: object | None = None,
        development_override: bool = False,
        step_count: int | None = None,
        step_index: int | None = None,
        elapsed_seconds: float | None = None,
        started_at: datetime | None = None,
        control_owner: object = ControlOwner.AUTOMATION,
        actor: str = "automation",
        human_confirmed: bool = False,
    ) -> PolicyDecision:
        """Evaluate an action without side effects.

        Both discovery and replay call this method.  ``step_count`` is the number
        of already executed actions; an optional zero-based ``step_index`` is
        accepted for convenient integration with replay loops.
        """

        action_name = _action_name(action)
        risk_name = _risk_name(risk, action)
        normalized_run_type = str(run_type).casefold()

        if action_name not in self.config.allowed_actions:
            return self._deny(
                "ACTION_NOT_ALLOWED",
                f"action {action_name!r} is outside the allowlist",
                action=action_name,
                run_type=normalized_run_type,
                risk=risk_name,
            )

        owner_name = _enum_value(control_owner).casefold()
        if actor == "automation" and owner_name != ControlOwner.AUTOMATION.value:
            return self._deny(
                "CONTROL_NOT_OWNED",
                f"automation cannot act while control owner is {owner_name}",
                action=action_name,
                run_type=normalized_run_type,
                risk=risk_name,
            )

        current_count = step_count if step_count is not None else step_index
        if current_count is not None and current_count >= self.config.max_steps:
            return self._deny(
                "MAX_STEPS_EXCEEDED",
                f"run reached the maximum of {self.config.max_steps} actions",
                action=action_name,
                run_type=normalized_run_type,
                risk=risk_name,
            )

        if elapsed_seconds is None and started_at is not None:
            now = datetime.now(UTC)
            if started_at.tzinfo is None:
                started_at = started_at.replace(tzinfo=UTC)
            elapsed_seconds = (now - started_at).total_seconds()
        if elapsed_seconds is not None and elapsed_seconds >= self.config.run_timeout_seconds:
            return self._deny(
                "RUN_TIMEOUT",
                f"run exceeded its {self.config.run_timeout_seconds:g}s timeout",
                action=action_name,
                run_type=normalized_run_type,
                risk=risk_name,
            )

        if (
            normalized_run_type == "replay"
            and self.config.require_approved_capability
            and not development_override
        ):
            approval = self._approval_value(capability, capability_approved)
            if approval != ApprovalState.APPROVED.value:
                return self._deny(
                    "CAPABILITY_NOT_APPROVED",
                    "replay requires an approved capability",
                    action=action_name,
                    run_type=normalized_run_type,
                    risk=risk_name,
                )

        checked_url = url if action_name == ActionType.NAVIGATE.value else current_url
        if action_name == ActionType.NAVIGATE.value and checked_url is None:
            return self._deny(
                "NAVIGATION_URL_REQUIRED",
                "navigation requires a destination URL",
                action=action_name,
                run_type=normalized_run_type,
                risk=risk_name,
            )
        if checked_url is not None:
            try:
                absolute_url = urljoin(base_url, checked_url) if base_url else checked_url
                candidate_origin = _origin(absolute_url)
            except (TypeError, ValueError) as error:
                return self._deny(
                    "INVALID_URL",
                    str(error),
                    action=action_name,
                    run_type=normalized_run_type,
                    risk=risk_name,
                )
            if candidate_origin not in self.config.allowed_origins:
                return self._deny(
                    "ORIGIN_NOT_ALLOWED",
                    f"navigation or action attempted outside configured origins: {candidate_origin}",
                    action=action_name,
                    run_type=normalized_run_type,
                    risk=risk_name,
                )
            if not self._route_allowed(absolute_url):
                return self._deny(
                    "ROUTE_NOT_ALLOWED",
                    "route is outside the configured allowlist",
                    action=action_name,
                    run_type=normalized_run_type,
                    risk=risk_name,
                )

        if risk_name in self.config.always_handoff_risks:
            return self._decision(
                PolicyDisposition.REQUIRE_HUMAN,
                "HUMAN_CONTROL_REQUIRED",
                "irreversible action requires transfer to human control",
                action=action_name,
                run_type=normalized_run_type,
                risk=risk_name,
            )
        if risk_name in self.config.confirmation_risks and not human_confirmed:
            return self._decision(
                PolicyDisposition.REQUIRE_HUMAN,
                "HUMAN_CONFIRMATION_REQUIRED",
                "action risk requires human confirmation",
                action=action_name,
                run_type=normalized_run_type,
                risk=risk_name,
            )

        return self._decision(
            PolicyDisposition.ALLOW,
            "POLICY_ALLOWED",
            "action satisfies configured policy",
            action=action_name,
            run_type=normalized_run_type,
            risk=risk_name,
        )

    # Explicit aliases make it difficult for discovery and replay integrations to
    # accidentally grow separate authorization semantics.
    check_action = evaluate_action
    authorize_action = evaluate_action

    def enforce_action(self, action: object, **context: Any) -> PolicyDecision:
        """Return an allow decision or raise a redacted :class:`PolicyViolation`."""

        decision = self.evaluate_action(action, **context)
        if not decision.allowed:
            raise PolicyViolation(decision)
        return decision

    def redact_decision(self, decision: PolicyDecision) -> dict[str, object]:
        """Return safe policy-decision evidence."""

        return cast(dict[str, object], redact(decision.model_dump()))
