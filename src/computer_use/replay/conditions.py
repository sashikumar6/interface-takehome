"""Unified condition evaluation facade."""

from __future__ import annotations

from computer_use.domain.models import Condition, ConditionResult
from computer_use.surfaces.base import SurfaceDriver


class ConditionEvaluator:
    """Evaluate the one shared predicate model through a surface driver."""

    async def evaluate(
        self, driver: SurfaceDriver, condition: Condition, *, timeout_ms: int
    ) -> ConditionResult:
        return await driver.wait_for(condition, timeout_ms)
