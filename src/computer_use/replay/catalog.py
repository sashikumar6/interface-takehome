"""Addressable capability repository used by production replay entry points."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from computer_use.domain.models import CapabilityArtifact


class CapabilityRepository(Protocol):
    """Resolve immutable capabilities by stable identity and semantic version."""

    def get(self, capability_id: str, version: str | None = None) -> CapabilityArtifact: ...

    def list(self) -> tuple[CapabilityArtifact, ...]: ...


class FileCapabilityCatalog:
    """Read a validated, duplicate-free catalog from a local artifact directory."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)

    def list(self) -> tuple[CapabilityArtifact, ...]:
        artifacts = tuple(
            CapabilityArtifact.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self.root.glob("*.json"))
            if path.name.endswith(".v1.json")
        )
        identities = [(item.capability_id, item.semantic_version) for item in artifacts]
        if len(identities) != len(set(identities)):
            raise ValueError("capability catalog contains duplicate identity/version entries")
        return artifacts

    def get(self, capability_id: str, version: str | None = None) -> CapabilityArtifact:
        matches = [item for item in self.list() if item.capability_id == capability_id]
        if version is not None:
            matches = [item for item in matches if item.semantic_version == version]
        if not matches:
            suffix = f" version {version}" if version else ""
            raise LookupError(f"capability {capability_id!r}{suffix} was not found")
        if version is None:
            matches.sort(
                key=lambda item: tuple(int(part) for part in item.semantic_version.split(".")),
                reverse=True,
            )
        if version is not None and len(matches) > 1:
            raise ValueError("capability catalog contains duplicate identity/version entries")
        return matches[0]
