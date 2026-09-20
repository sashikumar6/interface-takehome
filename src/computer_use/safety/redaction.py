"""Centralized, conservative redaction for persisted and logged data.

Redaction intentionally happens at serialization boundaries as well as at call
sites.  This makes accidental persistence of a newly-added sensitive field much
less likely.  The functions in this module return copies and never mutate input
objects.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping, Sequence
from enum import Enum
from pathlib import Path
from typing import Any

try:  # Pydantic is a project dependency, but keep this helper independently usable.
    from pydantic import BaseModel
except ImportError:  # pragma: no cover - exercised only outside the project environment
    BaseModel = None  # type: ignore[assignment,misc]

REDACTED = "[REDACTED]"
MASKED_MEMBER_PREFIX = "***"

_NORMALIZE_KEY = re.compile(r"[^a-z0-9]")

# Key matching is deliberately broad at persistence boundaries.  Benign fields
# such as ``token_count`` and ``session_timeout`` are explicitly excluded below.
_FULL_SECRET_KEYS = {
    "apikey",
    "authorization",
    "proxyauthorization",
    "cookie",
    "setcookie",
    "password",
    "passwd",
    "pwd",
    "ssn",
    "socialsecuritynumber",
    "accountnumber",
    "bankaccount",
    "clientsecret",
    "credential",
    "credentials",
    "secret",
    "token",
    "accesstoken",
    "refreshtoken",
    "idtoken",
    "sessionid",
    "sessionidentifier",
    "storageState",
}
_SAFE_TOKEN_KEYS = {"tokencount", "maxtokens", "tokenbudget"}
_SAFE_SESSION_KEYS = {"sessiontimeout", "sessionreference"}
_MEMBER_KEYS = {"memberid", "membernumber", "membershipid", "membershipnumber"}

_BEARER_RE = re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{6,})")
_KNOWN_API_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-ant-[A-Za-z0-9_-]{12,}|sk-[A-Za-z0-9_-]{16,})(?![A-Za-z0-9])"
)
_NAMED_SECRET_RE = re.compile(
    r"(?ix)\b("
    r"(?:openai|anthropic)?[_-]?api[_-]?key|"
    r"authorization|password|passwd|pwd|secret|credential|"
    r"access[_-]?token|refresh[_-]?token|session[_-]?id|"
    r"ssn|social[_ -]?security[_ -]?number|account[_ -]?number"
    r")\b(\s*(?::|=)\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
_COOKIE_HEADER_RE = re.compile(r"(?im)\b(set-cookie|cookie)(\s*:\s*)[^\r\n]+")
_MEMBER_TEXT_RE = re.compile(
    r"(?i)\b(member(?:ship)?[_ -]?(?:id|number))(\s*(?::|=)\s*|\s+)"
    r"(?P<quote>[\"']?)(?P<value>[A-Za-z0-9-]{2,})(?P=quote)"
)
_MEMBER_URL_RE = re.compile(r"(?i)(/members/)(?P<value>[0-9]{5,})(?=/|$|[?#\s])")


def _normalized_key(key: object) -> str:
    return _NORMALIZE_KEY.sub("", str(key).casefold())


def _key_kind(key: object) -> str | None:
    normalized = _normalized_key(key)
    if normalized in _MEMBER_KEYS or (
        "member" in normalized and (normalized.endswith("id") or normalized.endswith("number"))
    ):
        return "member"
    if normalized in _SAFE_TOKEN_KEYS or normalized in _SAFE_SESSION_KEYS:
        return None
    if normalized in _FULL_SECRET_KEYS:
        return "secret"
    if any(
        marker in normalized
        for marker in ("password", "credential", "authorization", "apikey", "accountnumber")
    ):
        return "secret"
    if normalized.endswith("token") or normalized.endswith("secret"):
        return "secret"
    if normalized.startswith("cookie") or normalized.endswith("cookie"):
        return "secret"
    if normalized.startswith("session") and normalized.endswith(("id", "identifier")):
        return "secret"
    if normalized in {"ssn", "socialsecurity", "socialsecuritynumber"}:
        return "secret"
    return None


def mask_member_id(value: object, *, visible_suffix: int = 2) -> str:
    """Mask a member identifier while retaining only a short diagnostic suffix."""

    raw = str(value)
    if not raw or visible_suffix <= 0:
        return MASKED_MEMBER_PREFIX
    suffix = raw[-visible_suffix:] if len(raw) > visible_suffix else ""
    return f"{MASKED_MEMBER_PREFIX}{suffix}"


def redact_text(value: str) -> str:
    """Redact common secrets and identifiers embedded in free-form text."""

    text = _BEARER_RE.sub("Bearer [REDACTED]", value)
    text = _KNOWN_API_KEY_RE.sub(REDACTED, text)
    text = _COOKIE_HEADER_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}{REDACTED}", text)

    def replace_named(match: re.Match[str]) -> str:
        return f"{match.group(1)}{match.group(2)}{REDACTED}"

    text = _NAMED_SECRET_RE.sub(replace_named, text)

    def replace_member(match: re.Match[str]) -> str:
        masked = mask_member_id(match.group("value"))
        quote = match.group("quote")
        return f"{match.group(1)}{match.group(2)}{quote}{masked}{quote}"

    text = _MEMBER_TEXT_RE.sub(replace_member, text)
    return _MEMBER_URL_RE.sub(
        lambda match: f"{match.group(1)}{mask_member_id(match.group('value'))}", text
    )


def _redact(value: Any, key_hint: object | None, seen: set[int]) -> Any:
    key_kind = _key_kind(key_hint) if key_hint is not None else None
    if key_kind == "secret":
        return REDACTED
    if key_kind == "member":
        return mask_member_id(value)
    if (
        _normalized_key(key_hint) in {"value", "readvalue"}
        and isinstance(value, str)
        and re.fullmatch(r"[0-9]{5,}", value)
    ):
        return mask_member_id(value)

    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, bytes):
        return f"[REDACTED BINARY: {len(value)} bytes]"
    if isinstance(value, (Path, Enum)):
        primitive = str(value) if isinstance(value, Path) else value.value
        return _redact(primitive, key_hint, seen)
    if isinstance(value, BaseException):
        return redact_text(str(value))

    object_id = id(value)
    if object_id in seen:
        return "[CYCLE]"

    if BaseModel is not None and isinstance(value, BaseModel):
        seen.add(object_id)
        try:
            return _redact(value.model_dump(mode="python"), key_hint, seen)
        finally:
            seen.remove(object_id)
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        seen.add(object_id)
        try:
            return _redact(dataclasses.asdict(value), key_hint, seen)
        finally:
            seen.remove(object_id)
    if isinstance(value, Mapping):
        seen.add(object_id)
        try:
            return {str(key): _redact(item, key, seen) for key, item in value.items()}
        finally:
            seen.remove(object_id)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        seen.add(object_id)
        try:
            redacted = [_redact(item, key_hint, seen) for item in value]
            return tuple(redacted) if isinstance(value, tuple) else redacted
        finally:
            seen.remove(object_id)
    if isinstance(value, (set, frozenset)):
        seen.add(object_id)
        try:
            return sorted((_redact(item, key_hint, seen) for item in value), key=repr)
        finally:
            seen.remove(object_id)

    # Unknown rich objects are never introspected: __dict__ can contain browser
    # state, credentials, or non-serializable handles.  Persist only safe text.
    return redact_text(str(value))


def redact(value: Any, *, key_hint: object | None = None) -> Any:
    """Return a recursively redacted copy suitable for evidence or logging."""

    return _redact(value, key_hint, set())


def redact_exception(error: BaseException) -> str:
    """Return a safe exception summary without traceback-local state."""

    return redact_text(f"{type(error).__name__}: {error}")


class Redactor:
    """Configurable facade used for dependency injection and tests."""

    def redact(self, value: Any, *, key_hint: object | None = None) -> Any:
        return redact(value, key_hint=key_hint)

    def text(self, value: str) -> str:
        return redact_text(value)

    def exception(self, error: BaseException) -> str:
        return redact_exception(error)


DEFAULT_REDACTOR = Redactor()
