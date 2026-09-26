"""Strict, versioned domain contracts shared by discovery, compilation, and replay."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Literal, TypeAlias
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

JsonScalar: TypeAlias = str | int | float | Decimal | bool | None
# Evidence observations can contain bounded nested objects. Pydantic's explicit
# models provide the structure where it is load-bearing; these safe-detail bags
# intentionally remain provider/driver extensible.
JsonValue: TypeAlias = Any


class StrictModel(BaseModel):
    """Base model rejecting unknown fields and validating assignment."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class ActionType(StrEnum):
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    READ = "read"
    WAIT_FOR = "wait_for"


class ConditionKind(StrEnum):
    ELEMENT_PRESENT = "element_present"
    ELEMENT_ABSENT = "element_absent"
    TEXT_MATCHES = "text_matches"
    URL_MATCHES = "url_matches"


class RiskLevel(StrEnum):
    SAFE = "safe"
    REQUIRES_CONFIRMATION = "requires_confirmation"
    IRREVERSIBLE = "irreversible"


class ReplayStatus(StrEnum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    HARD_FAILURE = "hard_failure"
    ESCALATED = "escalated"


class ControlOwner(StrEnum):
    AUTOMATION = "automation"
    HUMAN = "human"
    RELEASED = "released"


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED_FOR_HUMAN = "paused_for_human"
    RESUMED = "resumed"
    COMPLETED = "completed"
    FAILED = "failed"
    ABORTED = "aborted"


class ApprovalState(StrEnum):
    DRAFT = "draft"
    APPROVED = "approved"


class ValueType(StrEnum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    DECIMAL = "decimal"
    BOOLEAN = "boolean"


class DiscoveryStatus(StrEnum):
    SUCCESS = "success"
    ESCALATED = "escalated"
    HARD_FAILURE = "hard_failure"


class EvidenceRef(StrictModel):
    path: str = Field(min_length=1)
    kind: str = Field(default="file", min_length=1)
    sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    description: str | None = Field(default=None, max_length=300)


def _canonical_text(value: str | None) -> str | None:
    return " ".join(value.split()) if value is not None else None


class TargetScope(StrictModel):
    """A semantic container used to disambiguate repeated controls."""

    role: str | None = None
    accessible_name: str | None = None
    text: str | None = None

    @field_validator("role", "accessible_name", "text", mode="before")
    @classmethod
    def normalize_whitespace(cls, value: Any) -> Any:
        return _canonical_text(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_strategy(self) -> TargetScope:
        if not (self.text or self.role):
            raise ValueError("target scope needs a semantic role and/or contained text")
        if self.accessible_name is not None and self.role is None:
            raise ValueError("scope accessible_name requires role")
        return self

    def interpolate(self, parameters: dict[str, Any]) -> TargetScope:
        data = self.model_dump()
        for field in ("accessible_name", "text"):
            value = data.get(field)
            if isinstance(value, str):
                data[field] = interpolate_template(value, parameters, regex_escape=False)
        return TargetScope.model_validate(data)


class Target(StrictModel):
    """Portable semantic intent; never a generated Playwright selector."""

    role: str | None = None
    accessible_name: str | None = None
    text: str | None = None
    near_label: str | None = None
    frame_name: str | None = None
    frame_title: str | None = None
    within: TargetScope | None = None
    ordinal: int | None = Field(default=None, ge=0)
    structural_fallback: str | None = None
    structural_surface: Literal["playwright"] | None = None

    @field_validator(
        "role",
        "accessible_name",
        "text",
        "near_label",
        "frame_name",
        "frame_title",
        "structural_fallback",
        mode="before",
    )
    @classmethod
    def normalize_whitespace(cls, value: Any) -> Any:
        return _canonical_text(value) if isinstance(value, str) else value

    @model_validator(mode="after")
    def validate_strategy(self) -> Target:
        if not any(
            [
                self.role and self.accessible_name,
                self.text,
                self.near_label,
                self.structural_fallback,
            ]
        ):
            raise ValueError(
                "target needs role+accessible_name, text, near_label, or explicit structural fallback"
            )
        if (self.role is None) != (self.accessible_name is None):
            raise ValueError("role and accessible_name must be supplied together")
        if (self.structural_fallback is None) != (self.structural_surface is None):
            raise ValueError("structural fallback must declare its chosen surface")
        return self

    def interpolate(self, parameters: dict[str, Any]) -> Target:
        data = self.model_dump()
        for field in ("accessible_name", "text", "near_label", "frame_name", "frame_title"):
            value = data.get(field)
            if isinstance(value, str):
                data[field] = interpolate_template(value, parameters, regex_escape=False)
        if self.within is not None:
            data["within"] = self.within.interpolate(parameters).model_dump()
        return Target.model_validate(data)


_PLACEHOLDER = re.compile(r"\{\{([A-Za-z_][A-Za-z0-9_]*)\}\}")


def interpolate_template(template: str, parameters: dict[str, Any], *, regex_escape: bool) -> str:
    """Interpolate declared placeholders and reject missing values."""

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in parameters:
            raise ValueError(f"missing interpolation parameter: {name}")
        value = str(parameters[name])
        return re.escape(value) if regex_escape else value

    return _PLACEHOLDER.sub(replace, template)


class Condition(StrictModel):
    """The only predicate language used throughout waits and classification."""

    kind: ConditionKind
    target: Target | None = None
    pattern: str | None = None

    @model_validator(mode="after")
    def validate_fields(self) -> Condition:
        if self.kind in {ConditionKind.ELEMENT_PRESENT, ConditionKind.ELEMENT_ABSENT}:
            if self.target is None or self.pattern is not None:
                raise ValueError(f"{self.kind.value} requires target and forbids pattern")
        elif self.kind is ConditionKind.TEXT_MATCHES:
            if not self.pattern:
                raise ValueError("text_matches requires pattern")
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"invalid text pattern: {exc}") from exc
        elif self.kind is ConditionKind.URL_MATCHES:
            if not self.pattern or self.target is not None:
                raise ValueError("url_matches requires pattern and forbids target")
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"invalid URL pattern: {exc}") from exc
        return self

    def interpolate(self, parameters: dict[str, Any]) -> Condition:
        return Condition(
            kind=self.kind,
            target=self.target.interpolate(parameters) if self.target else None,
            pattern=(
                interpolate_template(self.pattern, parameters, regex_escape=True)
                if self.pattern is not None
                else None
            ),
        )


class InputSpec(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    type: ValueType
    required: bool = True
    description: str = Field(min_length=1, max_length=500)
    validation_pattern: str | None = None

    @field_validator("validation_pattern")
    @classmethod
    def valid_pattern(cls, value: str | None) -> str | None:
        if value is not None:
            try:
                re.compile(value)
            except re.error as exc:
                raise ValueError(f"invalid validation pattern: {exc}") from exc
        return value

    def parse(self, raw: Any) -> str | int | float | Decimal | bool:
        if self.type is ValueType.STRING:
            value = str(raw)
            if self.validation_pattern and not re.fullmatch(self.validation_pattern, value):
                raise ValueError(f"input {self.name} does not match its validation pattern")
            return value
        if self.type is ValueType.INTEGER:
            if isinstance(raw, bool):
                raise ValueError(f"input {self.name} must be an integer")
            return int(raw)
        if self.type is ValueType.NUMBER:
            if isinstance(raw, bool):
                raise ValueError(f"input {self.name} must be a number")
            return float(raw)
        if self.type is ValueType.DECIMAL:
            try:
                return Decimal(str(raw))
            except InvalidOperation as exc:
                raise ValueError(f"input {self.name} must be a decimal") from exc
        if isinstance(raw, bool):
            return raw
        normalized = str(raw).casefold()
        if normalized in {"true", "1", "yes"}:
            return True
        if normalized in {"false", "0", "no"}:
            return False
        raise ValueError(f"input {self.name} must be a boolean")


class OutputSpec(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    type: ValueType
    description: str = Field(min_length=1, max_length=500)
    source: str = Field(min_length=1)
    parser: (
        Literal["text", "integer", "number", "decimal", "currency_decimal", "boolean"] | None
    ) = None

    @model_validator(mode="after")
    def parser_matches_type(self) -> OutputSpec:
        allowed = {
            ValueType.STRING: {None, "text"},
            ValueType.INTEGER: {None, "integer"},
            ValueType.NUMBER: {None, "number"},
            ValueType.DECIMAL: {None, "decimal", "currency_decimal"},
            ValueType.BOOLEAN: {None, "boolean"},
        }
        if self.parser not in allowed[self.type]:
            raise ValueError(
                f"parser {self.parser!r} is incompatible with output type {self.type.value}"
            )
        return self


class RetryPolicy(StrictModel):
    maximum_attempts: int = Field(default=1, ge=1, le=10)
    backoff_seconds: float = Field(default=0, ge=0, le=30)
    retryable_error_codes: tuple[str, ...] = ()


class Step(StrictModel):
    id: str = Field(pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")
    action: ActionType
    target: Target | None = None
    value: str | None = None
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    timeout_ms: int = Field(default=5_000, ge=100, le=120_000)
    outcome_probe_timeout_ms: int = Field(default=100, ge=50, le=5_000)
    precondition: Condition | None = None
    checkpoint: Condition | None = None
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    reads_into: str | None = None
    risk: RiskLevel = RiskLevel.SAFE

    @model_validator(mode="after")
    def validate_action(self) -> Step:
        target_actions = {ActionType.CLICK, ActionType.TYPE, ActionType.READ}
        if self.action in target_actions and self.target is None:
            raise ValueError(f"{self.action.value} step requires target")
        if self.action is ActionType.TYPE and self.value is None:
            raise ValueError("type step requires value")
        if self.action is ActionType.NAVIGATE and self.value is None:
            raise ValueError("navigate step requires URL value")
        if self.action is ActionType.READ and not self.reads_into:
            raise ValueError("read step requires reads_into")
        if self.action is not ActionType.READ and self.reads_into is not None:
            raise ValueError("reads_into is legal only on read steps")
        if self.action not in {ActionType.TYPE, ActionType.NAVIGATE} and self.value is not None:
            raise ValueError("value is legal only on type or navigate steps")
        if self.action is ActionType.WAIT_FOR and self.checkpoint is None:
            raise ValueError("wait_for step requires checkpoint")
        if (
            self.risk is RiskLevel.IRREVERSIBLE
            and self.precondition is None
            and self.idempotency_key is None
        ):
            raise ValueError("irreversible step requires a precondition or idempotency key")
        return self


class BusinessOutcome(StrictModel):
    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    description: str = Field(min_length=1, max_length=500)
    detection_condition: Condition


class FailureOutcome(StrictModel):
    """Artifact-declared UI state that maps to a typed hard-failure code."""

    name: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    error_code: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$")
    description: str = Field(min_length=1, max_length=500)
    detection_condition: Condition


class AppMetadata(StrictModel):
    app_id: str = Field(min_length=1)
    vendor_id: str = Field(min_length=1)
    supported_version_range: str = Field(min_length=1)
    base_origin: str
    entry_route: str = Field(pattern=r"^/")

    @field_validator("base_origin")
    @classmethod
    def valid_origin(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.path not in {"", "/"}
        ):
            raise ValueError("base_origin must be an HTTP(S) origin with no route")
        return value.rstrip("/")


class Provenance(StrictModel):
    source_discovery_run_id: str = Field(min_length=1)
    compiler_version: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    approval_state: ApprovalState = ApprovalState.DRAFT
    approved_at: datetime | None = None
    approved_by: str | None = None

    @model_validator(mode="after")
    def approval_fields(self) -> Provenance:
        if self.approval_state is ApprovalState.APPROVED:
            if self.approved_at is None or not self.approved_by:
                raise ValueError("approved provenance requires approved_at and approved_by")
        elif self.approved_at is not None or self.approved_by is not None:
            raise ValueError("draft provenance cannot contain approval metadata")
        return self


class CapabilityArtifact(StrictModel):
    schema_version: str = Field(default="1.0", pattern=r"^\d+\.\d+$")
    capability_id: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    semantic_version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+\.\d+$")
    description: str = Field(min_length=1, max_length=1_000)
    risk: RiskLevel
    target_app: AppMetadata
    inputs: tuple[InputSpec, ...]
    steps: tuple[Step, ...] = Field(min_length=1)
    known_business_outcomes: tuple[BusinessOutcome, ...] = ()
    known_failure_outcomes: tuple[FailureOutcome, ...] = ()
    outputs: tuple[OutputSpec, ...]
    final_success_condition: Condition
    provenance: Provenance

    @field_validator("schema_version")
    @classmethod
    def supported_schema_version(cls, value: str) -> str:
        if value != "1.0":
            raise ValueError(f"unsupported capability schema_version {value!r}; expected '1.0'")
        return value

    @model_validator(mode="after")
    def validate_contract(self) -> CapabilityArtifact:
        input_names = [item.name for item in self.inputs]
        step_ids = [item.id for item in self.steps]
        output_names = [item.name for item in self.outputs]
        outcome_names = [item.name for item in self.known_business_outcomes]
        failure_outcome_names = [item.name for item in self.known_failure_outcomes]
        for label, names in (
            ("input", input_names),
            ("step", step_ids),
            ("output", output_names),
            ("outcome", outcome_names),
            ("failure outcome", failure_outcome_names),
        ):
            if len(names) != len(set(names)):
                raise ValueError(f"duplicate {label} names")
        reads = {step.reads_into for step in self.steps if step.reads_into}
        for output in self.outputs:
            if output.source not in reads:
                raise ValueError(
                    f"output {output.name} references unknown read binding {output.source}"
                )
        return self

    def stable_json(self) -> bytes:
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()


class FailureDetail(StrictModel):
    error_code: str = Field(min_length=1)
    step_id: str | None = None
    step_index: int | None = Field(default=None, ge=0)
    message: str = Field(min_length=1, max_length=1_000)
    expected: JsonValue | None = None
    observed: JsonValue | None = None
    evidence_references: tuple[EvidenceRef, ...] = ()
    retry_count: int = Field(default=0, ge=0)


class ReplayResult(StrictModel):
    run_id: str = Field(min_length=1)
    status: ReplayStatus
    outputs: dict[str, JsonScalar] | None = None
    business_outcome: str | None = None
    safe_details: str | None = None
    failure: FailureDetail | None = None
    intervention_request_id: str | None = None

    @model_validator(mode="after")
    def valid_terminal_shape(self) -> ReplayResult:
        if self.status is ReplayStatus.SUCCESS:
            if (
                self.outputs is None
                or self.failure
                or self.business_outcome
                or self.intervention_request_id
            ):
                raise ValueError(
                    "success requires outputs and forbids failure/outcome/intervention"
                )
        elif self.status is ReplayStatus.BUSINESS_OUTCOME:
            if (
                not self.business_outcome
                or self.failure is not None
                or self.outputs is not None
                or self.intervention_request_id is not None
            ):
                raise ValueError("business_outcome requires its name only")
        elif self.status is ReplayStatus.HARD_FAILURE:
            if (
                self.failure is None
                or self.outputs is not None
                or self.business_outcome is not None
                or self.intervention_request_id is not None
            ):
                raise ValueError("hard_failure requires FailureDetail only")
        elif self.status is ReplayStatus.ESCALATED and (
            not self.intervention_request_id
            or self.failure is not None
            or self.outputs is not None
            or self.business_outcome is not None
        ):
            raise ValueError("escalated requires intervention reference only")
        return self


class InterventionRequest(StrictModel):
    request_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    capability_id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    step_id: str = Field(min_length=1)
    step_index: int = Field(ge=0)
    reason: str = Field(min_length=1, max_length=1_000)
    current_observation: JsonValue
    screenshot_reference: EvidenceRef
    control_owner: ControlOwner = ControlOwner.AUTOMATION
    requested_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    resolved_at: datetime | None = None


class GoalSpec(StrictModel):
    natural_language_goal: str = Field(min_length=1, max_length=2_000)
    target_entry_point: str = Field(min_length=1)
    input_specs: tuple[InputSpec, ...]
    inputs: dict[str, JsonValue]
    requested_outputs: tuple[OutputSpec, ...]
    maximum_steps: int = Field(default=20, ge=1, le=100)
    timeout_seconds: float = Field(default=120, gt=0, le=3_600)

    @model_validator(mode="after")
    def validate_inputs(self) -> GoalSpec:
        specs = {item.name: item for item in self.input_specs}
        unknown = set(self.inputs) - set(specs)
        if unknown:
            raise ValueError(f"inputs not declared in input_specs: {sorted(unknown)}")
        missing = [
            name for name, spec in specs.items() if spec.required and name not in self.inputs
        ]
        if missing:
            raise ValueError(f"missing required inputs: {missing}")
        for name, value in self.inputs.items():
            if isinstance(value, str) and value.startswith("***"):
                continue  # valid only for a redacted persisted discovery trace
            specs[name].parse(value)
        return self


class DiscoveryAction(StrictModel):
    index: int = Field(ge=0)
    action: ActionType
    target: Target | None = None
    value: str | None = None
    expected_postcondition: Condition | None = None
    rationale: str = Field(min_length=1, max_length=300)
    observed_result: JsonValue
    evidence_reference: EvidenceRef | None = None
    reads_into: str | None = None
    read_value: str | None = None
    risk: RiskLevel = RiskLevel.SAFE
    incidental: bool = False

    @model_validator(mode="after")
    def validate_shape(self) -> DiscoveryAction:
        if self.action in {ActionType.CLICK, ActionType.TYPE, ActionType.READ} and not self.target:
            raise ValueError(f"{self.action.value} requires target")
        if self.action in {ActionType.TYPE, ActionType.NAVIGATE} and self.value is None:
            raise ValueError(f"{self.action.value} requires value")
        if self.action is ActionType.READ and not self.reads_into:
            raise ValueError("read requires reads_into")
        return self


class DiscoveryTrace(StrictModel):
    schema_version: str = "1.0"
    run_id: str = Field(min_length=1)
    goal: GoalSpec
    provider: str = Field(min_length=1)
    model: str = Field(min_length=1)
    started_at: datetime
    completed_at: datetime
    actions: tuple[DiscoveryAction, ...]
    final_status: DiscoveryStatus
    declared_outputs: dict[str, JsonValue]
    success_condition: Condition | None = None
    evidence_references: tuple[EvidenceRef, ...] = ()
    failure_message: str | None = None

    @model_validator(mode="after")
    def validate_final(self) -> DiscoveryTrace:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at cannot precede started_at")
        if self.final_status is DiscoveryStatus.SUCCESS:
            if self.success_condition is None or not self.declared_outputs:
                raise ValueError("successful trace requires outputs and success condition")
            read_names = {action.reads_into for action in self.actions if action.reads_into}
            if not set(self.declared_outputs).issubset(read_names):
                raise ValueError("declared outputs must come from read actions")
        return self


class ActionableElement(StrictModel):
    role: str | None = None
    name: str | None = None
    text: str | None = None
    frame: str | None = None


class Observation(StrictModel):
    url: str
    title: str
    elements: tuple[ActionableElement, ...] = ()
    alerts: tuple[str, ...] = ()
    visible_text: str = Field(max_length=8_000)
    screenshot: EvidenceRef | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    digest: str = Field(pattern=r"^[a-f0-9]{64}$")


class ActionResult(StrictModel):
    success: bool
    locator_strategy: str | None = None
    observed: str | None = None
    duration_ms: int = Field(default=0, ge=0)


class ReadResult(ActionResult):
    value: str | None = None

    @model_validator(mode="after")
    def successful_read_has_value(self) -> ReadResult:
        if self.success and self.value is None:
            raise ValueError("successful read requires value")
        return self


class ConditionResult(StrictModel):
    matched: bool
    observed: str | None = None
    duration_ms: int = Field(default=0, ge=0)


SCHEMA_MODELS: tuple[type[BaseModel], ...] = (
    TargetScope,
    Target,
    Condition,
    InputSpec,
    OutputSpec,
    RetryPolicy,
    Step,
    BusinessOutcome,
    FailureOutcome,
    AppMetadata,
    Provenance,
    CapabilityArtifact,
    FailureDetail,
    ReplayResult,
    InterventionRequest,
    GoalSpec,
    DiscoveryAction,
    DiscoveryTrace,
    Observation,
)
