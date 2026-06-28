from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from math import atan2, degrees, dist

from .beatmap import Beatmap, HitObject
from .osr import OsrReplay, ReplayFrame, decode_lazer_replay_metadata


MOD_EASY = 1 << 1
MOD_NO_FAIL = 1 << 0
MOD_HIDDEN = 1 << 3
MOD_HARD_ROCK = 1 << 4
MOD_SUDDEN_DEATH = 1 << 5
MOD_DOUBLE_TIME = 1 << 6
MOD_RELAX = 1 << 7
MOD_HALF_TIME = 1 << 8
MOD_FLASHLIGHT = 1 << 10
MOD_NIGHTCORE = 1 << 9
MOD_AUTOPILOT = 1 << 13
MOD_SPUN_OUT = 1 << 12

JUDGEMENT_300 = 300
JUDGEMENT_100 = 100
JUDGEMENT_50 = 50
JUDGEMENT_MISS = 0


@dataclass(frozen=True)
class AbsoluteFrame:
    time_ms: int
    x: float
    y: float
    keys: int


@dataclass(frozen=True)
class ClickInterval:
    index: int
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class ScoreMetadata:
    count_300: int
    count_100: int
    count_50: int
    count_geki: int
    count_katu: int
    count_miss: int
    score: int
    max_combo: int
    perfect: bool
    rank: str
    statistics: dict[str, int]
    maximum_statistics: dict[str, int]

    @property
    def total_hits(self) -> int:
        return self.count_300 + self.count_100 + self.count_50 + self.count_miss

    @property
    def accuracy(self) -> float:
        if self.total_hits == 0:
            return 0.0
        points = self.count_300 * 300 + self.count_100 * 100 + self.count_50 * 50
        return points / (self.total_hits * 300)


@dataclass(frozen=True)
class RulesetDifficulty:
    overall_difficulty: float
    circle_radius: float
    hit_window_300_ms: int
    hit_window_100_ms: int
    hit_window_50_ms: int


@dataclass
class SpinnerSpinHistory:
    total_accumulated_rotation: float = 0.0
    total_accumulated_rotation_at_last_completion: float = 0.0
    current_spin_max_rotation: float = 0.0
    completed_spins: int = 0
    last_report_time: float = float("-inf")

    def report_delta(self, current_time: float, delta: float) -> None:
        if delta == 0:
            return

        self.total_accumulated_rotation += delta
        current_spin_rotation = self.total_accumulated_rotation - self.total_accumulated_rotation_at_last_completion

        if current_time >= self.last_report_time:
            self.current_spin_max_rotation = max(self.current_spin_max_rotation, abs(current_spin_rotation))

            while self.current_spin_max_rotation >= 360:
                direction = 1 if current_spin_rotation >= 0 else -1
                self.completed_spins += 1
                self.total_accumulated_rotation_at_last_completion += direction * 360
                current_spin_rotation = self.total_accumulated_rotation - self.total_accumulated_rotation_at_last_completion
                self.current_spin_max_rotation = abs(current_spin_rotation)
        else:
            self.current_spin_max_rotation = abs(current_spin_rotation)

        self.last_report_time = current_time

    @property
    def total_rotation(self) -> float:
        return 360 * self.completed_spins + self.current_spin_max_rotation


@dataclass(frozen=True)
class ObjectResult:
    obj: HitObject
    judgement: int
    combo_increment: int

    @property
    def hit(self) -> bool:
        return self.judgement != JUDGEMENT_MISS


def replay_with_recomputed_score_metadata(replay: OsrReplay, beatmap: Beatmap) -> OsrReplay:
    metadata = score_replay(replay, beatmap)
    return replay.with_score_metadata(
        count_300=metadata.count_300,
        count_100=metadata.count_100,
        count_50=metadata.count_50,
        count_geki=metadata.count_geki,
        count_katu=metadata.count_katu,
        count_miss=metadata.count_miss,
        score=metadata.score,
        max_combo=metadata.max_combo,
        perfect=metadata.perfect,
        rank=metadata.rank,
        statistics=metadata.statistics,
        maximum_statistics=metadata.maximum_statistics,
    )


def score_replay(replay: OsrReplay, beatmap: Beatmap) -> ScoreMetadata:
    frames = split_sentinel(replay.frames)
    absolute_frames = to_absolute_frames(frames)
    if not absolute_frames:
        return empty_score_metadata(beatmap, replay.mods)
    difficulty = ruleset_difficulty(beatmap, replay.mods)
    clicks = extract_click_intervals(absolute_frames)
    used_clicks: set[int] = set()
    results: list[ObjectResult] = []

    for obj in beatmap.hit_objects:
        if obj.is_circle or obj.is_slider:
            result = judge_click_object(
                obj,
                absolute_frames,
                clicks,
                used_clicks,
                difficulty,
            )
        elif obj.is_spinner:
            result = judge_spinner(obj, absolute_frames, difficulty)
        else:
            result = ObjectResult(obj, JUDGEMENT_MISS, 0)
        results.append(result)

    counts = Counter(result.judgement for result in results)
    max_combo = calculate_max_combo(results)
    perfect = max_combo == beatmap.max_combo and counts[JUDGEMENT_MISS] == 0
    maximum_statistics = maximum_statistics_template(replay, beatmap)
    statistics = build_statistics(results, maximum_statistics=maximum_statistics)
    rank = calculate_rank(
        count_300=counts[JUDGEMENT_300],
        count_100=counts[JUDGEMENT_100],
        count_50=counts[JUDGEMENT_50],
        count_miss=counts[JUDGEMENT_MISS],
        mods=replay.mods,
    )
    score = calculate_score(results, beatmap, replay.mods, statistics, maximum_statistics)
    return ScoreMetadata(
        count_300=counts[JUDGEMENT_300],
        count_100=counts[JUDGEMENT_100],
        count_50=counts[JUDGEMENT_50],
        count_geki=0,
        count_katu=0,
        count_miss=counts[JUDGEMENT_MISS],
        score=score,
        max_combo=max_combo,
        perfect=perfect,
        rank=rank,
        statistics=statistics,
        maximum_statistics=maximum_statistics,
    )


def empty_score_metadata(beatmap: Beatmap, mods: int) -> ScoreMetadata:
    maximum_statistics = maximum_statistics_for_beatmap(beatmap)
    return ScoreMetadata(
        count_300=0,
        count_100=0,
        count_50=0,
        count_geki=0,
        count_katu=0,
        count_miss=len(beatmap.hit_objects),
        score=0,
        max_combo=0,
        perfect=False,
        rank=calculate_rank(count_300=0, count_100=0, count_50=0, count_miss=len(beatmap.hit_objects), mods=mods),
        statistics={"miss": len(beatmap.hit_objects)},
        maximum_statistics=maximum_statistics,
    )


def ruleset_difficulty(beatmap: Beatmap, mods: int) -> RulesetDifficulty:
    od = beatmap.overall_difficulty
    cs = beatmap.circle_size
    if mods & MOD_HARD_ROCK:
        od = min(10.0, od * 1.4)
        cs = min(10.0, cs * 1.3)
    if mods & MOD_EASY:
        od *= 0.5
        cs *= 0.5

    return RulesetDifficulty(
        overall_difficulty=od,
        circle_radius=54.4 - 4.48 * cs,
        hit_window_300_ms=round(80 - 6 * od),
        hit_window_100_ms=round(140 - 8 * od),
        hit_window_50_ms=round(200 - 10 * od),
    )


def mod_clock_rate(mods: int) -> float:
    if mods & (MOD_DOUBLE_TIME | MOD_NIGHTCORE):
        return 1.5
    if mods & MOD_HALF_TIME:
        return 0.75
    return 1.0


def judge_click_object(
    obj: HitObject,
    frames: list[AbsoluteFrame],
    clicks: list[ClickInterval],
    used_clicks: set[int],
    difficulty: RulesetDifficulty,
) -> ObjectResult:
    candidates: list[tuple[int, int, ClickInterval]] = []
    for click_index, click in enumerate(clicks):
        if click_index in used_clicks:
            continue
        time_delta = click.start_ms - obj.time_ms
        if abs(time_delta) > difficulty.hit_window_50_ms:
            continue
        cursor = interpolate_position(frames, click.start_ms)
        if dist(cursor, (obj.x, obj.y)) > difficulty.circle_radius:
            continue
        candidates.append((abs(time_delta), click_index, click))

    if not candidates:
        return ObjectResult(obj, JUDGEMENT_MISS, 0)

    time_delta_abs, click_index, _ = min(candidates)
    used_clicks.add(click_index)
    judgement = judgement_for_delta(time_delta_abs, difficulty)
    combo_increment = obj.combo_increment if judgement != JUDGEMENT_MISS else 0
    return ObjectResult(obj, judgement, combo_increment)


def judgement_for_delta(time_delta_abs: int, difficulty: RulesetDifficulty) -> int:
    if time_delta_abs <= difficulty.hit_window_300_ms:
        return JUDGEMENT_300
    if time_delta_abs <= difficulty.hit_window_100_ms:
        return JUDGEMENT_100
    if time_delta_abs <= difficulty.hit_window_50_ms:
        return JUDGEMENT_50
    return JUDGEMENT_MISS


def judge_spinner(
    obj: HitObject,
    frames: list[AbsoluteFrame],
    difficulty: RulesetDifficulty,
) -> ObjectResult:
    spins_required = spinner_spins_required(obj, difficulty)
    if spins_required == 0:
        return ObjectResult(obj, JUDGEMENT_300, obj.combo_increment)

    total_rotation = spinner_total_rotation(obj, frames)
    progress = total_rotation / 360 / spins_required
    if progress >= 1:
        judgement = JUDGEMENT_300
    elif progress > 0.9:
        judgement = JUDGEMENT_100
    elif progress > 0.75:
        judgement = JUDGEMENT_50
    else:
        judgement = JUDGEMENT_MISS

    combo_increment = obj.combo_increment if judgement != JUDGEMENT_MISS else 0
    return ObjectResult(obj, judgement, combo_increment)


def calculate_max_combo(results: list[ObjectResult]) -> int:
    current = 0
    maximum = 0
    for result in results:
        if result.hit:
            current += result.combo_increment
            maximum = max(maximum, current)
        else:
            current = 0
    return maximum


def spinner_spins_required(obj: HitObject, difficulty: RulesetDifficulty) -> int:
    duration_ms = max(0, obj.resolved_end_time_ms - obj.time_ms)
    seconds_duration = duration_ms / 1000.0
    min_rpm = difficulty_range(difficulty.overall_difficulty, 90, 150, 225)
    min_rps = min_rpm / 60.0
    return int(min_rps * seconds_duration + 0.0001)


def spinner_total_rotation(obj: HitObject, frames: list[AbsoluteFrame]) -> float:
    if len(frames) < 2:
        return 0.0

    history = SpinnerSpinHistory()
    last_angle: float | None = None

    for frame in frames:
        if frame.time_ms >= obj.resolved_end_time_ms:
            break

        angle = spinner_angle(frame.x, frame.y, obj.x, obj.y)
        if last_angle is not None:
            delta = normalize_spinner_delta(angle - last_angle)
            if frame.time_ms >= obj.time_ms and is_spinner_button_pressed(frame.keys):
                history.report_delta(frame.time_ms, delta)
        last_angle = angle

    return history.total_rotation


def build_statistics(
    results: list[ObjectResult],
    *,
    maximum_statistics: dict[str, int],
) -> dict[str, int]:
    statistics: Counter[str] = Counter()
    for result in results:
        obj = result.obj
        key = statistic_key_for_judgement(result.judgement)
        statistics[key] += 1
        if obj.is_slider:
            if result.hit:
                statistics["ignore_hit"] += 1
                statistics["slider_tail_hit"] += obj.repeat_count
                ticks = max(0, obj.slider_nested_hit_count - obj.repeat_count)
                if ticks:
                    statistics["small_bonus"] += ticks
            else:
                statistics["slider_tail_miss"] += obj.repeat_count
        elif obj.is_spinner and result.hit:
            statistics["large_bonus"] += 1
    if all(result.hit for result in results):
        for key in ("ignore_hit", "slider_tail_hit", "small_bonus", "large_bonus"):
            if key in maximum_statistics:
                statistics[key] = maximum_statistics[key]
    return dict(statistics)


def spinner_angle(x: float, y: float, center_x: float, center_y: float) -> float:
    return -degrees(atan2(x - center_x, y - center_y))


def normalize_spinner_delta(delta: float) -> float:
    if delta > 180:
        delta -= 360
    if delta < -180:
        delta += 360
    return delta


def is_spinner_button_pressed(keys: int) -> bool:
    return bool(keys & 0x0F)


def difficulty_range(difficulty: float, minimum: float, mid: float, maximum: float) -> float:
    if difficulty > 5:
        return mid + (maximum - mid) * (difficulty - 5) / 5
    if difficulty < 5:
        return minimum + (mid - minimum) * difficulty / 5
    return mid


def maximum_statistics_template(replay: OsrReplay, beatmap: Beatmap) -> dict[str, int]:
    metadata = decode_lazer_replay_metadata(replay.trailing_bytes)
    if metadata is not None:
        maximum_statistics = metadata.get("maximum_statistics")
        if is_int_dict(maximum_statistics):
            return dict(maximum_statistics)
    return maximum_statistics_for_beatmap(beatmap)


def is_int_dict(value: object) -> bool:
    return isinstance(value, dict) and all(isinstance(key, str) and isinstance(item, int) for key, item in value.items())


def maximum_statistics_for_beatmap(beatmap: Beatmap) -> dict[str, int]:
    statistics: Counter[str] = Counter()
    for obj in beatmap.hit_objects:
        statistics["great"] += 1
        if obj.is_slider:
            statistics["ignore_hit"] += 1
            statistics["slider_tail_hit"] += obj.repeat_count
            ticks = max(0, obj.slider_nested_hit_count - obj.repeat_count)
            if ticks:
                statistics["small_bonus"] += ticks
        elif obj.is_spinner:
            statistics["large_bonus"] += 1
    return dict(statistics)


def statistic_key_for_judgement(judgement: int) -> str:
    if judgement == JUDGEMENT_300:
        return "great"
    if judgement == JUDGEMENT_100:
        return "ok"
    if judgement == JUDGEMENT_50:
        return "meh"
    return "miss"


def calculate_rank(
    *,
    count_300: int,
    count_100: int,
    count_50: int,
    count_miss: int,
    mods: int,
) -> str:
    total = count_300 + count_100 + count_50 + count_miss
    if total == 0:
        return "F"
    accuracy = (count_300 * 300 + count_100 * 100 + count_50 * 50) / (total * 300)
    ratio_300 = count_300 / total
    ratio_50 = count_50 / total
    if count_miss > 0:
        rank = "A" if accuracy > 0.90 else "B" if accuracy > 0.80 else "C" if accuracy > 0.70 else "D"
    elif count_300 == total:
        rank = "X"
    elif ratio_300 > 0.90 and ratio_50 <= 0.01:
        rank = "S"
    elif ratio_300 > 0.80:
        rank = "A"
    elif ratio_300 > 0.70:
        rank = "B"
    elif ratio_300 > 0.60:
        rank = "C"
    else:
        rank = "D"

    if rank in {"X", "S"} and mods & (MOD_HIDDEN | MOD_FLASHLIGHT):
        return f"{rank}H"
    return rank


def calculate_score(
    results: list[ObjectResult],
    beatmap: Beatmap,
    mods: int,
    statistics: dict[str, int],
    maximum_statistics: dict[str, int],
) -> int:
    return calculate_standardised_score(results, beatmap, mods, statistics, maximum_statistics)


def calculate_standardised_score(
    results: list[ObjectResult],
    beatmap: Beatmap,
    mods: int,
    statistics: dict[str, int],
    maximum_statistics: dict[str, int],
) -> int:
    accuracy = replay_accuracy(results)
    maximum_combo_portion = calculate_maximum_combo_portion(beatmap)
    combo_progress = calculate_combo_portion(results) / maximum_combo_portion if maximum_combo_portion else 1.0
    accuracy_progress = 1.0 if results else 0.0
    bonus_portion = calculate_bonus_portion(statistics, maximum_statistics)
    score_without_mods = 500_000 * accuracy * combo_progress
    score_without_mods += 500_000 * (accuracy**5) * accuracy_progress
    score_without_mods += bonus_portion
    return round(score_without_mods * mod_score_multiplier(mods))


def calculate_combo_portion(results: list[ObjectResult]) -> float:
    combo = 0
    portion = 0.0
    for result in results:
        if not result.hit:
            combo = 0
            continue
        combo += 1
        portion += 300 * (combo**0.5)
        if result.obj.is_slider:
            combo += result.obj.repeat_count
            for tail_index in range(result.obj.repeat_count):
                portion += 150 * ((combo - result.obj.repeat_count + tail_index + 1) ** 0.5)
    return portion


def calculate_maximum_combo_portion(beatmap: Beatmap) -> float:
    return calculate_combo_portion(
        [
            ObjectResult(
                obj=obj,
                judgement=JUDGEMENT_300,
                combo_increment=obj.combo_increment,
            )
            for obj in beatmap.hit_objects
        ]
    )


def calculate_bonus_portion(statistics: dict[str, int], maximum_statistics: dict[str, int]) -> int:
    small_bonus = min(statistics.get("small_bonus", 0), maximum_statistics.get("small_bonus", 0))
    large_bonus = min(statistics.get("large_bonus", 0), maximum_statistics.get("large_bonus", 0))
    return small_bonus * 10 + large_bonus * 50


def replay_accuracy(results: list[ObjectResult]) -> float:
    if not results:
        return 0.0
    points = sum(result.judgement for result in results)
    return points / (len(results) * 300)


def calculate_legacy_score(results: list[ObjectResult], beatmap: Beatmap, mods: int) -> int:
    total_score = 0.0
    combo = 0
    difficulty_multiplier = round(beatmap.hp_drain_rate + beatmap.circle_size + beatmap.overall_difficulty + 3) / 38
    for result in results:
        judgement_value = result.judgement
        if judgement_value == JUDGEMENT_MISS:
            combo = 0
            continue
        total_score += judgement_value + judgement_value * ((max(combo - 1, 0) * difficulty_multiplier * mod_score_multiplier(mods)) / 25)
        combo += result.combo_increment
    return round(total_score)


def mod_score_multiplier(mods: int) -> float:
    multiplier = 1.0
    if mods & MOD_NO_FAIL:
        multiplier *= 0.5
    if mods & MOD_EASY:
        multiplier *= 0.5
    if mods & MOD_HALF_TIME:
        multiplier *= rate_adjust_multiplier(0.75)
    if mods & MOD_HARD_ROCK:
        multiplier *= 1.06
    if mods & MOD_NIGHTCORE:
        multiplier *= rate_adjust_multiplier(1.5)
    elif mods & MOD_DOUBLE_TIME:
        multiplier *= rate_adjust_multiplier(1.5)
    if mods & MOD_HIDDEN:
        multiplier *= 1.06
    if mods & MOD_FLASHLIGHT:
        multiplier *= 1.12
    if mods & MOD_RELAX:
        multiplier *= 0.1
    if mods & MOD_AUTOPILOT:
        multiplier *= 0.5
    if mods & MOD_SPUN_OUT:
        multiplier *= 0.9
    if mods & MOD_SUDDEN_DEATH:
        multiplier *= 1.0
    return multiplier


def rate_adjust_multiplier(speed_change: float) -> float:
    value = int(speed_change * 10) / 10.0
    value -= 1
    if speed_change >= 1:
        return 1 + value / 5
    return 0.6 + value


def split_sentinel(frames: tuple[ReplayFrame, ...]) -> tuple[ReplayFrame, ...]:
    if frames and frames[-1].delta_ms == -12345:
        return frames[:-1]
    return frames


def to_absolute_frames(frames: tuple[ReplayFrame, ...]) -> list[AbsoluteFrame]:
    absolute: list[AbsoluteFrame] = []
    time_ms = 0
    for frame in frames:
        time_ms += frame.delta_ms
        absolute.append(AbsoluteFrame(time_ms, frame.x, frame.y, frame.keys))
    return absolute


def extract_click_intervals(frames: list[AbsoluteFrame]) -> list[ClickInterval]:
    intervals: list[ClickInterval] = []
    for mask in (0x01 | 0x04, 0x02 | 0x08):
        intervals.extend(extract_click_intervals_for_mask(frames, mask, len(intervals)))
    return sorted(intervals, key=lambda interval: (interval.start_ms, interval.end_ms, interval.index))


def extract_click_intervals_for_mask(
    frames: list[AbsoluteFrame],
    mask: int,
    start_index: int,
) -> list[ClickInterval]:
    intervals: list[ClickInterval] = []
    active = False
    start_ms = 0
    for frame in frames:
        pressed = bool(frame.keys & mask)
        if pressed and not active:
            active = True
            start_ms = frame.time_ms
        elif not pressed and active:
            if frame.time_ms > start_ms:
                intervals.append(ClickInterval(start_index + len(intervals), start_ms, frame.time_ms))
            active = False
    if active and frames[-1].time_ms > start_ms:
        intervals.append(ClickInterval(start_index + len(intervals), start_ms, frames[-1].time_ms))
    return intervals


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
