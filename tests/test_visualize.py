from pathlib import Path

import pytest

from synthesis_osu_play.beatmap import Beatmap, HitObject
from synthesis_osu_play.mods import MOD_HARD_ROCK
from synthesis_osu_play.osr import OsrReplay, ReplayFrame
from synthesis_osu_play.synthesis import LEGACY_X_KEY, LEGACY_Z_KEY, SynthesisError, to_absolute_frames
from synthesis_osu_play.visualize import key_hold_intervals_in_window, make_layout, slider_draw_points, slider_timeline_markers, timeline_x_for_time, prepare_debug_tracks, write_replay_debug_video, world_bounds_for_objects


def make_replay(
    frames: tuple[ReplayFrame, ...],
    *,
    mods: int = 0,
) -> OsrReplay:
    return OsrReplay(
        mode=0,
        game_version=20240101,
        beatmap_md5="same-map",
        player_name="player",
        replay_md5="hash",
        count_300=0,
        count_100=0,
        count_50=0,
        count_geki=0,
        count_katu=0,
        count_miss=0,
        score=0,
        max_combo=0,
        perfect=False,
        mods=mods,
        life_bar_graph="",
        timestamp=1,
        frames=frames,
        online_score_id=0,
    )


def test_prepare_debug_tracks_normalizes_sources_like_synthesis() -> None:
    first = make_replay((ReplayFrame(0, 100.0, 100.0, 0),), mods=MOD_HARD_ROCK)
    second = make_replay((ReplayFrame(0, 100.0, 284.0, 0),))

    tracks = prepare_debug_tracks(first, second)

    assert tracks.output_mods == 0
    assert tracks.first[0].y == pytest.approx(284.0)
    assert tracks.second[0].y == pytest.approx(284.0)
    assert tracks.synthesized[0].y == pytest.approx(284.0)


def test_write_replay_debug_video_creates_file(tmp_path: Path) -> None:
    beatmap = Beatmap(
        md5="same-map",
        audio_lead_in_ms=0,
        circle_size=5.0,
        overall_difficulty=5.0,
        hit_objects=(HitObject(0, 256.0, 192.0, 100, 1),),
    )
    first = make_replay(
        (
            ReplayFrame(0, 256.0, 192.0, 0),
            ReplayFrame(100, 256.0, 192.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 192.0, 0),
        )
    )
    second = make_replay(
        (
            ReplayFrame(0, 256.0, 192.0, 0),
            ReplayFrame(100, 256.0, 192.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 192.0, 0),
        )
    )
    output = tmp_path / "debug.mp4"

    try:
        write_replay_debug_video(
            first,
            second,
            output,
            beatmap=beatmap,
            start_ms=0,
            end_ms=200,
            fps=10,
            width=640,
            height=360,
        )
    except SynthesisError as exc:
        if "opencv-python" in str(exc) or "video writer" in str(exc):
            pytest.skip(str(exc))
        raise

    assert output.exists()
    assert output.stat().st_size > 0


def test_key_hold_intervals_in_window_clips_left_and_right_buttons() -> None:
    frames = to_absolute_frames(
        (
            ReplayFrame(0, 0.0, 0.0, 0),
            ReplayFrame(100, 0.0, 0.0, LEGACY_Z_KEY),
            ReplayFrame(50, 0.0, 0.0, LEGACY_Z_KEY | LEGACY_X_KEY),
            ReplayFrame(50, 0.0, 0.0, 0),
        )
    )

    assert key_hold_intervals_in_window(frames, 120, 180) == [
        (120, 150, LEGACY_Z_KEY),
        (150, 180, LEGACY_Z_KEY | LEGACY_X_KEY),
    ]


def test_timeline_layout_keeps_playfield_below_object_row() -> None:
    layout = make_layout(1280, 720)

    assert layout.timeline_height == 148
    assert layout.playfield_top > layout.timeline_top + layout.timeline_height
    assert timeline_x_for_time(1500, 0, 3000, 100, 900) == 550


def test_world_bounds_expand_to_include_out_of_playfield_slider() -> None:
    beatmap = Beatmap(
        md5="same-map",
        audio_lead_in_ms=0,
        circle_size=5.0,
        overall_difficulty=5.0,
        hit_objects=(),
    )
    obj = HitObject(
        0,
        224.0,
        169.0,
        1577,
        2,
        end_time_ms=2000,
        slider_curve_type="P",
        slider_control_points=((224.0, 169.0), (218.0, 94.0), (226.0, -16.0)),
    )

    world_left, world_top, world_width, world_height = world_bounds_for_objects(beatmap, 0, [obj])
    layout = make_layout(1280, 720, beatmap=beatmap, objects=[obj])

    assert world_left == pytest.approx(0.0)
    assert world_top < -16.0
    assert world_width == pytest.approx(512.0)
    assert world_height > 400.0
    assert layout.point(obj.x, -16.0)[1] > layout.playfield_top


def test_slider_draw_points_samples_perfect_curve() -> None:
    obj = HitObject(
        0,
        224.0,
        169.0,
        1577,
        2,
        end_time_ms=2000,
        slider_curve_type="P",
        slider_control_points=((224.0, 169.0), (218.0, 94.0), (226.0, -16.0)),
    )

    points = slider_draw_points(obj)

    assert points[0] == pytest.approx((224.0, 169.0))
    assert points[-1] == pytest.approx((226.0, -16.0))
    assert len(points) > 3


def test_slider_draw_points_splits_bezier_on_repeated_anchors() -> None:
    obj = HitObject(
        0,
        0.0,
        0.0,
        1000,
        2,
        end_time_ms=1200,
        slider_curve_type="B",
        slider_control_points=((0.0, 0.0), (50.0, 80.0), (100.0, 0.0), (100.0, 0.0), (150.0, -80.0), (200.0, 0.0)),
    )

    points = slider_draw_points(obj)

    assert any(point == pytest.approx((100.0, 0.0)) for point in points)
    assert points[-1] == pytest.approx((200.0, 0.0))
    assert len(points) > 64


def test_slider_timeline_markers_include_ticks_repeats_and_tail() -> None:
    obj = HitObject(
        0,
        0.0,
        0.0,
        1000,
        2,
        end_time_ms=1400,
        repeat_count=2,
        slider_nested_hit_count=4,
    )

    assert slider_timeline_markers(obj) == [
        (1100, "tick"),
        (1200, "repeat"),
        (1300, "tick"),
        (1400, "tail"),
    ]
