#!/usr/bin/env python3
"""Conservative repository secret and persisted-member-ID scan."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP_PARTS = {".git", ".venv", ".mypy_cache", ".pytest_cache", ".ruff_cache", "build"}
TEXT_SUFFIXES = {
    ".css",
    ".env",
    ".example",
    ".html",
    ".json",
    ".jsonl",
    ".md",
    ".py",
    ".sh",
    ".toml",
    ".txt",
    ".yaml",
    ".yml",
}
SECRET_PATTERNS = {
    "OpenAI key": re.compile(r"sk-(?:proj-)?[A-Za-z0-9_-]{20,}"),
    "Anthropic key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"gh[oprsu]_[A-Za-z0-9]{20,}"),
    "bearer token": re.compile(r"Bearer\s+[A-Za-z0-9._~-]{20,}", re.IGNORECASE),
}
RAW_MEMBER_IDS = re.compile(r"(?<!\d)(?:12345|55555|77777|99999)(?!\d)")


def candidates() -> list[Path]:
    return [
        path
        for path in ROOT.rglob("*")
        if path.is_file()
        and not any(part in SKIP_PARTS for part in path.relative_to(ROOT).parts)
        and (path.suffix in TEXT_SUFFIXES or path.name in {".env.example", ".gitignore"})
    ]


def main() -> int:
    findings: list[str] = []
    for path in candidates():
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        relative = path.relative_to(ROOT)
        for label, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append(f"{relative}: possible {label}")
        if (
            relative.parts
            and relative.parts[0] in {"artifacts", "evidence"}
            and RAW_MEMBER_IDS.search(text)
        ):
            findings.append(f"{relative}: raw synthetic member ID persisted")
    if findings:
        print("secret/data scan failed:", file=sys.stderr)
        for finding in findings:
            print(f"  - {finding}", file=sys.stderr)
        return 1
    print("secret/data scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
