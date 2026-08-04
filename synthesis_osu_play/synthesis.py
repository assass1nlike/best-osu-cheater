from __future__ import annotations

from collections.abc import Callable
import hashlib
import logging
import random
from dataclasses import dataclass, replace
from math import dist, isfinite, nextafter
from random import Random
from typing import TYPE_CHECKING

from .beatmap import Beatmap, HitObject
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
PRIMARY_KEY_REPEAT_THRESHOLD_MS = 500
PRIMARY_KEY_REPEAT_THRESHOLD_JITTER_MS = 100
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
            sequential_match=sequential_match,
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
    )
    if seed is not None:
        output_frames.append(ReplayFrame(SENTINEL_DELTA, 0.0, 0.0, seed))

    replay = first.with_frames(tuple(output_frames), player_name=player_name, mods=compatibility.output_mods)
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
) -> AbsoluteFrame:
    x, y = average_position_at_time(
        first,
        second,
        time_ms,
        first_weight=first_weight,
        second_weight=second_weight,
        skip_press_ms=skip_press_ms,
        intro_end_ms=intro_end_ms,
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
) -> tuple[float, float]:
    w1, w2 = effective_weights(time_ms, first_weight, second_weight)
    first_x, first_y = interpolate_position(first, time_ms)
    second_x, second_y = interpolate_position(second, time_ms)
    if skip_press_ms is not None and intro_end_ms is not None and intro_end_ms > skip_press_ms and time_ms <= skip_press_ms:
        anchor_x, anchor_y = average_position_at_time(
            first,
            second,
            intro_end_ms,
            first_weight=first_weight,
            second_weight=second_weight,
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
            w1, w2 = effective_weights(mid_ms, first_weight, second_weight)
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
    sequential_match: bool = True,
) -> tuple[dict[int, list[KeyInterval]], int, int]:
    objects = modded_hit_objects_for_matching(beatmap.hit_objects, output_mods)
    if not objects:
        return {LEGACY_Z_KEY: [], LEGACY_X_KEY: []}, 0, 0

    clickable_objects = [obj for obj in objects if obj.is_clickable]
    first_clicks = match_effective_clicks(
        first,
        clickable_objects,
        hit_window_ms=modded_hit_window_50_ms(beatmap, output_mods),
        circle_radius=modded_circle_radius(beatmap, output_mods),
        sequential=sequential_match,
    )
    second_clicks = match_effective_clicks(
        second,
        clickable_objects,
        hit_window_ms=modded_hit_window_50_ms(beatmap, output_mods),
        circle_radius=modded_circle_radius(beatmap, output_mods),
        sequential=sequential_match,
    )

    first_object_clicks = align_object_clicks(objects, first_clicks)
    second_object_clicks = align_object_clicks(objects, second_clicks)
    first_previous_clicks = previous_effective_clicks(first_object_clicks)
    first_next_clicks = next_effective_clicks(first_object_clicks)
    second_previous_clicks = previous_effective_clicks(second_object_clicks)
    second_next_clicks = next_effective_clicks(second_object_clicks)

    averaged_intervals: list[KeyInterval] = []
    dropped_object_count = 0
    matched_object_count = 0
    clickable_index = 0
    spinner_rng = Random(spinner_seed if spinner_seed is not None else 0)
    for object_index, obj in enumerate(objects):
        if obj.is_clickable:
            first_click = first_clicks[clickable_index]
            second_click = second_clicks[clickable_index]
            clickable_index += 1
            if first_click is None or second_click is None:
                dropped_object_count += 1
                continue
            w1, w2 = effective_weights(obj.time_ms, first_weight, second_weight)
            start = round(weighted_average_pair(first_click.start_ms, second_click.start_ms, w1, w2))
            end = round(weighted_average_pair(first_click.end_ms, second_click.end_ms, w1, w2))
            if end <= start:
                end = start + 1
            averaged_intervals.append(KeyInterval(start, end))
            matched_object_count += 1
        elif obj.is_spinner:
            interval = synthesize_spinner_interval(
                obj,
                first_previous_clicks[object_index],
                first_next_clicks[object_index],
                second_previous_clicks[object_index],
                second_next_clicks[object_index],
                spinner_rng,
            )
            averaged_intervals.append(interval)
            matched_object_count += 1

    ensure_no_triple_overlap(averaged_intervals)
    return (
        assign_natural_keys(
            averaged_intervals,
            repeat_threshold_ms=randomized_primary_key_repeat_threshold_ms(),
        ),
        matched_object_count,
        dropped_object_count,
    )


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
    return fill_missed_object_clicks(objects, clicks, matched)


def match_sequential_clicks(
    frames: list[AbsoluteFrame],
    objects: list[HitObject],
    clicks: list[ClickInterval],
    *,
    hit_window_ms: int,
    circle_radius: float,
) -> list[ClickInterval | None]:
    """Sequential matching: process clicks in order, match to first qualifying object.

    For each click in temporal order:
    - If it falls within the OD50 window of the current object AND cursor is in CS
      radius → match, advance both click and object pointers.
    - If it's before the object's window → skip this click (wasted press).
    - If it's after the object's window → skip the object (miss), re-check same click.
    """
    n_objects = len(objects)
    n_clicks = len(clicks)
    output: list[ClickInterval | None] = [None] * n_objects
    obj_idx = 0
    click_idx = 0

    while obj_idx < n_objects and click_idx < n_clicks:
        obj = objects[obj_idx]
        click = clicks[click_idx]
        delta = click.start_ms - obj.time_ms

        if abs(delta) <= hit_window_ms:
            # Check cursor distance
            if cursor_distance(frames, obj, click.start_ms) <= circle_radius:
                output[obj_idx] = click
                obj_idx += 1
                click_idx += 1
            else:
                # In window but cursor too far → skip this click
                click_idx += 1
        elif delta < -hit_window_ms:
            # Click is before the object's window → skip click
            click_idx += 1
        else:
            # delta > hit_window_ms: click is after the object's window → object missed
            obj_idx += 1
            # Don't advance click_idx; re-check against next object

    return output


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
    return sorted(clicks, key=lambda click: (click.start_ms, click.end_ms, click.bit))


def match_valid_object_clicks(
    frames: list[AbsoluteFrame],
    objects: list[HitObject],
    clicks: list[ClickInterval],
    *,
    hit_window_ms: int,
    circle_radius: float,
) -> list[ClickInterval | None]:
    candidates: list[tuple[int, int, int]] = []
    for object_index, obj in enumerate(objects):
        for click_index, click in enumerate(clicks):
            time_delta = abs(click.start_ms - obj.time_ms)
            if time_delta > hit_window_ms:
                continue
            if cursor_distance(frames, obj, click.start_ms) > circle_radius:
                continue
            candidates.append((time_delta, object_index, click_index))

    matched_objects: set[int] = set()
    matched_clicks: set[int] = set()
    output: list[ClickInterval | None] = [None] * len(objects)
    for _, object_index, click_index in sorted(candidates):
        if object_index in matched_objects or click_index in matched_clicks:
            continue
        output[object_index] = clicks[click_index]
        matched_objects.add(object_index)
        matched_clicks.add(click_index)
    return output


def fill_missed_object_clicks(
    objects: list[HitObject],
    clicks: list[ClickInterval],
    matched_clicks: list[ClickInterval | None],
) -> list[ClickInterval | None]:
    output = list(matched_clicks)
    assigned_click_indices = {click.index for click in output if click is not None}
    index = 0
    while index < len(output):
        if output[index] is not None:
            index += 1
            continue

        chain_start = index
        while index < len(output) and output[index] is None:
            index += 1
        chain_end = index
        chain_indices = list(range(chain_start, chain_end))
        candidates = candidate_clicks_for_miss_chain(
            clicks,
            assigned_click_indices,
            output[chain_start - 1] if chain_start > 0 else None,
            output[chain_end] if chain_end < len(output) else None,
            len(chain_indices),
        )
        assignments = greedy_match_clicks_to_objects(objects, chain_indices, candidates)
        for object_index, click in assignments.items():
            output[object_index] = click
            assigned_click_indices.add(click.index)

    return output


def candidate_clicks_for_miss_chain(
    clicks: list[ClickInterval],
    assigned_click_indices: set[int],
    left_click: ClickInterval | None,
    right_click: ClickInterval | None,
    needed_count: int,
) -> list[ClickInterval]:
    candidates = [click for click in clicks if click.index not in assigned_click_indices]
    if left_click is not None and right_click is not None:
        return [
            click
            for click in candidates
            if left_click.start_ms < click.start_ms < right_click.start_ms
        ]
    if right_click is not None:
        before = [click for click in candidates if click.start_ms < right_click.start_ms]
        return sorted(before, key=lambda click: right_click.start_ms - click.start_ms)[:needed_count]
    if left_click is not None:
        after = [click for click in candidates if click.start_ms > left_click.start_ms]
        return sorted(after, key=lambda click: click.start_ms - left_click.start_ms)[:needed_count]
    return []


def greedy_match_clicks_to_objects(
    objects: list[HitObject],
    object_indices: list[int],
    clicks: list[ClickInterval],
) -> dict[int, ClickInterval]:
    candidates: list[tuple[int, int, int]] = []
    for object_index in object_indices:
        for click_index, click in enumerate(clicks):
            candidates.append((abs(click.start_ms - objects[object_index].time_ms), object_index, click_index))

    used_objects: set[int] = set()
    used_clicks: set[int] = set()
    output: dict[int, ClickInterval] = {}
    for _, object_index, click_index in sorted(candidates):
        if object_index in used_objects or click_index in used_clicks:
            continue
        output[object_index] = clicks[click_index]
        used_objects.add(object_index)
        used_clicks.add(click_index)
    return output


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
    od = beatmap.overall_difficulty
    if mods & MOD_HARD_ROCK:
        od = min(10.0, od * 1.4)
    if mods & MOD_EASY:
        od *= 0.5
    return round(200 - 10 * od)


def modded_circle_radius(beatmap: Beatmap, mods: int) -> float:
    cs = beatmap.circle_size
    if mods & MOD_HARD_ROCK:
        cs = min(10.0, cs * 1.3)
    if mods & MOD_EASY:
        cs *= 0.5
    return 54.4 - 4.48 * cs


def randomized_primary_key_repeat_threshold_ms() -> float:
    """Return the repeat threshold for one synthesis run."""
    return PRIMARY_KEY_REPEAT_THRESHOLD_MS + random.uniform(
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

    The first note uses ``primary_key``. A note assigned to the secondary key
    is always followed by the primary key. Otherwise, the next note repeats the
    primary key only when its start is more than ``repeat_threshold_ms`` later;
    shorter gaps use the secondary key.
    """
    output = {primary_key: [], secondary_key: []}
    ordered_intervals = sorted(intervals, key=lambda interval: (interval.start_ms, interval.end_ms))
    current_key = primary_key
    for index, interval in enumerate(ordered_intervals):
        output[current_key].append(interval)
        if index == len(ordered_intervals) - 1:
            break
        if current_key == secondary_key:
            current_key = primary_key
            continue
        gap_ms = ordered_intervals[index + 1].start_ms - interval.start_ms
        current_key = primary_key if gap_ms > repeat_threshold_ms else secondary_key
    return output


def synthesize_spinner_interval(
    obj: HitObject,
    first_previous_click: ClickInterval | None,
    first_next_click: ClickInterval | None,
    second_previous_click: ClickInterval | None,
    second_next_click: ClickInterval | None,
    rng: Random,
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


def align_object_clicks(objects: list[HitObject], clicks: list[ClickInterval | None]) -> list[ClickInterval | None]:
    output: list[ClickInterval | None] = []
    click_index = 0
    for obj in objects:
        if obj.is_clickable:
            output.append(clicks[click_index])
            click_index += 1
        else:
            output.append(None)
    return output


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
) -> int | None:
    first_blend_weight, second_blend_weight = effective_weights(0, first_weight, second_weight)
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


def dynamic_first_weight(time_ms: int) -> float:
    """Time-varying weight for the first replay.  f(t) ∈ [0, 1], period 30s."""
    import math
    t_sec = (time_ms / 1000.0) % 30.0
    raw = 0.5 + 0.5 * math.sin(math.pi * t_sec / 15.0)
    return max(0.0, min(1.0, raw))


def effective_weights(time_ms: int, first_w: float, second_w: float) -> tuple[float, float]:
    """Return (w1, w2) combining dynamic weight with user-specified modifier weights."""
    dynamic_w1 = dynamic_first_weight(time_ms)
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
