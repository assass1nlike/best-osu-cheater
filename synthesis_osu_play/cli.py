from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .beatmap import Beatmap, BeatmapFormatError
from .finalize import ReplayFinalizeError, finalize_replay_file
from .lazer import replay_to_lazer_export, write_lazer_json
from .osr import OsrFormatError, OsrReplay
from .synthesis import SynthesisError, synthesize_replays


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "synthesize":
            return synthesize_command(args)
        if args.command == "finalize":
            return finalize_command(args)
    except (OSError, BeatmapFormatError, OsrFormatError, ReplayFinalizeError, SynthesisError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    parser.error("missing command")
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="synthesis-osu-play",
        description="Offline osu!stable replay synthesis tools.",
    )
    subparsers = parser.add_subparsers(dest="command")
    synthesize = subparsers.add_parser(
        "synthesize",
        help="average two .osr replay input streams into a new .osr file",
    )
    synthesize.add_argument("first", type=Path, help="first source .osr replay")
    synthesize.add_argument("second", type=Path, help="second source .osr replay")
    synthesize.add_argument("output", type=Path, help="output .osr replay")
    synthesize.add_argument(
        "--beatmap",
        type=Path,
        default=None,
        help="optional .osu beatmap used for object-aware click matching",
    )
    synthesize.add_argument(
        "--player-name",
        default=None,
        help="player name stored in the synthesized replay metadata",
    )
    synthesize.add_argument(
        "--allow-key-mismatch",
        action="store_true",
        help="pair only matching key intervals when source interval counts differ",
    )
    synthesize.add_argument(
        "--first-skip-ms",
        type=int,
        default=None,
        help="time when the first replay presses skip; earliest provided skip time is reported",
    )
    synthesize.add_argument(
        "--second-skip-ms",
        type=int,
        default=None,
        help="time when the second replay presses skip; earliest provided skip time is reported",
    )
    synthesize.add_argument(
        "--intro-end-ms",
        type=int,
        default=None,
        help="fixed post-skip/intro-end time; defaults to lazer skip target when --beatmap is set",
    )
    synthesize.add_argument(
        "--lazer-json",
        type=Path,
        default=None,
        help="optional JSON export of lazer OsuReplayFrame-style actions for local tooling",
    )
    synthesize.add_argument(
        "--local-score",
        action="store_true",
        help="debug fallback: recompute score metadata locally instead of leaving provisional metadata",
    )
    finalize = subparsers.add_parser(
        "finalize",
        help="copy osu!lazer-scored metadata from another .osr back into a synthesized replay",
    )
    finalize.add_argument("provisional", type=Path, help="synthesized replay that was imported into osu!lazer")
    finalize.add_argument("scored", type=Path, help=".osr containing the score metadata produced by osu!lazer")
    finalize.add_argument(
        "output",
        type=Path,
        nargs="?",
        default=None,
        help="final output .osr; defaults to overwriting the provisional replay",
    )
    finalize.add_argument(
        "--delete-provisional",
        action="store_true",
        help="delete the provisional replay after writing a separate output path",
    )
    finalize.add_argument(
        "--allow-frame-mismatch",
        action="store_true",
        help="allow metadata copy even if replay frames differ; beatmap and mods are still checked",
    )
    return parser


def synthesize_command(args: argparse.Namespace) -> int:
    first = OsrReplay.read_path(args.first)
    second = OsrReplay.read_path(args.second)
    beatmap = Beatmap.read_path(args.beatmap) if args.beatmap is not None else None
    if beatmap is not None and beatmap.md5 != first.beatmap_md5:
        print(
            "warning: beatmap MD5 differs from replay metadata; "
            "continuing with the provided beatmap",
            file=sys.stderr,
        )
    result = synthesize_replays(
        first,
        second,
        beatmap=beatmap,
        player_name=args.player_name,
        allow_key_mismatch=args.allow_key_mismatch,
        first_skip_ms=args.first_skip_ms,
        second_skip_ms=args.second_skip_ms,
        intro_end_ms=args.intro_end_ms,
        recompute_score_metadata=args.local_score,
    )
    result.replay.write_path(args.output)
    if args.lazer_json is not None:
        write_lazer_json(
            replay_to_lazer_export(
                result.replay,
                skip_press_ms=result.report.skip_press_ms,
                skip_target_ms=result.report.intro_end_ms,
            ),
            args.lazer_json,
        )
    print(
        "wrote "
        f"{args.output} "
        f"({result.report.frame_count} frames, {result.report.duration_ms} ms)"
    )
    for bit, count in sorted(result.report.key_interval_counts.items()):
        print(f"key bit {bit}: {count} intervals")
    if result.report.matched_object_count or result.report.dropped_object_count:
        print(
            f"matched objects: {result.report.matched_object_count}, "
            f"dropped objects: {result.report.dropped_object_count}"
        )
    if result.report.skip_press_ms is not None:
        print(
            f"skip press: {result.report.skip_press_ms} ms"
            + (
                f", skip target: {result.report.intro_end_ms} ms"
                if result.report.intro_end_ms is not None
                else ""
            )
        )
    if args.lazer_json is not None:
        print(f"wrote lazer json {args.lazer_json}")
    if beatmap is not None and not args.local_score:
        print("score metadata is provisional; run this replay in osu!lazer and finalize it from a lazer-scored metadata source")
    return 0


def finalize_command(args: argparse.Namespace) -> int:
    report = finalize_replay_file(
        args.provisional,
        args.scored,
        args.output,
        delete_provisional=args.delete_provisional,
        require_matching_frames=not args.allow_frame_mismatch,
    )
    output = args.provisional if args.output is None else args.output
    print(
        "finalized "
        f"{output} "
        f"(score {report.score}, combo {report.max_combo}, "
        f"300/100/50/miss {report.count_300}/{report.count_100}/{report.count_50}/{report.count_miss})"
    )
    return 0
