from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

from .beatmap import Beatmap, BeatmapFormatError
from .finalize import ReplayFinalizeError, finalize_replay_file
from .lazer import replay_to_lazer_export, write_lazer_json
from .online import (
    DEFAULT_BATCH_OUTPUT_DIR,
    DEFAULT_BATCH_WORK_DIR,
    OnlineSynthesisError,
    batch_synthesize_dt,
    download_and_synthesize,
    import_batch_beatmaps,
)
from .osr import OsrFormatError, OsrReplay
from .synthesis import SynthesisError, synthesize_replays
from .visualize import (
    DEFAULT_VIDEO_FPS,
    DEFAULT_VIDEO_HEIGHT,
    DEFAULT_VIDEO_SPEED,
    DEFAULT_VIDEO_WIDTH,
    write_replay_debug_video,
)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "synthesize":
            return synthesize_command(args)
        if args.command == "download-synthesize":
            return download_synthesize_command(args)
        if args.command == "batch-dt":
            return batch_dt_command(args)
        if args.command == "finalize":
            return finalize_command(args)
    except (OSError, BeatmapFormatError, OsrFormatError, OnlineSynthesisError, ReplayFinalizeError, SynthesisError) as exc:
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
        "--first-weight",
        type=float,
        default=1.0,
        help="multiplier applied to the first replay's dynamic blend weight",
    )
    synthesize.add_argument(
        "--second-weight",
        type=float,
        default=1.0,
        help="multiplier applied to the second replay's dynamic blend weight",
    )
    synthesize.add_argument(
        "--synthesis-seed",
        type=int,
        default=None,
        help="optional seed for reproducible synthesis randomness",
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
    synthesize.add_argument(
        "--spinner-library",
        type=Path,
        default=None,
        help="spinner trajectory library JSON; defaults to the bundled library",
    )
    synthesize.add_argument(
        "--spinner-mode",
        default="all",
        choices=("all", "threshold", "never"),
        help="spinner replacement mode: all | threshold | never (default: all)",
    )
    synthesize.add_argument(
        "--dt",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="assume DT playback for spinner RPM checks; use --no-dt to disable (default: on)",
    )
    synthesize.add_argument(
        "--greedy-match",
        "--any-order-match",
        action="store_true",
        help="use osu!lazer AnyOrder matching instead of the default Legacy matching",
    )
    add_debug_video_arguments(synthesize)
    download_synthesize = subparsers.add_parser(
        "download-synthesize",
        help="download two leaderboard replays for a beatmap and synthesize them",
    )
    download_synthesize.add_argument("beatmap_id", type=int, help="online beatmap difficulty id")
    download_synthesize.add_argument("first_rank", type=int, help="first global leaderboard rank")
    download_synthesize.add_argument("second_rank", type=int, help="second global leaderboard rank")
    download_synthesize.add_argument("output", type=Path, help="output .osr replay")
    download_synthesize.add_argument(
        "--work-dir",
        type=Path,
        default=None,
        help="directory for downloaded beatmap, leaderboard JSON, and source replays; defaults to artifacts/<beatmap_id>",
    )
    download_synthesize.add_argument(
        "--player-name",
        default=None,
        help="player name stored in the synthesized replay metadata",
    )
    download_synthesize.add_argument(
        "--first-weight",
        type=float,
        default=1.0,
        help="multiplier applied to the first replay's dynamic blend weight",
    )
    download_synthesize.add_argument(
        "--second-weight",
        type=float,
        default=1.0,
        help="multiplier applied to the second replay's dynamic blend weight",
    )
    download_synthesize.add_argument(
        "--synthesis-seed",
        type=int,
        default=None,
        help="optional seed for reproducible synthesis randomness",
    )
    download_synthesize.add_argument(
        "--mods",
        nargs="*",
        default=(),
        help="optional exact-mod leaderboard filter, e.g. --mods HD DT",
    )
    download_synthesize.add_argument(
        "--scope",
        default="global",
        choices=("global", "country", "friend", "team"),
        help="online leaderboard scope",
    )
    download_synthesize.add_argument(
        "--ruleset",
        default="osu",
        help="ruleset short name; only osu replays are supported by synthesis",
    )
    download_synthesize.add_argument(
        "--lazer-storage",
        type=Path,
        default=None,
        help="osu!lazer storage directory containing game.ini; defaults to the path in %%APPDATA%%\\osu\\storage.ini",
    )
    download_synthesize.add_argument(
        "--token-config",
        type=Path,
        default=None,
        help="explicit osu!lazer game.ini path to read the saved API token from",
    )
    download_synthesize.add_argument(
        "--api-url",
        default="https://osu.ppy.sh",
        help="osu! API root; defaults to production",
    )
    download_synthesize.add_argument(
        "--api-version",
        type=int,
        default=None,
        help="x-api-version header; defaults to today's YYYYMMDD",
    )
    download_synthesize.add_argument(
        "--with-video",
        action="store_true",
        help="download beatmapset video assets too",
    )
    download_synthesize.add_argument(
        "--lazer-path",
        type=Path,
        default=None,
        help="osu!lazer install directory or osu!.exe used for automatic beatmap import; defaults to OSU_LAZER_PATH or %%LOCALAPPDATA%%\\osulazer\\current",
    )
    download_synthesize.add_argument(
        "--lazer-json",
        type=Path,
        default=None,
        help="optional JSON export of lazer OsuReplayFrame-style actions for local tooling",
    )
    download_synthesize.add_argument(
        "--local-score",
        action="store_true",
        help="debug fallback: recompute score metadata locally instead of leaving provisional metadata",
    )
    download_synthesize.add_argument(
        "--spinner-library",
        type=Path,
        default=None,
        help="spinner trajectory library JSON; defaults to the bundled library",
    )
    download_synthesize.add_argument(
        "--spinner-mode",
        default="all",
        choices=("all", "threshold", "never"),
        help="spinner replacement mode: all | threshold | never (default: all)",
    )
    download_synthesize.add_argument(
        "--dt",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="assume DT playback for spinner RPM checks; use --no-dt to disable (default: on)",
    )
    download_synthesize.add_argument(
        "--greedy-match",
        "--any-order-match",
        action="store_true",
        help="use osu!lazer AnyOrder matching instead of the default Legacy matching",
    )
    add_debug_video_arguments(download_synthesize)
    batch_dt = subparsers.add_parser(
        "batch-dt",
        help="randomly select ranked osu! beatmaps and synthesize a batch of DT replays",
    )
    batch_dt.add_argument("count", type=int, help="number of synthesized replays to create")
    batch_dt.add_argument(
        "--beatmap-selection",
        choices=("recent", "random-all"),
        default="random-all",
        help="beatmap candidate selection: random-all (default) or recent age-filtered results",
    )
    batch_dt.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_BATCH_OUTPUT_DIR,
        help=f"output directory for [beatmap_id].osr files; defaults to {DEFAULT_BATCH_OUTPUT_DIR}",
    )
    batch_dt.add_argument(
        "--work-dir",
        type=Path,
        default=DEFAULT_BATCH_WORK_DIR,
        help=f"cache directory for search pages, beatmaps, and source replays; defaults to {DEFAULT_BATCH_WORK_DIR}",
    )
    batch_dt.add_argument(
        "--min-age-days",
        type=int,
        default=3,
        help="minimum ranked age in days; defaults to 3",
    )
    batch_dt.add_argument(
        "--min-star",
        type=float,
        default=4.5,
        help="minimum star rating; defaults to 4.5",
    )
    batch_dt.add_argument(
        "--max-star",
        type=float,
        default=5.0,
        help="maximum star rating; defaults to 5.0",
    )
    batch_dt.add_argument(
        "--min-skip-time",
        type=int,
        default=0,
        help="minimum first hit object time in ms; 0 disables the filter (default: 0)",
    )
    batch_dt.add_argument(
        "--no-skip-filter",
        action="store_true",
        help="force-disable the short-intro filter (the default)",
    )
    batch_dt.add_argument(
        "--spinner-library",
        type=Path,
        default=None,
        help="spinner trajectory library JSON; defaults to the bundled library",
    )
    batch_dt.add_argument(
        "--spinner-mode",
        default="all",
        choices=("all", "threshold", "never"),
        help="spinner replacement mode: all | threshold | never (default: all)",
    )
    batch_dt.add_argument(
        "--dt",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="assume DT playback for spinner RPM checks; use --no-dt to disable (default: on)",
    )
    batch_dt.add_argument(
        "--player-name",
        default=None,
        help="player name stored in synthesized replay metadata",
    )
    batch_dt.add_argument(
        "--search-pages-limit",
        type=int,
        default=None,
        help="maximum ranked search pages in recent mode; defaults to 25 (ignored by random-all)",
    )
    batch_dt.add_argument(
        "--random-search-pages",
        "--random-id-batches",
        dest="random_search_pages",
        type=int,
        default=None,
        help="maximum consecutive ranked search pages after a random start page (API pages 1-200); defaults to 200",
    )
    batch_dt.add_argument(
        "--leaderboard-limit",
        type=int,
        default=100,
        help="maximum leaderboard scores to consider per beatmap; defaults to 100",
    )
    batch_dt.add_argument(
        "--request-delay-s",
        type=float,
        default=2.0,
        help="minimum delay between osu! API requests; defaults to 2 seconds",
    )
    batch_dt.add_argument(
        "--random-seed",
        type=int,
        default=None,
        help="optional seed for random ranked-time pages and replay-pair selection",
    )
    batch_dt.add_argument(
        "--synthesis-seed",
        type=int,
        default=None,
        help="optional seed for reproducible synthesis randomness",
    )
    batch_dt.add_argument(
        "--lazer-storage",
        type=Path,
        default=None,
        help="osu!lazer storage directory containing game.ini; defaults to the path in %%APPDATA%%\\osu\\storage.ini",
    )
    batch_dt.add_argument(
        "--token-config",
        type=Path,
        default=None,
        help="explicit osu!lazer game.ini path to read the saved API token from",
    )
    batch_dt.add_argument(
        "--api-url",
        default="https://osu.ppy.sh",
        help="osu! API root; defaults to production",
    )
    batch_dt.add_argument(
        "--api-version",
        type=int,
        default=None,
        help="x-api-version header; defaults to today's YYYYMMDD",
    )
    batch_dt.add_argument(
        "--with-video",
        action="store_true",
        help="download beatmapset video assets too",
    )
    batch_dt.add_argument(
        "--lazer-path",
        type=Path,
        default=None,
        help="osu!lazer install directory or osu!.exe used for post-batch import; defaults to OSU_LAZER_PATH or %%LOCALAPPDATA%%\\osulazer\\current",
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


def add_debug_video_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--debug-video",
        type=Path,
        default=None,
        help="optional MP4/AVI showing both source cursors and the synthesized cursor",
    )
    parser.add_argument(
        "--debug-video-start-ms",
        type=int,
        default=None,
        help="debug video start time in beatmap milliseconds; defaults to replay start",
    )
    parser.add_argument(
        "--debug-video-end-ms",
        type=int,
        default=None,
        help="debug video end time in beatmap milliseconds; defaults to replay end",
    )
    parser.add_argument(
        "--debug-video-fps",
        type=float,
        default=DEFAULT_VIDEO_FPS,
        help=f"debug video frame rate; defaults to {DEFAULT_VIDEO_FPS:g}",
    )
    parser.add_argument(
        "--debug-video-speed",
        type=float,
        default=DEFAULT_VIDEO_SPEED,
        help=f"debug video playback speed multiplier; defaults to {DEFAULT_VIDEO_SPEED:g}",
    )
    parser.add_argument(
        "--debug-video-size",
        default=f"{DEFAULT_VIDEO_WIDTH}x{DEFAULT_VIDEO_HEIGHT}",
        help=f"debug video size WIDTHxHEIGHT; defaults to {DEFAULT_VIDEO_WIDTH}x{DEFAULT_VIDEO_HEIGHT}",
    )


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
        first_weight=args.first_weight,
        second_weight=args.second_weight,
        synthesis_seed=args.synthesis_seed,
        allow_key_mismatch=args.allow_key_mismatch,
        first_skip_ms=args.first_skip_ms,
        second_skip_ms=args.second_skip_ms,
        intro_end_ms=args.intro_end_ms,
        recompute_score_metadata=args.local_score,
        spinner_library_path=str(args.spinner_library) if (args.spinner_library and args.spinner_mode != "never") else None,
        sequential_match=not args.greedy_match,
        dt_mode=args.dt,
        spinner_mode=args.spinner_mode,
    )
    result.replay.write_path(args.output)
    if args.debug_video is not None:
        width, height = parse_video_size(args.debug_video_size)
        write_replay_debug_video(
            first,
            second,
            args.debug_video,
            beatmap=beatmap,
            synthesized=result.replay,
            player_name=args.player_name,
            first_weight=args.first_weight,
            second_weight=args.second_weight,
            allow_key_mismatch=args.allow_key_mismatch,
            first_skip_ms=args.first_skip_ms,
            second_skip_ms=args.second_skip_ms,
            intro_end_ms=args.intro_end_ms,
            start_ms=args.debug_video_start_ms,
            end_ms=args.debug_video_end_ms,
            fps=args.debug_video_fps,
            speed=args.debug_video_speed,
            width=width,
            height=height,
        )
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
    if result.report.spinner_replacements:
        replacements = result.report.spinner_replacements
        print(f"spinners replaced: {len(replacements)}")
        for r in replacements:
            print(f"  spinner at {r.start_ms}ms ({r.end_ms-r.start_ms}ms): "
                  f"RPM {r.rpm_original:.0f} -> {r.rpm_replacement:.0f} "
                  f"({r.candidates_used} candidates)")
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
    if args.debug_video is not None:
        print(f"wrote debug video {args.debug_video}")
    if beatmap is not None and not args.local_score:
        print("score metadata is provisional; run this replay in osu!lazer and finalize it from a lazer-scored metadata source")
    return 0


def download_synthesize_command(args: argparse.Namespace) -> int:
    report = download_and_synthesize(
        args.beatmap_id,
        args.first_rank,
        args.second_rank,
        args.output,
        work_dir=args.work_dir,
        player_name=args.player_name,
        first_weight=args.first_weight,
        second_weight=args.second_weight,
        synthesis_seed=args.synthesis_seed,
        scope=args.scope,
        ruleset=args.ruleset,
        mods=args.mods,
        no_video=not args.with_video,
        lazer_storage=args.lazer_storage,
        token_config=args.token_config,
        api_url=args.api_url,
        api_version=args.api_version,
        lazer_json_path=args.lazer_json,
        recompute_score_metadata=args.local_score,
        debug_video_path=args.debug_video,
        debug_video_start_ms=args.debug_video_start_ms,
        debug_video_end_ms=args.debug_video_end_ms,
        debug_video_fps=args.debug_video_fps,
        debug_video_speed=args.debug_video_speed,
        debug_video_size=parse_video_size(args.debug_video_size),
        spinner_library_path=str(args.spinner_library) if (args.spinner_library and args.spinner_mode != "never") else None,
        sequential_match=not args.greedy_match,
        dt_mode=args.dt,
        spinner_mode=args.spinner_mode,
    )
    print(
        "wrote "
        f"{report.output_path} "
        f"({report.synthesis_report.frame_count} frames, {report.synthesis_report.duration_ms} ms)"
    )
    print(
        f"rank {report.first_score.rank}: score {report.first_score.score_id}"
        + (f" by {report.first_score.username}" if report.first_score.username else "")
        + f" -> {report.first_replay_path}"
    )
    print(
        f"rank {report.second_score.rank}: score {report.second_score.score_id}"
        + (f" by {report.second_score.username}" if report.second_score.username else "")
        + f" -> {report.second_replay_path}"
    )
    print(f"beatmap: {report.beatmap_path}")
    print(f"leaderboard: {report.leaderboard_path}")
    for bit, count in sorted(report.synthesis_report.key_interval_counts.items()):
        print(f"key bit {bit}: {count} intervals")
    if report.synthesis_report.matched_object_count or report.synthesis_report.dropped_object_count:
        print(
            f"matched objects: {report.synthesis_report.matched_object_count}, "
            f"dropped objects: {report.synthesis_report.dropped_object_count}"
        )
    if report.lazer_json_path is not None:
        print(f"wrote lazer json {report.lazer_json_path}")
    if report.debug_video_path is not None:
        print(f"wrote debug video {report.debug_video_path}")
    if not args.local_score:
        print("score metadata is provisional; run this replay in osu!lazer and finalize it from a lazer-scored metadata source")

    # Import beatmap into osu!lazer
    from .online import resolve_lazer_executable
    work = report.work_dir or (Path(args.work_dir) if args.work_dir else Path("artifacts") / str(args.beatmap_id))
    osz_candidates = sorted(Path(work).glob("beatmapset_*.osz"))
    if osz_candidates:
        lazer_exe = resolve_lazer_executable(args.lazer_path)
        for osz in osz_candidates:
            try:
                subprocess.Popen(
                    [str(lazer_exe), str(osz)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, 'CREATE_NO_WINDOW') else 0,
                )
                print(f"imported beatmapset: {osz}")
                break
            except OSError as exc:
                print(f"warning: could not import beatmapset {osz}: {exc}", file=sys.stderr)
    return 0


def batch_dt_command(args: argparse.Namespace) -> int:
    min_skip_time_ms = 0 if args.no_skip_filter else args.min_skip_time
    report = batch_synthesize_dt(
        args.count,
        output_dir=args.output_dir,
        work_dir=args.work_dir,
        beatmap_selection=args.beatmap_selection,
        min_age_days=args.min_age_days,
        min_star=args.min_star,
        max_star=args.max_star,
        player_name=args.player_name,
        no_video=not args.with_video,
        lazer_storage=args.lazer_storage,
        token_config=args.token_config,
        api_url=args.api_url,
        api_version=args.api_version,
        leaderboard_limit=args.leaderboard_limit,
        search_pages_limit=args.search_pages_limit,
        random_search_pages=args.random_search_pages,
        request_delay_s=args.request_delay_s,
        random_seed=args.random_seed,
        synthesis_seed=args.synthesis_seed,
        min_skip_time_ms=min_skip_time_ms,
        spinner_library_path=(
            str(args.spinner_library) if (args.spinner_library and args.spinner_mode != "never") else None
        ),
        spinner_mode=args.spinner_mode,
        dt_mode=args.dt,
    )
    imported_archives = import_batch_beatmaps(report, lazer_path=args.lazer_path)
    print(f"wrote {len(report.items)} DT replay(s) to {report.output_dir}")
    print(f"manifest: {report.manifest_path}")
    for item in report.items:
        print(
            f"{item.output_path.name}: beatmap {item.beatmap_id} "
            f"{item.difficulty_rating:.2f}* "
            f"{item.title} [{item.version}] "
            f"from ranks {item.first_score.rank}/{item.second_score.rank} "
            f"({item.selection_pool})"
        )
    print(f"imported {len(imported_archives)} beatmapset(s) into osu!lazer")
    return 0


def parse_video_size(value: str) -> tuple[int, int]:
    separator = "x" if "x" in value.lower() else None
    if separator is None:
        raise SynthesisError(f"invalid debug video size: {value!r}")
    left, right = value.lower().split("x", 1)
    try:
        width = int(left)
        height = int(right)
    except ValueError as exc:
        raise SynthesisError(f"invalid debug video size: {value!r}") from exc
    if width <= 0 or height <= 0:
        raise SynthesisError(f"invalid debug video size: {value!r}")
    return width, height


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
