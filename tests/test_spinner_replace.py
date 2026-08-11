from __future__ import annotations

import math

import pytest

import synthesis_osu_play.spinner_replace as spinner_module
from synthesis_osu_play.beatmap import HitObject
from synthesis_osu_play.spinner_replace import (
    replace_spinner_segments,
    search_spinner_trajectories,
    spinner_playback_duration_ms,
)
from synthesis_osu_play.synthesis import AbsoluteFrame, to_delta_frames


def make_library_spinner(
    duration_ms: int,
    *,
    rpm: float = 300.0,
    player: str = "source",
    clockwise: bool = True,
) -> dict:
    revolutions = rpm * duration_ms / 60000.0
    steps = max(16, math.ceil(revolutions * 16))
    direction = 1.0 if clockwise else -1.0
    trajectory = []
    for step in range(steps + 1):
        progress = step / steps
        angle = direction * 2 * math.pi * revolutions * progress
        trajectory.append({
            "t_ms": round(duration_ms * progress),
            "x": 256.0 + 64.0 * math.cos(angle),
            "y": 192.0 + 64.0 * math.sin(angle),
        })
    return {
        "player": player,
        "beatmap_title": "test",
        "spinner_duration_ms": duration_ms,
        "avg_rpm": rpm,
        "total_revolutions": revolutions,
        "trajectory": trajectory,
    }


def test_dt_spinner_search_uses_shortened_playback_duration() -> None:
    library = [
        make_library_spinner(3000, player="map-duration"),
        make_library_spinner(2000, player="dt-duration"),
    ]

    target_duration_ms = spinner_playback_duration_ms(3000, dt_mode=True)
    matches = search_spinner_trajectories(
        library,
        target_duration_ms,
        target_start_pos=(320.0, 192.0),
        target_end_pos=(320.0, 192.0),
        top_n=1,
        dt_mode=True,
    )

    assert target_duration_ms == 2000
    assert matches[0]["player"] == "dt-duration"


def test_spinner_search_only_returns_clockwise_trajectories() -> None:
    library = [
        make_library_spinner(2000, player="counterclockwise", clockwise=False),
        make_library_spinner(2000, player="clockwise"),
    ]

    matches = search_spinner_trajectories(
        library,
        2000,
        target_start_pos=(320.0, 192.0),
        target_end_pos=(320.0, 192.0),
    )

    assert [match["player"] for match in matches] == ["clockwise"]


def test_spinner_search_only_returns_300_to_500_rpm_trajectories() -> None:
    library = [
        make_library_spinner(2000, rpm=299.0, player="below"),
        make_library_spinner(2000, rpm=300.0, player="minimum"),
        make_library_spinner(2000, rpm=500.0, player="maximum"),
        make_library_spinner(2000, rpm=501.0, player="above"),
    ]

    matches = search_spinner_trajectories(
        library,
        2000,
        target_start_pos=(320.0, 192.0),
        target_end_pos=(320.0, 192.0),
        top_n=4,
    )

    assert {match["player"] for match in matches} == {"minimum", "maximum"}


def test_blend_trajectories_draws_first_weight_from_85_to_95() -> None:
    first = {
        "trajectory": [
            {"t_ms": 0, "x": 100.0, "y": 50.0},
            {"t_ms": 10, "x": 100.0, "y": 50.0},
        ],
    }
    second = {
        "trajectory": [
            {"t_ms": 0, "x": 300.0, "y": 150.0},
            {"t_ms": 10, "x": 300.0, "y": 150.0},
        ],
    }

    class FixedRandom:
        def uniform(self, lower: float, upper: float) -> float:
            assert (lower, upper) == (0.85, 0.95)
            return 0.9

    blended = spinner_module.blend_trajectories([first, second], 10, rng=FixedRandom())

    assert blended[0] == pytest.approx((120.0, 60.0))


def test_all_mode_replaces_with_dt_duration_at_minimum_library_rpm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = [make_library_spinner(2000, rpm=300.0, player="minimum-rpm")]
    monkeypatch.setattr(spinner_module, "_load_library", lambda _path: library)

    frames = [
        AbsoluteFrame(time_ms=0, x=0.0, y=0.0, keys=0),
        AbsoluteFrame(time_ms=3000, x=300.0, y=0.0, keys=0),
    ]
    spinner = HitObject(0, 0.0, 0.0, 0, 8, end_time_ms=3000)

    _modified, replacements = replace_spinner_segments(
        frames,
        [spinner],
        [(0, 3000)],
        dt_mode=True,
        spinner_mode="all",
    )

    assert len(replacements) == 1
    assert len(replacements[0].blended_trajectory) == 3000
    assert replacements[0].blend_weights == [1.0]
    assert replacements[0].library_ids[0]["duration_ms"] == 2000
    assert replacements[0].library_ids[0]["target_duration_ms"] == 2000


def test_spinner_blend_weights_are_reproducible_with_synthesis_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = [
        make_library_spinner(2000, player="first"),
        make_library_spinner(2000, player="second"),
    ]
    monkeypatch.setattr(spinner_module, "_load_library", lambda _path: library)
    frames = [
        AbsoluteFrame(time_ms=0, x=0.0, y=0.0, keys=0),
        AbsoluteFrame(time_ms=3000, x=300.0, y=0.0, keys=0),
    ]
    spinner = HitObject(0, 0.0, 0.0, 0, 8, end_time_ms=3000)

    def weights(seed: int) -> list[float]:
        _modified, replacements = replace_spinner_segments(
            frames,
            [spinner],
            [(0, 3000)],
            dt_mode=True,
            spinner_mode="all",
            synthesis_seed=seed,
        )
        return replacements[0].blend_weights

    first = weights(123)
    same = weights(123)
    different = weights(124)

    assert first == same
    assert first != different
    assert 0.85 <= first[0] <= 0.95
    assert first[0] + first[1] == pytest.approx(1.0)


def test_replaced_final_spinner_discards_trailing_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = [make_library_spinner(2000, rpm=300.0, player="final-spinner")]
    monkeypatch.setattr(spinner_module, "_load_library", lambda _path: library)
    frames = [
        AbsoluteFrame(time_ms=0, x=0.0, y=0.0, keys=0),
        AbsoluteFrame(time_ms=3000, x=400.0, y=300.0, keys=5),
        AbsoluteFrame(time_ms=3500, x=100.0, y=50.0, keys=10),
    ]
    spinner = HitObject(0, 256.0, 192.0, 0, 8, end_time_ms=3000)

    modified, replacements = replace_spinner_segments(
        frames,
        [spinner],
        [(0, 3000)],
        dt_mode=True,
        spinner_mode="all",
    )

    assert len(replacements) == 1
    assert modified[-1].time_ms == 3000
    assert modified[-1].keys == 0
    assert (modified[-1].x, modified[-1].y) == pytest.approx(
        replacements[0].blended_trajectory[-1]
    )
    assert all(frame.time_ms <= 3000 for frame in modified)


def test_replaced_initial_spinner_discards_leading_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = [make_library_spinner(2000, rpm=300.0, player="initial-spinner")]
    monkeypatch.setattr(spinner_module, "_load_library", lambda _path: library)
    frames = [
        AbsoluteFrame(time_ms=0, x=100.0, y=50.0, keys=0),
        AbsoluteFrame(time_ms=500, x=150.0, y=100.0, keys=0),
        AbsoluteFrame(time_ms=1000, x=320.0, y=192.0, keys=5),
        AbsoluteFrame(time_ms=3000, x=320.0, y=192.0, keys=0),
        AbsoluteFrame(time_ms=3500, x=256.0, y=192.0, keys=1),
    ]
    spinner = HitObject(0, 256.0, 192.0, 1000, 8, end_time_ms=3000)
    circle = HitObject(1, 256.0, 192.0, 3500, 1)

    modified, replacements = replace_spinner_segments(
        frames,
        [spinner, circle],
        [(1000, 3000)],
        spinner_mode="all",
    )

    assert len(replacements) == 1
    assert modified[0].time_ms == 1000
    assert modified[0].keys == 5
    assert (modified[0].x, modified[0].y) == pytest.approx(
        replacements[0].blended_trajectory[0]
    )
    assert all(frame.time_ms >= 1000 for frame in modified)
    assert modified[-1].time_ms == 3500
    assert to_delta_frames(modified)[0].delta_ms == 1000
