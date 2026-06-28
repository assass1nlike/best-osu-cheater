from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import dist

from .beatmap import Beatmap, HitObject
from .osr import OsrReplay, ReplayFrame


OSU_STANDARD_MODE = 0
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


@dataclass(frozen=True)
class SynthesizedReplay:
    replay: OsrReplay
    report: SynthesisReport


def synthesize_replays(
    first: OsrReplay,
    second: OsrReplay,
    *,
    beatmap: Beatmap | None = None,
    player_name: str | None = None,
    allow_key_mismatch: bool = False,
    key_mask: int = DEFAULT_KEY_MASK,
    first_skip_ms: int | None = None,
    second_skip_ms: int | None = None,
    intro_end_ms: int | None = None,
    recompute_score_metadata: bool = False,
) -> SynthesizedReplay:
    validate_compatible(first, second)
    first_frames, first_seed = split_sentinel(first.frames)
    second_frames, second_seed = split_sentinel(second.frames)
    first_absolute = to_absolute_frames(first_frames)
    second_absolute = to_absolute_frames(second_frames)
    if not first_absolute or not second_absolute:
        raise SynthesisError("both replays need at least one non-sentinel frame")

    if beatmap is None:
        key_intervals = average_key_intervals(
            first_absolute,
            second_absolute,
            allow_mismatch=allow_key_mismatch,
            key_mask=key_mask,
        )
        matched_object_count = 0
        dropped_object_count = 0
    else:
        key_intervals, matched_object_count, dropped_object_count = synthesize_key_intervals_for_beatmap(
            first_absolute,
            second_absolute,
            beatmap,
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
            skip_press_ms=skip_press_ms,
            intro_end_ms=intro_end_ms,
        )
        for time_ms in output_times
    ]
    output_frames = to_delta_frames(output_absolute)
    seed = average_seed(first_seed, second_seed)
    if seed is not None:
        output_frames.append(ReplayFrame(SENTINEL_DELTA, 0.0, 0.0, seed))

    replay = first.with_frames(tuple(output_frames), player_name=player_name)
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
    )
    return SynthesizedReplay(replay=replay, report=report)


def average_frame_at_time(
    first: list[AbsoluteFrame],
    second: list[AbsoluteFrame],
    key_intervals: dict[int, list[KeyInterval]],
    time_ms: int,
    *,
    skip_press_ms: int | None = None,
    intro_end_ms: int | None = None,
) -> AbsoluteFrame:
    x, y = average_position_at_time(
        first,
        second,
        time_ms,
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
    skip_press_ms: int | None = None,
    intro_end_ms: int | None = None,
) -> tuple[float, float]:
    first_x, first_y = interpolate_position(first, time_ms)
    second_x, second_y = interpolate_position(second, time_ms)
    if skip_press_ms is not None and intro_end_ms is not None and time_ms <= skip_press_ms:
        anchor_x, anchor_y = average_position_at_time(first, second, intro_end_ms)
        return (first_x + second_x + anchor_x) / 3.0, (first_y + second_y + anchor_y) / 3.0
    return (first_x + second_x) / 2.0, (first_y + second_y) / 2.0


def validate_compatible(first: OsrReplay, second: OsrReplay) -> None:
    if first.mode != second.mode:
        raise SynthesisError(f"replay modes differ: {first.mode} != {second.mode}")
    if first.mode != OSU_STANDARD_MODE:
        raise SynthesisError("only osu!standard replays are supported")
    if first.beatmap_md5 != second.beatmap_md5:
        raise SynthesisError("beatmap MD5 hashes differ")
    if first.mods != second.mods:
        raise SynthesisError(f"mods differ: {first.mods} != {second.mods}")


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
            start = round((first_interval.start_ms + second_interval.start_ms) / 2)
            end = round((first_interval.end_ms + second_interval.end_ms) / 2)
            if end > start:
                intervals.append(KeyInterval(start, end))
        output[bit] = intervals
    return output


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
) -> tuple[dict[int, list[KeyInterval]], int, int]:
    objects = list(beatmap.clickable_hit_objects)
    if not objects:
        return {LEGACY_Z_KEY: [], LEGACY_X_KEY: []}, 0, 0

    first_clicks = match_effective_clicks(
        first,
        objects,
        hit_window_ms=beatmap.hit_window_50_ms,
        circle_radius=beatmap.circle_radius,
    )
    second_clicks = match_effective_clicks(
        second,
        objects,
        hit_window_ms=beatmap.hit_window_50_ms,
        circle_radius=beatmap.circle_radius,
    )

    averaged_intervals: list[KeyInterval] = []
    dropped_object_count = 0
    for first_click, second_click in zip(first_clicks, second_clicks, strict=True):
        if first_click is None or second_click is None:
            dropped_object_count += 1
            continue
        start = round((first_click.start_ms + second_click.start_ms) / 2)
        end = round((first_click.end_ms + second_click.end_ms) / 2)
        if end <= start:
            end = start + 1
        averaged_intervals.append(KeyInterval(start, end))

    ensure_no_triple_overlap(averaged_intervals)
    return assign_alternating_keys(averaged_intervals), len(averaged_intervals), dropped_object_count


def match_effective_clicks(
    frames: list[AbsoluteFrame],
    objects: list[HitObject],
    *,
    hit_window_ms: int,
    circle_radius: float,
) -> list[ClickInterval | None]:
    clicks = extract_click_intervals(frames)
    valid_clicks = match_valid_object_clicks(
        frames,
        objects,
        clicks,
        hit_window_ms=hit_window_ms,
        circle_radius=circle_radius,
    )
    return fill_missed_object_clicks(objects, clicks, valid_clicks)


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


def assign_alternating_keys(intervals: list[KeyInterval]) -> dict[int, list[KeyInterval]]:
    output = {LEGACY_Z_KEY: [], LEGACY_X_KEY: []}
    for index, interval in enumerate(intervals):
        bit = LEGACY_Z_KEY if index % 2 == 0 else LEGACY_X_KEY
        output[bit].append(interval)
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


def average_seed(first_seed: int | None, second_seed: int | None) -> int | None:
    if first_seed is None and second_seed is None:
        return None
    if first_seed is None:
        return second_seed
    if second_seed is None:
        return first_seed
    return round((first_seed + second_seed) / 2)


def earliest_time(first: int | None, second: int | None) -> int | None:
    if first is None:
        return second
    if second is None:
        return first
    return min(first, second)


def default_lazer_skip_target_ms(first_hit_object_time_ms: int) -> int:
    return max(0, first_hit_object_time_ms - LAZER_MINIMUM_SKIP_TIME_MS)
