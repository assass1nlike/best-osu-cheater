#!/usr/bin/env python3
"""Prepare osu!lazer's persisted mouse settings for WSLg."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import tempfile
from typing import Any


MOUSE_HANDLER_TYPE = "osu.Framework.Input.Handlers.Mouse.MouseHandler"


def disable_relative_mouse_mode(payload: dict[str, Any]) -> bool:
    """Disable SDL relative mouse mode and return whether the payload changed."""

    handlers = payload.get("InputHandlers")
    if not isinstance(handlers, list):
        raise ValueError("input.json does not contain an InputHandlers list")

    mouse_handlers = [
        handler
        for handler in handlers
        if isinstance(handler, dict)
        and str(handler.get("$type", "")).split(",", 1)[0] == MOUSE_HANDLER_TYPE
    ]
    if len(mouse_handlers) != 1:
        raise ValueError(f"expected exactly one mouse input handler, found {len(mouse_handlers)}")

    handler = mouse_handlers[0]
    changed = handler.get("UseRelativeMode") is not False
    handler["UseRelativeMode"] = False
    return changed


def prepare_input_config(path: Path) -> bool:
    """Update *path* atomically and return whether its contents changed."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"osu!lazer input configuration does not exist: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid JSON in osu!lazer input configuration: {path}") from exc

    if not isinstance(payload, dict):
        raise ValueError("input.json root must be an object")
    if not disable_relative_mouse_mode(payload):
        return False

    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    mode = path.stat().st_mode
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(encoded)
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
        description="Disable SDL relative mouse mode in osu!lazer's input.json before WSLg startup."
    )
    parser.add_argument("--config", type=Path, required=True, help="Path to osu!lazer input.json")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        changed = prepare_input_config(args.config.expanduser())
    except ValueError as exc:
        print(f"error: {exc}")
        return 1

    state = "disabled" if changed else "already disabled"
    print(f"WSLg relative mouse mode: {state}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
