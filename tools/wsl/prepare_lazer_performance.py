#!/usr/bin/env python3
"""Prepare osu!lazer's renderer settings for a WSLg performance profile."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import tempfile


PROFILE_SETTINGS = {
    "balanced": {
        "WindowedSize": "1366x768",
        "WindowMode": "Windowed",
        "ExecutionMode": "MultiThreaded",
    },
    "quality": {
        "WindowedSize": "1920x1080",
        "WindowMode": "Windowed",
        "ExecutionMode": "MultiThreaded",
    },
    "native": {
        "WindowMode": "Windowed",
        "ExecutionMode": "MultiThreaded",
    },
}


def update_settings(contents: str, settings: dict[str, str]) -> str:
    """Return *contents* with the requested framework settings applied."""

    pending = dict(settings)
    result: list[str] = []
    line_ending = "\r\n" if "\r\n" in contents else "\n"

    for line in contents.splitlines():
        match = re.match(r"^(\s*([^=\s]+)\s*=\s*)(.*)$", line)
        if match is not None and match.group(2) in pending:
            key = match.group(2)
            result.append(f"{match.group(1)}{pending.pop(key)}")
        else:
            result.append(line)

    result.extend(f"{key} = {value}" for key, value in pending.items())
    trailing_newline = line_ending if contents.endswith(("\n", "\r")) or result else ""
    return line_ending.join(result) + trailing_newline


def prepare_framework_config(path: Path, profile: str) -> bool:
    """Apply *profile* atomically and return whether the file changed."""

    try:
        settings = PROFILE_SETTINGS[profile]
    except KeyError as exc:
        raise ValueError(f"unknown performance profile: {profile}") from exc

    try:
        contents = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(f"osu!lazer framework configuration does not exist: {path}") from exc

    updated = update_settings(contents, settings)
    if updated == contents:
        return False

    mode = path.stat().st_mode
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            newline="",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(updated)
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)

        os.chmod(temporary_path, mode)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Apply a WSLg performance profile to osu!lazer's framework.ini."
    )
    parser.add_argument("--config", type=Path, required=True, help="Path to framework.ini")
    parser.add_argument("--profile", choices=PROFILE_SETTINGS, default="balanced")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        changed = prepare_framework_config(args.config.expanduser(), args.profile)
    except ValueError as exc:
        print(f"error: {exc}")
        return 1

    state = "applied" if changed else "already applied"
    print(f"WSLg {args.profile} performance profile: {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
