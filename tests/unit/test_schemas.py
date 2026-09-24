import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import ValidationError

from computer_use.domain.models import CapabilityArtifact


def test_example_artifacts_validate_against_model_and_exported_schema() -> None:
    schema = json.loads(Path("artifacts/schemas/capability-artifact.schema.json").read_text())
    for path in sorted(Path("artifacts/examples").glob("*.v1.json")):
        payload = json.loads(path.read_text())
        CapabilityArtifact.model_validate(payload)
        jsonschema.validate(payload, schema)


def test_capability_rejects_unsupported_schema_version() -> None:
    payload = json.loads(Path("artifacts/examples/lookup_member_balance.v1.json").read_text())
    payload["schema_version"] = "2.0"
    with pytest.raises(ValidationError, match="unsupported capability schema_version"):
        CapabilityArtifact.model_validate(payload)
