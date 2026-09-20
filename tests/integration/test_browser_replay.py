from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from computer_use.domain.errors import TargetAmbiguousError
from computer_use.domain.models import CapabilityArtifact, ControlOwner, ReplayStatus, Target
from computer_use.examples import open_sub_account_artifact
from computer_use.observability.evidence import EvidenceRecorder
from computer_use.replay.engine import ReplayEngine
from computer_use.safety.policy import PolicyEngine
from computer_use.surfaces.playwright import PlaywrightSurfaceDriver


def lookup_artifact(origin: str) -> CapabilityArtifact:
    data = json.loads(Path("artifacts/examples/lookup_member_balance.v1.json").read_text())
    data["target_app"]["base_origin"] = origin
    data["steps"][0]["value"] = f"{origin}/members/search"
    return CapabilityArtifact.model_validate(data)


async def replay_fixture(
    origin: str, member_id: str, directory: Path
) -> tuple[object, list[dict[str, object]]]:
    recorder = EvidenceRecorder(directory.parent, "test", run_directory=directory)
    driver = await PlaywrightSurfaceDriver.launch(
        headless=True, observation_directory=directory / "observations"
    )
    try:
        engine = ReplayEngine(
            policy=PolicyEngine.development(origin),
            recorder=recorder,
            step_timeout_ms=1_200,
        )
        result = await engine.run(lookup_artifact(origin), {"member_id": member_id}, driver)
        return result, recorder.read_events()
    finally:
        await driver.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_real_browser_success_returns_typed_decimal(
    live_demo_origin: str, tmp_path: Path
) -> None:
    result, _ = await replay_fixture(live_demo_origin, "12345", tmp_path / "success")
    assert result.status is ReplayStatus.SUCCESS
    assert result.outputs == {"balance": Decimal("1234.56")}


@pytest.mark.browser
@pytest.mark.asyncio
async def test_not_found_is_business_outcome(live_demo_origin: str, tmp_path: Path) -> None:
    result, _ = await replay_fixture(live_demo_origin, "99999", tmp_path / "not-found")
    assert result.status is ReplayStatus.BUSINESS_OUTCOME
    assert result.business_outcome == "member_not_found"


@pytest.mark.browser
@pytest.mark.asyncio
async def test_transient_panel_recovers_with_bounded_retry(
    live_demo_origin: str, tmp_path: Path
) -> None:
    async with httpx.AsyncClient() as client:
        response = await client.post(f"{live_demo_origin}/__dev__/reset")
        response.raise_for_status()
    result, events = await replay_fixture(live_demo_origin, "77777", tmp_path / "retry")
    assert result.status is ReplayStatus.SUCCESS
    assert result.outputs == {"balance": Decimal("987.65")}
    retries = [event for event in events if event["event_type"] == "step_retry"]
    assert len(retries) == 1
    assert retries[0]["error_code"] == "CHECKPOINT_MISMATCH"


@pytest.mark.browser
@pytest.mark.asyncio
async def test_permission_denial_is_structured_and_redacted(
    live_demo_origin: str, tmp_path: Path
) -> None:
    result, _ = await replay_fixture(live_demo_origin, "55555", tmp_path / "denied")
    assert result.status is ReplayStatus.HARD_FAILURE
    assert result.failure is not None
    assert result.failure.error_code == "PERMISSION_DENIED"
    serialized = result.model_dump_json()
    assert "55555" not in serialized
    assert "/members/***55" in serialized


@pytest.mark.browser
@pytest.mark.asyncio
async def test_replay_blocks_off_origin_navigation_before_network_access(
    live_demo_origin: str, tmp_path: Path
) -> None:
    data = lookup_artifact(live_demo_origin).model_dump(mode="json")
    data["steps"][0]["value"] = "https://example.invalid/members/search"
    artifact = CapabilityArtifact.model_validate(data)
    recorder = EvidenceRecorder(tmp_path, "off-origin", run_directory=tmp_path / "off-origin")
    driver = await PlaywrightSurfaceDriver.launch(
        headless=True, observation_directory=tmp_path / "off-origin" / "observations"
    )
    try:
        result = await ReplayEngine(
            policy=PolicyEngine.development(live_demo_origin), recorder=recorder
        ).run(artifact, {"member_id": "12345"}, driver)
        assert result.status is ReplayStatus.HARD_FAILURE
        assert result.failure is not None
        assert result.failure.error_code == "POLICY_DENIED"
        events = recorder.read_events()
        decisions = [event for event in events if event["event_type"] == "policy_decision"]
        assert decisions[0]["policy_decision"]["code"] == "ORIGIN_NOT_ALLOWED"
    finally:
        await driver.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_ambiguous_semantic_target_is_distinct_failure(
    live_demo_origin: str, tmp_path: Path
) -> None:
    driver = await PlaywrightSurfaceDriver.launch(headless=True)
    try:
        await driver.navigate(f"{live_demo_origin}/operator")
        with pytest.raises(TargetAmbiguousError) as captured:
            await driver.click(Target(text="Operator handoff"))
        assert captured.value.code == "TARGET_AMBIGUOUS"
        screenshot = tmp_path / "ambiguous.png"
        await driver.screenshot(screenshot)
        assert screenshot.exists()
    finally:
        await driver.close()


@pytest.mark.browser
@pytest.mark.asyncio
async def test_risky_commit_pauses_same_session_and_validates_resume(
    live_demo_origin: str, tmp_path: Path
) -> None:
    recorder = EvidenceRecorder(tmp_path, "handoff", run_directory=tmp_path / "handoff")
    driver = await PlaywrightSurfaceDriver.launch(
        headless=True, observation_directory=tmp_path / "handoff" / "observations"
    )
    engine = ReplayEngine(policy=PolicyEngine.development(live_demo_origin), recorder=recorder)
    artifact = open_sub_account_artifact(live_demo_origin, approved=True)
    try:
        result = await engine.run(
            artifact,
            {
                "member_id": "12345",
                "account_type": "holiday_savings",
                "nickname": "Travel Fund",
            },
            driver,
        )
        assert result.status is ReplayStatus.ESCALATED
        assert engine.handoff_manager is not None
        manager = engine.handoff_manager
        assert manager.owner is ControlOwner.RELEASED
        with pytest.raises(RuntimeError, match="automation cannot act"):
            manager.assert_automation_control()
        manager.take_control("integration-operator")
        await manager.operator_click(
            driver,
            Target(role="button", accessible_name="Commit sub-account"),
            operator_id="integration-operator",
        )
        assert await manager.release_and_resume(
            driver,
            operator_id="integration-operator",
            resume_condition=artifact.final_success_condition,
        )
        assert manager.owner is ControlOwner.AUTOMATION
        assert (tmp_path / "handoff" / "control-events.jsonl").exists()
    finally:
        await driver.close()
