"""Replace low-RPM spinner segments with library trajectories.

After synthesis, each spinner below 225 RPM is replaced by a weighted blend
of top-3 matching trajectories from the spinner trajectory library, with smooth
linear transitions at the spinner boundaries.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .osr import OsrReplay, ReplayFrame
from .synthesis import (
    AbsoluteFrame,
    to_absolute_frames,
    to_delta_frames,
    OSU_STANDARD_PLAYFIELD_HEIGHT,
)

_CENTER_X, _CENTER_Y = 256.0, 192.0
_MIN_RPM = 225
_MAX_RPM = 450
_SLIDER_TAIL_LENIENCY_MS = 36


# ---------------------------------------------------------------------------
# RPM calculation
# ---------------------------------------------------------------------------

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
        path = Path(__file__).resolve().parent.parent / "artifacts" / "spinner-trajectories" / "spinner_trajectory_library.json"
    with open(path) as f:
        return json.load(f)["trajectories"]


def _resample_trajectory(trajectory: list[dict], target_duration_ms: int) -> list[tuple[float, float]]:
    """Time-scale a library trajectory to a target duration, returning [(x, y), ...] at 1ms intervals."""
    src_start = trajectory[0]["t_ms"]
    src_end = trajectory[-1]["t_ms"]
    src_dur = src_end - src_start
    if src_dur <= 0:
        return [(trajectory[0]["x"], trajectory[0]["y"])] * max(1, target_duration_ms)

    scale = target_duration_ms / src_dur
    points = []
    src_idx = 0
    for t_offset in range(target_duration_ms):
        src_t = src_start + t_offset / scale
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
) -> float:
    """Score a library trajectory for suitability. Lower = better."""
    # Check RPM after scaling: revolutions stay the same, duration changes
    revs = lib_traj["total_revolutions"]
    scaled_rpm = revs * 60000 / target_duration_ms
    if scaled_rpm < _MIN_RPM or scaled_rpm > _MAX_RPM:
        return float("inf")

    # Cursor position match at start and end
    lib_start_x = lib_traj["trajectory"][0]["x"]
    lib_start_y = lib_traj["trajectory"][0]["y"]
    lib_end_x = lib_traj["trajectory"][-1]["x"]
    lib_end_y = lib_traj["trajectory"][-1]["y"]

    dist_start = math.hypot(lib_start_x - target_start_pos[0], lib_start_y - target_start_pos[1])
    dist_end = math.hypot(lib_end_x - target_end_pos[0], lib_end_y - target_end_pos[1])
    return dist_start + dist_end


def search_spinner_trajectories(
    library: list[dict],
    target_duration_ms: int,
    target_start_pos: tuple[float, float],
    target_end_pos: tuple[float, float],
    top_n: int = 3,
) -> list[dict]:
    """Search library for best matching spinner trajectories."""
    scored = []
    for t in library:
        score = _score_candidate(t, target_duration_ms, target_start_pos, target_end_pos)
        if math.isfinite(score):
            scored.append((score, t))
    scored.sort(key=lambda x: x[0])
    return [t for _, t in scored[:top_n]]


# ---------------------------------------------------------------------------
# Trajectory blending
# ---------------------------------------------------------------------------

def blend_trajectories(
    candidates: list[dict],
    target_duration_ms: int,
    weights: list[float] | None = None,
) -> list[tuple[float, float]]:
    """Blend multiple library trajectories into one, returning [(x, y), ...] at 1ms steps."""
    if weights is None:
        weights = [0.6, 0.3, 0.1][:len(candidates)]
    # Normalize weights
    total_w = sum(weights[:len(candidates)])
    weights = [w / total_w for w in weights[:len(candidates)]]

    resampled = [_resample_trajectory(t["trajectory"], target_duration_ms) for t in candidates]

    blended = []
    for step in range(target_duration_ms):
        x = sum(weights[i] * resampled[i][step][0] for i in range(len(candidates)))
        y = sum(weights[i] * resampled[i][step][1] for i in range(len(candidates)))
        blended.append((x, y))

    return blended


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
    library_ids: list[dict]


def replace_spinner_segments(
    absolute_frames: list[AbsoluteFrame],
    hit_objects: list,
    spinners: list[tuple[int, int]],  # [(start_ms, end_ms), ...]
    library_path: str | Path | None = None,
    rpm_threshold: float = _MIN_RPM,
) -> tuple[list[AbsoluteFrame], list[SpinnerReplacement]]:
    """Replace spinner segments with library blends where RPM is below threshold.

    Returns (modified_absolute_frames, list_of_replacements).
    """
    library = _load_library(library_path)
    replacements: list[SpinnerReplacement] = []

    for spin_idx, (sp_start_ms, sp_end_ms) in enumerate(spinners):
        # Check RPM
        rpm, revs = spinner_rpm_from_absolute_frames(absolute_frames, sp_start_ms, sp_end_ms)
        if rpm >= rpm_threshold:
            continue

        duration_ms = sp_end_ms - sp_start_ms
        if duration_ms <= 0:
            continue

        # Find transition boundaries
        t1 = last_hit_time_before_spinner(hit_objects, sp_start_ms)
        t4 = first_hit_time_after_spinner(hit_objects, sp_end_ms)
        if t1 is None or t4 is None:
            continue

        t2 = sp_start_ms
        t3 = sp_end_ms

        # Get cursor positions at spinner boundaries from synthesized replay
        start_pos = _interpolate_pos(absolute_frames, sp_start_ms)
        end_pos = _interpolate_pos(absolute_frames, sp_end_ms)

        # Search library
        candidates = search_spinner_trajectories(library, duration_ms, start_pos, end_pos)
        if not candidates:
            continue

        # Blend
        weights = [0.6, 0.3, 0.1][:len(candidates)]
        blended = blend_trajectories(candidates, duration_ms, weights)

        # Calculate effective RPM of blended trajectory
        eff_rpm, _ = calc_spinner_rpm([
            {"t_ms": i, "x": pt[0], "y": pt[1]}
            for i, pt in enumerate(blended)
        ])

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
            library_ids=[{"player": c["player"], "beatmap": c.get("beatmap_title",""),
                          "duration_ms": c["spinner_duration_ms"], "rpm": c.get("avg_rpm",0)}
                         for c in candidates],
        ))

    if not replacements:
        return absolute_frames, []

    # Build modified frames
    modified = _apply_replacements(absolute_frames, replacements)
    return modified, replacements


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

    output: list[AbsoluteFrame] = []
    for t in output_times:
        x, y = _interpolate_pos(frames, t)

        # Check if we're inside any replacement zone
        for r in replacements:
            if r.t1_before_ms < t < r.t4_after_ms:
                # Get blended T position at the spinner-relative time
                if t <= r.t2_start_ms:
                    # Transition in: t1 -> t2
                    if r.t2_start_ms > r.t1_before_ms:
                        alpha = (t - r.t1_before_ms) / (r.t2_start_ms - r.t1_before_ms)
                    else:
                        alpha = 1.0
                    # T position at spinner start
                    tx, ty = r.blended_trajectory[0]
                elif t >= r.t3_end_ms:
                    # Transition out: t3 -> t4
                    if r.t4_after_ms > r.t3_end_ms:
                        alpha = (r.t4_after_ms - t) / (r.t4_after_ms - r.t3_end_ms)
                    else:
                        alpha = 1.0
                    tx, ty = r.blended_trajectory[-1]
                else:
                    # In spinner: full T
                    offset_ms = int(t - r.t2_start_ms)
                    if 0 <= offset_ms < len(r.blended_trajectory):
                        tx, ty = r.blended_trajectory[offset_ms]
                    else:
                        tx, ty = r.blended_trajectory[-1]
                    alpha = 1.0

                alpha = max(0.0, min(1.0, alpha))
                x = x * (1 - alpha) + tx * alpha
                y = y * (1 - alpha) + ty * alpha
                break

        output.append(AbsoluteFrame(time_ms=t, x=x, y=y, keys=0))

    # Copy keys from original frames (we don't change key timing)
    _copy_keys_from_original(frames, output)
    return output


def _copy_keys_from_original(original: list[AbsoluteFrame], output: list[AbsoluteFrame]) -> None:
    """Copy key states from original frames to output frames at matching times."""
    time_to_keys = {f.time_ms: f.keys for f in original}
    for f in output:
        f.keys = time_to_keys.get(f.time_ms, 0)
