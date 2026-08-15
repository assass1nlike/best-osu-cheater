from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path


HIT_CIRCLE = 1
SLIDER = 2
SPINNER = 8


class BeatmapFormatError(ValueError):
    """Raised when a .osu beatmap file cannot be parsed."""


@dataclass(frozen=True)
class TimingPoint:
    time_ms: int
    beat_length: float
    uninherited: bool


@dataclass(frozen=True)
class BreakPeriod:
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class HitObject:
    index: int
    x: float
    y: float
    time_ms: int
    type_flags: int
    end_time_ms: int | None = None
    repeat_count: int = 1
    pixel_length: float = 0.0
    slider_nested_hit_count: int = 0
    slider_curve_type: str = ""
    slider_control_points: tuple[tuple[float, float], ...] = ()

    @property
    def is_clickable(self) -> bool:
        return bool(self.type_flags & (HIT_CIRCLE | SLIDER))

    @property
    def is_circle(self) -> bool:
        return bool(self.type_flags & HIT_CIRCLE)

    @property
    def is_slider(self) -> bool:
        return bool(self.type_flags & SLIDER)

    @property
    def is_spinner(self) -> bool:
        return bool(self.type_flags & SPINNER)

    @property
    def resolved_end_time_ms(self) -> int:
        return self.end_time_ms if self.end_time_ms is not None else self.time_ms

    @property
    def combo_increment(self) -> int:
        if self.is_slider:
            return 1 + self.slider_nested_hit_count
        return 1


@dataclass(frozen=True)
class Beatmap:
    md5: str
    audio_lead_in_ms: int
    circle_size: float
    overall_difficulty: float
    hit_objects: tuple[HitObject, ...]
    hp_drain_rate: float = 5.0
    slider_multiplier: float = 1.4
    slider_tick_rate: float = 1.0
    timing_points: tuple[TimingPoint, ...] = ()
    break_periods: tuple[BreakPeriod, ...] = ()

    @classmethod
    def read_path(cls, path: str | Path) -> "Beatmap":
        data = Path(path).read_bytes()
        return cls.from_bytes(data)

    @classmethod
    def from_bytes(cls, data: bytes) -> "Beatmap":
        text = data.decode("utf-8-sig")
        current_section = ""
        audio_lead_in_ms = 0
        hp_drain_rate = 5.0
        circle_size = 5.0
        overall_difficulty = 5.0
        slider_multiplier = 1.4
        slider_tick_rate = 1.0
        timing_points: list[TimingPoint] = []
        break_periods: list[BreakPeriod] = []
        hit_objects: list[HitObject] = []

        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("//"):
                continue
            if line.startswith("[") and line.endswith("]"):
                current_section = line[1:-1]
                continue
            if current_section == "General":
                key, value = parse_key_value(line)
                if key == "AudioLeadIn":
                    audio_lead_in_ms = int(float(value))
            elif current_section == "Difficulty":
                key, value = parse_key_value(line)
                if key == "HPDrainRate":
                    hp_drain_rate = float(value)
                elif key == "CircleSize":
                    circle_size = float(value)
                elif key == "OverallDifficulty":
                    overall_difficulty = float(value)
                elif key == "SliderMultiplier":
                    slider_multiplier = float(value)
                elif key == "SliderTickRate":
                    slider_tick_rate = float(value)
            elif current_section == "TimingPoints":
                timing_points.append(parse_timing_point(line))
            elif current_section == "Events":
                break_period = parse_break_period(line)
                if break_period is not None:
                    break_periods.append(break_period)
            elif current_section == "HitObjects":
                hit_objects.append(
                    parse_hit_object(
                        line,
                        len(hit_objects),
                        timing_points=tuple(timing_points),
                        slider_multiplier=slider_multiplier,
                        slider_tick_rate=slider_tick_rate,
                    )
                )

        if not hit_objects:
            raise BeatmapFormatError("beatmap contains no hit objects")

        return cls(
            md5=hashlib.md5(data).hexdigest(),
            audio_lead_in_ms=audio_lead_in_ms,
            circle_size=circle_size,
            overall_difficulty=overall_difficulty,
            hit_objects=tuple(hit_objects),
            hp_drain_rate=hp_drain_rate,
            slider_multiplier=slider_multiplier,
            slider_tick_rate=slider_tick_rate,
            timing_points=tuple(timing_points),
            break_periods=tuple(break_periods),
        )

    @property
    def clickable_hit_objects(self) -> tuple[HitObject, ...]:
        return tuple(obj for obj in self.hit_objects if obj.is_clickable)

    @property
    def first_hit_object_time_ms(self) -> int:
        return min(obj.time_ms for obj in self.hit_objects)

    @property
    def hit_window_50_ms(self) -> int:
        return round(200 - 10 * self.overall_difficulty)

    @property
    def circle_radius(self) -> float:
        return 54.4 - 4.48 * self.circle_size

    @property
    def max_combo(self) -> int:
        return sum(obj.combo_increment for obj in self.hit_objects)

    @property
    def drain_time_ms(self) -> int:
        return max(obj.resolved_end_time_ms for obj in self.hit_objects) - self.first_hit_object_time_ms


def parse_key_value(line: str) -> tuple[str, str]:
    if ":" not in line:
        raise BeatmapFormatError(f"invalid key/value line: {line!r}")
    key, value = line.split(":", 1)
    return key.strip(), value.strip()


def parse_timing_point(line: str) -> TimingPoint:
    parts = line.split(",")
    if len(parts) < 2:
        raise BeatmapFormatError(f"invalid timing point line: {line!r}")
    try:
        uninherited = True if len(parts) < 7 else int(parts[6]) == 1
        return TimingPoint(
            time_ms=int(float(parts[0])),
            beat_length=float(parts[1]),
            uninherited=uninherited,
        )
    except ValueError as exc:
        raise BeatmapFormatError(f"invalid timing point values: {line!r}") from exc


def parse_break_period(line: str) -> BreakPeriod | None:
    parts = line.split(",")
    if not parts or parts[0].strip() != "2":
        return None
    if len(parts) < 3:
        raise BeatmapFormatError(f"invalid break period: {line!r}")
    try:
        start_ms = int(float(parts[1]))
        end_ms = int(float(parts[2]))
    except ValueError as exc:
        raise BeatmapFormatError(f"invalid break period: {line!r}") from exc
    if end_ms <= start_ms:
        raise BeatmapFormatError(f"invalid break period: {line!r}")
    return BreakPeriod(start_ms=start_ms, end_ms=end_ms)


def parse_hit_object(
    line: str,
    index: int,
    *,
    timing_points: tuple[TimingPoint, ...] = (),
    slider_multiplier: float = 1.4,
    slider_tick_rate: float = 1.0,
) -> HitObject:
    parts = line.split(",")
    if len(parts) < 5:
        raise BeatmapFormatError(f"invalid hit object line: {line!r}")
    try:
        time_ms = int(parts[2])
        type_flags = int(parts[3])
        end_time_ms: int | None = None
        repeat_count = 1
        pixel_length = 0.0
        slider_nested_hit_count = 0
        slider_curve_type = ""
        slider_control_points: tuple[tuple[float, float], ...] = ()
        if type_flags & SLIDER and len(parts) >= 8:
            curve_type, slider_control_points = parse_slider_path(parts[5], float(parts[0]), float(parts[1]))
            slider_curve_type = curve_type
            repeat_count = max(1, int(parts[6]))
            pixel_length = max(0.0, float(parts[7]))
            duration = slider_duration_ms(
                time_ms,
                repeat_count,
                pixel_length,
                timing_points=timing_points,
                slider_multiplier=slider_multiplier,
            )
            end_time_ms = time_ms + duration
            slider_nested_hit_count = slider_nested_hits(
                repeat_count,
                pixel_length,
                slider_multiplier=slider_multiplier,
                slider_tick_rate=slider_tick_rate,
            )
        elif type_flags & SPINNER and len(parts) >= 6:
            end_time_ms = int(parts[5])
        return HitObject(
            index=index,
            x=float(parts[0]),
            y=float(parts[1]),
            time_ms=time_ms,
            type_flags=type_flags,
            end_time_ms=end_time_ms,
            repeat_count=repeat_count,
            pixel_length=pixel_length,
            slider_nested_hit_count=slider_nested_hit_count,
            slider_curve_type=slider_curve_type,
            slider_control_points=slider_control_points,
        )
    except ValueError as exc:
        raise BeatmapFormatError(f"invalid hit object values: {line!r}") from exc


def parse_slider_path(path: str, start_x: float, start_y: float) -> tuple[str, tuple[tuple[float, float], ...]]:
    if not path:
        return "", ((start_x, start_y),)
    raw_parts = path.split("|")
    curve_type = raw_parts[0]
    points = [(start_x, start_y)]
    for raw_point in raw_parts[1:]:
        if ":" not in raw_point:
            raise BeatmapFormatError(f"invalid slider control point: {raw_point!r}")
        raw_x, raw_y = raw_point.split(":", 1)
        points.append((float(raw_x), float(raw_y)))
    return curve_type, tuple(points)


def slider_duration_ms(
    time_ms: int,
    repeat_count: int,
    pixel_length: float,
    *,
    timing_points: tuple[TimingPoint, ...],
    slider_multiplier: float,
) -> int:
    beat_length = timing_beat_length(time_ms, timing_points)
    velocity_multiplier = timing_velocity_multiplier(time_ms, timing_points)
    pixels_per_beat = slider_multiplier * 100.0 * velocity_multiplier
    if pixels_per_beat <= 0:
        return 0
    span_duration = pixel_length / pixels_per_beat * beat_length
    return round(span_duration * repeat_count)


def slider_nested_hits(
    repeat_count: int,
    pixel_length: float,
    *,
    slider_multiplier: float,
    slider_tick_rate: float,
) -> int:
    if pixel_length <= 0 or slider_tick_rate <= 0:
        return repeat_count
    tick_distance = slider_multiplier * 100.0 / slider_tick_rate
    ticks_per_span = max(0, int((pixel_length - 0.1) // tick_distance))
    return repeat_count + ticks_per_span * repeat_count


def timing_beat_length(time_ms: int, timing_points: tuple[TimingPoint, ...]) -> float:
    inherited = [point for point in timing_points if point.uninherited and point.time_ms <= time_ms]
    if inherited:
        return inherited[-1].beat_length
    any_uninherited = [point for point in timing_points if point.uninherited]
    if any_uninherited:
        return any_uninherited[0].beat_length
    return 500.0


def timing_velocity_multiplier(time_ms: int, timing_points: tuple[TimingPoint, ...]) -> float:
    inherited = [point for point in timing_points if not point.uninherited and point.time_ms <= time_ms]
    if not inherited:
        return 1.0
    beat_length = inherited[-1].beat_length
    if beat_length >= 0:
        return 1.0
    return max(0.1, min(10.0, -100.0 / beat_length))
