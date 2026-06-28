from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .synthesis import (
    LEGACY_LEFT_ACTION_MASK,
    LEGACY_RIGHT_ACTION_MASK,
    AbsoluteFrame,
    default_lazer_skip_target_ms,
    to_absolute_frames,
)
from .osr import OsrReplay


@dataclass(frozen=True)
class LazerReplayFrame:
    time_ms: int
    x: float
    y: float
    actions: tuple[str, ...]


@dataclass(frozen=True)
class LazerReplayExport:
    beatmap_md5: str
    player_name: str
    frames: tuple[LazerReplayFrame, ...]
    skip_press_ms: int | None = None
    skip_target_ms: int | None = None


def replay_to_lazer_export(
    replay: OsrReplay,
    *,
    skip_press_ms: int | None = None,
    skip_target_ms: int | None = None,
) -> LazerReplayExport:
    frames = tuple(absolute_frame_to_lazer_frame(frame) for frame in to_absolute_frames(replay.frames))
    return LazerReplayExport(
        beatmap_md5=replay.beatmap_md5,
        player_name=replay.player_name,
        frames=frames,
        skip_press_ms=skip_press_ms,
        skip_target_ms=skip_target_ms,
    )


def absolute_frame_to_lazer_frame(frame: AbsoluteFrame) -> LazerReplayFrame:
    actions: list[str] = []
    if frame.keys & LEGACY_LEFT_ACTION_MASK:
        actions.append("LeftButton")
    if frame.keys & LEGACY_RIGHT_ACTION_MASK:
        actions.append("RightButton")
    if frame.keys & 16:
        actions.append("Smoke")
    return LazerReplayFrame(
        time_ms=frame.time_ms,
        x=frame.x,
        y=frame.y,
        actions=tuple(actions),
    )


def write_lazer_json(export: LazerReplayExport, path: str | Path) -> None:
    payload = asdict(export)
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
