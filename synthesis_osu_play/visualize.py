from __future__ import annotations

from bisect import bisect_left, bisect_right
from dataclasses import dataclass
from math import atan2, ceil, cos, hypot, pi, sin
from pathlib import Path

from .beatmap import Beatmap, HitObject
from .mods import legacy_mod_acronyms
from .osr import OsrReplay
from .synthesis import (
    LEGACY_LEFT_ACTION_MASK,
    LEGACY_RIGHT_ACTION_MASK,
    AbsoluteFrame,
    SynthesisError,
    interpolate_position,
    modded_circle_radius,
    modded_hit_objects_for_matching,
    normalize_absolute_frames,
    split_sentinel,
    synthesize_replays,
    to_absolute_frames,
    validate_compatible,
    effective_weights,
)


DEFAULT_VIDEO_WIDTH = 1280
DEFAULT_VIDEO_HEIGHT = 720
DEFAULT_VIDEO_FPS = 60.0
DEFAULT_VIDEO_SPEED = 1.0
DEFAULT_TRAIL_MS = 650
KEY_TIMELINE_WINDOW_MS = 3000
PLAYFIELD_WIDTH = 512.0
PLAYFIELD_HEIGHT = 384.0
SLIDER_SAMPLE_SPACING = 2.0
SLIDER_MIN_SAMPLES = 32
SLIDER_MAX_SAMPLES = 512
BACKGROUND_COLOR = (24, 26, 30)


@dataclass(frozen=True)
class DebugReplayTracks:
    first: list[AbsoluteFrame]
    second: list[AbsoluteFrame]
    synthesized: list[AbsoluteFrame]
    output_mods: int

    @property
    def start_ms(self) -> int:
        return min(self.first[0].time_ms, self.second[0].time_ms, self.synthesized[0].time_ms)

    @property
    def end_ms(self) -> int:
        return max(self.first[-1].time_ms, self.second[-1].time_ms, self.synthesized[-1].time_ms)


@dataclass(frozen=True)
class RenderLayout:
    width: int
    height: int
    playfield_left: int
    playfield_top: int
    scale: float
    panel_left: int
    timeline_left: int
    timeline_top: int
    timeline_width: int
    timeline_height: int
    world_left: float = 0.0
    world_top: float = 0.0
    world_width: float = PLAYFIELD_WIDTH
    world_height: float = PLAYFIELD_HEIGHT

    @property
    def playfield_right(self) -> int:
        return round(self.playfield_left + self.world_width * self.scale)

    @property
    def playfield_bottom(self) -> int:
        return round(self.playfield_top + self.world_height * self.scale)

    def point(self, x: float, y: float) -> tuple[int, int]:
        return (
            round(self.playfield_left + (x - self.world_left) * self.scale),
            round(self.playfield_top + (y - self.world_top) * self.scale),
        )


def prepare_debug_tracks(
    first: OsrReplay,
    second: OsrReplay,
    *,
    beatmap: Beatmap | None = None,
    synthesized: OsrReplay | None = None,
    player_name: str | None = None,
    first_weight: float = 1.0,
    second_weight: float = 1.0,
    allow_key_mismatch: bool = False,
    first_skip_ms: int | None = None,
    second_skip_ms: int | None = None,
    intro_end_ms: int | None = None,
) -> DebugReplayTracks:
    compatibility = validate_compatible(first, second)
    first_frames, _ = split_sentinel(first.frames)
    second_frames, _ = split_sentinel(second.frames)
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

    if synthesized is None:
        synthesized = synthesize_replays(
            first,
            second,
            beatmap=beatmap,
            player_name=player_name,
            first_weight=first_weight,
            second_weight=second_weight,
            allow_key_mismatch=allow_key_mismatch,
            first_skip_ms=first_skip_ms,
            second_skip_ms=second_skip_ms,
            intro_end_ms=intro_end_ms,
        ).replay
    synthesized_frames, _ = split_sentinel(synthesized.frames)
    synthesized_absolute = to_absolute_frames(synthesized_frames)
    if not first_absolute or not second_absolute or not synthesized_absolute:
        raise SynthesisError("all visualized replays need at least one non-sentinel frame")

    return DebugReplayTracks(
        first=first_absolute,
        second=second_absolute,
        synthesized=synthesized_absolute,
        output_mods=compatibility.output_mods,
    )


def write_replay_debug_video(
    first: OsrReplay,
    second: OsrReplay,
    output_path: str | Path,
    *,
    beatmap: Beatmap | None = None,
    synthesized: OsrReplay | None = None,
    player_name: str | None = None,
    first_weight: float = 1.0,
    second_weight: float = 1.0,
    allow_key_mismatch: bool = False,
    first_skip_ms: int | None = None,
    second_skip_ms: int | None = None,
    intro_end_ms: int | None = None,
    start_ms: int | None = None,
    end_ms: int | None = None,
    fps: float = DEFAULT_VIDEO_FPS,
    speed: float = DEFAULT_VIDEO_SPEED,
    width: int = DEFAULT_VIDEO_WIDTH,
    height: int = DEFAULT_VIDEO_HEIGHT,
    trail_ms: int = DEFAULT_TRAIL_MS,
) -> Path:
    try:
        import cv2
        import numpy as np
    except ImportError as exc:
        raise SynthesisError("debug video rendering requires opencv-python and numpy") from exc

    if fps <= 0:
        raise SynthesisError("debug video fps must be positive")
    if speed <= 0:
        raise SynthesisError("debug video speed must be positive")
    if trail_ms < 0:
        raise SynthesisError("debug video trail length must be non-negative")

    tracks = prepare_debug_tracks(
        first,
        second,
        beatmap=beatmap,
        synthesized=synthesized,
        player_name=player_name,
        first_weight=first_weight,
        second_weight=second_weight,
        allow_key_mismatch=allow_key_mismatch,
        first_skip_ms=first_skip_ms,
        second_skip_ms=second_skip_ms,
        intro_end_ms=intro_end_ms,
    )
    start = tracks.start_ms if start_ms is None else start_ms
    end = tracks.end_ms if end_ms is None else end_ms
    if end <= start:
        raise SynthesisError("debug video end time must be after start time")

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    objects = modded_hit_objects_for_matching(beatmap.hit_objects, tracks.output_mods) if beatmap is not None else []
    layout = make_layout(width, height, beatmap=beatmap, output_mods=tracks.output_mods, objects=objects)
    frame_step_ms = 1000.0 * speed / fps
    frame_count = max(1, int((end - start) / frame_step_ms) + 1)
    writer = cv2.VideoWriter(
        str(output_path),
        cv2.VideoWriter_fourcc(*codec_for_path(output_path)),
        fps,
        (width, height),
    )
    if not writer.isOpened():
        raise SynthesisError(f"could not open debug video writer for {output_path}")

    object_times = [obj.time_ms for obj in objects]
    try:
        for frame_index in range(frame_count):
            time_ms = round(start + frame_index * frame_step_ms)
            canvas = np.full((height, width, 3), BACKGROUND_COLOR, dtype=np.uint8)
            draw_key_timeline(canvas, layout, tracks, objects, time_ms, cv2)
            draw_playfield(canvas, layout, cv2)
            draw_hit_objects(canvas, layout, objects, object_times, time_ms, tracks.output_mods, beatmap, cv2)
            draw_tracks(canvas, layout, tracks, time_ms, trail_ms, first_weight, second_weight, cv2)
            draw_panel(canvas, layout, tracks, time_ms, first_weight, second_weight, fps, speed, cv2)
            writer.write(canvas)
    finally:
        writer.release()
    return output_path


def make_layout(
    width: int,
    height: int,
    *,
    beatmap: Beatmap | None = None,
    output_mods: int = 0,
    objects: list[HitObject] | None = None,
) -> RenderLayout:
    panel_width = 330
    margin = 44
    timeline_height = 148
    timeline_gap = 22
    available_width = width - panel_width - margin * 3
    available_height = height - margin * 2 - timeline_height - timeline_gap
    if available_width <= 0 or available_height <= 0:
        raise SynthesisError("debug video dimensions are too small")
    world_left, world_top, world_width, world_height = world_bounds_for_objects(
        beatmap,
        output_mods,
        objects,
    )
    scale = min(available_width / world_width, available_height / world_height)
    if scale <= 0:
        raise SynthesisError("debug video dimensions are too small")
    playfield_left = margin
    timeline_left = margin
    timeline_top = margin
    playfield_top = timeline_top + timeline_height + timeline_gap
    panel_left = playfield_left + round(world_width * scale) + margin
    return RenderLayout(
        width,
        height,
        playfield_left,
        playfield_top,
        scale,
        panel_left,
        timeline_left,
        timeline_top,
        width - margin * 2,
        timeline_height,
        world_left,
        world_top,
        world_width,
        world_height,
    )


def world_bounds_for_objects(
    beatmap: Beatmap | None,
    output_mods: int,
    objects: list[HitObject] | None,
) -> tuple[float, float, float, float]:
    min_x = 0.0
    min_y = 0.0
    max_x = PLAYFIELD_WIDTH
    max_y = PLAYFIELD_HEIGHT
    if objects:
        padding = modded_circle_radius(beatmap, output_mods) if beatmap is not None else 0.0
        for obj in objects:
            points = [(obj.x, obj.y), *obj.slider_control_points]
            if obj.is_slider and obj.slider_control_points:
                points.extend(slider_draw_points(obj))
            for x, y in points:
                min_x = min(min_x, x - padding)
                min_y = min(min_y, y - padding)
                max_x = max(max_x, x + padding)
                max_y = max(max_y, y + padding)
    return min_x, min_y, max(1.0, max_x - min_x), max(1.0, max_y - min_y)


def codec_for_path(path: Path) -> str:
    if path.suffix.lower() == ".avi":
        return "MJPG"
    return "mp4v"


def draw_playfield(canvas, layout: RenderLayout, cv2) -> None:
    cv2.rectangle(
        canvas,
        (layout.playfield_left, layout.playfield_top),
        (layout.playfield_right, layout.playfield_bottom),
        (88, 96, 110),
        1,
        cv2.LINE_AA,
    )
    standard_top_left = layout.point(0.0, 0.0)
    standard_bottom_right = layout.point(PLAYFIELD_WIDTH, PLAYFIELD_HEIGHT)
    is_standard_view = (
        abs(layout.world_left) < 1e-6
        and abs(layout.world_top) < 1e-6
        and abs(layout.world_width - PLAYFIELD_WIDTH) < 1e-6
        and abs(layout.world_height - PLAYFIELD_HEIGHT) < 1e-6
    )
    if is_standard_view:
        cv2.rectangle(canvas, standard_top_left, standard_bottom_right, (88, 96, 110), 1, cv2.LINE_AA)
    for x in (128, 256, 384):
        px, _ = layout.point(x, 0)
        cv2.line(canvas, (px, standard_top_left[1]), (px, standard_bottom_right[1]), (38, 42, 49), 1)
    for y in (96, 192, 288):
        _, py = layout.point(0, y)
        cv2.line(canvas, (standard_top_left[0], py), (standard_bottom_right[0], py), (38, 42, 49), 1)


def draw_hit_objects(
    canvas,
    layout: RenderLayout,
    objects: list[HitObject],
    object_times: list[int],
    time_ms: int,
    output_mods: int,
    beatmap: Beatmap | None,
    cv2,
) -> None:
    if beatmap is None:
        return
    start = bisect_left(object_times, time_ms - 1800)
    end = bisect_right(object_times, time_ms + 1800)
    radius = max(4, round(modded_circle_radius(beatmap, output_mods) * layout.scale))
    for obj in objects[start:end]:
        delta = obj.time_ms - time_ms
        if obj.is_spinner:
            top = layout.point(0, obj.y - 20)[1]
            bottom = layout.point(0, obj.y + 20)[1]
            color = (90, 90, 120) if delta >= 0 else (55, 55, 65)
            cv2.rectangle(canvas, (layout.playfield_left, top), (layout.playfield_right, bottom), color, 1, cv2.LINE_AA)
            continue
        if obj.is_slider:
            draw_slider_body(canvas, layout, obj, time_ms, radius, cv2)
        center = layout.point(obj.x, obj.y)
        if delta >= 0:
            color = (110, 120, 135)
            approach = max(radius + 2, round(radius * (1.0 + min(delta, 1200) / 1200)))
            cv2.circle(canvas, center, approach, (45, 50, 58), 1, cv2.LINE_AA)
        else:
            color = (58, 64, 72)
        cv2.circle(canvas, center, radius, color, 1, cv2.LINE_AA)


def draw_slider_body(canvas, layout: RenderLayout, obj: HitObject, time_ms: int, radius: int, cv2) -> None:
    if not obj.slider_control_points:
        return
    if time_ms < obj.time_ms - 1200 or time_ms > obj.resolved_end_time_ms + 250:
        return
    points = slider_draw_points(obj)
    if len(points) < 2:
        return
    draw_points = [layout.point(x, y) for x, y in points]
    active = obj.time_ms <= time_ms <= obj.resolved_end_time_ms
    edge_color = (145, 166, 190) if active else (86, 98, 112)
    draw_round_stroke(canvas, draw_points, radius + 1, edge_color, cv2)
    draw_round_stroke(canvas, draw_points, radius, BACKGROUND_COLOR, cv2)
    end_point = draw_points[-1]
    cv2.circle(canvas, end_point, radius, edge_color, 1, cv2.LINE_AA)


def draw_round_stroke(
    canvas,
    points: list[tuple[float, float]],
    radius: int,
    color: tuple[int, int, int],
    cv2,
) -> None:
    if radius <= 0:
        return
    rounded_points = [round_point(point) for point in points]
    for start, end in zip(points, points[1:], strict=False):
        cv2.line(canvas, round_point(start), round_point(end), color, radius * 2, cv2.LINE_AA)
    for point in rounded_points:
        cv2.circle(canvas, point, radius, color, -1, cv2.LINE_AA)


def round_point(point: tuple[float, float]) -> tuple[int, int]:
    return round(point[0]), round(point[1])


def slider_draw_points(obj: HitObject) -> list[tuple[float, float]]:
    points = list(obj.slider_control_points)
    if len(points) < 2:
        return points
    curve_type = obj.slider_curve_type.upper()
    if curve_type == "L":
        return points
    if curve_type == "P" and len(points) >= 3:
        return perfect_curve_points(points[:3])
    if curve_type == "C":
        return catmull_points(points)
    return bezier_path_points(points)


def bezier_path_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    output: list[tuple[float, float]] = []
    for segment in split_bezier_segments(points):
        sampled = bezier_points(segment)
        if output and sampled and output[-1] == sampled[0]:
            output.extend(sampled[1:])
        else:
            output.extend(sampled)
    return output


def split_bezier_segments(points: list[tuple[float, float]]) -> list[list[tuple[float, float]]]:
    segments: list[list[tuple[float, float]]] = []
    current = [points[0]]
    previous = points[0]
    for point in points[1:]:
        if point == previous:
            if len(current) >= 2:
                segments.append(current)
            current = [point]
        else:
            current.append(point)
        previous = point
    if len(current) >= 2:
        segments.append(current)
    return segments or [points]


def bezier_points(points: list[tuple[float, float]], samples: int | None = None) -> list[tuple[float, float]]:
    if len(points) <= 2:
        return points
    if samples is None:
        samples = samples_for_length(control_polygon_length(points))
    output: list[tuple[float, float]] = []
    for sample in range(samples + 1):
        t = sample / samples
        working = points[:]
        while len(working) > 1:
            working = [
                (
                    working[index][0] + (working[index + 1][0] - working[index][0]) * t,
                    working[index][1] + (working[index + 1][1] - working[index][1]) * t,
                )
                for index in range(len(working) - 1)
            ]
        output.append(working[0])
    return output


def catmull_points(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if len(points) <= 2:
        return points
    output: list[tuple[float, float]] = []
    padded = [points[0], *points, points[-1]]
    for index in range(1, len(padded) - 2):
        p0, p1, p2, p3 = padded[index - 1], padded[index], padded[index + 1], padded[index + 2]
        samples_per_segment = samples_for_length(distance(p1, p2), min_samples=12, max_samples=96)
        for sample in range(samples_per_segment):
            t = sample / samples_per_segment
            t2 = t * t
            t3 = t2 * t
            x = 0.5 * (
                2 * p1[0]
                + (-p0[0] + p2[0]) * t
                + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
                + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3
            )
            y = 0.5 * (
                2 * p1[1]
                + (-p0[1] + p2[1]) * t
                + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3
            )
            output.append((x, y))
    output.append(points[-1])
    return output


def perfect_curve_points(points: list[tuple[float, float]], samples: int | None = None) -> list[tuple[float, float]]:
    p1, p2, p3 = points[:3]
    center = circle_center(p1, p2, p3)
    if center is None:
        return points
    cx, cy = center
    radius = hypot(p1[0] - cx, p1[1] - cy)
    if radius == 0:
        return points
    start_angle = atan2(p1[1] - cy, p1[0] - cx)
    mid_angle = atan2(p2[1] - cy, p2[0] - cx)
    end_angle = atan2(p3[1] - cy, p3[0] - cx)
    sweep = normalize_angle(end_angle - start_angle)
    mid_sweep = normalize_angle(mid_angle - start_angle)
    if not angle_between(mid_sweep, sweep):
        sweep = sweep - 2 * pi if sweep > 0 else sweep + 2 * pi
    if samples is None:
        samples = samples_for_length(abs(sweep) * radius)
    return [
        (
            cx + cos(start_angle + sweep * sample / samples) * radius,
            cy + sin(start_angle + sweep * sample / samples) * radius,
        )
        for sample in range(samples + 1)
    ]


def samples_for_length(
    length: float,
    *,
    min_samples: int = SLIDER_MIN_SAMPLES,
    max_samples: int = SLIDER_MAX_SAMPLES,
) -> int:
    if length <= 0:
        return min_samples
    return max(min_samples, min(max_samples, ceil(length / SLIDER_SAMPLE_SPACING)))


def control_polygon_length(points: list[tuple[float, float]]) -> float:
    return sum(distance(start, end) for start, end in zip(points, points[1:], strict=False))


def distance(start: tuple[float, float], end: tuple[float, float]) -> float:
    return hypot(end[0] - start[0], end[1] - start[1])


def circle_center(
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
) -> tuple[float, float] | None:
    ax, ay = p1
    bx, by = p2
    cx, cy = p3
    determinant = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(determinant) < 1e-6:
        return None
    ux = (
        (ax * ax + ay * ay) * (by - cy)
        + (bx * bx + by * by) * (cy - ay)
        + (cx * cx + cy * cy) * (ay - by)
    ) / determinant
    uy = (
        (ax * ax + ay * ay) * (cx - bx)
        + (bx * bx + by * by) * (ax - cx)
        + (cx * cx + cy * cy) * (bx - ax)
    ) / determinant
    return ux, uy


def normalize_angle(angle: float) -> float:
    while angle <= -pi:
        angle += 2 * pi
    while angle > pi:
        angle -= 2 * pi
    return angle


def angle_between(mid_sweep: float, sweep: float) -> bool:
    if sweep >= 0:
        return 0 <= mid_sweep <= sweep
    return sweep <= mid_sweep <= 0


def draw_tracks(
    canvas,
    layout: RenderLayout,
    tracks: DebugReplayTracks,
    time_ms: int,
    trail_ms: int,
    first_weight: float,
    second_weight: float,
    cv2,
) -> None:
    first_color = (68, 105, 255)
    second_color = (255, 145, 70)
    synth_color = (96, 230, 170)
    first_pos = interpolate_position(tracks.first, time_ms)
    second_pos = interpolate_position(tracks.second, time_ms)
    synth_pos = interpolate_position(tracks.synthesized, time_ms)
    effective_first_weight, effective_second_weight = effective_weights(time_ms, first_weight, second_weight)
    cv2.line(canvas, layout.point(*first_pos), layout.point(*second_pos), (62, 72, 82), 1, cv2.LINE_AA)
    draw_trail(canvas, layout, tracks.first, time_ms, trail_ms, first_color, cv2)
    draw_trail(canvas, layout, tracks.second, time_ms, trail_ms, second_color, cv2)
    draw_trail(canvas, layout, tracks.synthesized, time_ms, trail_ms, synth_color, cv2, thickness=3)
    draw_cursor(canvas, layout, first_pos, first_color, "1", cv2)
    draw_cursor(canvas, layout, second_pos, second_color, "2", cv2)
    draw_cursor(canvas, layout, synth_pos, synth_color, "S", cv2, radius=8)

    expected = weighted_position(first_pos, second_pos, effective_first_weight, effective_second_weight)
    expected_point = layout.point(*expected)
    cv2.drawMarker(canvas, expected_point, (80, 255, 255), cv2.MARKER_CROSS, 18, 1, cv2.LINE_AA)


def draw_trail(
    canvas,
    layout: RenderLayout,
    frames: list[AbsoluteFrame],
    time_ms: int,
    trail_ms: int,
    color: tuple[int, int, int],
    cv2,
    *,
    thickness: int = 2,
) -> None:
    if trail_ms == 0:
        return
    sample_step = max(12, min(40, trail_ms // 20 if trail_ms else 12))
    times = range(max(frames[0].time_ms, time_ms - trail_ms), time_ms + 1, sample_step)
    points = [layout.point(*interpolate_position(frames, sample_time)) for sample_time in times]
    if len(points) < 2:
        return
    for index, (start, end) in enumerate(zip(points, points[1:], strict=False)):
        fade = (index + 1) / (len(points) - 1)
        segment_color = tuple(round(channel * (0.25 + 0.75 * fade)) for channel in color)
        cv2.line(canvas, start, end, segment_color, thickness, cv2.LINE_AA)


def draw_cursor(
    canvas,
    layout: RenderLayout,
    position: tuple[float, float],
    color: tuple[int, int, int],
    label: str,
    cv2,
    *,
    radius: int = 7,
) -> None:
    center = layout.point(*position)
    cv2.circle(canvas, center, radius + 3, (18, 20, 24), -1, cv2.LINE_AA)
    cv2.circle(canvas, center, radius, color, -1, cv2.LINE_AA)
    cv2.putText(canvas, label, (center[0] + 10, center[1] - 10), cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1, cv2.LINE_AA)


def draw_panel(
    canvas,
    layout: RenderLayout,
    tracks: DebugReplayTracks,
    time_ms: int,
    first_weight: float,
    second_weight: float,
    fps: float,
    speed: float,
    cv2,
) -> None:
    x = layout.panel_left
    y = layout.playfield_top
    mods = ",".join(legacy_mod_acronyms(tracks.output_mods)) or "NM"
    first_pos = interpolate_position(tracks.first, time_ms)
    second_pos = interpolate_position(tracks.second, time_ms)
    synth_pos = interpolate_position(tracks.synthesized, time_ms)
    effective_first_weight, effective_second_weight = effective_weights(time_ms, first_weight, second_weight)
    expected = weighted_position(first_pos, second_pos, effective_first_weight, effective_second_weight)
    distance = ((first_pos[0] - second_pos[0]) ** 2 + (first_pos[1] - second_pos[1]) ** 2) ** 0.5
    residual = ((expected[0] - synth_pos[0]) ** 2 + (expected[1] - synth_pos[1]) ** 2) ** 0.5

    lines = [
        "synthesis debug",
        f"time {time_ms / 1000:.3f}s",
        f"weights {effective_first_weight:.2f}:{effective_second_weight:.2f}",
        f"output mods {mods}",
        f"fps {fps:g} speed {speed:g}x",
        f"source distance {distance:.2f}px",
        f"weighted residual {residual:.3f}px",
    ]
    for line in lines:
        cv2.putText(canvas, line, (x, y), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (214, 220, 230), 1, cv2.LINE_AA)
        y += 28
    y += 10
    draw_key_row(canvas, x, y, "first", keys_at_time(tracks.first, time_ms), (68, 105, 255), cv2)
    draw_key_row(canvas, x, y + 38, "second", keys_at_time(tracks.second, time_ms), (255, 145, 70), cv2)
    draw_key_row(canvas, x, y + 76, "synth", keys_at_time(tracks.synthesized, time_ms), (96, 230, 170), cv2)


def draw_key_timeline(
    canvas,
    layout: RenderLayout,
    tracks: DebugReplayTracks,
    objects: list[HitObject],
    time_ms: int,
    cv2,
) -> None:
    left = layout.timeline_left
    top = layout.timeline_top
    width = layout.timeline_width
    height = layout.timeline_height
    row_height = 27
    label_width = 78
    lane_left = left + label_width
    lane_width = width - label_width
    center_x = lane_left + lane_width // 2
    window_start = time_ms - KEY_TIMELINE_WINDOW_MS // 2
    window_end = time_ms + KEY_TIMELINE_WINDOW_MS // 2
    rows = (
        ("first", tracks.first, (68, 105, 255)),
        ("second", tracks.second, (255, 145, 70)),
        ("synth", tracks.synthesized, (96, 230, 170)),
    )

    cv2.rectangle(canvas, (left, top), (left + width, top + height), (30, 34, 40), -1, cv2.LINE_AA)
    cv2.rectangle(canvas, (left, top), (left + width, top + height), (74, 82, 96), 1, cv2.LINE_AA)
    cv2.putText(
        canvas,
        "key hold timeline",
        (left + 12, top + 20),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.48,
        (210, 216, 226),
        1,
        cv2.LINE_AA,
    )
    for offset_ms in (-1000, 0, 1000):
        x = timeline_x_for_time(time_ms + offset_ms, window_start, window_end, lane_left, lane_width)
        color = (84, 94, 108) if offset_ms == 0 else (48, 54, 64)
        cv2.line(canvas, (x, top + 25), (x, top + height - 8), color, 1, cv2.LINE_AA)
        if offset_ms != 0:
            cv2.putText(
                canvas,
                f"{offset_ms / 1000:+.0f}s",
                (x - 12, top + height - 10),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.36,
                (130, 138, 150),
                1,
                cv2.LINE_AA,
            )
    cv2.line(canvas, (center_x, top + 24), (center_x, top + height - 8), (220, 226, 238), 1, cv2.LINE_AA)

    for index, (label, frames, color) in enumerate(rows):
        y = top + 31 + index * row_height
        cv2.putText(canvas, label, (left + 12, y + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.43, (198, 205, 216), 1, cv2.LINE_AA)
        cv2.rectangle(canvas, (lane_left, y), (lane_left + lane_width, y + 18), (39, 43, 50), -1, cv2.LINE_AA)
        cv2.rectangle(canvas, (lane_left, y), (lane_left + lane_width, y + 18), (58, 65, 76), 1, cv2.LINE_AA)
        draw_timeline_key_intervals(
            canvas,
            frames,
            window_start,
            window_end,
            lane_left,
            lane_width,
            y,
            color,
            cv2,
        )
    object_y = top + 31 + len(rows) * row_height
    draw_timeline_objects(canvas, objects, window_start, window_end, lane_left, lane_width, object_y, cv2)


def draw_timeline_key_intervals(
    canvas,
    frames: list[AbsoluteFrame],
    window_start: int,
    window_end: int,
    lane_left: int,
    lane_width: int,
    y: int,
    color: tuple[int, int, int],
    cv2,
) -> None:
    intervals = key_hold_intervals_in_window(frames, window_start, window_end)
    for start_ms, end_ms, keys in intervals:
        start_x = timeline_x_for_time(start_ms, window_start, window_end, lane_left, lane_width)
        end_x = timeline_x_for_time(end_ms, window_start, window_end, lane_left, lane_width)
        if end_x <= start_x:
            end_x = start_x + 1
        left_pressed = bool(keys & LEGACY_LEFT_ACTION_MASK)
        right_pressed = bool(keys & LEGACY_RIGHT_ACTION_MASK)
        if left_pressed:
            cv2.rectangle(canvas, (start_x, y + 3), (end_x, y + 9), color, -1, cv2.LINE_AA)
        if right_pressed:
            cv2.rectangle(canvas, (start_x, y + 10), (end_x, y + 16), color, -1, cv2.LINE_AA)
        if left_pressed and right_pressed:
            cv2.line(canvas, (start_x, y + 9), (end_x, y + 9), (235, 240, 248), 1, cv2.LINE_AA)


def draw_timeline_objects(
    canvas,
    objects: list[HitObject],
    window_start: int,
    window_end: int,
    lane_left: int,
    lane_width: int,
    y: int,
    cv2,
) -> None:
    cv2.putText(canvas, "objects", (lane_left - 66, y + 16), cv2.FONT_HERSHEY_SIMPLEX, 0.43, (198, 205, 216), 1, cv2.LINE_AA)
    cv2.rectangle(canvas, (lane_left, y), (lane_left + lane_width, y + 18), (39, 43, 50), -1, cv2.LINE_AA)
    cv2.rectangle(canvas, (lane_left, y), (lane_left + lane_width, y + 18), (58, 65, 76), 1, cv2.LINE_AA)
    object_times = [obj.time_ms for obj in objects]
    start = bisect_left(object_times, window_start)
    end = bisect_right(object_times, window_end)
    active_sliders = [
        obj
        for obj in objects
        if obj.is_slider and obj.time_ms < window_start and obj.resolved_end_time_ms >= window_start
    ]
    for obj in [*active_sliders, *objects[start:end]]:
        if obj.is_slider:
            draw_timeline_slider(canvas, obj, window_start, window_end, lane_left, lane_width, y, cv2)
        else:
            x = timeline_x_for_time(obj.time_ms, window_start, window_end, lane_left, lane_width)
            cv2.line(canvas, (x, y + 2), (x, y + 16), (110, 200, 255), 1, cv2.LINE_AA)
            cv2.circle(canvas, (x, y + 9), 2, (110, 200, 255), -1, cv2.LINE_AA)


def draw_timeline_slider(
    canvas,
    obj: HitObject,
    window_start: int,
    window_end: int,
    lane_left: int,
    lane_width: int,
    y: int,
    cv2,
) -> None:
    start_ms = max(obj.time_ms, window_start)
    end_ms = min(obj.resolved_end_time_ms, window_end)
    if start_ms >= end_ms:
        return
    start_x = timeline_x_for_time(start_ms, window_start, window_end, lane_left, lane_width)
    end_x = timeline_x_for_time(end_ms, window_start, window_end, lane_left, lane_width)
    if end_x <= start_x:
        end_x = start_x + 1
    cv2.rectangle(canvas, (start_x, y + 5), (end_x, y + 13), (65, 92, 112), -1, cv2.LINE_AA)
    cv2.rectangle(canvas, (start_x, y + 5), (end_x, y + 13), (110, 200, 255), 1, cv2.LINE_AA)
    for marker_time, marker_kind in slider_timeline_markers(obj):
        if marker_time < window_start or marker_time > window_end:
            continue
        x = timeline_x_for_time(marker_time, window_start, window_end, lane_left, lane_width)
        if marker_kind == "tick":
            cv2.line(canvas, (x, y + 3), (x, y + 15), (245, 235, 120), 1, cv2.LINE_AA)
        elif marker_kind == "repeat":
            cv2.drawMarker(canvas, (x, y + 9), (255, 180, 110), cv2.MARKER_TRIANGLE_UP, 9, 1, cv2.LINE_AA)
        elif marker_kind == "tail":
            cv2.drawMarker(canvas, (x, y + 9), (110, 200, 255), cv2.MARKER_CROSS, 9, 1, cv2.LINE_AA)


def slider_timeline_markers(obj: HitObject) -> list[tuple[int, str]]:
    duration = obj.resolved_end_time_ms - obj.time_ms
    if duration <= 0:
        return [(obj.time_ms, "tail")]
    repeat_count = max(1, obj.repeat_count)
    span_duration = duration / repeat_count
    tick_count_per_span = max(0, obj.slider_nested_hit_count // repeat_count - 1)
    markers: list[tuple[int, str]] = []
    for span in range(repeat_count):
        span_start = obj.time_ms + span_duration * span
        for tick_index in range(1, tick_count_per_span + 1):
            markers.append((round(span_start + span_duration * tick_index / (tick_count_per_span + 1)), "tick"))
    for repeat in range(1, repeat_count):
        markers.append((round(obj.time_ms + span_duration * repeat), "repeat"))
    markers.append((obj.resolved_end_time_ms, "tail"))
    return sorted(markers)


def key_hold_intervals_in_window(
    frames: list[AbsoluteFrame],
    window_start: int,
    window_end: int,
) -> list[tuple[int, int, int]]:
    intervals: list[tuple[int, int, int]] = []
    if not frames or window_end <= window_start:
        return intervals
    times = [frame.time_ms for frame in frames]
    start_index = max(0, bisect_right(times, window_start) - 1)
    for index in range(start_index, len(frames)):
        frame = frames[index]
        start_ms = max(frame.time_ms, window_start)
        end_ms = frames[index + 1].time_ms if index + 1 < len(frames) else window_end
        end_ms = min(end_ms, window_end)
        if start_ms < end_ms and frame.keys & (LEGACY_LEFT_ACTION_MASK | LEGACY_RIGHT_ACTION_MASK):
            intervals.append((start_ms, end_ms, frame.keys))
        if frame.time_ms > window_end:
            break
    return intervals


def timeline_x_for_time(time_ms: int, window_start: int, window_end: int, lane_left: int, lane_width: int) -> int:
    if window_end <= window_start:
        return lane_left
    ratio = (time_ms - window_start) / (window_end - window_start)
    ratio = max(0.0, min(1.0, ratio))
    return round(lane_left + ratio * lane_width)


def draw_key_row(canvas, x: int, y: int, label: str, keys: int, color: tuple[int, int, int], cv2) -> None:
    cv2.putText(canvas, label, (x, y + 21), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (205, 210, 220), 1, cv2.LINE_AA)
    for index, (name, mask) in enumerate((("K1", LEGACY_LEFT_ACTION_MASK), ("K2", LEGACY_RIGHT_ACTION_MASK))):
        left = x + 90 + index * 58
        pressed = bool(keys & mask)
        fill = color if pressed else (42, 46, 54)
        cv2.rectangle(canvas, (left, y), (left + 44, y + 28), fill, -1, cv2.LINE_AA)
        cv2.rectangle(canvas, (left, y), (left + 44, y + 28), (98, 106, 122), 1, cv2.LINE_AA)
        cv2.putText(canvas, name, (left + 10, y + 19), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (18, 20, 24) if pressed else (180, 188, 200), 1, cv2.LINE_AA)


def keys_at_time(frames: list[AbsoluteFrame], time_ms: int) -> int:
    times = [frame.time_ms for frame in frames]
    index = bisect_right(times, time_ms) - 1
    if index < 0:
        return frames[0].keys
    return frames[index].keys


def weighted_position(
    first: tuple[float, float],
    second: tuple[float, float],
    first_weight: float,
    second_weight: float,
) -> tuple[float, float]:
    total = first_weight + second_weight
    if total == 0:
        return ((first[0] + second[0]) / 2.0, (first[1] + second[1]) / 2.0)
    return (
        (first[0] * first_weight + second[0] * second_weight) / total,
        (first[1] * first_weight + second[1] * second_weight) / total,
    )
