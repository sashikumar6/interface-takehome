"""Strict semantic target resolution isolated from capability/domain code."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Frame, Locator, Page

from computer_use.domain.errors import (
    SurfaceExecutionError,
    TargetAmbiguousError,
    TargetNotFoundError,
    UnsupportedTargetError,
)
from computer_use.domain.models import Target


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    locator: Locator
    strategy: str


class TargetResolver:
    """Resolve a semantic target in a documented order with exact cardinality."""

    def __init__(self, page: Page) -> None:
        self.page = page

    async def _scope(self, target: Target) -> Page | Frame:
        if not target.frame_name and not target.frame_title:
            return self.page
        matches: list[Frame] = []
        for frame in self.page.frames:
            name_ok = not target.frame_name or frame.name == target.frame_name
            title_ok = True
            if target.frame_title:
                try:
                    title_ok = (
                        await (await frame.frame_element()).get_attribute("title")
                        == target.frame_title
                    )
                except Exception:
                    title_ok = False
            if name_ok and title_ok:
                matches.append(frame)
        if not matches:
            raise TargetNotFoundError("TARGET_NOT_FOUND", "target frame was not found")
        if len(matches) > 1:
            raise TargetAmbiguousError(
                "TARGET_AMBIGUOUS", f"target frame matched {len(matches)} frames"
            )
        return matches[0]

    async def candidates(self, target: Target) -> list[tuple[str, Locator]]:
        scope: Page | Frame | Locator = await self._scope(target)
        if target.within is not None:
            relational = target.within
            if relational.role:
                container = scope.get_by_role(
                    cast(Any, relational.role),
                    name=relational.accessible_name,
                    exact=relational.accessible_name is not None,
                )
            else:
                assert relational.text is not None
                container = scope.get_by_text(relational.text, exact=False)
            if relational.text is not None and relational.role:
                container = container.filter(has_text=relational.text)
            try:
                container_count = await container.count()
            except PlaywrightError as error:
                raise SurfaceExecutionError(
                    "SURFACE_EXECUTION_FAILED", "browser changed while resolving target scope"
                ) from error
            if container_count == 0:
                raise TargetNotFoundError("TARGET_NOT_FOUND", "target scope was not found")
            if container_count > 1:
                raise TargetAmbiguousError(
                    "TARGET_AMBIGUOUS",
                    f"target scope matched {container_count} containers",
                    {"match_count": container_count},
                )
            scope = container
        candidates: list[tuple[str, Locator]] = []
        if target.role and target.accessible_name:
            candidates.append(
                (
                    "role+accessible_name",
                    scope.get_by_role(
                        cast(Any, target.role), name=target.accessible_name, exact=True
                    ),
                )
            )
        if target.text:
            candidates.append(("visible_text", scope.get_by_text(target.text, exact=True)))
        if target.near_label:
            candidates.append(("near_label", scope.get_by_label(target.near_label, exact=True)))
        if target.structural_fallback:
            if target.structural_surface != "playwright":
                raise UnsupportedTargetError(
                    "UNSUPPORTED_TARGET",
                    "structural fallback is restricted to its declared surface",
                )
            candidates.append(("structural_fallback", scope.locator(target.structural_fallback)))
        return candidates

    async def resolve(self, target: Target) -> ResolvedTarget:
        attempted: list[str] = []
        for strategy, locator in await self.candidates(target):
            attempted.append(strategy)
            try:
                count = await locator.count()
            except PlaywrightError as error:
                raise SurfaceExecutionError(
                    "SURFACE_EXECUTION_FAILED", "browser changed while resolving target"
                ) from error
            if target.ordinal is not None:
                if count > target.ordinal:
                    return ResolvedTarget(
                        locator=locator.nth(target.ordinal),
                        strategy=f"{strategy}+ordinal[{target.ordinal}]",
                    )
                continue
            if count == 1:
                return ResolvedTarget(locator=locator, strategy=strategy)
            if count > 1:
                raise TargetAmbiguousError(
                    "TARGET_AMBIGUOUS",
                    f"semantic target matched {count} elements via {strategy}",
                    {"strategy": strategy, "match_count": count},
                )
        raise TargetNotFoundError(
            "TARGET_NOT_FOUND",
            f"semantic target had zero matches via {', '.join(attempted) or 'no strategy'}",
            {"strategies": attempted},
        )

    async def total_matches(self, target: Target) -> int:
        """Return matches for absence checks while retaining ambiguity semantics elsewhere."""

        total = 0
        try:
            candidates = await self.candidates(target)
        except TargetNotFoundError:
            return 0
        for _, locator in candidates:
            try:
                total += await locator.count()
            except PlaywrightError as error:
                raise SurfaceExecutionError(
                    "SURFACE_EXECUTION_FAILED", "browser changed while counting targets"
                ) from error
        return total
