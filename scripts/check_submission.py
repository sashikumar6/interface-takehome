#!/usr/bin/env python3
"""Validate exported schemas, examples, evidence shape, and architectural boundaries."""

from __future__ import annotations

import ast
import json
import os
import sys
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def require(path: Path, errors: list[str]) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        errors.append(f"missing or empty: {path.relative_to(ROOT)}")


def validate_artifacts(errors: list[str]) -> None:
    schema_path = ROOT / "artifacts/schemas/capability-artifact.schema.json"
    require(schema_path, errors)
    if not schema_path.exists():
        return
    validator = Draft202012Validator(load_json(schema_path))
    for path in sorted((ROOT / "artifacts/examples").glob("*.v1.json")):
        issues = sorted(validator.iter_errors(load_json(path)), key=lambda item: list(item.path))
        for issue in issues:
            errors.append(f"{path.relative_to(ROOT)}: schema: {issue.message}")
    lookup = load_json(ROOT / "artifacts/examples/lookup_member_balance.v1.json")
    if lookup["provenance"]["approval_state"] != "approved":
        errors.append("lookup example is not explicitly approved")
    if "{{member_id}}" not in json.dumps(lookup):
        errors.append("lookup example is not parameterized with {{member_id}}")


def validate_replay_boundary(errors: list[str]) -> None:
    replay_root = ROOT / "src/computer_use/replay"
    for path in replay_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name.startswith("computer_use.discovery") for name in names):
                errors.append(f"replay imports discovery provider path: {path.relative_to(ROOT)}")


def validate_replay_evidence(errors: list[str]) -> None:
    expected = {
        "replay-success": "success",
        "replay-not-found": "business_outcome",
        "replay-permission-denied": "hard_failure",
        "replay-retry": "success",
    }
    for directory, status in expected.items():
        root = ROOT / "evidence" / directory
        for name in ("events.jsonl", "final.png", "invocation.json", "result.json"):
            require(root / name, errors)
        result_path = root / "result.json"
        if result_path.exists() and load_json(result_path).get("status") != status:
            errors.append(f"{directory}/result.json has unexpected status")
    not_found = ROOT / "evidence/replay-not-found/result.json"
    if not_found.exists() and load_json(not_found).get("business_outcome") != "member_not_found":
        errors.append("not-found evidence does not classify member_not_found")
    retry_events = ROOT / "evidence/replay-retry/events.jsonl"
    if retry_events.exists() and '"event_type":"step_retry"' not in retry_events.read_text():
        errors.append("retry evidence does not contain a bounded step_retry event")


def validate_handoff(errors: list[str]) -> None:
    root = ROOT / "evidence/handoff"
    for name in (
        "control-events.jsonl",
        "intervention.json",
        "paused.png",
        "resumed-or-completed.png",
    ):
        require(root / name, errors)
    control = root / "control-events.jsonl"
    if control.exists():
        text = control.read_text(encoding="utf-8")
        for event in (
            "automation_released_control",
            "human_control_acquired",
            "human_released_control",
            "automation_resumed",
        ):
            if event not in text:
                errors.append(f"handoff evidence lacks {event}")


def validate_discovery(errors: list[str]) -> None:
    root = ROOT / "evidence/discovery"
    trace = root / "discovery-trace.json"
    events = root / "events.jsonl"
    screenshots = list(root.glob("**/*.png"))
    genuine = False
    if trace.exists():
        payload = load_json(trace)
        genuine = payload.get("provider") in {"anthropic", "openai"}
    if not genuine or not events.exists() or not screenshots:
        message = (
            "genuine provider discovery evidence is incomplete; run scripts/run_demo.sh "
            "with ANTHROPIC_API_KEY or OPENAI_API_KEY"
        )
        if os.getenv("ALLOW_MISSING_PROVIDER_EVIDENCE") == "1":
            print(f"EXTERNAL BLOCKER: {message}")
        else:
            errors.append(message)


def main() -> int:
    errors: list[str] = []
    validate_artifacts(errors)
    validate_replay_boundary(errors)
    validate_replay_evidence(errors)
    validate_handoff(errors)
    validate_discovery(errors)
    if errors:
        print("submission validation failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("schema, artifact, architecture, and evidence validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
