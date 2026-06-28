from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .osr import OsrReplay, ReplayFrame


class ReplayFinalizeError(ValueError):
    """Raised when lazer-scored metadata cannot be safely applied."""


@dataclass(frozen=True)
class FinalizeReport:
    count_300: int
    count_100: int
    count_50: int
    count_miss: int
    score: int
    max_combo: int
    perfect: bool


def finalize_replay_metadata(
    provisional: OsrReplay,
    scored: OsrReplay,
    *,
    require_matching_frames: bool = True,
) -> OsrReplay:
    validate_scored_replay_matches(
        provisional,
        scored,
        require_matching_frames=require_matching_frames,
    )
    return provisional.with_score_metadata_from(scored)


def finalize_replay_file(
    provisional_path: str | Path,
    scored_path: str | Path,
    output_path: str | Path | None = None,
    *,
    delete_provisional: bool = False,
    require_matching_frames: bool = True,
) -> FinalizeReport:
    provisional_path = Path(provisional_path)
    scored_path = Path(scored_path)
    output_path = provisional_path if output_path is None else Path(output_path)

    provisional = OsrReplay.read_path(provisional_path)
    scored = OsrReplay.read_path(scored_path)
    finalized = finalize_replay_metadata(
        provisional,
        scored,
        require_matching_frames=require_matching_frames,
    )
    finalized.write_path(output_path)
    if delete_provisional and output_path.resolve() != provisional_path.resolve():
        provisional_path.unlink()
    return FinalizeReport(
        count_300=finalized.count_300,
        count_100=finalized.count_100,
        count_50=finalized.count_50,
        count_miss=finalized.count_miss,
        score=finalized.score,
        max_combo=finalized.max_combo,
        perfect=finalized.perfect,
    )


def validate_scored_replay_matches(
    provisional: OsrReplay,
    scored: OsrReplay,
    *,
    require_matching_frames: bool,
) -> None:
    if provisional.mode != scored.mode:
        raise ReplayFinalizeError(f"replay modes differ: {provisional.mode} != {scored.mode}")
    if provisional.beatmap_md5 != scored.beatmap_md5:
        raise ReplayFinalizeError("beatmap MD5 hashes differ")
    if provisional.mods != scored.mods:
        raise ReplayFinalizeError(f"mods differ: {provisional.mods} != {scored.mods}")
    if require_matching_frames and normalized_frames(provisional.frames) != normalized_frames(scored.frames):
        raise ReplayFinalizeError("replay input frames differ")


def normalized_frames(frames: tuple[ReplayFrame, ...]) -> tuple[ReplayFrame, ...]:
    if frames and frames[-1].delta_ms == -12345:
        return frames[:-1]
    return frames
