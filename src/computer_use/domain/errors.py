"""Typed, safe-to-report errors shared across the computer-use system."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


class ComputerUseError(Exception):
    """Base error carrying a stable machine-readable code.

    Callers must pass already-redacted messages and details.  Keeping the error
    small and serializable makes it suitable for CLI rendering and evidence
    events without leaking driver/provider implementation exceptions.
    """

    def __init__(
        self,
        code: str,
        message: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = dict(details or {})

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class CompilationError(ComputerUseError):
    """Raised when a discovery trace cannot be compiled without guessing."""


class InputBindingError(CompilationError):
    """Raised for missing, malformed, or ambiguous parameter bindings."""


class ArtifactApprovalError(ComputerUseError):
    """Raised when replay is attempted with an artifact that is not approved."""


class TargetResolutionError(ComputerUseError):
    """Base class for strict target-resolution failures."""


class TargetNotFoundError(TargetResolutionError):
    """No element matched a semantic target."""


class TargetAmbiguousError(TargetResolutionError):
    """More than one element matched a semantic target."""


class UnsupportedTargetStrategyError(TargetResolutionError):
    """The selected surface cannot honor a requested target strategy."""


class UnsupportedTargetError(UnsupportedTargetStrategyError):
    """Compatibility name used by surface adapters."""


class PolicyDeniedError(ComputerUseError):
    """A policy gate rejected an action before surface execution."""


class SurfaceExecutionError(ComputerUseError):
    """A browser/desktop surface classified an execution failure."""
