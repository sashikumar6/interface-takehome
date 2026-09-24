"""Browser-independent surface protocol."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from computer_use.domain.models import (
    ActionResult,
    Condition,
    ConditionResult,
    EvidenceRef,
    Observation,
    ReadResult,
    Target,
)


class SurfaceDriver(Protocol):
    """Async UI surface boundary used by both discovery and replay."""

    async def observe(self) -> Observation: ...

    async def current_url(self) -> str: ...

    async def navigate(self, url: str, timeout_ms: int) -> ActionResult: ...

    async def click(self, target: Target, timeout_ms: int) -> ActionResult: ...

    async def type(self, target: Target, text: str, timeout_ms: int) -> ActionResult: ...

    async def read(self, target: Target, timeout_ms: int) -> ReadResult: ...

    async def wait_for(self, condition: Condition, timeout_ms: int) -> ConditionResult: ...

    async def screenshot(self, destination: Path) -> EvidenceRef: ...

    async def close(self) -> None: ...
