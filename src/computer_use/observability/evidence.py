"""Redacted JSON/JSONL evidence recording for discovery, replay, and handoff."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
import uuid
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any, cast

from computer_use.domain.models import EvidenceRef
from computer_use.safety.redaction import DEFAULT_REDACTOR, Redactor

_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


def new_run_id(prefix: str = "run") -> str:
    """Return a collision-resistant, filesystem-safe run identifier."""

    safe_prefix = _SAFE_COMPONENT.sub("-", prefix).strip(".-") or "run"
    return f"{safe_prefix}-{uuid.uuid4().hex}"


def _iso_timestamp(value: datetime | None = None) -> str:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _json_compatible(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (Path, uuid.UUID)):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_compatible(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_compatible(item) for item in value]
    return str(value)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(64 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


class EvidenceRecorder:
    """Append-only, per-run evidence store with mandatory redaction.

    By default evidence is stored at ``base_directory/run_type/run_id``.  Pass a
    concrete ``run_directory`` when producing one of the reviewer-facing fixed
    evidence folders.
    """

    schema_version = "1.0"

    def __init__(
        self,
        base_directory: Path | str,
        run_type: str,
        *,
        run_id: str | None = None,
        run_directory: Path | str | None = None,
        redactor: Redactor = DEFAULT_REDACTOR,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        safe_run_type = self._safe_component(run_type)
        self.run_type = safe_run_type
        self.run_id = self._safe_component(run_id or new_run_id(safe_run_type))
        self.base_directory = Path(base_directory)
        self.run_directory = (
            Path(run_directory)
            if run_directory is not None
            else self.base_directory / self.run_type / self.run_id
        )
        self.run_directory.mkdir(parents=True, exist_ok=True)
        self.events_path = self.run_directory / "events.jsonl"
        self.redactor = redactor
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()

    @staticmethod
    def _safe_component(value: str) -> str:
        result = _SAFE_COMPONENT.sub("-", str(value)).strip(".-")
        if not result or result in {".", ".."}:
            raise ValueError("evidence path component cannot be empty")
        return result

    def _destination(self, name: str | Path) -> Path:
        candidate = Path(name)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("evidence destination must remain inside the run directory")
        safe_parts = [self._safe_component(part) for part in candidate.parts]
        destination = self.run_directory.joinpath(*safe_parts)
        destination.parent.mkdir(parents=True, exist_ok=True)
        return destination

    def _safe_payload(self, value: Any) -> Any:
        return _json_compatible(self.redactor.redact(value))

    def _append(self, path: Path, value: Mapping[str, Any]) -> None:
        encoded = json.dumps(
            self._safe_payload(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        with self._lock, path.open("a", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.write("\n")
            handle.flush()

    def record_event(
        self,
        event_type: str,
        *,
        step_id: str | None = None,
        step_index: int | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Append a redacted standard event and return the persisted shape."""

        event: dict[str, Any] = {
            "schema_version": self.schema_version,
            "timestamp": _iso_timestamp(self._clock()),
            "run_id": self.run_id,
            "run_type": self.run_type,
            "event_type": str(event_type),
        }
        if step_id is not None:
            event["step_id"] = step_id
        if step_index is not None:
            event["step_index"] = step_index
        # Reserved envelope fields cannot be spoofed by caller-provided details.
        for key, value in fields.items():
            if key not in event:
                event[key] = value
        safe_event = cast(dict[str, Any], self._safe_payload(event))
        self._append(self.events_path, safe_event)
        return safe_event

    # Short alias convenient inside execution loops.
    record = record_event

    def record_stream(
        self,
        stream_name: str,
        event_type: str,
        *,
        step_id: str | None = None,
        step_index: int | None = None,
        **fields: Any,
    ) -> dict[str, Any]:
        """Append to a named JSONL stream, e.g. ``control-events.jsonl``."""

        filename = stream_name if stream_name.endswith(".jsonl") else f"{stream_name}.jsonl"
        path = self._destination(filename)
        event: dict[str, Any] = {
            "schema_version": self.schema_version,
            "timestamp": _iso_timestamp(self._clock()),
            "run_id": self.run_id,
            "run_type": self.run_type,
            "event_type": str(event_type),
        }
        if step_id is not None:
            event["step_id"] = step_id
        if step_index is not None:
            event["step_index"] = step_index
        for key, value in fields.items():
            if key not in event:
                event[key] = value
        safe_event = cast(dict[str, Any], self._safe_payload(event))
        self._append(path, safe_event)
        return safe_event

    def write_json(self, name: str | Path, value: Any) -> Path:
        """Atomically write a redacted JSON evidence document."""

        destination = self._destination(name)
        safe_value = self._safe_payload(value)
        encoded = json.dumps(safe_value, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
        with self._lock:
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", dir=destination.parent
            )
            temporary = Path(temporary_name)
            try:
                with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                    handle.write(encoded)
                    handle.flush()
                    os.fsync(handle.fileno())
                temporary.replace(destination)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return destination

    def evidence_ref(
        self,
        path: Path | str,
        *,
        kind: str = "file",
        description: str | None = None,
    ) -> EvidenceRef:
        """Build a redacted reference with an integrity digest when available."""

        candidate = Path(path)
        try:
            stored_path = candidate.relative_to(self.base_directory).as_posix()
        except ValueError:
            try:
                stored_path = candidate.relative_to(self.run_directory).as_posix()
            except ValueError:
                # Persist no arbitrary host path details for external references.
                stored_path = candidate.name
        safe_metadata = self._safe_payload(
            {"path": stored_path, "kind": kind, "description": description}
        )
        digest = _sha256(candidate) if candidate.is_file() else None
        return EvidenceRef(
            path=str(safe_metadata["path"]),
            kind=str(safe_metadata["kind"]),
            sha256=digest,
            description=safe_metadata.get("description"),
        )

    async def capture_screenshot(
        self,
        driver: Any,
        name: str = "checkpoint.png",
        *,
        description: str | None = None,
        record_event: bool = True,
    ) -> EvidenceRef:
        """Capture through ``SurfaceDriver`` and persist only safe metadata."""

        if not name.casefold().endswith(".png"):
            name = f"{name}.png"
        destination = self._destination(name)
        returned = await driver.screenshot(destination)
        source_path = destination
        if isinstance(returned, EvidenceRef):
            returned_path = Path(returned.path)
            source_path = returned_path if returned_path.is_absolute() else destination
            description = description or returned.description
        reference = self.evidence_ref(
            source_path, kind="screenshot", description=description or "run checkpoint"
        )
        if record_event:
            self.record_event("screenshot_captured", evidence_references=[reference])
        return reference

    def record_failure_snapshot(
        self, observation: Any, *, name: str = "failure-observation.json"
    ) -> Path:
        """Persist a bounded observation after applying centralized redaction."""

        return self.write_json(name, observation)

    def read_events(self, stream_name: str = "events.jsonl") -> list[dict[str, Any]]:
        """Read a run-local event stream; primarily useful for audit/tests."""

        path = self._destination(stream_name)
        if not path.exists():
            return []
        with path.open(encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
