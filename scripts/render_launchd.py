#!/usr/bin/env python3
"""Render user-local launchd files without committing absolute paths."""

from __future__ import annotations

import argparse
import plistlib
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def replace_strings(value: Any, replacements: dict[str, str]) -> Any:
    if isinstance(value, str):
        for old, new in replacements.items():
            value = value.replace(old, new)
        return value
    if isinstance(value, list):
        return [replace_strings(item, replacements) for item in value]
    if isinstance(value, dict):
        return {key: replace_strings(item, replacements) for key, item in value.items()}
    return value


def render(template: Path, destination: Path, replacements: dict[str, str]) -> None:
    with template.open("rb") as handle:
        payload = plistlib.load(handle)
    rendered = replace_strings(payload, replacements)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as handle:
        plistlib.dump(rendered, handle, sort_keys=False)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--project-dir", type=Path, default=ROOT)
    parser.add_argument("--output-dir", type=Path, default=ROOT / "local-launchd")
    args = parser.parse_args()
    python = args.python.expanduser().resolve()
    project = args.project_dir.expanduser().resolve()
    output = args.output_dir.expanduser().resolve()
    if not python.is_file() or not project.joinpath("monitor.py").is_file():
        parser.error("--python and --project-dir must point to existing files")
    replacements = {
        "/ABSOLUTE/PATH/TO/python": str(python),
        "/ABSOLUTE/PATH/TO/germany-housing-monitor": str(project),
    }
    render(
        project / "ai.housing-monitor.plist",
        output / "io.github.housing-monitor.local.plist",
        replacements,
    )
    render(
        project / "ai.housing-browser.plist",
        output / "io.github.housing-browser.local.plist",
        replacements,
    )
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
