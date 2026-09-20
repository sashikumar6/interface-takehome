"""Playwright adapter; raw browser objects never leave this module."""

from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic

from playwright.async_api import (
    Browser,
    BrowserContext,
    Dialog,
    Page,
    Playwright,
    async_playwright,
    expect,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from computer_use.domain.errors import SurfaceExecutionError, TargetNotFoundError
from computer_use.domain.models import (
    ActionableElement,
    ActionResult,
    Condition,
    ConditionKind,
    ConditionResult,
    EvidenceRef,
    Observation,
    ReadResult,
    Target,
)
from computer_use.safety.redaction import DEFAULT_REDACTOR, Redactor
from computer_use.surfaces.resolver import ResolvedTarget, TargetResolver


class PlaywrightSurfaceDriver:
    """One-context browser session with strict targets and observable-state waits."""

    def __init__(
        self,
        *,
        playwright: Playwright,
        browser: Browser,
        context: BrowserContext,
        page: Page,
        observation_directory: Path | None = None,
        redactor: Redactor = DEFAULT_REDACTOR,
    ) -> None:
        self._playwright = playwright
        self._browser = browser
        self._context = context
        self._page = page
        self._resolver = TargetResolver(page)
        self._observation_directory = observation_directory
        self._redactor = redactor
        self._observation_count = 0
        self._unexpected_dialog: str | None = None
        page.on("dialog", self._on_dialog)

    @classmethod
    async def launch(
        cls,
        *,
        headless: bool = True,
        observation_directory: Path | None = None,
        redactor: Redactor = DEFAULT_REDACTOR,
    ) -> PlaywrightSurfaceDriver:
        playwright = await async_playwright().start()
        browser = await playwright.chromium.launch(headless=headless)
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()
        return cls(
            playwright=playwright,
            browser=browser,
            context=context,
            page=page,
            observation_directory=observation_directory,
            redactor=redactor,
        )

    def _on_dialog(self, dialog: Dialog) -> None:
        self._unexpected_dialog = self._redactor.text(dialog.message)
        task = asyncio.create_task(dialog.dismiss())
        task.add_done_callback(lambda _task: None)

    def _check_session(self) -> None:
        if self._page.is_closed():
            raise SurfaceExecutionError("SESSION_EXPIRED", "browser page is closed")
        if self._unexpected_dialog is not None:
            message = self._unexpected_dialog
            self._unexpected_dialog = None
            raise SurfaceExecutionError(
                "UNEXPECTED_DIALOG",
                "unexpected dialog interrupted the browser",
                {"message": message},
            )

    @staticmethod
    def _duration(started: float) -> int:
        return max(0, round((monotonic() - started) * 1_000))

    async def _resolved(self, target: Target, *, timeout_ms: int = 3_000) -> ResolvedTarget:
        deadline = monotonic() + timeout_ms / 1_000
        last_error: TargetNotFoundError | None = None
        while monotonic() < deadline:
            try:
                return await self._resolver.resolve(target)
            except TargetNotFoundError as error:
                last_error = error
                await asyncio.sleep(0.05)
        raise last_error or TargetNotFoundError("TARGET_NOT_FOUND", "target was not found")

    async def navigate(self, url: str) -> ActionResult:
        self._check_session()
        started = monotonic()
        try:
            await self._page.goto(url, wait_until="domcontentloaded", timeout=10_000)
            return ActionResult(
                success=True,
                locator_strategy="url",
                observed=self._redactor.text(self._page.url),
                duration_ms=self._duration(started),
            )
        except PlaywrightTimeoutError as exc:
            raise SurfaceExecutionError("NAVIGATION_FAILED", "navigation timed out") from exc
        except PlaywrightError as exc:
            raise SurfaceExecutionError("NAVIGATION_FAILED", self._redactor.exception(exc)) from exc

    async def click(self, target: Target) -> ActionResult:
        self._check_session()
        started = monotonic()
        resolved = await self._resolved(target)
        try:
            await resolved.locator.click(timeout=5_000)
            return ActionResult(
                success=True,
                locator_strategy=resolved.strategy,
                observed="clicked unique semantic target",
                duration_ms=self._duration(started),
            )
        except PlaywrightTimeoutError as exc:
            raise SurfaceExecutionError("TARGET_NOT_FOUND", "target was not actionable") from exc
        except PlaywrightError as exc:
            raise SurfaceExecutionError("SESSION_EXPIRED", self._redactor.exception(exc)) from exc

    async def type(self, target: Target, text: str) -> ActionResult:
        self._check_session()
        started = monotonic()
        resolved = await self._resolved(target)
        try:
            tag_name = await resolved.locator.evaluate("el => el.tagName")
            if str(tag_name).casefold() == "select":
                await resolved.locator.select_option(value=text, timeout=5_000)
            else:
                await resolved.locator.fill(text, timeout=5_000)
            return ActionResult(
                success=True,
                locator_strategy=resolved.strategy,
                observed="filled unique semantic target",
                duration_ms=self._duration(started),
            )
        except PlaywrightTimeoutError as exc:
            raise SurfaceExecutionError("TARGET_NOT_FOUND", "target was not editable") from exc

    async def read(self, target: Target) -> ReadResult:
        self._check_session()
        started = monotonic()
        resolved = await self._resolved(target)
        try:
            value = " ".join((await resolved.locator.inner_text(timeout=5_000)).split())
            return ReadResult(
                success=True,
                value=self._redactor.text(value),
                locator_strategy=resolved.strategy,
                observed="read unique semantic target",
                duration_ms=self._duration(started),
            )
        except PlaywrightTimeoutError as exc:
            raise SurfaceExecutionError("TARGET_NOT_FOUND", "target could not be read") from exc

    async def wait_for(self, condition: Condition, timeout_ms: int) -> ConditionResult:
        self._check_session()
        started = monotonic()
        try:
            if condition.kind is ConditionKind.URL_MATCHES:
                assert condition.pattern is not None
                await self._page.wait_for_url(re.compile(condition.pattern), timeout=timeout_ms)
                observed = self._page.url
            elif condition.kind is ConditionKind.ELEMENT_PRESENT:
                assert condition.target is not None
                resolved = await self._resolved(condition.target, timeout_ms=timeout_ms)
                await resolved.locator.wait_for(state="visible", timeout=timeout_ms)
                observed = f"present via {resolved.strategy}"
            elif condition.kind is ConditionKind.ELEMENT_ABSENT:
                assert condition.target is not None
                deadline = monotonic() + timeout_ms / 1_000
                while await self._resolver.total_matches(condition.target):
                    if monotonic() >= deadline:
                        return ConditionResult(
                            matched=False,
                            observed="element remained present",
                            duration_ms=self._duration(started),
                        )
                    await asyncio.sleep(0.05)
                observed = "element absent"
            else:
                assert condition.pattern is not None
                if condition.target:
                    resolved = await self._resolved(condition.target, timeout_ms=timeout_ms)
                    locator = resolved.locator
                else:
                    locator = self._page.locator("body")
                await expect(locator).to_contain_text(
                    re.compile(condition.pattern), timeout=timeout_ms
                )
                observed = "text pattern matched"
            return ConditionResult(
                matched=True,
                observed=self._redactor.text(observed),
                duration_ms=self._duration(started),
            )
        except (PlaywrightTimeoutError, AssertionError, TargetNotFoundError) as exc:
            return ConditionResult(
                matched=False,
                observed=self._redactor.exception(exc),
                duration_ms=self._duration(started),
            )

    async def screenshot(self, destination: Path) -> EvidenceRef:
        self._check_session()
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Text redaction covers persisted observations, but screenshots need a
        # separate pixel boundary. Temporarily mask form values and five-digit
        # synthetic identifiers in text nodes, then restore the live DOM so the
        # same browser session continues unchanged.
        await self._page.evaluate(
            r"""() => {
              const mask = value => value.replace(/\b\d{5,}\b/g, value => `***${value.slice(-2)}`);
              window.__computerUseScreenshotRestore = [];
              const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
              let node;
              while ((node = walker.nextNode())) {
                const masked = mask(node.nodeValue || '');
                if (masked !== node.nodeValue) {
                  window.__computerUseScreenshotRestore.push([node, node.nodeValue]);
                  node.nodeValue = masked;
                }
              }
              for (const field of document.querySelectorAll('input, textarea, select')) {
                window.__computerUseScreenshotRestore.push([field, field.value]);
                field.value = field.value ? '[REDACTED]' : '';
              }
            }"""
        )
        try:
            await self._page.screenshot(path=destination, full_page=True)
        finally:
            await self._page.evaluate(
                """() => {
                  for (const [node, value] of window.__computerUseScreenshotRestore || []) {
                    if (node.nodeType === Node.TEXT_NODE) node.nodeValue = value;
                    else node.value = value;
                  }
                  delete window.__computerUseScreenshotRestore;
                }"""
            )
        image_bytes = await asyncio.to_thread(destination.read_bytes)
        digest = hashlib.sha256(image_bytes).hexdigest()
        return EvidenceRef(
            path=str(destination),
            kind="screenshot",
            sha256=digest,
            description="redacted synthetic demo UI checkpoint",
        )

    async def observe(self) -> Observation:
        self._check_session()
        self._observation_count += 1
        visible = await self._page.locator("body").inner_text(timeout=5_000)
        visible = self._redactor.text(" ".join(visible.split())[:8_000])
        alerts: list[str] = []
        elements: list[ActionableElement] = []
        for frame in self._page.frames:
            try:
                frame_alerts = await frame.locator(
                    '[role="alert"],[role="status"],dialog'
                ).all_inner_texts()
                alerts.extend(self._redactor.text(" ".join(item.split())) for item in frame_alerts)
                raw_elements: list[dict[str, str | None]] = await frame.locator(
                    "a,button,input,select,textarea,[role],h1,h2"
                ).evaluate_all(
                    """els => els.slice(0, 80).map(el => ({
                      role: el.getAttribute('role') || ({A:'link',BUTTON:'button',INPUT:'textbox',SELECT:'combobox',TEXTAREA:'textbox',H1:'heading',H2:'heading'}[el.tagName] || null),
                      name: el.getAttribute('aria-label') || (el.labels && el.labels[0] ? el.labels[0].innerText : null) || el.innerText || el.getAttribute('value') || el.getAttribute('placeholder'),
                      text: el.innerText || null
                    }))"""
                )
                for item in raw_elements:
                    elements.append(
                        ActionableElement(
                            role=item.get("role"),
                            name=self._redactor.text(" ".join((item.get("name") or "").split()))[
                                :200
                            ]
                            or None,
                            text=self._redactor.text(" ".join((item.get("text") or "").split()))[
                                :200
                            ]
                            or None,
                            frame=frame.name or None,
                        )
                    )
            except PlaywrightError:
                continue
        screenshot_ref: EvidenceRef | None = None
        if self._observation_directory is not None:
            screenshot_ref = await self.screenshot(
                self._observation_directory / f"observation-{self._observation_count:03d}.png"
            )
        url = self._redactor.text(self._page.url)
        title = self._redactor.text(await self._page.title())
        digest_payload = f"{url}\n{title}\n{visible}".encode()
        return Observation(
            url=url,
            title=title,
            elements=tuple(elements[:120]),
            alerts=tuple(alerts[:20]),
            visible_text=visible,
            screenshot=screenshot_ref,
            timestamp=datetime.now(UTC),
            digest=hashlib.sha256(digest_payload).hexdigest(),
        )

    async def operator_click(self, target: Target) -> ActionResult:
        """Execute a human-directed click without exposing the raw Playwright page."""

        return await self.click(target)

    async def close(self) -> None:
        try:
            await self._context.close()
            await self._browser.close()
        finally:
            await self._playwright.stop()
