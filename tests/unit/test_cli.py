from pathlib import Path

from typer.testing import CliRunner

from computer_use.cli import app

runner = CliRunner()


def test_schema_export_and_example_generation_commands(tmp_path: Path) -> None:
    schemas = tmp_path / "schemas"
    examples = tmp_path / "examples"

    schema_result = runner.invoke(app, ["schema", "export", "--output", str(schemas)])
    example_result = runner.invoke(
        app,
        ["examples", "generate", "--output", str(examples), "--approve-local"],
    )

    assert schema_result.exit_code == 0
    assert (schemas / "capability-artifact.schema.json").exists()
    assert example_result.exit_code == 0
    assert (examples / "open_sub_account.v1.json").exists()


def test_replay_requires_exactly_one_artifact_selector() -> None:
    result = runner.invoke(app, ["replay"])
    assert result.exit_code != 0
    assert "provide exactly one of --artifact or --capability-id" in result.output
