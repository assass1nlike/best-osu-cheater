"""Replace spinner segments with blended library trajectories."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass
from pathlib import Path
from random import Random

from .osr import OsrReplay, ReplayFrame
from .synthesis import (
    AbsoluteFrame,
    to_absolute_frames,
    to_delta_frames,
    OSU_STANDARD_PLAYFIELD_HEIGHT,
)

LOGGER = logging.getLogger(__name__)

_CENTER_X, _CENTER_Y = 256.0, 192.0
_MIN_RPM = 225
_MIN_LIBRARY_RPM = 300
_MAX_RPM = 500
_DT_SPEED = 1.5
_FIRST_BLEND_WEIGHT_MIN = 0.85
_FIRST_BLEND_WEIGHT_MAX = 0.95
_SLIDER_TAIL_LENIENCY_MS = 36
DEFAULT_SPINNER_LIBRARY_PATH = (
    Path(__file__).resolve().parent.parent
    / "artifacts"
    / "spinner-trajectories"
    / "spinner_trajectory_library.json"
)


# ---------------------------------------------------------------------------
# RPM calculation
# ---------------------------------------------------------------------------

def _calc_rpm_for_segment(trajectory: list[dict], start_ms: float, end_ms: float) -> tuple[float, float]:
    """Calculate RPM using net signed rotation.

    Signed angles naturally cancel direction reversals.  No harsh
    per-reversal reset that amplifies synthesis noise.
    """
    net_angle = 0.0
    prev = None
    for pt in trajectory:
        t = pt["t_ms"]
        if t < start_ms - 0.5:
            continue
        if t > end_ms + 0.5:
            break
        if prev is not None:
            x1, y1 = prev["x"] - _CENTER_X, prev["y"] - _CENTER_Y
            x2, y2 = pt["x"] - _CENTER_X, pt["y"] - _CENTER_Y
            r1, r2 = math.hypot(x1, y1), math.hypot(x2, y2)
            if r1 >= 1 and r2 >= 1:
                net_angle += math.atan2(x1 * y2 - y1 * x2, x1 * x2 + y1 * y2)
        prev = pt

    dur = (end_ms - start_ms) / 1000.0
    if dur <= 0:
        return 0.0, 0.0
    revs = abs(net_angle) / (2 * math.pi)
    return revs / (dur / 60.0), revs


def spinner_needs_replacement(trajectory: list[dict], threshold_rpm: float = _MIN_RPM, dt_mode: bool = False) -> bool:
    """Return whether any complete one-second window is below the RPM threshold."""
    if len(trajectory) < 2:
        return True
    duration_ms = trajectory[-1]["t_ms"] - trajectory[0]["t_ms"]
    if duration_ms <= 1000:
        return False

    # Check each full second (including the first)
    window_rpms = []
    fail_windows = []
    for window_start in range(0, int(duration_ms), 1000):
        window_end = min(window_start + 1000, duration_ms)
        if window_end - window_start < 1000:
            break
        rpm, _ = _calc_rpm_for_segment(trajectory, window_start, window_end)
        display_rpm = rpm * (_DT_SPEED if dt_mode else 1.0)
        display_thresh = threshold_rpm
        window_rpms.append(display_rpm)
        if display_rpm < display_thresh:
            fail_windows.append((window_start, window_end, display_rpm))
    if window_rpms:
        status = 'FAIL' if fail_windows else 'pass'
        LOGGER.info(
            "RPM%s/sec: %s (thresh=%s) -> %s",
            "x1.5 " if dt_mode else "",
            [f"{r:.0f}" for r in window_rpms],
            display_thresh,
            status,
        )
        for ws, we, r in fail_windows:
            LOGGER.info(
                "window [%s-%s]ms: %.0f RPM%s < %s",
                ws,
                we,
                r,
                "x1.5" if dt_mode else "",
                display_thresh,
            )
    return bool(fail_windows)


def calc_spinner_rpm(trajectory: list[dict]) -> tuple[float, float]:
    """Return (avg_rpm, total_revolutions) from a trajectory."""
    if len(trajectory) < 2:
        return 0.0, 0.0
    total_angle = 0.0
    for i in range(1, len(trajectory)):
        x1 = trajectory[i - 1]["x"] - _CENTER_X
        y1 = trajectory[i - 1]["y"] - _CENTER_Y
        x2 = trajectory[i]["x"] - _CENTER_X
        y2 = trajectory[i]["y"] - _CENTER_Y
        r1 = math.hypot(x1, y1)
        r2 = math.hypot(x2, y2)
        if r1 < 1 or r2 < 1:
            continue
        total_angle += abs(math.atan2(x1 * y2 - y1 * x2, x1 * x2 + y1 * y2))
    duration_s = (trajectory[-1]["t_ms"] - trajectory[0]["t_ms"]) / 1000.0
    if duration_s <= 0:
        return 0.0, 0.0
    revs = total_angle / (2 * math.pi)
    return revs / (duration_s / 60.0), revs


def _is_clockwise_trajectory(trajectory: list[dict]) -> bool:
    """Return whether net rotation is clockwise in screen coordinates."""
    net_angle = 0.0
    for previous, current in zip(trajectory, trajectory[1:]):
        x1 = previous["x"] - _CENTER_X
        y1 = previous["y"] - _CENTER_Y
        x2 = current["x"] - _CENTER_X
        y2 = current["y"] - _CENTER_Y
        if math.hypot(x1, y1) < 1 or math.hypot(x2, y2) < 1:
            continue
        net_angle += math.atan2(x1 * y2 - y1 * x2, x1 * x2 + y1 * y2)
    return net_angle > 0.0


def spinner_rpm_from_absolute_frames(frames: list[AbsoluteFrame], start_ms: int, end_ms: int) -> tuple[float, float]:
    """Calculate RPM of a spinner segment from absolute frames."""
    trajectory = [
        {"t_ms": f.time_ms, "x": f.x, "y": f.y}
        for f in frames
        if start_ms <= f.time_ms <= end_ms
    ]
    return calc_spinner_rpm(trajectory)


# ---------------------------------------------------------------------------
# Library loading and search
# ---------------------------------------------------------------------------

def _load_library(path: str | Path | None = None) -> list[dict]:
    if path is None:
        path = DEFAULT_SPINNER_LIBRARY_PATH
    with open(path) as f:
        return json.load(f)["trajectories"]


def spinner_playback_duration_ms(map_duration_ms: int, dt_mode: bool = False) -> int:
    """Return the spinner's real gameplay duration for the selected speed mod."""
    speed = _DT_SPEED if dt_mode else 1.0
    return max(1, round(map_duration_ms / speed))


def _trajectory_duration_ms(trajectory: list[dict]) -> int:
    if not trajectory:
        return 0
    return max(0, round(trajectory[-1]["t_ms"] - trajectory[0]["t_ms"]))


def _point_at_offset(trajectory: list[dict], offset_ms: float) -> tuple[float, float]:
    """Sample a trajectory at its native timestamp offset without time-scaling it."""
    src_start = trajectory[0]["t_ms"]
    src_t = src_start + max(0.0, offset_ms)
    if src_t <= trajectory[0]["t_ms"]:
        return trajectory[0]["x"], trajectory[0]["y"]
    if src_t >= trajectory[-1]["t_ms"]:
        return trajectory[-1]["x"], trajectory[-1]["y"]

    lo, hi = 0, len(trajectory) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if trajectory[mid]["t_ms"] == src_t:
            return trajectory[mid]["x"], trajectory[mid]["y"]
        if trajectory[mid]["t_ms"] < src_t:
            lo = mid + 1
        else:
            hi = mid - 1

    a, b = trajectory[hi], trajectory[lo]
    span = b["t_ms"] - a["t_ms"]
    ratio = (src_t - a["t_ms"]) / span if span > 0 else 0.0
    x = a["x"] + (b["x"] - a["x"]) * ratio
    y = a["y"] + (b["y"] - a["y"]) * ratio
    return x, y


def _candidate_rpm(lib_traj: dict) -> float:
    rpm = lib_traj.get("avg_rpm")
    if rpm is not None:
        return float(rpm)
    duration_ms = max(
        1,
        int(lib_traj.get("spinner_duration_ms") or _trajectory_duration_ms(lib_traj["trajectory"])),
    )
    return float(lib_traj["total_revolutions"]) * 60000.0 / duration_ms


def _resample_trajectory(
    trajectory: list[dict],
    output_duration_ms: int,
    playback_duration_ms: int | None = None,
) -> list[tuple[float, float]]:
    """Sample a library trajectory onto replay map-time without changing its gameplay speed."""
    if playback_duration_ms is None:
        playback_duration_ms = output_duration_ms
    src_start = trajectory[0]["t_ms"]
    src_end = trajectory[-1]["t_ms"]
    src_dur = src_end - src_start
    if src_dur <= 0:
        return [(trajectory[0]["x"], trajectory[0]["y"])] * max(1, output_duration_ms)

    points = []
    src_idx = 0
    map_to_playback = playback_duration_ms / max(1, output_duration_ms)
    for t_offset in range(output_duration_ms):
        src_t = src_start + t_offset * map_to_playback
        # Advance src_idx to bracket src_t
        while src_idx + 1 < len(trajectory) and trajectory[src_idx + 1]["t_ms"] <= src_t:
            src_idx += 1
        if src_idx + 1 >= len(trajectory):
            points.append((trajectory[-1]["x"], trajectory[-1]["y"]))
        else:
            a, b = trajectory[src_idx], trajectory[src_idx + 1]
            span = b["t_ms"] - a["t_ms"]
            ratio = (src_t - a["t_ms"]) / span if span > 0 else 0
            x = a["x"] + (b["x"] - a["x"]) * ratio
            y = a["y"] + (b["y"] - a["y"]) * ratio
            points.append((x, y))
    return points


def _score_candidate(
    lib_traj: dict,
    target_duration_ms: int,
    target_start_pos: tuple[float, float],
    target_end_pos: tuple[float, float],
    dt_mode: bool = False,
    *,
    strict_rpm: bool = True,
) -> float:
    """Score a library trajectory for suitability. Lower = better."""
    # Library trajectories are captured at normal playback speed. For DT,
    # target_duration_ms is already the shortened gameplay duration.
    _ = dt_mode
    rpm = _candidate_rpm(lib_traj)
    if strict_rpm and (rpm < _MIN_RPM or rpm > _MAX_RPM):
        return float("inf")

    # Cursor position match at start and end
    trajectory = lib_traj["trajectory"]
    lib_start_x, lib_start_y = _point_at_offset(trajectory, 0)
    lib_end_x, lib_end_y = _point_at_offset(trajectory, target_duration_ms)

    dist_start = math.hypot(lib_start_x - target_start_pos[0], lib_start_y - target_start_pos[1])
    dist_end = math.hypot(lib_end_x - target_end_pos[0], lib_end_y - target_end_pos[1])
    duration_ms = int(lib_traj.get("spinner_duration_ms") or _trajectory_duration_ms(trajectory))
    duration_penalty = abs(duration_ms - target_duration_ms)
    rpm_penalty = 0.0
    if rpm < _MIN_RPM:
        rpm_penalty = (_MIN_RPM - rpm) * 2.0
    elif rpm > _MAX_RPM:
        rpm_penalty = (rpm - _MAX_RPM) * 2.0
    return duration_penalty + (dist_start + dist_end) * 0.25 + rpm_penalty


def search_spinner_trajectories(
    library: list[dict],
    target_duration_ms: int,
    target_start_pos: tuple[float, float],
    target_end_pos: tuple[float, float],
    top_n: int = 2,
    dt_mode: bool = False,
    strict_rpm: bool = True,
) -> list[dict]:
    """Search library for best matching spinner trajectories."""
    scored = []
    for t in library:
        if not _is_clockwise_trajectory(t["trajectory"]):
            continue
        rpm = _candidate_rpm(t)
        if rpm < _MIN_LIBRARY_RPM or rpm > _MAX_RPM:
            continue
        score = _score_candidate(
            t,
            target_duration_ms,
            target_start_pos,
            target_end_pos,
            dt_mode=dt_mode,
            strict_rpm=strict_rpm,
        )
        if math.isfinite(score):
            scored.append((score, t))
    scored.sort(key=lambda x: x[0])
    return [t for _, t in scored[:top_n]]


# ---------------------------------------------------------------------------
# Trajectory blending
# ---------------------------------------------------------------------------

def blend_trajectories(
    candidates: list[dict],
    output_duration_ms: int,
    weights: list[float] | None = None,
    playback_duration_ms: int | None = None,
    rng: Random | None = None,
) -> list[tuple[float, float]]:
    """Blend multiple library trajectories into one, returning [(x, y), ...] at 1ms steps."""
    if weights is None:
        weights = _random_blend_weights(len(candidates), rng or Random())
    # Normalize weights
    total_w = sum(weights[:len(candidates)])
    weights = [w / total_w for w in weights[:len(candidates)]]

    if playback_duration_ms is None:
        playback_duration_ms = output_duration_ms
    resampled = [
        _resample_trajectory(t["trajectory"], output_duration_ms, playback_duration_ms)
        for t in candidates
    ]

    blended = []
    for step in range(output_duration_ms):
        x = sum(weights[i] * resampled[i][step][0] for i in range(len(candidates)))
        y = sum(weights[i] * resampled[i][step][1] for i in range(len(candidates)))
        blended.append((x, y))

    return blended


def _random_blend_weights(candidate_count: int, rng: Random) -> list[float]:
    if candidate_count <= 0:
        return []
    if candidate_count == 1:
        return [1.0]
    first_weight = rng.uniform(_FIRST_BLEND_WEIGHT_MIN, _FIRST_BLEND_WEIGHT_MAX)
    return [first_weight, 1.0 - first_weight]


# ---------------------------------------------------------------------------
# Slider tail / last-object time
# ---------------------------------------------------------------------------

def last_hit_time_before_spinner(
    objects: list,
    spinner_start_ms: int,
) -> int | None:
    """Return the time of the last meaningful hit object before a spinner.

    For sliders, uses slider_end - 36ms (LegacyLastTick).  For circles,
    uses the object's time.  Returns None if no object before the spinner.
    """
    candidates = [obj for obj in objects if obj.time_ms < spinner_start_ms]
    if not candidates:
        return None
    prev = max(candidates, key=lambda obj: obj.time_ms)
    # Check if it's a slider via resolved_end_time_ms
    if hasattr(prev, "resolved_end_time_ms") and prev.resolved_end_time_ms > prev.time_ms:
        # Slider: use LegacyLastTick = end - 36, capped at end - duration/2 for very short sliders
        duration = prev.resolved_end_time_ms - prev.time_ms
        tail_time = prev.resolved_end_time_ms - _SLIDER_TAIL_LENIENCY_MS
        # Cap: >= start_time + duration / 2
        floor = prev.time_ms + duration / 2
        return int(max(floor, tail_time))
    return int(prev.time_ms)


def first_hit_time_after_spinner(
    objects: list,
    spinner_end_ms: int,
) -> int | None:
    """Return the hit time of the first meaningful object after a spinner."""
    candidates = [obj for obj in objects if obj.time_ms > spinner_end_ms]
    if not candidates:
        return None
    nxt = min(candidates, key=lambda obj: obj.time_ms)
    return int(nxt.time_ms)


# ---------------------------------------------------------------------------
# Main replacement logic
# ---------------------------------------------------------------------------

@dataclass
class SpinnerReplacement:
    spinner_index: int
    start_ms: int
    end_ms: int
    t1_before_ms: int      # transition start (last hit before spinner)
    t2_start_ms: int       # spinner start (transition end / T begins)
    t3_end_ms: int         # spinner end (T ends / transition back begins)
    t4_after_ms: int       # transition end (first hit after spinner)
    blended_trajectory: list[tuple[float, float]]  # 1ms-step (x, y) for spinner duration
    rpm_original: float
    rpm_replacement: float
    candidates_used: int
    blend_weights: list[float]
    library_ids: list[dict]


def replace_spinner_segments(
    absolute_frames: list[AbsoluteFrame],
    hit_objects: list,
    spinners: list[tuple[int, int]],  # [(start_ms, end_ms), ...]
    library_path: str | Path | None = None,
    rpm_threshold: float = _MIN_RPM,
    dt_mode: bool = False,
    spinner_mode: str = "all",
    synthesis_seed: int | None = None,
) -> tuple[list[AbsoluteFrame], list[SpinnerReplacement]]:
    """Replace spinner segments with library blends where RPM is below threshold.

    Returns (modified_absolute_frames, list_of_replacements).
    """
    if spinner_mode not in {"all", "threshold", "never"}:
        raise ValueError(f"unknown spinner replacement mode: {spinner_mode}")
    if spinner_mode == "never":
        return absolute_frames, []

    library = _load_library(library_path)
    blend_rng = Random(synthesis_seed)
    replacements: list[SpinnerReplacement] = []

    for spin_idx, (sp_start_ms, sp_end_ms) in enumerate(spinners):
        # Build trajectory from absolute frames
        spinner_traj = [
            {"t_ms": f.time_ms - sp_start_ms, "x": f.x, "y": f.y}
            for f in absolute_frames
            if sp_start_ms <= f.time_ms <= sp_end_ms
        ]
        duration_ms = sp_end_ms - sp_start_ms
        playback_duration_ms = spinner_playback_duration_ms(duration_ms, dt_mode=dt_mode)
        if spinner_mode == 'all':
            LOGGER.info(
                "spinner %s: dur=%sms, playback_dur=%sms, traj_pts=%s -> REPLACE (all mode)",
                spin_idx + 1,
                duration_ms,
                playback_duration_ms,
                len(spinner_traj),
            )
        else:
            needs = spinner_needs_replacement(spinner_traj, rpm_threshold, dt_mode=dt_mode)
            if not needs:
                LOGGER.info(
                    "spinner %s: dur=%sms, traj_pts=%s -> pass",
                    spin_idx + 1,
                    duration_ms,
                    len(spinner_traj),
                )
                continue
            LOGGER.info(
                "spinner %s: dur=%sms, playback_dur=%sms, traj_pts=%s -> REPLACE",
                spin_idx + 1,
                duration_ms,
                playback_duration_ms,
                len(spinner_traj),
            )
        rpm, _ = spinner_rpm_from_absolute_frames(absolute_frames, sp_start_ms, sp_end_ms)
        rpm *= _DT_SPEED if dt_mode else 1.0
        if duration_ms <= 0:
            continue

        # Find transition boundaries
        t1 = last_hit_time_before_spinner(hit_objects, sp_start_ms)
        t4 = first_hit_time_after_spinner(hit_objects, sp_end_ms)
        if t1 is None:
            t1 = sp_start_ms  # first object: no transition in
        if t4 is None:
            t4 = sp_end_ms   # last object: no transition out

        t2 = sp_start_ms
        t3 = sp_end_ms

        try:
            start_pos = _interpolate_pos(absolute_frames, sp_start_ms)
            end_pos = _interpolate_pos(absolute_frames, sp_end_ms)
        except Exception as e:
            LOGGER.warning("spinner position interpolation failed: %s", e)
            continue

        # Search library
        candidates = search_spinner_trajectories(
            library,
            playback_duration_ms,
            start_pos,
            end_pos,
            dt_mode=dt_mode,
            strict_rpm=(spinner_mode != "all"),
        )
        LOGGER.info(
            "spinner search: %s candidates for playback_dur=%sms (pos=(%.0f,%.0f)->(%.0f,%.0f))",
            len(candidates),
            playback_duration_ms,
            start_pos[0],
            start_pos[1],
            end_pos[0],
            end_pos[1],
        )
        if not candidates:
            LOGGER.warning("no library match for %sms spinner", duration_ms)
            continue

        # Blend
        try:
            weights = _random_blend_weights(len(candidates), blend_rng)
            LOGGER.info("spinner blend weights: %s", [round(weight, 4) for weight in weights])
            blended = blend_trajectories(
                candidates,
                duration_ms,
                weights,
                playback_duration_ms=playback_duration_ms,
            )
            eff_rpm, _ = calc_spinner_rpm([
                {"t_ms": i, "x": pt[0], "y": pt[1]}
                for i, pt in enumerate(blended)
            ])
            eff_rpm *= _DT_SPEED if dt_mode else 1.0
        except Exception as e:
            LOGGER.warning("spinner blend failed: %s", e)
            continue

        replacements.append(SpinnerReplacement(
            spinner_index=spin_idx,
            start_ms=sp_start_ms,
            end_ms=sp_end_ms,
            t1_before_ms=t1,
            t2_start_ms=t2,
            t3_end_ms=t3,
            t4_after_ms=t4,
            blended_trajectory=blended,
            rpm_original=rpm,
            rpm_replacement=eff_rpm,
            candidates_used=len(candidates),
            blend_weights=weights,
            library_ids=[{"player": c["player"], "beatmap": c.get("beatmap_title",""),
                          "duration_ms": c["spinner_duration_ms"], "target_duration_ms": playback_duration_ms,
                          "rpm": c.get("avg_rpm",0)}
                         for c in candidates],
        ))

    if not replacements:
        return absolute_frames, []

    # Build modified frames
    modified = _apply_replacements(absolute_frames, replacements)
    modified = _start_at_replaced_initial_spinner(
        modified,
        hit_objects,
        replacements,
    )
    modified = _stop_after_replaced_final_spinner(
        modified,
        hit_objects,
        replacements,
    )
    return modified, replacements


def _start_at_replaced_initial_spinner(
    frames: list[AbsoluteFrame],
    hit_objects: list,
    replacements: list[SpinnerReplacement],
) -> list[AbsoluteFrame]:
    """Discard input before a replaced spinner when it is the first object."""
    if not frames or not hit_objects:
        return frames

    initial_object = hit_objects[0]
    if not initial_object.is_spinner:
        return frames

    initial_start_ms = initial_object.time_ms
    initial_end_ms = initial_object.resolved_end_time_ms
    replacement = next(
        (
            item
            for item in replacements
            if item.start_ms == initial_start_ms and item.end_ms == initial_end_ms
        ),
        None,
    )
    if replacement is None or not replacement.blended_trajectory:
        return frames

    initial_x, initial_y = replacement.blended_trajectory[0]
    start_frame = next(
        (frame for frame in frames if frame.time_ms == initial_start_ms),
        None,
    )
    initial_keys = start_frame.keys if start_frame is not None else 0
    discarded_count = sum(frame.time_ms < initial_start_ms for frame in frames)
    trimmed = [
        AbsoluteFrame(
            time_ms=initial_start_ms,
            x=initial_x,
            y=initial_y,
            keys=initial_keys,
        ),
        *(frame for frame in frames if frame.time_ms > initial_start_ms),
    ]
    LOGGER.info(
        "initial spinner starts at %sms; discarded %s leading replay frame(s)",
        initial_start_ms,
        discarded_count,
    )
    return trimmed


def _stop_after_replaced_final_spinner(
    frames: list[AbsoluteFrame],
    hit_objects: list,
    replacements: list[SpinnerReplacement],
) -> list[AbsoluteFrame]:
    """End input at a replaced spinner when it is the map's final object."""
    if not frames or not hit_objects:
        return frames

    final_object = hit_objects[-1]
    if not final_object.is_spinner:
        return frames

    final_end_ms = final_object.resolved_end_time_ms
    replacement = next(
        (
            item
            for item in reversed(replacements)
            if item.start_ms == final_object.time_ms and item.end_ms == final_end_ms
        ),
        None,
    )
    if replacement is None or not replacement.blended_trajectory:
        return frames

    final_x, final_y = replacement.blended_trajectory[-1]
    trimmed = [frame for frame in frames if frame.time_ms < final_end_ms]
    trimmed.append(
        AbsoluteFrame(
            time_ms=final_end_ms,
            x=final_x,
            y=final_y,
            keys=0,
        )
    )
    LOGGER.info(
        "final spinner ends at %sms; discarded %s trailing replay frame(s)",
        final_end_ms,
        len(frames) - len(trimmed),
    )
    return trimmed


def _interpolate_pos(frames: list[AbsoluteFrame], time_ms: int) -> tuple[float, float]:
    """Interpolate cursor position at a given time."""
    if time_ms <= frames[0].time_ms:
        return frames[0].x, frames[0].y
    if time_ms >= frames[-1].time_ms:
        return frames[-1].x, frames[-1].y

    lo, hi = 0, len(frames) - 1
    while lo <= hi:
        mid = (lo + hi) // 2
        if frames[mid].time_ms == time_ms:
            return frames[mid].x, frames[mid].y
        if frames[mid].time_ms < time_ms:
            lo = mid + 1
        else:
            hi = mid - 1

    before, after = frames[hi], frames[lo]
    span = after.time_ms - before.time_ms
    ratio = (time_ms - before.time_ms) / span if span > 0 else 0
    return before.x + (after.x - before.x) * ratio, before.y + (after.y - before.y) * ratio


def _apply_replacements(
    frames: list[AbsoluteFrame],
    replacements: list[SpinnerReplacement],
) -> list[AbsoluteFrame]:
    """Apply spinner replacements with linear transition zones."""
    # Build time-indexed output
    output_times = sorted({f.time_ms for f in frames})
    # Also include all 1ms steps from replacements
    for r in replacements:
        for i in range(r.t4_after_ms - r.t1_before_ms + 1):
            output_times.append(r.t1_before_ms + i)
        # Also include precise spinner boundaries
        output_times.extend([r.t1_before_ms, r.t2_start_ms, r.t3_end_ms, r.t4_after_ms])
    output_times = sorted(set(output_times))

    # Build key lookup (for times not in original, use nearest frame's keys)
    sorted_orig = sorted(frames, key=lambda f: f.time_ms)
    def key_at(t_ms: int) -> int:
        lo, hi = 0, len(sorted_orig) - 1
        while lo <= hi:
            mid = (lo + hi) // 2
            if sorted_orig[mid].time_ms == t_ms:
                return sorted_orig[mid].keys
            if sorted_orig[mid].time_ms < t_ms:
                lo = mid + 1
            else:
                hi = mid - 1
        return sorted_orig[hi].keys if hi >= 0 else 0

    output: list[AbsoluteFrame] = []
    for t in output_times:
        x, y = _interpolate_pos(frames, t)

        # Check if inside replacement zone
        for r in replacements:
            if r.t1_before_ms < t < r.t4_after_ms:
                if t <= r.t2_start_ms:
                    denom = r.t2_start_ms - r.t1_before_ms
                    alpha = (t - r.t1_before_ms) / denom if denom > 0 else 1.0
                    tx, ty = r.blended_trajectory[0]
                elif t >= r.t3_end_ms:
                    denom = r.t4_after_ms - r.t3_end_ms
                    alpha = (r.t4_after_ms - t) / denom if denom > 0 else 1.0
                    tx, ty = r.blended_trajectory[-1]
                else:
                    offset_ms = int(t - r.t2_start_ms)
                    idx = min(offset_ms, len(r.blended_trajectory) - 1)
                    tx, ty = r.blended_trajectory[max(0, idx)]
                    alpha = 1.0

                alpha = max(0.0, min(1.0, alpha))
                x = x * (1 - alpha) + tx * alpha
                y = y * (1 - alpha) + ty * alpha
                break

        output.append(AbsoluteFrame(time_ms=t, x=x, y=y, keys=key_at(t)))

    return output
