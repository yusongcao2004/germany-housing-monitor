#!/usr/bin/env python3
"""Fail closed when a release tree contains personal data or runtime artifacts."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IGNORED_PARTS = {".git", "__pycache__", ".venv", "node_modules"}
FORBIDDEN_PARTS = {"state", "browser-profile", "backups"}
FORBIDDEN_FILES = {"config.json", ".env"}
MAX_FILE_BYTES = 2 * 1024 * 1024
TEXT_SUFFIXES = {
    "",
    ".json",
    ".md",
    ".plist",
    ".py",
    ".toml",
    ".txt",
    ".yml",
    ".yaml",
}
CONTENT_PATTERNS = {
    "macOS user path": re.compile(r"/Users/[^/\s]+/"),
    "private key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "GitHub token": re.compile(r"\b(?:gh[opusr]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b"),
    "Telegram bot token": re.compile(r"\b\d{8,12}:[A-Za-z0-9_-]{30,}\b"),
}
EMAIL_PATTERN = re.compile(r"(?i)\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b")
RESERVED_EMAIL_SUFFIXES = (
    ".example",
    ".invalid",
    ".test",
    "example.com",
    "example.net",
    "example.org",
)
PUBLIC_SERVICE_DOMAINS = {
    "immoscout24.de",
    "immowelt.de",
    "kleinanzeigen.de",
    "wg-gesucht.de",
}


def allowed_email_domain(domain: str) -> bool:
    normalized = domain.lower().rstrip(".")
    if normalized.endswith(RESERVED_EMAIL_SUFFIXES):
        return True
    return any(
        normalized == service or normalized.endswith(f".{service}")
        for service in PUBLIC_SERVICE_DOMAINS
    )


def tracked_candidate(path: Path) -> bool:
    relative = path.relative_to(ROOT)
    return not any(part in IGNORED_PARTS for part in relative.parts)


def main() -> int:
    findings: list[dict[str, str]] = []
    checked = 0
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file() or not tracked_candidate(path):
            continue
        relative = path.relative_to(ROOT)
        checked += 1
        if relative.name in FORBIDDEN_FILES:
            findings.append({"file": str(relative), "issue": "local secret/config file"})
        if any(part in FORBIDDEN_PARTS for part in relative.parts):
            findings.append({"file": str(relative), "issue": "runtime/private directory"})
        size = path.stat().st_size
        if size > MAX_FILE_BYTES:
            findings.append({"file": str(relative), "issue": f"file too large: {size}"})
            continue
        if relative != Path(".env.example") and path.suffix.lower() not in TEXT_SUFFIXES:
            findings.append({"file": str(relative), "issue": "unexpected binary/file type"})
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            findings.append({"file": str(relative), "issue": "non-UTF-8 file"})
            continue
        if relative == Path("scripts/preflight.py"):
            continue
        for match in EMAIL_PATTERN.finditer(content):
            if not allowed_email_domain(match.group(1)):
                findings.append(
                    {
                        "file": str(relative),
                        "issue": "non-example email address",
                        "sample": match.group(0)[:80],
                    }
                )
        for label, pattern in CONTENT_PATTERNS.items():
            match = pattern.search(content)
            if match:
                findings.append(
                    {
                        "file": str(relative),
                        "issue": label,
                        "sample": match.group(0)[:80],
                    }
                )
    result = {
        "status": "PASS" if not findings else "FAIL",
        "files_checked": checked,
        "findings": findings,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if not findings else 1


if __name__ == "__main__":
    raise SystemExit(main())
