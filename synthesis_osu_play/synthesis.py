from __future__ import annotations

from collections.abc import Callable
import hashlib
import logging
import random
from dataclasses import dataclass, replace
from math import dist, floor, isfinite, nextafter
from random import Random
from typing import TYPE_CHECKING

from .beatmap import Beatmap, BreakPeriod, HitObject
from .mods import (
    MOD_EASY,
    MOD_HARD_ROCK,
    MOD_HIDDEN,
    SPEED_NORMAL,
    SYNTHESIS_VARIABLE_MOD_MASK,
    canonical_speed_mod_bits,
    speed_mod_category,
)
from .osr import OsrReplay, ReplayFrame

if TYPE_CHECKING:
    from .spinner_replace import SpinnerReplacement


LOGGER = logging.getLogger(__name__)


OSU_STANDARD_MODE = 0
OSU_STANDARD_PLAYFIELD_HEIGHT = 384.0
SENTINEL_DELTA = -12345
DEFAULT_KEY_MASK = 0x1F
LAZER_MINIMUM_SKIP_TIME_MS = 1000
LEGACY_LEFT_BUTTON = 1
LEGACY_RIGHT_BUTTON = 2
LEGACY_KEY_1 = 4
LEGACY_KEY_2 = 8
LEGACY_Z_KEY = LEGACY_LEFT_BUTTON | LEGACY_KEY_1
LEGACY_X_KEY = LEGACY_RIGHT_BUTTON | LEGACY_KEY_2
LEGACY_LEFT_ACTION_MASK = LEGACY_LEFT_BUTTON | LEGACY_KEY_1
LEGACY_RIGHT_ACTION_MASK = LEGACY_RIGHT_BUTTON | LEGACY_KEY_2
PRIMARY_KEY_REPEAT_THRESHOLD_MS = 350
PRIMARY_KEY_REPEAT_THRESHOLD_JITTER_MS = 50
SPINNER_PREPRESS_LEAD_MS = 100
DYNAMIC_PHASE_NOISE_STD_RAD = 0.15
LEGACY_INPUT_ACTION_MASKS = (
    (LEGACY_Z_KEY, LEGACY_LEFT_ACTION_MASK),
    (LEGACY_X_KEY, LEGACY_RIGHT_ACTION_MASK),
)


class SynthesisError(ValueError):
    """Raised when two replays cannot be synthesized safely."""


@dataclass(frozen=True)
class AbsoluteFrame:
    time_ms: int
    x: float
    y: float
    keys: int


@dataclass(frozen=True)
class KeyInterval:
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class ClickInterval:
    index: int
    bit: int
    start_ms: int
    end_ms: int

    @property
    def interval(self) -> KeyInterval:
        return KeyInterval(self.start_ms, self.end_ms)


@dataclass(frozen=True)
class SynthesisReport:
    frame_count: int
    duration_ms: int
    key_interval_counts: dict[int, int]
    matched_object_count: int = 0
    dropped_object_count: int = 0
    skip_press_ms: int | None = None
    intro_end_ms: int | None = None
    spinner_replacements: list[SpinnerReplacement] | None = None


@dataclass(frozen=True)
class SynthesizedReplay:
    replay: OsrReplay
    report: SynthesisReport


@dataclass(frozen=True)
class ReplayCompatibility:
    output_mods: int


def synthesize_replays(
    first: OsrReplay,
    second: OsrReplay,
    *,
    beatmap: Beatmap | None = None,
    player_name: str | None = None,
    first_weight: float = 1.0,
    second_weight: float = 1.0,
    synthesis_seed: int | None = None,
    allow_key_mismatch: bool = False,
    key_mask: int = DEFAULT_KEY_MASK,
    first_skip_ms: int | None = None,
    second_skip_ms: int | None = None,
    intro_end_ms: int | None = None,
    recompute_score_metadata: bool = False,
    output_mods: int | None = None,
    allow_source_mod_mismatch: bool = False,
    spinner_library_path: str | None = None,
    sequential_match: bool = True,
    dt_mode: bool = False,
    spinner_mode: str = "all",
) -> SynthesizedReplay:
    validate_weights(first_weight, second_weight)
    synthesis_rng = Random(synthesis_seed)
    phase_offset_rad = synthesis_rng.gauss(0.0, DYNAMIC_PHASE_NOISE_STD_RAD)
    repeat_threshold_ms = randomized_primary_key_repeat_threshold_ms(synthesis_rng)
    if spinner_mode not in {"all", "threshold", "never"}:
        raise SynthesisError(f"unknown spinner replacement mode: {spinner_mode}")
    compatibility = validate_compatible(
        first,
        second,
        output_mods=output_mods,
        allow_source_mod_mismatch=allow_source_mod_mismatch,
    )
    first_frames, first_seed = split_sentinel(first.frames)
    second_frames, second_seed = split_sentinel(second.frames)
    first_absolute = normalize_absolute_frames(
        to_absolute_frames(first_frames),
        source_mods=first.mods,
        output_mods=compatibility.output_mods,
    )
    second_absolute = normalize_absolute_frames(
        to_absolute_frames(second_frames),
        source_mods=second.mods,
        output_mods=compatibility.output_mods,
    )
    if not first_absolute or not second_absolute:
        raise SynthesisError("both replays need at least one non-sentinel frame")

    if beatmap is None:
        key_intervals = average_key_intervals(
            first_absolute,
            second_absolute,
            first_weight=first_weight,
            second_weight=second_weight,
            phase_offset_rad=phase_offset_rad,
            allow_mismatch=allow_key_mismatch,
            key_mask=key_mask,
        )
        matched_object_count = 0
        dropped_object_count = 0
    else:
        spinner_seed = spinner_noise_seed(
            first,
            second,
            beatmap,
        )
        key_intervals, matched_object_count, dropped_object_count = synthesize_key_intervals_for_beatmap(
            first_absolute,
            second_absolute,
            beatmap,
            output_mods=compatibility.output_mods,
            first_weight=first_weight,
            second_weight=second_weight,
            spinner_seed=spinner_seed,
            synthesis_rng=synthesis_rng,
            sequential_match=sequential_match,
            phase_offset_rad=phase_offset_rad,
            repeat_threshold_ms=repeat_threshold_ms,
        )

    skip_press_ms = earliest_time(first_skip_ms, second_skip_ms)
    if beatmap is not None and skip_press_ms is not None and intro_end_ms is None:
        intro_end_ms = default_lazer_skip_target_ms(beatmap.first_hit_object_time_ms)
    if skip_press_ms is not None and intro_end_ms is None:
        raise SynthesisError("intro_end_ms is required when a skip time is provided")
    if skip_press_ms is not None and intro_end_ms is not None and skip_press_ms > intro_end_ms:
        raise SynthesisError("skip time cannot be later than intro_end_ms")
    output_times = build_output_times(
        first_absolute,
        second_absolute,
        key_intervals,
        skip_press_ms=skip_press_ms,
        intro_end_ms=intro_end_ms,
    )
    output_absolute = [
        average_frame_at_time(
            first_absolute,
            second_absolute,
            key_intervals,
            time_ms,
            first_weight=first_weight,
            second_weight=second_weight,
            skip_press_ms=skip_press_ms,
            intro_end_ms=intro_end_ms,
            phase_offset_rad=phase_offset_rad,
        )
        for time_ms in output_times
    ]
    spinner_replacements: list[SpinnerReplacement] = []
    if spinner_library_path is None and beatmap is not None and spinner_mode != "never":
        from .spinner_replace import DEFAULT_SPINNER_LIBRARY_PATH

        spinner_library_path = str(DEFAULT_SPINNER_LIBRARY_PATH)
    if spinner_library_path is not None and spinner_mode != "never" and beatmap is not None:
        spinner_intervals = [
            (obj.time_ms, obj.resolved_end_time_ms)
            for obj in beatmap.hit_objects
            if obj.is_spinner
        ]
        if spinner_intervals:
            from .spinner_replace import replace_spinner_segments
            LOGGER.info("spinner check: %s spinners, library=%s", len(spinner_intervals), spinner_library_path)
            output_absolute, spinner_replacements = replace_spinner_segments(
                output_absolute,
                list(beatmap.hit_objects),
                spinner_intervals,
                library_path=spinner_library_path,
                dt_mode=dt_mode,
                spinner_mode=spinner_mode,
                synthesis_seed=synthesis_seed,
            )
            LOGGER.info("spinners replaced: %s/%s", len(spinner_replacements), len(spinner_intervals))
        else:
            LOGGER.info("spinner check: no spinners in this beatmap")
    elif spinner_library_path is not None and spinner_mode != "never":
        LOGGER.info("spinner check: no beatmap loaded, skipping")

    output_frames = to_delta_frames(output_absolute)
    seed = average_seed(
        first_seed,
        second_seed,
        first_weight=first_weight,
        second_weight=second_weight,
        phase_offset_rad=phase_offset_rad,
    )
    if seed is not None:
        output_frames.append(ReplayFrame(SENTINEL_DELTA, 0.0, 0.0, seed))

    replay = first.with_frames(
        tuple(output_frames),
        player_name=player_name,
        mods=compatibility.output_mods,
        timestamp=first.timestamp if synthesis_seed is not None else None,
    )
    if beatmap is not None and recompute_score_metadata:
        from .scoring import replay_with_recomputed_score_metadata

        replay = replay_with_recomputed_score_metadata(replay, beatmap)
    report = SynthesisReport(
        frame_count=len(output_frames),
        duration_ms=output_absolute[-1].time_ms if output_absolute else 0,
        key_interval_counts={bit: len(intervals) for bit, intervals in key_intervals.items()},
        matched_object_count=matched_object_count,
        dropped_object_count=dropped_object_count,
        skip_press_ms=skip_press_ms,
        intro_end_ms=intro_end_ms,
        spinner_replacements=spinner_replacements if spinner_replacements else None,
    )
    return SynthesizedReplay(replay=replay, report=report)


def average_frame_at_time(
    first: list[AbsoluteFrame],
    second: list[AbsoluteFrame],
    key_intervals: dict[int, list[KeyInterval]],
    time_ms: int,
    *,
    first_weight: float = 1.0,
    second_weight: float = 1.0,
    skip_press_ms: int | None = None,
    intro_end_ms: int | None = None,
    phase_offset_rad: float = 0.0,
) -> AbsoluteFrame:
    x, y = average_position_at_time(
        first,
        second,
        time_ms,
        first_weight=first_weight,
        second_weight=second_weight,
        skip_press_ms=skip_press_ms,
        intro_end_ms=intro_end_ms,
        phase_offset_rad=phase_offset_rad,
    )
    return AbsoluteFrame(
        time_ms=time_ms,
        x=x,
        y=y,
        keys=keys_at_time(key_intervals, time_ms),
    )


def average_position_at_time(
    first: list[AbsoluteFrame],
    second: list[AbsoluteFrame],
    time_ms: int,
    *,
    first_weight: float = 1.0,
    second_weight: float = 1.0,
    skip_press_ms: int | None = None,
    intro_end_ms: int | None = None,
    phase_offset_rad: float = 0.0,
) -> tuple[float, float]:
    w1, w2 = effective_weights(
        time_ms,
        first_weight,
        second_weight,
        phase_offset_rad=phase_offset_rad,
    )
    first_x, first_y = interpolate_position(first, time_ms)
    second_x, second_y = interpolate_position(second, time_ms)
    if skip_press_ms is not None and intro_end_ms is not None and intro_end_ms > skip_press_ms and time_ms <= skip_press_ms:
        anchor_x, anchor_y = average_position_at_time(
            first,
            second,
            intro_end_ms,
            first_weight=first_weight,
            second_weight=second_weight,
            phase_offset_rad=phase_offset_rad,
        )
        return (
            weighted_average_three(first_x, second_x, anchor_x, w1, w2, 1.0),
            weighted_average_three(first_y, second_y, anchor_y, w1, w2, 1.0),
        )
    return (
        weighted_average_pair(first_x, second_x, w1, w2),
        weighted_average_pair(first_y, second_y, w1, w2),
    )


def validate_compatible(
    first: OsrReplay,
    second: OsrReplay,
    *,
    output_mods: int | None = None,
    allow_source_mod_mismatch: bool = False,
) -> ReplayCompatibility:
    if first.mode != second.mode:
        raise SynthesisError(f"replay modes differ: {first.mode} != {second.mode}")
    if first.mode != OSU_STANDARD_MODE:
        raise SynthesisError("only osu!standard replays are supported")
    if first.beatmap_md5 != second.beatmap_md5:
        raise SynthesisError("beatmap MD5 hashes differ")
    if output_mods is None:
        return ReplayCompatibility(output_mods=synthesis_output_mods(first.mods, second.mods))
    validate_replay_mods_against_output(
        first.mods,
        output_mods,
        "first",
        require_base_match=not allow_source_mod_mismatch,
    )
    validate_replay_mods_against_output(
        second.mods,
        output_mods,
        "second",
        require_base_match=not allow_source_mod_mismatch,
    )
    return ReplayCompatibility(output_mods=output_mods)


def synthesis_output_mods(first_mods: int, second_mods: int) -> int:
    first_speed = replay_speed_category(first_mods, "first")
    second_speed = replay_speed_category(second_mods, "second")
    first_base = first_mods & ~SYNTHESIS_VARIABLE_MOD_MASK
    second_base = second_mods & ~SYNTHESIS_VARIABLE_MOD_MASK
    if first_base != second_base:
        raise SynthesisError(
            "mods differ after HD/HR/DT/NC/HT normalization: "
            f"{first_mods} != {second_mods}"
        )

    output_mods = first_base
    if first_mods & MOD_HIDDEN and second_mods & MOD_HIDDEN:
        output_mods |= MOD_HIDDEN
    if first_mods & MOD_HARD_ROCK and second_mods & MOD_HARD_ROCK:
        output_mods |= MOD_HARD_ROCK
    if first_speed == second_speed and first_speed != SPEED_NORMAL:
        output_mods |= canonical_speed_mod_bits(first_speed, first_mods, second_mods)
    return output_mods


def replay_speed_category(mods: int, label: str) -> str:
    try:
        return speed_mod_category(mods)
    except ValueError as exc:
        raise SynthesisError(f"{label} replay has conflicting speed mods: {mods}") from exc


def validate_replay_mods_against_output(
    source_mods: int,
    output_mods: int,
    label: str,
    *,
    require_base_match: bool = True,
) -> None:
    replay_speed_category(source_mods, label)
    replay_speed_category(output_mods, "output")
    source_base = source_mods & ~SYNTHESIS_VARIABLE_MOD_MASK
    output_base = output_mods & ~SYNTHESIS_VARIABLE_MOD_MASK
    if require_base_match and source_base != output_base:
        raise SynthesisError(
            f"{label} replay mods cannot be normalized to output mods: "
            f"{source_mods} -> {output_mods}"
        )


def normalize_absolute_frames(
    frames: list[AbsoluteFrame],
    *,
    source_mods: int,
    output_mods: int,
) -> list[AbsoluteFrame]:
    if bool(source_mods & MOD_HARD_ROCK) == bool(output_mods & MOD_HARD_ROCK):
        return frames
    return [
        AbsoluteFrame(
            time_ms=frame.time_ms,
            x=frame.x,
            y=OSU_STANDARD_PLAYFIELD_HEIGHT - frame.y,
            keys=frame.keys,
        )
        for frame in frames
    ]


def split_sentinel(frames: tuple[ReplayFrame, ...]) -> tuple[tuple[ReplayFrame, ...], int | None]:
    if frames and frames[-1].delta_ms == SENTINEL_DELTA:
        return frames[:-1], frames[-1].keys
    return frames, None


def to_absolute_frames(frames: tuple[ReplayFrame, ...] | list[ReplayFrame]) -> list[AbsoluteFrame]:
    absolute: list[AbsoluteFrame] = []
    time_ms = 0
    for frame in frames:
        time_ms += frame.delta_ms
        absolute.append(AbsoluteFrame(time_ms, frame.x, frame.y, frame.keys))
    return absolute


def to_delta_frames(frames: list[AbsoluteFrame]) -> list[ReplayFrame]:
    output: list[ReplayFrame] = []
    previous_time = 0
    for frame in frames:
        output.append(ReplayFrame(frame.time_ms - previous_time, frame.x, frame.y, frame.keys))
        previous_time = frame.time_ms
    return output


def interpolate_position(frames: list[AbsoluteFrame], time_ms: int) -> tuple[float, float]:
    if time_ms <= frames[0].time_ms:
        return frames[0].x, frames[0].y
    if time_ms >= frames[-1].time_ms:
        return frames[-1].x, frames[-1].y

    low = 0
    high = len(frames) - 1
    while low <= high:
        mid = (low + high) // 2
        if frames[mid].time_ms == time_ms:
            return frames[mid].x, frames[mid].y
        if frames[mid].time_ms < time_ms:
            low = mid + 1
        else:
            high = mid - 1

    before = frames[high]
    after = frames[low]
    span = after.time_ms - before.time_ms
    if span <= 0:
        return after.x, after.y
    ratio = (time_ms - before.time_ms) / span
    return (
        before.x + (after.x - before.x) * ratio,
        before.y + (after.y - before.y) * ratio,
    )


def average_key_intervals(
    first: list[AbsoluteFrame],
    second: list[AbsoluteFrame],
    *,
    first_weight: float = 1.0,
    second_weight: float = 1.0,
    allow_mismatch: bool,
    key_mask: int,
    phase_offset_rad: float = 0.0,
) -> dict[int, list[KeyInterval]]:
    output: dict[int, list[KeyInterval]] = {}
    for bit in iter_key_bits(key_mask):
        first_intervals = extract_key_intervals(first, bit)
        second_intervals = extract_key_intervals(second, bit)
        if len(first_intervals) != len(second_intervals) and not allow_mismatch:
            raise SynthesisError(
                f"key bit {bit} interval counts differ: "
                f"{len(first_intervals)} != {len(second_intervals)}"
            )
        paired_count = min(len(first_intervals), len(second_intervals))
        intervals: list[KeyInterval] = []
        for index in range(paired_count):
            first_interval = first_intervals[index]
            second_interval = second_intervals[index]
            mid_ms = (first_interval.start_ms + second_interval.start_ms) // 2
            w1, w2 = effective_weights(
                mid_ms,
                first_weight,
                second_weight,
                phase_offset_rad=phase_offset_rad,
            )
            start = round(weighted_average_pair(first_interval.start_ms, second_interval.start_ms, w1, w2))
            end = round(weighted_average_pair(first_interval.end_ms, second_interval.end_ms, w1, w2))
            if end > start:
                intervals.append(KeyInterval(start, end))
        output[bit] = intervals
    return output


def weighted_average_pair(first: float, second: float, first_weight: float, second_weight: float) -> float:
    total_weight = first_weight + second_weight
    if total_weight == 0:
        return (first + second) / 2.0
    return (first * first_weight + second * second_weight) / total_weight


def weighted_average_three(
    first: float,
    second: float,
    third: float,
    first_weight: float,
    second_weight: float,
    third_weight: float,
) -> float:
    total_weight = first_weight + second_weight + third_weight
    if total_weight == 0:
        return (first + second + third) / 3.0
    return (
        first * first_weight
        + second * second_weight
        + third * third_weight
    ) / total_weight


def extract_key_intervals(frames: list[AbsoluteFrame], bit: int) -> list[KeyInterval]:
    return extract_intervals_by_predicate(frames, lambda keys: (keys & bit) == bit)


def extract_any_key_intervals(frames: list[AbsoluteFrame], mask: int) -> list[KeyInterval]:
    return extract_intervals_by_predicate(frames, lambda keys: bool(keys & mask))


def extract_intervals_by_predicate(
    frames: list[AbsoluteFrame],
    is_pressed: Callable[[int], bool],
) -> list[KeyInterval]:
    intervals: list[KeyInterval] = []
    pressed_since: int | None = None
    for frame in frames:
        pressed = is_pressed(frame.keys)
        if pressed and pressed_since is None:
            pressed_since = frame.time_ms
        elif not pressed and pressed_since is not None:
            if frame.time_ms > pressed_since:
                intervals.append(KeyInterval(pressed_since, frame.time_ms))
            pressed_since = None
    if pressed_since is not None:
        final_time = frames[-1].time_ms
        if final_time > pressed_since:
            intervals.append(KeyInterval(pressed_since, final_time))
    return intervals


def synthesize_key_intervals_for_beatmap(
    first: list[AbsoluteFrame],
    second: list[AbsoluteFrame],
    beatmap: Beatmap,
    *,
    output_mods: int = 0,
    first_weight: float = 1.0,
    second_weight: float = 1.0,
    spinner_seed: int | None = None,
    synthesis_rng: Random | None = None,
    sequential_match: bool = True,
    phase_offset_rad: float = 0.0,
    repeat_threshold_ms: float = PRIMARY_KEY_REPEAT_THRESHOLD_MS,
) -> tuple[dict[int, list[KeyInterval]], int, int]:
    objects = modded_hit_objects_for_matching(beatmap.hit_objects, output_mods)
    if not objects:
        return {LEGACY_Z_KEY: [], LEGACY_X_KEY: []}, 0, 0

    first_clicks = match_effective_clicks(
        first,
        objects,
        hit_window_ms=modded_hit_window_50_ms(beatmap, output_mods),
        circle_radius=modded_circle_radius(beatmap, output_mods),
        sequential=sequential_match,
    )
    second_clicks = match_effective_clicks(
        second,
        objects,
        hit_window_ms=modded_hit_window_50_ms(beatmap, output_mods),
        circle_radius=modded_circle_radius(beatmap, output_mods),
        sequential=sequential_match,
    )

    first_object_clicks = first_clicks
    second_object_clicks = second_clicks
    first_object_clicks = extend_slider_clicks_to_hold_coverage(
        first,
        objects,
        first_object_clicks,
        slider_follow_radius=modded_circle_radius(beatmap, output_mods) * 2.4,
    )
    second_object_clicks = extend_slider_clicks_to_hold_coverage(
        second,
        objects,
        second_object_clicks,
        slider_follow_radius=modded_circle_radius(beatmap, output_mods) * 2.4,
    )
    clip_rng = synthesis_rng if synthesis_rng is not None else Random()
    first_object_clicks = clip_clicks_before_spinners(objects, first_object_clicks, clip_rng)
    second_object_clicks = clip_clicks_before_spinners(objects, second_object_clicks, clip_rng)
    first_previous_clicks = previous_effective_clicks(first_object_clicks)
    first_next_clicks = next_effective_clicks(first_object_clicks)
    second_previous_clicks = previous_effective_clicks(second_object_clicks)
    second_next_clicks = next_effective_clicks(second_object_clicks)

    averaged_intervals: list[KeyInterval] = []
    required_end_times: list[int] = []
    dropped_object_count = 0
    matched_object_count = 0
    spinner_rng = Random(spinner_seed if spinner_seed is not None else 0)
    for object_index, obj in enumerate(objects):
        if obj.is_clickable:
            first_click = first_object_clicks[object_index]
            second_click = second_object_clicks[object_index]
            if first_click is None or second_click is None:
                dropped_object_count += 1
                continue
            w1, w2 = effective_weights(
                obj.time_ms,
                first_weight,
                second_weight,
                phase_offset_rad=phase_offset_rad,
            )
            start = round(weighted_average_pair(first_click.start_ms, second_click.start_ms, w1, w2))
            end = round(weighted_average_pair(first_click.end_ms, second_click.end_ms, w1, w2))
            if end <= start:
                end = start + 1
            averaged_intervals.append(KeyInterval(start, end))
            required_end_times.append(obj.resolved_end_time_ms if obj.is_slider else start)
            matched_object_count += 1
        elif obj.is_spinner:
            interval = synthesize_spinner_interval(
                obj,
                first_previous_clicks[object_index],
                first_next_clicks[object_index],
                second_previous_clicks[object_index],
                second_next_clicks[object_index],
                spinner_rng,
                break_periods=beatmap.break_periods,
            )
            averaged_intervals.append(interval)
            required_end_times.append(interval.end_ms)
            matched_object_count += 1

    averaged_intervals = resolve_synthesized_overlaps(
        averaged_intervals,
        required_end_times,
    )
    ensure_no_triple_overlap(averaged_intervals)
    return (
        assign_natural_keys(
            averaged_intervals,
            repeat_threshold_ms=repeat_threshold_ms,
        ),
        matched_object_count,
        dropped_object_count,
    )


def clip_clicks_before_spinners(
    objects: list[HitObject],
    object_clicks: list[ClickInterval | None],
    rng: Random,
) -> list[ClickInterval | None]:
    """Clip a matched note click that incorrectly extends into a following spinner."""
    clipped = list(object_clicks)
    for spinner_index, spinner in enumerate(objects):
        if not spinner.is_spinner:
            continue

        previous_index = spinner_index - 1
        while previous_index >= 0 and not objects[previous_index].is_clickable:
            previous_index -= 1
        if previous_index < 0:
            continue

        click = clipped[previous_index]
        if click is None or click.end_ms <= spinner.time_ms:
            continue

        previous = objects[previous_index]
        if previous.is_circle:
            lower_bound = click.start_ms + 50
            label = "circle"
        elif previous.is_slider:
            lower_bound = previous.resolved_end_time_ms
            label = "slider"
        else:
            continue

        if lower_bound > spinner.time_ms:
            raise SynthesisError(
                f"matched {label} click at {click.start_ms}ms cannot be clipped "
                f"before spinner at {spinner.time_ms}ms"
            )

        clipped_end = round(rng.uniform(lower_bound, spinner.time_ms))
        clipped[previous_index] = replace(click, end_ms=clipped_end)
        LOGGER.debug(
            "clipped %s click %sms-%sms to %sms before spinner at %sms",
            label,
            click.start_ms,
            click.end_ms,
            clipped_end,
            spinner.time_ms,
        )

    return clipped


def extend_slider_clicks_to_hold_coverage(
    frames: list[AbsoluteFrame],
    objects: list[HitObject],
    object_clicks: list[ClickInterval | None],
    *,
    slider_follow_radius: float | None = None,
) -> list[ClickInterval | None]:
    extended = list(object_clicks)
    for object_index, obj in enumerate(objects):
        if not obj.is_slider:
            continue
        click = extended[object_index]
        if click is None:
            continue

        coverage_end = slider_tracking_end_ms(
            frames,
            obj,
            click,
            slider_follow_radius=slider_follow_radius,
        )
        if coverage_end <= click.end_ms:
            continue
        extended[object_index] = replace(click, end_ms=coverage_end)
        LOGGER.debug(
            "extended slider click at %sms from %sms to continuous hold end %sms",
            obj.time_ms,
            click.end_ms,
            coverage_end,
        )
    return extended


def slider_tracking_end_ms(
    frames: list[AbsoluteFrame],
    obj: HitObject,
    click: ClickInterval,
    *,
    slider_follow_radius: float | None = None,
) -> int:
    """Return the end of the input that can actually track a slider.

    osu!lazer initially requires the key used for the slider head.  The other
    key becomes valid only after it was released in an earlier frame.  This
    matters when two keys overlap: simply taking the union of both key
    intervals can incorrectly turn an invalid handoff into full tracking.
    """
    if click.start_ms >= obj.resolved_end_time_ms:
        return click.end_ms

    other_key = LEGACY_X_KEY if click.bit == LEGACY_Z_KEY else LEGACY_Z_KEY
    previous_actions = pressed_action_keys(
        replay_keys_at_time(frames, click.start_ms, before=True)
    )
    time_to_accept_any: int | None = None
    sample_times = {
        click.start_ms,
        obj.resolved_end_time_ms,
        *(frame.time_ms for frame in frames if click.start_ms <= frame.time_ms <= obj.resolved_end_time_ms),
    }

    for time_ms in sorted(sample_times):
        actions = pressed_action_keys(replay_keys_at_time(frames, time_ms))
        if time_to_accept_any is None and other_key not in previous_actions:
            time_to_accept_any = time_ms

        if time_to_accept_any is None or time_ms <= time_to_accept_any:
            valid_action = click.bit in actions
        else:
            valid_action = bool(actions)

        if slider_follow_radius is not None:
            cursor = interpolate_position(frames, time_ms)
            slider_position = slider_position_at_time(obj, time_ms)
            valid_position = dist(cursor, slider_position) <= slider_follow_radius
        else:
            valid_position = True

        if not valid_action or not valid_position:
            return max(click.start_ms, time_ms)
        previous_actions = actions

    hold_intervals = extract_any_key_intervals(
        frames,
        LEGACY_LEFT_ACTION_MASK | LEGACY_RIGHT_ACTION_MASK,
    )
    coverage = next(
        (
            interval
            for interval in hold_intervals
            if interval.start_ms <= click.start_ms < interval.end_ms
        ),
        None,
    )
    return coverage.end_ms if coverage is not None else click.end_ms


def pressed_action_keys(keys: int) -> set[int]:
    actions: set[int] = set()
    if keys & LEGACY_LEFT_ACTION_MASK:
        actions.add(LEGACY_Z_KEY)
    if keys & LEGACY_RIGHT_ACTION_MASK:
        actions.add(LEGACY_X_KEY)
    return actions


def replay_keys_at_time(
    frames: list[AbsoluteFrame],
    time_ms: int,
    *,
    before: bool = False,
) -> int:
    result = frames[0].keys
    for frame in frames:
        if frame.time_ms < time_ms or (not before and frame.time_ms == time_ms):
            result = frame.keys
        elif frame.time_ms >= time_ms:
            break
    return result


def slider_position_at_time(obj: HitObject, time_ms: int) -> tuple[float, float]:
    points = list(obj.slider_control_points)
    if not points:
        return obj.x, obj.y
    if points[0] != (obj.x, obj.y):
        points.insert(0, (obj.x, obj.y))
    if len(points) == 1 or obj.resolved_end_time_ms <= obj.time_ms:
        return points[0]

    progress = (time_ms - obj.time_ms) / (obj.resolved_end_time_ms - obj.time_ms)
    progress = max(0.0, min(1.0, progress))
    span_count = max(1, obj.repeat_count)
    span_progress = progress * span_count
    span_index = min(span_count - 1, int(span_progress))
    local_progress = span_progress - span_index
    if span_index % 2:
        local_progress = 1.0 - local_progress
    return point_on_polyline(points, local_progress)


def point_on_polyline(
    points: list[tuple[float, float]],
    progress: float,
) -> tuple[float, float]:
    if len(points) == 1:
        return points[0]
    segment_lengths = [dist(start, end) for start, end in zip(points, points[1:])]
    total_length = sum(segment_lengths)
    if total_length <= 0:
        return points[0]
    target = total_length * max(0.0, min(1.0, progress))
    traversed = 0.0
    for index, segment_length in enumerate(segment_lengths):
        if traversed + segment_length >= target:
            ratio = (target - traversed) / segment_length if segment_length else 0.0
            start = points[index]
            end = points[index + 1]
            return (
                start[0] + (end[0] - start[0]) * ratio,
                start[1] + (end[1] - start[1]) * ratio,
            )
        traversed += segment_length
    return points[-1]


def match_effective_clicks(
    frames: list[AbsoluteFrame],
    objects: list[HitObject],
    *,
    hit_window_ms: int,
    circle_radius: float,
    sequential: bool = True,
) -> list[ClickInterval | None]:
    clicks = extract_click_intervals(frames)
    if sequential:
        matched = match_sequential_clicks(
            frames, objects, clicks,
            hit_window_ms=hit_window_ms, circle_radius=circle_radius,
        )
    else:
        matched = match_valid_object_clicks(
            frames, objects, clicks,
            hit_window_ms=hit_window_ms, circle_radius=circle_radius,
        )
    return matched


def match_sequential_clicks(
    frames: list[AbsoluteFrame],
    objects: list[HitObject],
    clicks: list[ClickInterval],
    *,
    hit_window_ms: int,
    circle_radius: float,
) -> list[ClickInterval | None]:
    return match_realtime_clicks(
        frames,
        objects,
        clicks,
        hit_window_ms=hit_window_ms,
        circle_radius=circle_radius,
        sequential=True,
    )


def extract_click_intervals(frames: list[AbsoluteFrame]) -> list[ClickInterval]:
    clicks: list[ClickInterval] = []
    for output_bit, input_mask in LEGACY_INPUT_ACTION_MASKS:
        for interval in extract_any_key_intervals(frames, input_mask):
            clicks.append(
                ClickInterval(
                    index=len(clicks),
                    bit=output_bit,
                    start_ms=interval.start_ms,
                    end_ms=interval.end_ms,
                )
            )
    ordered = sorted(clicks, key=lambda click: (click.start_ms, click.end_ms, click.bit))
    return [replace(click, index=index) for index, click in enumerate(ordered)]


def match_valid_object_clicks(
    frames: list[AbsoluteFrame],
    objects: list[HitObject],
    clicks: list[ClickInterval],
    *,
    hit_window_ms: int,
    circle_radius: float,
) -> list[ClickInterval | None]:
    return match_realtime_clicks(
        frames,
        objects,
        clicks,
        hit_window_ms=hit_window_ms,
        circle_radius=circle_radius,
        sequential=False,
    )


def match_realtime_clicks(
    frames: list[AbsoluteFrame],
    objects: list[HitObject],
    clicks: list[ClickInterval],
    *,
    hit_window_ms: int,
    circle_radius: float,
    sequential: bool,
) -> list[ClickInterval | None]:
    """Simulate the input-side part of osu!lazer hit judgement.

    A click is an input event, not a candidate in a global assignment.  An
    early or misplaced press is consumed without judging the object.  An
    object is removed only after its successful hit window has elapsed, which
    is when lazer's automatic miss judgement takes effect.  In sequential
    mode, the LegacyHitPolicy note lock is applied to earlier objects which
    have already ended but have not been judged.
    """
    output: list[ClickInterval | None] = [None] * len(objects)
    head_judged = [not obj.is_clickable for obj in objects]
    parent_judged = [not obj.is_clickable and not obj.is_spinner for obj in objects]

    for click in clicks:
        advance_realtime_judgement(
            objects,
            click.start_ms,
            hit_window_ms,
            head_judged,
            parent_judged,
        )

        for object_index, obj in enumerate(objects):
            if not obj.is_clickable or head_judged[object_index]:
                continue
            if abs(click.start_ms - obj.time_ms) > hit_window_ms:
                continue
            if cursor_distance(frames, obj, click.start_ms) > circle_radius:
                continue
            if sequential and not legacy_policy_allows(
                objects,
                object_index,
                obj.time_ms,
                parent_judged,
            ):
                continue

            output[object_index] = click
            head_judged[object_index] = True
            if obj.is_circle:
                parent_judged[object_index] = True
            break

    return output


def advance_realtime_judgement(
    objects: list[HitObject],
    time_ms: int,
    hit_window_ms: int,
    head_judged: list[bool],
    parent_judged: list[bool],
) -> None:
    """Apply the automatic results that lazer would have applied by a time."""
    for index, obj in enumerate(objects):
        if obj.is_clickable and not head_judged[index] and time_ms > obj.time_ms + hit_window_ms:
            head_judged[index] = True

        if parent_judged[index]:
            continue
        if obj.is_circle and head_judged[index]:
            parent_judged[index] = True
        elif obj.is_slider and time_ms >= obj.resolved_end_time_ms + 3:
            parent_judged[index] = True
        elif obj.is_spinner and time_ms >= obj.resolved_end_time_ms:
            parent_judged[index] = True


def legacy_policy_allows(
    objects: list[HitObject],
    object_index: int,
    target_time_ms: int,
    parent_judged: list[bool],
) -> bool:
    """Mirror LegacyHitPolicy's predecessor note-lock check."""
    for previous_index, previous in enumerate(objects[:object_index]):
        if parent_judged[previous_index]:
            continue
        if previous.resolved_end_time_ms + 3 < target_time_ms:
            return False
    return True


def cursor_distance(frames: list[AbsoluteFrame], obj: HitObject, time_ms: int) -> float:
    x, y = interpolate_position(frames, time_ms)
    return dist((x, y), (obj.x, obj.y))


def modded_hit_objects_for_matching(objects: tuple[HitObject, ...], mods: int) -> list[HitObject]:
    if not mods & MOD_HARD_ROCK:
        return list(objects)
    return [
        replace(
            obj,
            y=OSU_STANDARD_PLAYFIELD_HEIGHT - obj.y,
            slider_control_points=tuple(
                (x, OSU_STANDARD_PLAYFIELD_HEIGHT - y)
                for x, y in obj.slider_control_points
            ),
        )
        for obj in objects
    ]


def modded_hit_window_50_ms(beatmap: Beatmap, mods: int) -> int:
    """Return the largest successful lazer hit offset for integer replay times."""
    od = beatmap.overall_difficulty
    if mods & MOD_HARD_ROCK:
        od = min(10.0, od * 1.4)
    if mods & MOD_EASY:
        od *= 0.5
    if od > 5:
        window = 150 + (100 - 150) * (od - 5) / 5
    else:
        window = 150 + (150 - 200) * (od - 5) / 5
    # lazer stores this window as floor(window) - 0.5. Replay timestamps are
    # integral, so the largest accepted integer offset is floor(window) - 1.
    return max(0, floor(window) - 1)


def modded_circle_radius(beatmap: Beatmap, mods: int) -> float:
    cs = beatmap.circle_size
    if mods & MOD_HARD_ROCK:
        cs = min(10.0, cs * 1.3)
    if mods & MOD_EASY:
        cs *= 0.5
    return (54.4 - 4.48 * cs) * 1.00041


def randomized_primary_key_repeat_threshold_ms(rng: Random | None = None) -> float:
    """Return the repeat threshold for one synthesis run."""
    uniform = rng.uniform if rng is not None else random.uniform
    return PRIMARY_KEY_REPEAT_THRESHOLD_MS + uniform(
        -PRIMARY_KEY_REPEAT_THRESHOLD_JITTER_MS,
        PRIMARY_KEY_REPEAT_THRESHOLD_JITTER_MS,
    )


def assign_natural_keys(
    intervals: list[KeyInterval],
    *,
    primary_key: int = LEGACY_Z_KEY,
    secondary_key: int = LEGACY_X_KEY,
    repeat_threshold_ms: float = PRIMARY_KEY_REPEAT_THRESHOLD_MS,
) -> dict[int, list[KeyInterval]]:
    """Assign note intervals using a primary-key repetition preference.

    An interval that starts while one key is still occupied must use the other
    key so its start produces a distinct press event.  When both keys are free,
    the normal primary-key repetition preference applies.

    The first note uses ``primary_key``. A note assigned to the secondary key
    is always followed by the primary key. Otherwise, the next note repeats the
    primary key only when its start is more than ``repeat_threshold_ms`` later;
    shorter gaps use the secondary key.
    """
    output = {primary_key: [], secondary_key: []}
    ordered_intervals = sorted(intervals, key=lambda interval: (interval.start_ms, interval.end_ms))
    preferred_key = primary_key
    for index, interval in enumerate(ordered_intervals):
        active_keys = {
            key
            for key, assigned_intervals in output.items()
            if assigned_intervals and assigned_intervals[-1].end_ms > interval.start_ms
        }
        if len(active_keys) > 1:
            raise SynthesisError(
                f"both synthesized keys are occupied at {interval.start_ms} ms"
            )
        if active_keys:
            active_key = next(iter(active_keys))
            current_key = secondary_key if active_key == primary_key else primary_key
        else:
            current_key = preferred_key

        output[current_key].append(interval)
        if index == len(ordered_intervals) - 1:
            break
        if current_key == secondary_key:
            preferred_key = primary_key
            continue
        gap_ms = ordered_intervals[index + 1].start_ms - interval.start_ms
        preferred_key = primary_key if gap_ms > repeat_threshold_ms else secondary_key
    return output


def synthesize_spinner_interval(
    obj: HitObject,
    first_previous_click: ClickInterval | None,
    first_next_click: ClickInterval | None,
    second_previous_click: ClickInterval | None,
    second_next_click: ClickInterval | None,
    rng: Random,
    *,
    break_periods: tuple[BreakPeriod, ...] = (),
) -> KeyInterval:
    start_anchor = spinner_boundary_anchor(
        first_previous_click.end_ms if first_previous_click is not None else None,
        second_previous_click.end_ms if second_previous_click is not None else None,
        fallback=obj.time_ms,
        take_max=True,
    )
    end_anchor = spinner_boundary_anchor(
        first_next_click.start_ms if first_next_click is not None else None,
        second_next_click.start_ms if second_next_click is not None else None,
        fallback=obj.resolved_end_time_ms,
        take_max=False,
    )

    start_ms = spinner_boundary_time(start_anchor, obj.time_ms, rng)
    for break_period in break_periods:
        if break_period.start_ms <= start_ms < break_period.end_ms:
            start_ms = max(break_period.end_ms, obj.time_ms - SPINNER_PREPRESS_LEAD_MS)
            break
    end_ms = spinner_boundary_time(end_anchor, obj.resolved_end_time_ms, rng)
    if end_ms <= start_ms:
        end_ms = start_ms + 1
    return KeyInterval(start_ms, end_ms)


def spinner_boundary_anchor(
    first_time: int | None,
    second_time: int | None,
    *,
    fallback: int,
    take_max: bool,
) -> int:
    if first_time is None and second_time is None:
        return fallback
    if first_time is None:
        return second_time if second_time is not None else fallback
    if second_time is None:
        return first_time
    return max(first_time, second_time) if take_max else min(first_time, second_time)


def spinner_boundary_time(t1: int, t2: int, rng: Random) -> int:
    delta = abs(t1 - t2)
    mean = (t1 + 3 * t2) / 4.0
    if delta == 0:
        return round(mean)

    sigma = delta / 4.0
    epsilon = bounded_normal(rng, sigma)
    return round(mean + epsilon)


def bounded_normal(rng: Random, sigma: float) -> float:
    if sigma <= 0:
        return 0.0

    bound = sigma
    epsilon = 0.0
    for _ in range(32):
        epsilon = rng.gauss(0.0, sigma)
        if abs(epsilon) < bound:
            return epsilon
    limit = nextafter(bound, 0.0)
    return max(-limit, min(limit, epsilon))


def previous_effective_clicks(clicks: list[ClickInterval | None]) -> list[ClickInterval | None]:
    output: list[ClickInterval | None] = []
    previous: ClickInterval | None = None
    for click in clicks:
        output.append(previous)
        if click is not None:
            previous = click
    return output


def next_effective_clicks(clicks: list[ClickInterval | None]) -> list[ClickInterval | None]:
    output: list[ClickInterval | None] = [None] * len(clicks)
    next_click: ClickInterval | None = None
    for index in range(len(clicks) - 1, -1, -1):
        output[index] = next_click
        if clicks[index] is not None:
            next_click = clicks[index]
    return output


def resolve_synthesized_overlaps(
    intervals: list[KeyInterval],
    required_end_times: list[int],
) -> list[KeyInterval]:
    """Preserve real holds while removing only redundant averaged overlap.

    A source click interval may legitimately continue past a circle or slider
    tail.  We therefore do not truncate holds during source matching.  When
    two independently timed replays are averaged, however, their intervals
    can create a third simultaneous hold that neither source had.  At that
    point a circle can be released after its hit, and a slider can be released
    after its tail.  Only those no-longer-required portions are removed.
    """
    if len(intervals) != len(required_end_times):
        raise SynthesisError("interval requirement metadata is out of sync")

    resolved = list(intervals)
    ordered_indices = sorted(
        range(len(resolved)),
        key=lambda index: (resolved[index].start_ms, resolved[index].end_ms),
    )
    processed: list[int] = []
    for index in ordered_indices:
        start_ms = resolved[index].start_ms
        active = [
            previous
            for previous in processed
            if resolved[previous].end_ms > start_ms
        ]
        if len(active) >= 2:
            for previous in active:
                required_end = required_end_times[previous]
                # Keep a real press/release pair for circles.  A zero-length
                # interval would disappear from keys_at_time entirely.
                trim_end = max(resolved[previous].start_ms + 1, required_end)
                if trim_end <= start_ms and resolved[previous].end_ms > trim_end:
                    resolved[previous] = replace(
                        resolved[previous],
                        end_ms=trim_end,
                    )

            active = [
                previous
                for previous in processed
                if resolved[previous].end_ms > start_ms
            ]
            if len(active) >= 2:
                raise SynthesisError(
                    f"three synthesized key intervals overlap at {start_ms} ms"
                )
        processed.append(index)
    return resolved


def ensure_no_triple_overlap(intervals: list[KeyInterval]) -> None:
    events: list[tuple[int, int]] = []
    for interval in intervals:
        events.append((interval.start_ms, 1))
        events.append((interval.end_ms, -1))
    active = 0
    for time_ms, delta in sorted(events, key=lambda event: (event[0], event[1])):
        active += delta
        if active > 2:
            raise SynthesisError(f"three synthesized key intervals overlap at {time_ms} ms")


def build_output_times(
    first: list[AbsoluteFrame],
    second: list[AbsoluteFrame],
    key_intervals: dict[int, list[KeyInterval]],
    *,
    skip_press_ms: int | None = None,
    intro_end_ms: int | None = None,
) -> list[int]:
    times = {frame.time_ms for frame in first}
    times.update(frame.time_ms for frame in second)
    for intervals in key_intervals.values():
        for interval in intervals:
            times.add(interval.start_ms)
            times.add(interval.end_ms)
    if skip_press_ms is not None:
        times.add(skip_press_ms)
    if intro_end_ms is not None:
        times.add(intro_end_ms)
    if skip_press_ms is not None and intro_end_ms is not None and skip_press_ms < intro_end_ms:
        times = {time for time in times if time <= skip_press_ms or time >= intro_end_ms}
    return sorted(time for time in times if time >= 0)


def keys_at_time(key_intervals: dict[int, list[KeyInterval]], time_ms: int) -> int:
    keys = 0
    for bit, intervals in key_intervals.items():
        if any(interval.start_ms <= time_ms < interval.end_ms for interval in intervals):
            keys |= bit
    return keys


def iter_key_bits(key_mask: int) -> list[int]:
    bits: list[int] = []
    bit = 1
    while bit <= key_mask:
        if key_mask & bit:
            bits.append(bit)
        bit <<= 1
    return bits


def average_seed(
    first_seed: int | None,
    second_seed: int | None,
    *,
    first_weight: float = 1.0,
    second_weight: float = 1.0,
    phase_offset_rad: float = 0.0,
) -> int | None:
    first_blend_weight, second_blend_weight = effective_weights(
        0,
        first_weight,
        second_weight,
        phase_offset_rad=phase_offset_rad,
    )
    return average_seed_weighted(first_seed, second_seed, first_blend_weight, second_blend_weight)


def average_seed_weighted(
    first_seed: int | None,
    second_seed: int | None,
    first_weight: float,
    second_weight: float,
) -> int | None:
    if first_seed is None and second_seed is None:
        return None
    if first_seed is None:
        return second_seed
    if second_seed is None:
        return first_seed
    return round(weighted_average_pair(first_seed, second_seed, first_weight, second_weight))


def earliest_time(first: int | None, second: int | None) -> int | None:
    if first is None:
        return second
    if second is None:
        return first
    return min(first, second)


def default_lazer_skip_target_ms(first_hit_object_time_ms: int) -> int:
    return max(0, first_hit_object_time_ms - LAZER_MINIMUM_SKIP_TIME_MS)


def dynamic_first_weight(time_ms: int, *, phase_offset_rad: float = 0.0) -> float:
    """Time-varying weight with one phase offset shared by a synthesis run."""
    import math
    t_sec = (time_ms / 1000.0) % 30.0
    raw = 0.5 + 0.5 * math.sin(math.pi * t_sec / 15.0 + phase_offset_rad)
    return max(0.0, min(1.0, raw))


def effective_weights(
    time_ms: int,
    first_w: float,
    second_w: float,
    *,
    phase_offset_rad: float = 0.0,
) -> tuple[float, float]:
    """Return (w1, w2) combining dynamic weight with user-specified modifier weights."""
    dynamic_w1 = dynamic_first_weight(time_ms, phase_offset_rad=phase_offset_rad)
    dynamic_w2 = 1.0 - dynamic_w1
    # Apply user weights as secondary multipliers, then normalize
    w1 = dynamic_w1 * first_w
    w2 = dynamic_w2 * second_w
    total = w1 + w2
    if total <= 0:
        return 0.5, 0.5
    return w1 / total, w2 / total


def validate_weights(first_weight: float, second_weight: float) -> None:
    if not isfinite(first_weight) or not isfinite(second_weight):
        raise SynthesisError("weights must be finite")
    if first_weight < 0 or second_weight < 0:
        raise SynthesisError("weights must be non-negative")
    if first_weight == 0 and second_weight == 0:
        raise SynthesisError("at least one weight must be positive")


def spinner_noise_seed(
    first: OsrReplay,
    second: OsrReplay,
    beatmap: Beatmap,
) -> int:
    digest = hashlib.blake2b(digest_size=16)
    for part in (
        beatmap.md5,
        *sorted((first.replay_md5, second.replay_md5)),
    ):
        digest.update(part.encode("utf-8"))
        digest.update(b"\0")
    return int.from_bytes(digest.digest(), "big")
