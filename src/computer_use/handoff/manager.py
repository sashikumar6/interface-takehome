"""Minimal persisted ownership state machine for local headed browser handoff."""

from __future__ import annotations

import uuid
from typing import Any

from computer_use.domain.models import (
    Condition,
    ControlOwner,
    InterventionRequest,
    Observation,
    RunStatus,
    Step,
    Target,
)
from computer_use.observability.evidence import EvidenceRecorder


class HandoffManager:
    """Coordinate one paused run without replacing or recreating its browser session."""

    def __init__(self, *, run_id: str, recorder: EvidenceRecorder) -> None:
        self.run_id = run_id
        self.recorder = recorder
        self.owner = ControlOwner.AUTOMATION
        self.status = RunStatus.RUNNING
        self.intervention: InterventionRequest | None = None
        self._session_identity: int | None = None

    def assert_automation_control(self) -> None:
        if self.owner is not ControlOwner.AUTOMATION:
            raise RuntimeError(f"automation cannot act while control owner is {self.owner.value}")

    def _assert_same_session(self, driver: Any) -> None:
        if self._session_identity is None:
            self._session_identity = id(driver)
        elif self._session_identity != id(driver):
            raise RuntimeError("handoff must continue on the original surface session")

    async def request_intervention(
        self,
        *,
        driver: Any,
        capability_id: str,
        goal: str,
        step: Step,
        step_index: int,
        reason: str,
        observation: Observation,
    ) -> InterventionRequest:
        self.assert_automation_control()
        self._assert_same_session(driver)
        screenshot = await self.recorder.capture_screenshot(
            driver, "paused.png", description="browser at the human commit boundary"
        )
        self.status = RunStatus.PAUSED_FOR_HUMAN
        self.owner = ControlOwner.RELEASED
        request = InterventionRequest(
            request_id=f"intervention-{uuid.uuid4().hex}",
            run_id=self.run_id,
            capability_id=capability_id,
            goal=goal,
            step_id=step.id,
            step_index=step_index,
            reason=reason,
            current_observation=observation.model_dump(mode="json"),
            screenshot_reference=screenshot,
            control_owner=ControlOwner.RELEASED,
        )
        self.intervention = request
        self.recorder.write_json("intervention.json", request)
        self.recorder.record_stream(
            "control-events",
            "automation_released_control",
            step_id=step.id,
            step_index=step_index,
            control_owner=self.owner,
            reason=reason,
        )
        return request

    def take_control(self, operator_id: str = "local-operator") -> None:
        if self.status is not RunStatus.PAUSED_FOR_HUMAN or self.owner is not ControlOwner.RELEASED:
            raise RuntimeError("run is not awaiting a human owner")
        self.owner = ControlOwner.HUMAN
        self.recorder.record_stream(
            "control-events",
            "human_control_acquired",
            operator_id=operator_id,
            control_owner=self.owner,
        )

    async def operator_click(
        self, driver: Any, target: Target, *, operator_id: str = "local-operator"
    ) -> None:
        self._assert_same_session(driver)
        if self.owner is not ControlOwner.HUMAN:
            raise RuntimeError("operator action requires human control ownership")
        operator_click = getattr(driver, "operator_click", None)
        if operator_click is None:
            raise RuntimeError("surface does not support local operator actions")
        result = await operator_click(target)
        self.recorder.record_stream(
            "control-events",
            "human_action",
            operator_id=operator_id,
            control_owner=self.owner,
            action="click",
            target=target,
            result=result,
        )

    async def release_and_resume(
        self,
        driver: Any,
        *,
        operator_id: str = "local-operator",
        resume_condition: Condition | None = None,
        timeout_ms: int = 3_000,
    ) -> bool:
        self._assert_same_session(driver)
        if self.owner is not ControlOwner.HUMAN:
            raise RuntimeError("only the current human owner can release control")
        self.owner = ControlOwner.RELEASED
        self.recorder.record_stream(
            "control-events",
            "human_released_control",
            operator_id=operator_id,
            control_owner=self.owner,
        )
        observation = await driver.observe()
        compatible = True
        if resume_condition is not None:
            compatible = (await driver.wait_for(resume_condition, timeout_ms)).matched
        await self.recorder.capture_screenshot(
            driver,
            "resumed-or-completed.png",
            description="same browser session after human control",
        )
        if compatible:
            self.owner = ControlOwner.AUTOMATION
            self.status = RunStatus.RESUMED
            event_type = "automation_resumed"
        else:
            self.status = RunStatus.FAILED
            event_type = "resume_rejected"
        self.recorder.record_stream(
            "control-events",
            event_type,
            operator_id=operator_id,
            control_owner=self.owner,
            state_compatible=compatible,
            observation_digest=observation.digest,
        )
        return compatible

    async def mark_complete(self, driver: Any, *, operator_id: str = "local-operator") -> None:
        self._assert_same_session(driver)
        if self.owner is not ControlOwner.HUMAN:
            raise RuntimeError("completion requires human control")
        await self.recorder.capture_screenshot(
            driver,
            "resumed-or-completed.png",
            description="human-completed same-session handoff",
        )
        self.owner = ControlOwner.RELEASED
        self.status = RunStatus.COMPLETED
        self.recorder.record_stream(
            "control-events",
            "human_marked_complete",
            operator_id=operator_id,
            control_owner=self.owner,
        )

    def abort(self, *, operator_id: str = "local-operator") -> None:
        if self.owner not in {ControlOwner.HUMAN, ControlOwner.RELEASED}:
            raise RuntimeError("abort requires a paused handoff")
        self.owner = ControlOwner.RELEASED
        self.status = RunStatus.ABORTED
        self.recorder.record_stream(
            "control-events",
            "human_aborted",
            operator_id=operator_id,
            control_owner=self.owner,
        )
