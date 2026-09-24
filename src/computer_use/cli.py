"""Reviewer-facing CLI for discovery, compilation, approval, replay, and handoff."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer
import uvicorn

from computer_use.compiler.capability import CapabilityCompiler, CompilerConfig
from computer_use.config import Settings
from computer_use.discovery.engine import DiscoveryEngine
from computer_use.discovery.llm import (
    AnthropicMessagesClient,
    OpenAIResponsesClient,
    ScriptedLLMClient,
)
from computer_use.domain.models import (
    SCHEMA_MODELS,
    ApprovalState,
    CapabilityArtifact,
    DiscoveryTrace,
    GoalSpec,
    Target,
)
from computer_use.examples import (
    lookup_business_outcomes,
    lookup_failure_outcomes,
    lookup_goal,
    lookup_scripted_decisions,
    open_sub_account_artifact,
)
from computer_use.observability.evidence import EvidenceRecorder
from computer_use.replay.catalog import FileCapabilityCatalog
from computer_use.replay.engine import ReplayEngine
from computer_use.safety.policy import PolicyEngine
from computer_use.surfaces.playwright import PlaywrightSurfaceDriver

app = typer.Typer(no_args_is_help=True, help="Controlled UI capability discovery and replay.")
schema_app = typer.Typer(no_args_is_help=True, help="Export versioned JSON schemas.")
examples_app = typer.Typer(no_args_is_help=True, help="Generate reviewable local fixtures.")
app.add_typer(schema_app, name="schema")
app.add_typer(examples_app, name="examples")

DEFAULT_DISCOVERY_DIR = Path("evidence/discovery")
DEFAULT_FIXTURE_DISCOVERY_DIR = Path("evidence/fixture-discovery")
DEFAULT_REPLAY_DIR = Path("evidence/replay-run")
DEFAULT_HANDOFF_DIR = Path("evidence/handoff")
DEFAULT_SCHEMA_DIR = Path("artifacts/schemas")
DEFAULT_EXAMPLES_DIR = Path("artifacts/examples")


def _write_model(path: Path, model: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(model.model_dump(mode="json"), indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _load_model(path: Path, model_type: Any) -> Any:
    return model_type.model_validate_json(path.read_text(encoding="utf-8"))


def _parse_inputs(items: list[str]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise typer.BadParameter(f"input must be NAME=VALUE: {item}")
        name, value = item.split("=", 1)
        if not name or name in parsed:
            raise typer.BadParameter(f"invalid or duplicate input name: {name}")
        parsed[name] = value
    return parsed


def _provider_client(settings: Settings) -> Any:
    if settings.provider == "anthropic" and settings.anthropic_api_key is not None:
        return AnthropicMessagesClient(
            api_key=settings.anthropic_api_key,
            model=os.getenv("COMPUTER_USE_ANTHROPIC_MODEL", "claude-sonnet-4-5"),
        )
    if settings.provider == "openai" and settings.openai_api_key is not None:
        return OpenAIResponsesClient(api_key=settings.openai_api_key, model=settings.model)
    raise typer.BadParameter(
        f"genuine discovery selected {settings.provider!r}, but its API key was not configured"
    )


@app.command("demo-app")
def demo_app(
    host: str = typer.Option("127.0.0.1"),
    port: int = typer.Option(8765, min=1, max=65535),
) -> None:
    """Start the local synthetic legacy banking portal."""

    uvicorn.run("demo_app.app:app", host=host, port=port, reload=False)


async def _discover(
    *,
    goal: GoalSpec,
    output: Path,
    headed: bool,
    client: Any,
) -> DiscoveryTrace:
    settings = Settings()
    recorder = EvidenceRecorder(
        settings.evidence_dir,
        "discovery",
        run_directory=output,
    )
    driver = await PlaywrightSurfaceDriver.launch(
        headless=not headed,
        observation_directory=output / "observations",
    )
    try:
        engine = DiscoveryEngine(
            client=client,
            policy=PolicyEngine.development(
                settings.origin,
                max_steps=settings.max_steps,
                run_timeout_seconds=settings.run_timeout_seconds,
            ),
            recorder=recorder,
        )
        return await engine.run(goal, driver)
    finally:
        await driver.close()


@app.command()
def discover(
    goal_file: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    output: Path = typer.Option(DEFAULT_DISCOVERY_DIR),
    headed: bool = typer.Option(False, "--headed/--headless"),
    input_values: Annotated[list[str] | None, typer.Option("--input")] = None,
) -> None:
    """Run genuine provider-backed discovery against the live demo UI."""

    settings = Settings()
    goal = _load_model(goal_file, GoalSpec)
    if input_values:
        data = goal.model_dump(mode="json")
        data["inputs"].update(_parse_inputs(input_values))
        goal = GoalSpec.model_validate(data)
    trace = asyncio.run(
        _discover(goal=goal, output=output, headed=headed, client=_provider_client(settings))
    )
    typer.echo(
        f"discovery status={trace.final_status.value} trace={output / 'discovery-trace.json'}"
    )
    if trace.final_status.value != "success":
        raise typer.Exit(1)


@app.command("fixture-discover")
def fixture_discover(
    output: Path = typer.Option(DEFAULT_FIXTURE_DISCOVERY_DIR),
    headed: bool = typer.Option(False, "--headed/--headless"),
    member_id: str = typer.Option("12345"),
) -> None:
    """Run a clearly labeled scripted discovery fixture for tests and local evaluation."""

    settings = Settings()
    goal = lookup_goal(settings.origin, member_id)
    trace = asyncio.run(
        _discover(
            goal=goal,
            output=output,
            headed=headed,
            client=ScriptedLLMClient(lookup_scripted_decisions(settings.origin)),
        )
    )
    typer.echo(
        f"FIXTURE ONLY (not genuine provider evidence): status={trace.final_status.value} "
        f"trace={output / 'discovery-trace.json'}"
    )
    if trace.final_status.value != "success":
        raise typer.Exit(1)


@app.command("compile")
def compile_capability(
    trace_path: Annotated[Path, typer.Option("--trace", exists=True, dir_okay=False)],
    output: Annotated[Path, typer.Option("--output")],
    capability_id: str = typer.Option("lookup_member_balance"),
) -> None:
    """Deterministically compile a successful trace into a draft artifact."""

    trace = _load_model(trace_path, DiscoveryTrace)
    config = CompilerConfig(
        capability_id=capability_id,
        description="Look up a synthetic member and return the current savings balance.",
        known_business_outcomes=(
            lookup_business_outcomes() if capability_id == "lookup_member_balance" else ()
        ),
        known_failure_outcomes=(
            lookup_failure_outcomes() if capability_id == "lookup_member_balance" else ()
        ),
    )
    artifact = CapabilityCompiler(config).compile(trace, trace.goal)
    _write_model(output, artifact)
    typer.echo(f"compiled draft artifact={output}")


@app.command()
def approve(
    artifact: Annotated[Path, typer.Option(exists=True, dir_okay=False)],
    approved_by: str = typer.Option("local-demo-reviewer"),
) -> None:
    """Validate an artifact and change approval metadata only."""

    current = _load_model(artifact, CapabilityArtifact)
    data = current.model_dump(mode="json")
    data["provenance"]["approval_state"] = ApprovalState.APPROVED.value
    data["provenance"]["approved_at"] = datetime.now(UTC).isoformat()
    data["provenance"]["approved_by"] = approved_by
    approved = CapabilityArtifact.model_validate(data)
    _write_model(artifact, approved)
    typer.echo(f"approved artifact={artifact} by={approved_by}")


async def _replay(
    *,
    artifact: CapabilityArtifact,
    inputs: dict[str, str],
    evidence_dir: Path,
    headed: bool,
    development_override: bool,
) -> tuple[Any, ReplayEngine]:
    settings = Settings()
    recorder = EvidenceRecorder(
        settings.evidence_dir,
        "replay",
        run_directory=evidence_dir,
    )
    driver = await PlaywrightSurfaceDriver.launch(
        headless=not headed,
        observation_directory=evidence_dir / "observations",
    )
    engine = ReplayEngine(
        policy=PolicyEngine.development(
            artifact.target_app.base_origin,
            max_steps=settings.max_steps,
            run_timeout_seconds=settings.run_timeout_seconds,
        ),
        recorder=recorder,
    )
    try:
        result = await engine.run(
            artifact,
            inputs,
            driver,
            development_override=development_override,
        )
        if result.status.value == "escalated" and headed and engine.handoff_manager is not None:
            manager = engine.handoff_manager
            manager.take_control("local-operator")
            typer.echo(
                "Human now owns the live browser. Complete the requested step, then return here."
            )
            await asyncio.to_thread(input, "Press Enter to release control back to automation: ")
            compatible = await manager.release_and_resume(
                driver,
                operator_id="local-operator",
                resume_condition=engine.resume_condition,
            )
            if compatible:
                result = await engine.resume(driver)
        return result, engine
    finally:
        await driver.close()


@app.command()
def replay(
    artifact_path: Annotated[
        Path | None, typer.Option("--artifact", exists=True, dir_okay=False)
    ] = None,
    capability_id: str | None = typer.Option(None, "--capability-id"),
    version: str | None = typer.Option(None, "--version"),
    catalog_dir: Path = typer.Option(DEFAULT_EXAMPLES_DIR, "--catalog"),
    input_values: Annotated[list[str] | None, typer.Option("--input")] = None,
    evidence_dir: Path = typer.Option(DEFAULT_REPLAY_DIR),
    headed: bool = typer.Option(False, "--headed/--headless"),
    development_override: bool = typer.Option(False, "--allow-draft"),
) -> None:
    """Replay an approved artifact with zero LLM decisions or imports."""

    if (artifact_path is None) == (capability_id is None):
        raise typer.BadParameter("provide exactly one of --artifact or --capability-id")
    artifact = (
        _load_model(artifact_path, CapabilityArtifact)
        if artifact_path is not None
        else FileCapabilityCatalog(catalog_dir).get(capability_id or "", version)
    )
    result, _ = asyncio.run(
        _replay(
            artifact=artifact,
            inputs=_parse_inputs(input_values or []),
            evidence_dir=evidence_dir,
            headed=headed,
            development_override=development_override,
        )
    )
    typer.echo(json.dumps(result.model_dump(mode="json"), indent=2, sort_keys=True))
    if result.status.value == "hard_failure":
        raise typer.Exit(1)


@app.command("handoff-demo")
def handoff_demo(
    artifact_path: Annotated[Path, typer.Option("--artifact", exists=True, dir_okay=False)],
    evidence_dir: Path = typer.Option(DEFAULT_HANDOFF_DIR),
    headed: bool = typer.Option(True, "--headed/--headless"),
    automated_fixture_human: bool = typer.Option(False, "--automated-fixture-human"),
) -> None:
    """Pause before commit, transfer the same browser session, and validate resume."""

    artifact = _load_model(artifact_path, CapabilityArtifact)
    settings = Settings()

    async def run_handoff() -> None:
        recorder = EvidenceRecorder(settings.evidence_dir, "handoff", run_directory=evidence_dir)
        driver = await PlaywrightSurfaceDriver.launch(
            headless=not headed,
            observation_directory=evidence_dir / "observations",
        )
        engine = ReplayEngine(
            policy=PolicyEngine.development(
                artifact.target_app.base_origin,
                max_steps=settings.max_steps,
                run_timeout_seconds=settings.run_timeout_seconds,
            ),
            recorder=recorder,
        )
        try:
            result = await engine.run(
                artifact,
                {
                    "member_id": "12345",
                    "account_type": "holiday_savings",
                    "nickname": "Travel Fund",
                },
                driver,
                goal="Open the reviewed synthetic holiday sub-account",
            )
            if result.status.value != "escalated" or engine.handoff_manager is None:
                raise RuntimeError(f"expected escalation, got {result.status.value}")
            manager = engine.handoff_manager
            manager.take_control(
                "fixture-operator" if automated_fixture_human else "local-operator"
            )
            if automated_fixture_human:
                await manager.operator_click(
                    driver,
                    Target(role="button", accessible_name="Commit sub-account"),
                    timeout_ms=engine.pending_step_timeout_ms,
                    operator_id="fixture-operator",
                )
            else:
                typer.echo(
                    "Human now owns the live headed browser. Click 'Commit sub-account', then return here."
                )
                await asyncio.to_thread(input, "Press Enter after the human action: ")
            compatible = await manager.release_and_resume(
                driver,
                operator_id="fixture-operator" if automated_fixture_human else "local-operator",
                resume_condition=engine.resume_condition,
            )
            if not compatible:
                raise RuntimeError("resume validation found an incompatible browser state")
            final = await engine.resume(driver)
            if final.status.value != "success":
                raise RuntimeError(f"resumed replay ended as {final.status.value}")
            typer.echo("handoff complete: resumed replay finished successfully")
        finally:
            await driver.close()

    asyncio.run(run_handoff())


@schema_app.command("export")
def schema_export(output: Path = typer.Option(DEFAULT_SCHEMA_DIR)) -> None:
    """Export all public Pydantic JSON schemas."""

    output.mkdir(parents=True, exist_ok=True)
    for model in SCHEMA_MODELS:
        stem = "-".join(
            part.casefold() for part in __import__("re").findall(r"[A-Z][a-z0-9]*", model.__name__)
        )
        (output / f"{stem}.schema.json").write_text(
            json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    typer.echo(f"exported {len(SCHEMA_MODELS)} schemas to {output}")


@examples_app.command("generate")
def examples_generate(
    output: Path = typer.Option(DEFAULT_EXAMPLES_DIR),
    approve_local: bool = typer.Option(False, "--approve-local"),
) -> None:
    """Generate the static risky-flow artifact; lookup is compiled from discovery."""

    settings = Settings()
    output.mkdir(parents=True, exist_ok=True)
    artifact = open_sub_account_artifact(settings.origin, approved=approve_local)
    _write_model(output / "open_sub_account.v1.json", artifact)
    goal = lookup_goal(settings.origin)
    goal_data = goal.model_dump(mode="json")
    goal_data["inputs"]["member_id"] = "***45"
    _write_model(output / "lookup_goal.json", GoalSpec.model_validate(goal_data))
    typer.echo(f"generated example inputs in {output}")


if __name__ == "__main__":
    app()
