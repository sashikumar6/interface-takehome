import json
from pathlib import Path

import jsonschema

from computer_use.domain.models import CapabilityArtifact


def test_example_artifacts_validate_against_model_and_exported_schema() -> None:
    schema = json.loads(Path("artifacts/schemas/capability-artifact.schema.json").read_text())
    for path in sorted(Path("artifacts/examples").glob("*.v1.json")):
        payload = json.loads(path.read_text())
        CapabilityArtifact.model_validate(payload)
        jsonschema.validate(payload, schema)
