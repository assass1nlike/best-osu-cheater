from pathlib import Path
import random

import pytest

from synthesis_osu_play.beatmap import Beatmap, BreakPeriod, HitObject
from synthesis_osu_play.finalize import finalize_replay_metadata
from synthesis_osu_play.mods import (
    MOD_DOUBLE_TIME,
    MOD_EASY,
    MOD_HALF_TIME,
    MOD_HARD_ROCK,
    MOD_HIDDEN,
    MOD_NO_FAIL,
)
from synthesis_osu_play.osr import OsrReplay, ReplayFrame, decode_lazer_replay_metadata, encode_lazer_replay_metadata
import synthesis_osu_play.spinner_replace as spinner_module
import synthesis_osu_play.synthesis as synthesis_module
from synthesis_osu_play.synthesis import (
    ClickInterval,
    KeyInterval,
    DYNAMIC_PHASE_NOISE_STD_RAD,
    LEGACY_X_KEY,
    LEGACY_Z_KEY,
    SynthesisError,
    assign_natural_keys,
    clip_clicks_before_spinners,
    ensure_no_triple_overlap,
    effective_weights,
    extract_key_intervals,
    extend_slider_clicks_to_hold_coverage,
    match_effective_clicks,
    modded_hit_objects_for_matching,
    randomized_primary_key_repeat_threshold_ms,
    resolve_synthesized_overlaps,
    synthesize_spinner_interval,
    synthesize_replays,
    to_absolute_frames,
)
from synthesis_osu_play.scoring import score_replay


ARTIFACT_1643386 = Path(__file__).resolve().parents[1] / "artifacts" / "1643386"


def phase_for_seed(seed: int) -> float:
    return random.Random(seed).gauss(0.0, DYNAMIC_PHASE_NOISE_STD_RAD)


class LowerBoundRandom:
    def uniform(self, lower: float, _upper: float) -> float:
        return lower


def test_spinner_press_does_not_start_inside_break_period(monkeypatch) -> None:
    spinner = HitObject(1, 256.0, 192.0, 161974, 8, end_time_ms=163864)
    previous = ClickInterval(0, LEGACY_Z_KEY, 146800, 146914)
    monkeypatch.setattr(synthesis_module, "spinner_boundary_time", lambda *_args: 159066)

    interval = synthesize_spinner_interval(
        spinner,
        previous,
        None,
        previous,
        None,
        random.Random(1),
        break_periods=(BreakPeriod(147054, 161374),),
    )

    assert interval.start_ms == 161874


def make_replay(
    frames: tuple[ReplayFrame, ...],
    *,
    mods: int = 0,
    trailing_bytes: bytes = b"",
) -> OsrReplay:
    return OsrReplay(
        mode=0,
        game_version=30000016 if trailing_bytes else 20240101,
        beatmap_md5="same-map",
        player_name="player",
        replay_md5="hash",
        count_300=999,
        count_100=0,
        count_50=0,
        count_geki=0,
        count_katu=0,
        count_miss=0,
        score=123456,
        max_combo=999,
        perfect=True,
        mods=mods,
        life_bar_graph="",
        timestamp=1,
        frames=frames,
        online_score_id=-1 if trailing_bytes else 0,
        trailing_bytes=trailing_bytes,
    )


@pytest.mark.parametrize(
    ("starts", "expected_z", "expected_x"),
    (
        ((0, 600, 700), (0, 600), (700,)),
        ((0, 100, 200), (0, 200), (100,)),
        ((0, 100, 700, 800), (0, 700), (100, 800)),
        ((0, 500, 600), (0, 500), (600,)),
    ),
)
def test_assign_natural_keys_prefers_primary_key_with_short_gap_switches(
    starts: tuple[int, ...],
    expected_z: tuple[int, ...],
    expected_x: tuple[int, ...],
) -> None:
    intervals = [KeyInterval(start, start + 10) for start in starts]

    assigned = assign_natural_keys(intervals)

    assert [interval.start_ms for interval in assigned[LEGACY_Z_KEY]] == list(expected_z)
    assert [interval.start_ms for interval in assigned[LEGACY_X_KEY]] == list(expected_x)


def test_assign_natural_keys_uses_free_key_for_note_overlapping_slider_hold() -> None:
    slider = KeyInterval(1000, 2000)
    circle = KeyInterval(1800, 1850)

    assigned = assign_natural_keys([slider, circle], repeat_threshold_ms=350)

    assert assigned[LEGACY_Z_KEY] == [slider]
    assert assigned[LEGACY_X_KEY] == [circle]
    assert synthesis_module.keys_at_time(assigned, 1799) == LEGACY_Z_KEY
    assert synthesis_module.keys_at_time(assigned, 1800) == LEGACY_Z_KEY | LEGACY_X_KEY


@pytest.mark.parametrize("jitter_ms", (-50, 0, 50))
def test_randomized_primary_key_repeat_threshold_uses_configured_jitter(
    monkeypatch: pytest.MonkeyPatch,
    jitter_ms: int,
) -> None:
    calls: list[tuple[int, int]] = []

    def fake_uniform(lower: int, upper: int) -> int:
        calls.append((lower, upper))
        return jitter_ms

    monkeypatch.setattr(synthesis_module.random, "uniform", fake_uniform)

    threshold = randomized_primary_key_repeat_threshold_ms()

    assert threshold == 350 + jitter_ms
    assert calls == [(-50, 50)]


def test_circle_click_overlapping_spinner_is_clipped_after_50ms() -> None:
    objects = [
        HitObject(0, 256.0, 192.0, 1000, 1),
        HitObject(1, 256.0, 192.0, 2000, 8, end_time_ms=3000),
    ]
    clicks = [
        ClickInterval(0, LEGACY_Z_KEY, 950, 2200),
        None,
    ]

    clipped = clip_clicks_before_spinners(objects, clicks, LowerBoundRandom())

    assert clipped[0] == ClickInterval(0, LEGACY_Z_KEY, 950, 1000)


def test_slider_click_overlapping_spinner_is_clipped_at_slider_end() -> None:
    objects = [
        HitObject(0, 256.0, 192.0, 1000, 2, end_time_ms=1500),
        HitObject(1, 256.0, 192.0, 2000, 8, end_time_ms=3000),
    ]
    clicks = [
        ClickInterval(0, LEGACY_Z_KEY, 1000, 2200),
        None,
    ]

    clipped = clip_clicks_before_spinners(objects, clicks, LowerBoundRandom())

    assert clipped[0] == ClickInterval(0, LEGACY_Z_KEY, 1000, 1500)


def test_slider_click_uses_continuous_hold_across_key_handoff() -> None:
    objects = [
        HitObject(0, 256.0, 192.0, 1000, 1),
        HitObject(1, 256.0, 192.0, 1100, 2, end_time_ms=1500),
    ]
    frames = [
        synthesis_module.AbsoluteFrame(0, 256.0, 192.0, 0),
        synthesis_module.AbsoluteFrame(1000, 256.0, 192.0, LEGACY_Z_KEY),
        synthesis_module.AbsoluteFrame(1100, 256.0, 192.0, LEGACY_Z_KEY | LEGACY_X_KEY),
        synthesis_module.AbsoluteFrame(1150, 256.0, 192.0, LEGACY_X_KEY),
        synthesis_module.AbsoluteFrame(1500, 256.0, 192.0, LEGACY_X_KEY),
        synthesis_module.AbsoluteFrame(1550, 256.0, 192.0, 0),
    ]
    clicks = [
        ClickInterval(0, LEGACY_Z_KEY, 1000, 1050),
        ClickInterval(1, LEGACY_X_KEY, 1100, 1150),
    ]

    extended = extend_slider_clicks_to_hold_coverage(frames, objects, clicks)

    assert extended[0] == clicks[0]
    assert extended[1] == ClickInterval(1, LEGACY_X_KEY, 1100, 1550)


def test_slider_click_held_past_tail_preserves_actual_hold() -> None:
    objects = [HitObject(0, 256.0, 192.0, 1000, 2, end_time_ms=1500)]
    frames = [
        synthesis_module.AbsoluteFrame(0, 256.0, 192.0, 0),
        synthesis_module.AbsoluteFrame(1000, 256.0, 192.0, LEGACY_Z_KEY),
        synthesis_module.AbsoluteFrame(2000, 256.0, 192.0, 0),
    ]
    clicks = [ClickInterval(0, LEGACY_Z_KEY, 1000, 2000)]

    extended = extend_slider_clicks_to_hold_coverage(frames, objects, clicks)

    assert extended == [ClickInterval(0, LEGACY_Z_KEY, 1000, 2000)]


def test_resolve_synthesized_overlaps_trims_only_redundant_post_tail_holds() -> None:
    intervals = [
        KeyInterval(1000, 1800),
        KeyInterval(1100, 1800),
        KeyInterval(1200, 1900),
    ]

    resolved = resolve_synthesized_overlaps(intervals, [1000, 1100, 1900])

    assert resolved == [
        KeyInterval(1000, 1001),
        KeyInterval(1100, 1101),
        KeyInterval(1200, 1900),
    ]


def test_circle_click_cannot_be_clipped_when_spinner_starts_within_50ms() -> None:
    objects = [
        HitObject(0, 256.0, 192.0, 1000, 1),
        HitObject(1, 256.0, 192.0, 1040, 8, end_time_ms=2000),
    ]
    clicks = [
        ClickInterval(0, LEGACY_Z_KEY, 1000, 1200),
        None,
    ]

    with pytest.raises(SynthesisError, match="cannot be clipped"):
        clip_clicks_before_spinners(objects, clicks, LowerBoundRandom())


def test_synthesize_averages_positions_and_key_intervals() -> None:
    first = make_replay(
        (
            ReplayFrame(0, 0.0, 0.0, 0),
            ReplayFrame(10, 10.0, 0.0, 1),
            ReplayFrame(10, 20.0, 0.0, 0),
            ReplayFrame(-12345, 0.0, 0.0, 10),
        )
    )
    second = make_replay(
        (
            ReplayFrame(0, 0.0, 10.0, 0),
            ReplayFrame(20, 20.0, 10.0, 1),
            ReplayFrame(20, 40.0, 10.0, 0),
            ReplayFrame(-12345, 0.0, 0.0, 20),
        )
    )

    result = synthesize_replays(first, second, player_name="synthetic", synthesis_seed=0)

    assert result.replay.player_name == "synthetic"
    phase_offset = phase_for_seed(0)
    seed_first_weight, seed_second_weight = effective_weights(
        0,
        1.0,
        1.0,
        phase_offset_rad=phase_offset,
    )
    expected_seed = round(
        (10 * seed_first_weight) + (20 * seed_second_weight)
    )
    assert result.replay.frames[-1] == ReplayFrame(-12345, 0.0, 0.0, expected_seed)
    interval_first_weight, interval_second_weight = effective_weights(
        15,
        1.0,
        1.0,
        phase_offset_rad=phase_offset,
    )
    expected_press = round((10 * interval_first_weight) + (20 * interval_second_weight))
    expected_release = round((20 * interval_first_weight) + (40 * interval_second_weight))
    assert [(frame.delta_ms, frame.keys) for frame in result.replay.frames[:-1]] == [
        (0, 0),
        (10, 0),
        (expected_press - 10, 1),
        (20 - expected_press, 1),
        (expected_release - 20, 0),
        (40 - expected_release, 0),
    ]
    _, second_weight = effective_weights(
        expected_press,
        1.0,
        1.0,
        phase_offset_rad=phase_offset,
    )
    assert result.replay.frames[2].x == pytest.approx(float(expected_press))
    assert result.replay.frames[2].y == pytest.approx(10.0 * second_weight)


def test_synthesis_seed_makes_replay_bytes_reproducible() -> None:
    first = make_replay(
        (
            ReplayFrame(0, 0.0, 0.0, 0),
            ReplayFrame(1000, 0.0, 0.0, 0),
            ReplayFrame(1000, 512.0, 384.0, 0),
        )
    )
    second = make_replay(
        (
            ReplayFrame(0, 512.0, 384.0, 0),
            ReplayFrame(1000, 512.0, 384.0, 0),
            ReplayFrame(1000, 0.0, 0.0, 0),
        )
    )

    first_run = synthesize_replays(first, second, synthesis_seed=123)
    second_run = synthesize_replays(first, second, synthesis_seed=123)
    different_seed_run = synthesize_replays(first, second, synthesis_seed=124)

    assert first_run.replay.to_bytes() == second_run.replay.to_bytes()
    assert first_run.replay.to_bytes() != different_seed_run.replay.to_bytes()


def test_synthesize_rejects_unsupported_mod_mismatch() -> None:
    first = make_replay((ReplayFrame(0, 0.0, 0.0, 0),), mods=0)
    second = make_replay((ReplayFrame(0, 0.0, 0.0, 0),), mods=MOD_EASY)

    with pytest.raises(SynthesisError):
        synthesize_replays(first, second)


def test_synthesize_allows_hidden_mismatch_and_clears_output_hidden() -> None:
    first = make_replay(
        (
            ReplayFrame(0, 100.0, 100.0, 0),
            ReplayFrame(100, 100.0, 100.0, LEGACY_Z_KEY),
            ReplayFrame(50, 100.0, 100.0, 0),
        ),
        mods=MOD_HIDDEN,
    )
    second = make_replay(
        (
            ReplayFrame(0, 100.0, 100.0, 0),
            ReplayFrame(100, 100.0, 100.0, LEGACY_Z_KEY),
            ReplayFrame(50, 100.0, 100.0, 0),
        )
    )

    result = synthesize_replays(first, second)

    assert result.replay.mods == 0
    assert extract_key_intervals(to_absolute_frames(result.replay.frames), LEGACY_Z_KEY) == [KeyInterval(100, 150)]


def test_synthesize_preserves_hidden_when_both_replays_have_hidden() -> None:
    replay = make_replay((ReplayFrame(0, 0.0, 0.0, 0),), mods=MOD_HIDDEN)

    result = synthesize_replays(replay, replay)

    assert result.replay.mods == MOD_HIDDEN


def test_synthesize_allows_double_time_mismatch_without_rescaling_legacy_frame_times() -> None:
    first = make_replay(
        (
            ReplayFrame(0, 100.0, 100.0, 0),
            ReplayFrame(100, 100.0, 100.0, LEGACY_Z_KEY),
            ReplayFrame(50, 100.0, 100.0, 0),
        ),
        mods=MOD_DOUBLE_TIME,
    )
    second = make_replay(
        (
            ReplayFrame(0, 100.0, 100.0, 0),
            ReplayFrame(100, 100.0, 100.0, LEGACY_Z_KEY),
            ReplayFrame(50, 100.0, 100.0, 0),
        )
    )

    result = synthesize_replays(first, second)

    assert result.replay.mods == 0
    assert extract_key_intervals(to_absolute_frames(result.replay.frames), LEGACY_Z_KEY) == [KeyInterval(100, 150)]


def test_synthesize_allows_half_time_mismatch_without_rescaling_legacy_frame_times() -> None:
    first = make_replay(
        (
            ReplayFrame(0, 100.0, 100.0, 0),
            ReplayFrame(100, 100.0, 100.0, LEGACY_Z_KEY),
            ReplayFrame(50, 100.0, 100.0, 0),
        ),
        mods=MOD_HALF_TIME,
    )
    second = make_replay(
        (
            ReplayFrame(0, 100.0, 100.0, 0),
            ReplayFrame(100, 100.0, 100.0, LEGACY_Z_KEY),
            ReplayFrame(50, 100.0, 100.0, 0),
        )
    )

    result = synthesize_replays(first, second)

    assert result.replay.mods == 0
    assert extract_key_intervals(to_absolute_frames(result.replay.frames), LEGACY_Z_KEY) == [KeyInterval(100, 150)]


def test_synthesize_flips_hard_rock_replay_to_normal_playfield() -> None:
    first = make_replay((ReplayFrame(0, 100.0, 100.0, 0),), mods=MOD_HARD_ROCK)
    second = make_replay((ReplayFrame(0, 100.0, 284.0, 0),))

    result = synthesize_replays(first, second)

    assert result.replay.mods == 0
    assert result.replay.frames[0].y == pytest.approx(284.0)


def test_synthesize_can_project_source_mods_to_explicit_batch_output() -> None:
    first = make_replay(
        (ReplayFrame(0, 100.0, 100.0, 0),),
        mods=MOD_HARD_ROCK | MOD_NO_FAIL,
    )
    second = make_replay(
        (ReplayFrame(0, 100.0, 284.0, 0),),
        mods=MOD_HALF_TIME,
    )

    result = synthesize_replays(
        first,
        second,
        output_mods=MOD_DOUBLE_TIME,
        allow_source_mod_mismatch=True,
    )

    assert result.replay.mods == MOD_DOUBLE_TIME
    assert result.replay.frames[0].y == pytest.approx(284.0)


def test_hard_rock_object_flip_also_flips_slider_control_points() -> None:
    obj = HitObject(
        0,
        100.0,
        80.0,
        1000,
        2,
        end_time_ms=1200,
        slider_curve_type="L",
        slider_control_points=((100.0, 80.0), (200.0, 120.0)),
    )

    flipped = modded_hit_objects_for_matching((obj,), MOD_HARD_ROCK)[0]

    assert flipped.y == pytest.approx(304.0)
    assert flipped.slider_control_points == ((100.0, 304.0), (200.0, 264.0))


def test_object_aware_synthesis_matches_cross_hard_rock_after_flip() -> None:
    beatmap = Beatmap(
        md5="same-map",
        audio_lead_in_ms=0,
        circle_size=5.0,
        overall_difficulty=5.0,
        hit_objects=(HitObject(0, 256.0, 100.0, 1000, 1),),
    )
    first = make_replay(
        (
            ReplayFrame(0, 256.0, 284.0, 0),
            ReplayFrame(1000, 256.0, 284.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 284.0, 0),
        ),
        mods=MOD_HARD_ROCK,
    )
    second = make_replay(
        (
            ReplayFrame(0, 256.0, 100.0, 0),
            ReplayFrame(1000, 256.0, 100.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 100.0, 0),
        )
    )

    result = synthesize_replays(first, second, beatmap=beatmap, synthesis_seed=0)

    assert result.replay.mods == 0
    assert result.report.matched_object_count == 1
    assert result.report.dropped_object_count == 0
    assert result.replay.frames[1].y == pytest.approx(100.0)


def test_object_aware_synthesis_matches_shared_hard_rock_in_hard_rock_space() -> None:
    beatmap = Beatmap(
        md5="same-map",
        audio_lead_in_ms=0,
        circle_size=5.0,
        overall_difficulty=5.0,
        hit_objects=(HitObject(0, 256.0, 100.0, 1000, 1),),
    )
    first = make_replay(
        (
            ReplayFrame(0, 256.0, 284.0, 0),
            ReplayFrame(1000, 256.0, 284.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 284.0, 0),
        ),
        mods=MOD_HARD_ROCK,
    )
    second = make_replay(
        (
            ReplayFrame(0, 256.0, 284.0, 0),
            ReplayFrame(1000, 256.0, 284.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 284.0, 0),
        ),
        mods=MOD_HARD_ROCK,
    )

    result = synthesize_replays(first, second, beatmap=beatmap, synthesis_seed=0)

    assert result.replay.mods == MOD_HARD_ROCK
    assert result.report.matched_object_count == 1
    assert result.report.dropped_object_count == 0
    assert result.replay.frames[1].y == pytest.approx(284.0)


def test_local_score_uses_hard_rock_object_positions() -> None:
    beatmap = Beatmap(
        md5="same-map",
        audio_lead_in_ms=0,
        circle_size=5.0,
        overall_difficulty=5.0,
        hit_objects=(HitObject(0, 256.0, 100.0, 1000, 1),),
    )
    replay = make_replay(
        (
            ReplayFrame(0, 256.0, 284.0, 0),
            ReplayFrame(1000, 256.0, 284.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 284.0, 0),
        ),
        mods=MOD_HARD_ROCK,
    )

    metadata = score_replay(replay, beatmap)

    assert metadata.count_300 == 1
    assert metadata.count_miss == 0


def test_synthesize_combined_handled_mods_and_rewrites_lazer_metadata() -> None:
    beatmap = Beatmap(
        md5="same-map",
        audio_lead_in_ms=0,
        circle_size=5.0,
        overall_difficulty=5.0,
        hit_objects=(HitObject(0, 256.0, 100.0, 1000, 1),),
    )
    trailing_bytes = encode_lazer_replay_metadata(
        {
            "client_version": "",
            "rank": "XH",
            "user_id": 123,
            "online_id": 456,
            "mods": [{"acronym": "HD"}, {"acronym": "HR"}, {"acronym": "DT"}],
            "statistics": {"great": 1},
            "maximum_statistics": {"great": 1},
        }
    )
    first = make_replay(
        (
            ReplayFrame(0, 256.0, 284.0, 0),
            ReplayFrame(1000, 256.0, 284.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 284.0, 0),
        ),
        mods=MOD_HIDDEN | MOD_HARD_ROCK | MOD_DOUBLE_TIME,
        trailing_bytes=trailing_bytes,
    )
    second = make_replay(
        (
            ReplayFrame(0, 256.0, 100.0, 0),
            ReplayFrame(1000, 256.0, 100.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 100.0, 0),
        )
    )

    result = synthesize_replays(first, second, beatmap=beatmap, synthesis_seed=0)
    metadata = decode_lazer_replay_metadata(result.replay.trailing_bytes)

    assert result.replay.mods == 0
    assert result.report.matched_object_count == 1
    assert metadata is not None
    assert metadata["mods"] == []


def test_synthesize_rejects_conflicting_speed_mods() -> None:
    first = make_replay((ReplayFrame(0, 0.0, 0.0, 0),), mods=MOD_DOUBLE_TIME | MOD_HALF_TIME)
    second = make_replay((ReplayFrame(0, 0.0, 0.0, 0),))

    with pytest.raises(SynthesisError, match="conflicting speed mods"):
        synthesize_replays(first, second)


def test_synthesize_rejects_key_mismatch_by_default() -> None:
    first = make_replay(
        (
            ReplayFrame(0, 0.0, 0.0, 1),
            ReplayFrame(10, 0.0, 0.0, 0),
        )
    )
    second = make_replay((ReplayFrame(0, 0.0, 0.0, 0),))

    with pytest.raises(SynthesisError):
        synthesize_replays(first, second)


def test_object_aware_synthesis_does_not_backfill_misplaced_clicks() -> None:
    beatmap = Beatmap(
        md5="same-map",
        audio_lead_in_ms=0,
        circle_size=5.0,
        overall_difficulty=5.0,
        hit_objects=(
            HitObject(0, 256.0, 192.0, 1000, 1),
            HitObject(1, 256.0, 192.0, 2000, 1),
            HitObject(2, 256.0, 192.0, 3000, 1),
        ),
    )
    first = make_replay(
        (
            ReplayFrame(0, 256.0, 192.0, 0),
            ReplayFrame(1000, 256.0, 192.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 192.0, 0),
            ReplayFrame(900, 0.0, 0.0, LEGACY_X_KEY),
            ReplayFrame(50, 0.0, 0.0, 0),
            ReplayFrame(950, 256.0, 192.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 192.0, 0),
        )
    )
    second = make_replay(
        (
            ReplayFrame(0, 256.0, 192.0, 0),
            ReplayFrame(1020, 256.0, 192.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 192.0, 0),
            ReplayFrame(950, 256.0, 192.0, LEGACY_X_KEY),
            ReplayFrame(50, 256.0, 192.0, 0),
            ReplayFrame(930, 256.0, 192.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 192.0, 0),
        )
    )

    result = synthesize_replays(first, second, beatmap=beatmap, synthesis_seed=0)

    assert result.report.matched_object_count == 2
    assert result.report.dropped_object_count == 1
    absolute = to_absolute_frames(result.replay.frames)
    first_z = extract_key_intervals(absolute, LEGACY_Z_KEY)
    phase_offset = phase_for_seed(0)
    first_weight, second_weight = effective_weights(1000, 1.0, 1.0, phase_offset_rad=phase_offset)
    third_weight, fourth_weight = effective_weights(3000, 1.0, 1.0, phase_offset_rad=phase_offset)
    assert first_z == [
        KeyInterval(
            round(1000 * first_weight + 1020 * second_weight),
            round(1050 * first_weight + 1070 * second_weight),
        ),
        KeyInterval(
            round(2950 * third_weight + 3000 * fourth_weight),
            round(3000 * third_weight + 3050 * fourth_weight),
        ),
    ]
    assert extract_key_intervals(absolute, LEGACY_X_KEY) == []


def test_lazer_matching_keeps_object_after_misplaced_press() -> None:
    objects = [
        HitObject(0, 256.0, 192.0, 1000, 1),
        HitObject(1, 256.0, 192.0, 2000, 1),
    ]
    frames = [
        synthesis_module.AbsoluteFrame(0, 0.0, 0.0, 0),
        synthesis_module.AbsoluteFrame(1000, 0.0, 0.0, LEGACY_Z_KEY),
        synthesis_module.AbsoluteFrame(1050, 0.0, 0.0, 0),
        synthesis_module.AbsoluteFrame(1100, 256.0, 192.0, LEGACY_X_KEY),
        synthesis_module.AbsoluteFrame(1150, 256.0, 192.0, 0),
    ]

    matched = match_effective_clicks(
        frames,
        objects,
        hit_window_ms=150,
        circle_radius=1,
    )

    assert matched == [ClickInterval(1, LEGACY_X_KEY, 1100, 1150), None]


def test_lazer_matching_retries_same_press_after_automatic_miss() -> None:
    objects = [
        HitObject(0, 256.0, 192.0, 1000, 1),
        HitObject(1, 400.0, 192.0, 1200, 1),
    ]
    frames = [
        synthesis_module.AbsoluteFrame(0, 0.0, 0.0, 0),
        synthesis_module.AbsoluteFrame(1200, 400.0, 192.0, LEGACY_Z_KEY),
        synthesis_module.AbsoluteFrame(1250, 400.0, 192.0, 0),
    ]

    matched = match_effective_clicks(
        frames,
        objects,
        hit_window_ms=150,
        circle_radius=1,
    )

    assert matched == [None, ClickInterval(0, LEGACY_Z_KEY, 1200, 1250)]


def test_any_order_matching_allows_later_object_without_legacy_note_lock() -> None:
    objects = [
        HitObject(0, 100.0, 100.0, 1000, 1),
        HitObject(1, 400.0, 192.0, 1100, 1),
    ]
    frames = [
        synthesis_module.AbsoluteFrame(0, 0.0, 0.0, 0),
        synthesis_module.AbsoluteFrame(1100, 400.0, 192.0, LEGACY_Z_KEY),
        synthesis_module.AbsoluteFrame(1150, 400.0, 192.0, 0),
    ]

    legacy = match_effective_clicks(
        frames,
        objects,
        hit_window_ms=150,
        circle_radius=1,
    )
    any_order = match_effective_clicks(
        frames,
        objects,
        hit_window_ms=150,
        circle_radius=1,
        sequential=False,
    )

    assert legacy == [None, None]
    assert any_order == [None, ClickInterval(0, LEGACY_Z_KEY, 1100, 1150)]


def test_spinner_synthesis_holds_key_through_spinner(monkeypatch: pytest.MonkeyPatch) -> None:
    class DeterministicRandom:
        def __init__(self, seed: int) -> None:
            self.seed = seed

        def gauss(self, mu: float, sigma: float) -> float:
            return sigma / 2

        def uniform(self, lower: float, upper: float) -> float:
            return 0.0

    monkeypatch.setattr(synthesis_module, "Random", DeterministicRandom)
    monkeypatch.setattr(synthesis_module.random, "uniform", lambda _lower, _upper: 0)

    beatmap = Beatmap(
        md5="same-map",
        audio_lead_in_ms=0,
        circle_size=5.0,
        overall_difficulty=5.0,
        hit_objects=(
            HitObject(0, 256.0, 192.0, 1000, 1),
            HitObject(1, 256.0, 192.0, 1500, 8, end_time_ms=2000),
            HitObject(2, 256.0, 192.0, 2200, 1),
        ),
    )
    first = make_replay(
        (
            ReplayFrame(0, 256.0, 192.0, 0),
            ReplayFrame(1000, 256.0, 192.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 192.0, 0),
            ReplayFrame(1150, 256.0, 192.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 192.0, 0),
        )
    )
    second = make_replay(
        (
            ReplayFrame(0, 256.0, 192.0, 0),
            ReplayFrame(1010, 256.0, 192.0, LEGACY_Z_KEY),
            ReplayFrame(60, 256.0, 192.0, 0),
            ReplayFrame(1130, 256.0, 192.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 192.0, 0),
        )
    )

    result = synthesize_replays(first, second, beatmap=beatmap, synthesis_seed=0)
    absolute = to_absolute_frames(result.replay.frames)

    first_weight, second_weight = effective_weights(
        1000,
        1.0,
        1.0,
        phase_offset_rad=DYNAMIC_PHASE_NOISE_STD_RAD / 2,
    )
    assert extract_key_intervals(absolute, LEGACY_Z_KEY)[0] == KeyInterval(
        round((1000 * first_weight) + (1010 * second_weight)),
        round((1050 * first_weight) + (1070 * second_weight)),
    )
    assert extract_key_intervals(absolute, LEGACY_Z_KEY)[1:] == [
        KeyInterval(1446, 2075),
        KeyInterval(2200, 2250),
    ]
    assert extract_key_intervals(absolute, LEGACY_X_KEY) == []


def test_synthesis_uses_bundled_spinner_library_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def fake_replace_spinner_segments(*args, **kwargs):
        captured["library_path"] = kwargs["library_path"]
        captured["spinner_mode"] = kwargs["spinner_mode"]
        captured["synthesis_seed"] = kwargs["synthesis_seed"]
        return args[0], []

    monkeypatch.setattr(spinner_module, "replace_spinner_segments", fake_replace_spinner_segments)
    beatmap = Beatmap(
        md5="same-map",
        audio_lead_in_ms=0,
        circle_size=5.0,
        overall_difficulty=5.0,
        hit_objects=(HitObject(0, 256.0, 192.0, 1000, 8, end_time_ms=1800),),
    )
    first = make_replay(
        (
            ReplayFrame(0, 256.0, 192.0, 0),
            ReplayFrame(1800, 256.0, 192.0, 0),
        )
    )
    second = make_replay(
        (
            ReplayFrame(0, 256.0, 192.0, 0),
            ReplayFrame(1800, 256.0, 192.0, 0),
        )
    )

    synthesize_replays(first, second, beatmap=beatmap, synthesis_seed=123)

    assert captured == {
        "library_path": str(spinner_module.DEFAULT_SPINNER_LIBRARY_PATH),
        "spinner_mode": "all",
        "synthesis_seed": 123,
    }


def test_object_aware_synthesis_drops_object_missing_in_either_replay() -> None:
    beatmap = Beatmap(
        md5="same-map",
        audio_lead_in_ms=0,
        circle_size=5.0,
        overall_difficulty=5.0,
        hit_objects=(
            HitObject(0, 256.0, 192.0, 1000, 1),
            HitObject(1, 256.0, 192.0, 2000, 1),
        ),
    )
    first = make_replay(
        (
            ReplayFrame(0, 256.0, 192.0, 0),
            ReplayFrame(1000, 256.0, 192.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 192.0, 0),
        )
    )
    second = make_replay(
        (
            ReplayFrame(0, 256.0, 192.0, 0),
            ReplayFrame(1000, 256.0, 192.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 192.0, 0),
            ReplayFrame(950, 256.0, 192.0, LEGACY_X_KEY),
            ReplayFrame(50, 256.0, 192.0, 0),
        )
    )

    result = synthesize_replays(first, second, beatmap=beatmap)

    assert result.report.matched_object_count == 1
    assert result.report.dropped_object_count == 1
    assert [frame.keys for frame in result.replay.frames].count(LEGACY_Z_KEY) == 1


def test_miss_chain_without_valid_neighbors_is_not_backfilled() -> None:
    beatmap = Beatmap(
        md5="same-map",
        audio_lead_in_ms=0,
        circle_size=5.0,
        overall_difficulty=5.0,
        hit_objects=(HitObject(0, 256.0, 192.0, 1000, 1),),
    )
    first = make_replay(
        (
            ReplayFrame(0, 0.0, 0.0, 0),
            ReplayFrame(980, 0.0, 0.0, LEGACY_Z_KEY),
            ReplayFrame(50, 0.0, 0.0, 0),
        )
    )
    second = make_replay(
        (
            ReplayFrame(0, 0.0, 0.0, 0),
            ReplayFrame(1000, 0.0, 0.0, LEGACY_Z_KEY),
            ReplayFrame(50, 0.0, 0.0, 0),
        )
    )

    result = synthesize_replays(first, second, beatmap=beatmap)

    assert result.report.matched_object_count == 0
    assert result.report.dropped_object_count == 1


def test_skip_intro_position_uses_third_anchor_before_skip() -> None:
    first = make_replay(
        (
            ReplayFrame(0, 0.0, 0.0, 0),
            ReplayFrame(1000, 10.0, 0.0, 0),
            ReplayFrame(1000, 100.0, 100.0, 0),
        )
    )
    second = make_replay(
        (
            ReplayFrame(0, 30.0, 0.0, 0),
            ReplayFrame(1000, 40.0, 0.0, 0),
            ReplayFrame(1000, 200.0, 100.0, 0),
        )
    )

    result = synthesize_replays(
        first,
        second,
        first_skip_ms=1000,
        second_skip_ms=1500,
        intro_end_ms=2000,
        synthesis_seed=0,
    )

    assert result.report.skip_press_ms == 1000
    phase_offset = phase_for_seed(0)
    first_weight, second_weight = effective_weights(1000, 1.0, 1.0, phase_offset_rad=phase_offset)
    first_absolute = synthesis_module.to_absolute_frames(first.frames)
    second_absolute = synthesis_module.to_absolute_frames(second.frames)
    anchor_x, anchor_y = synthesis_module.average_position_at_time(
        first_absolute,
        second_absolute,
        2000,
        phase_offset_rad=phase_offset,
    )
    expected_x = synthesis_module.weighted_average_three(10.0, 40.0, anchor_x, first_weight, second_weight, 1.0)
    expected_y = synthesis_module.weighted_average_three(0.0, 0.0, anchor_y, first_weight, second_weight, 1.0)
    assert result.replay.frames[1].x == pytest.approx(expected_x)
    assert result.replay.frames[1].y == pytest.approx(expected_y)


def test_skip_requires_intro_end_without_beatmap() -> None:
    replay = make_replay((ReplayFrame(0, 0.0, 0.0, 0),))

    with pytest.raises(SynthesisError):
        synthesize_replays(replay, replay, first_skip_ms=1000)


def test_beatmap_skip_default_uses_lazer_skip_target() -> None:
    beatmap = Beatmap(
        md5="same-map",
        audio_lead_in_ms=0,
        circle_size=5.0,
        overall_difficulty=5.0,
        hit_objects=(HitObject(0, 0.0, 0.0, 2500, 1),),
    )
    replay = make_replay(
        (
            ReplayFrame(0, 0.0, 0.0, 0),
            ReplayFrame(1500, 100.0, 100.0, 0),
        )
    )

    result = synthesize_replays(replay, replay, beatmap=beatmap, first_skip_ms=500)

    assert result.report.intro_end_ms == 1500


def test_triple_overlap_rejected() -> None:
    with pytest.raises(SynthesisError):
        ensure_no_triple_overlap(
            [
                KeyInterval(100, 200),
                KeyInterval(120, 210),
                KeyInterval(130, 220),
            ]
        )


def test_beatmap_synthesis_keeps_provisional_metadata_by_default() -> None:
    beatmap = Beatmap(
        md5="same-map",
        audio_lead_in_ms=0,
        circle_size=5.0,
        overall_difficulty=5.0,
        hit_objects=(
            HitObject(0, 256.0, 192.0, 1000, 1),
            HitObject(1, 256.0, 192.0, 2000, 1),
        ),
    )
    first = make_replay(
        (
            ReplayFrame(0, 256.0, 192.0, 0),
            ReplayFrame(1000, 256.0, 192.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 192.0, 0),
        )
    )
    second = make_replay(
        (
            ReplayFrame(0, 256.0, 192.0, 0),
            ReplayFrame(1120, 256.0, 192.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 192.0, 0),
        )
    )

    result = synthesize_replays(first, second, beatmap=beatmap)

    assert result.replay.count_300 == 999
    assert result.replay.count_miss == 0
    assert result.replay.max_combo == 999
    assert result.replay.score == 123456


def test_beatmap_synthesis_can_recompute_score_metadata_locally() -> None:
    beatmap = Beatmap(
        md5="same-map",
        audio_lead_in_ms=0,
        circle_size=5.0,
        overall_difficulty=5.0,
        hit_objects=(
            HitObject(0, 256.0, 192.0, 1000, 1),
            HitObject(1, 256.0, 192.0, 2000, 1),
        ),
    )
    first = make_replay(
        (
            ReplayFrame(0, 256.0, 192.0, 0),
            ReplayFrame(1000, 256.0, 192.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 192.0, 0),
        )
    )
    second = make_replay(
        (
            ReplayFrame(0, 256.0, 192.0, 0),
            ReplayFrame(1120, 256.0, 192.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 192.0, 0),
        )
    )

    result = synthesize_replays(
        first,
        second,
        beatmap=beatmap,
        recompute_score_metadata=True,
        synthesis_seed=0,
    )

    assert result.replay.count_300 == 1
    assert result.replay.count_100 == 0
    assert result.replay.count_miss == 1
    assert result.replay.max_combo == 1
    assert not result.replay.perfect
    assert result.replay.score < 123456


def test_beatmap_synthesis_rewrites_lazer_score_metadata() -> None:
    beatmap = Beatmap(
        md5="same-map",
        audio_lead_in_ms=0,
        circle_size=5.0,
        overall_difficulty=5.0,
        hit_objects=(HitObject(0, 256.0, 192.0, 1000, 1),),
    )
    trailing_bytes = encode_lazer_replay_metadata(
        {
            "client_version": "",
            "rank": "XH",
            "user_id": 123,
            "online_id": 456,
            "mods": [],
            "statistics": {"great": 999},
            "maximum_statistics": {"great": 1},
        }
    )
    first = make_replay(
        (
            ReplayFrame(0, 256.0, 192.0, 0),
            ReplayFrame(1080, 256.0, 192.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 192.0, 0),
        ),
        trailing_bytes=trailing_bytes,
    )
    second = make_replay(
        (
            ReplayFrame(0, 256.0, 192.0, 0),
            ReplayFrame(1080, 256.0, 192.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 192.0, 0),
        ),
        trailing_bytes=trailing_bytes,
    )

    result = synthesize_replays(first, second, beatmap=beatmap, recompute_score_metadata=True)
    metadata = decode_lazer_replay_metadata(result.replay.trailing_bytes)

    assert metadata is not None
    assert metadata["rank"] == "D"
    assert metadata["statistics"] == {"ok": 1}
    assert metadata["maximum_statistics"] == {"great": 1}


def test_finalize_replay_metadata_copies_lazer_scored_fields() -> None:
    trailing_bytes = encode_lazer_replay_metadata(
        {
            "client_version": "2026.624.0-lazer",
            "rank": "B",
            "user_id": 36703588,
            "online_id": 123456789,
            "mods": [{"acronym": "HD"}],
            "statistics": {"great": 3, "miss": 1, "large_tick_hit": 2},
            "maximum_statistics": {"great": 4, "large_tick_hit": 2},
        }
    )
    provisional = make_replay(
        (
            ReplayFrame(0, 256.0, 192.0, 0),
            ReplayFrame(1000, 256.0, 192.0, LEGACY_Z_KEY),
        ),
        trailing_bytes=trailing_bytes,
    )
    scored = OsrReplay(
        mode=provisional.mode,
        game_version=provisional.game_version,
        beatmap_md5=provisional.beatmap_md5,
        player_name=provisional.player_name,
        replay_md5=provisional.replay_md5,
        count_300=3,
        count_100=0,
        count_50=0,
        count_geki=0,
        count_katu=0,
        count_miss=1,
        score=98765,
        max_combo=3,
        perfect=False,
        mods=provisional.mods,
        life_bar_graph="0|1,1000|0.5,",
        timestamp=123456789,
        frames=provisional.frames,
        online_score_id=123456789,
        trailing_bytes=trailing_bytes,
    )

    finalized = finalize_replay_metadata(provisional, scored)
    metadata = decode_lazer_replay_metadata(finalized.trailing_bytes)

    assert finalized.count_300 == 3
    assert finalized.count_miss == 1
    assert finalized.score == 98765
    assert finalized.max_combo == 3
    assert not finalized.perfect
    assert finalized.life_bar_graph == "0|1,1000|0.5,"
    assert finalized.timestamp == 123456789
    assert metadata is not None
    assert metadata["rank"] == "B"
    assert metadata["statistics"] == {"great": 3, "miss": 1, "large_tick_hit": 2}
    assert metadata["online_id"] == -1
    assert metadata["user_id"] == 1
    assert metadata["client_version"] == ""


def test_spinner_requires_rotation_and_key_hold() -> None:
    beatmap = Beatmap(
        md5="same-map",
        audio_lead_in_ms=0,
        circle_size=5.0,
        overall_difficulty=0.0,
        hit_objects=(HitObject(0, 256.0, 192.0, 1000, 8, end_time_ms=1800),),
    )
    hit_replay = make_replay(
        (
            ReplayFrame(950, 356.0, 192.0, 0),
            ReplayFrame(50, 256.0, 92.0, LEGACY_Z_KEY),
            ReplayFrame(200, 156.0, 192.0, LEGACY_Z_KEY),
            ReplayFrame(200, 256.0, 292.0, LEGACY_Z_KEY),
            ReplayFrame(200, 356.0, 192.0, LEGACY_Z_KEY),
        )
    )
    miss_replay = make_replay(
        (
            ReplayFrame(950, 356.0, 192.0, 0),
            ReplayFrame(50, 256.0, 92.0, 0),
            ReplayFrame(200, 156.0, 192.0, 0),
            ReplayFrame(200, 256.0, 292.0, 0),
            ReplayFrame(200, 356.0, 192.0, 0),
        )
    )

    hit_metadata = score_replay(hit_replay, beatmap)
    miss_metadata = score_replay(miss_replay, beatmap)

    assert hit_metadata.count_300 == 1
    assert hit_metadata.count_miss == 0
    assert hit_metadata.rank == "X"
    assert hit_metadata.statistics["large_bonus"] == 1

    assert miss_metadata.count_300 == 0
    assert miss_metadata.count_miss == 1
    assert miss_metadata.rank == "D"


def test_real_spinner_replay_is_scored_from_actual_input_state() -> None:
    beatmap = Beatmap.read_path(ARTIFACT_1643386 / "1643386.osu")
    source_12 = OsrReplay.read_path(ARTIFACT_1643386 / "rank12_3410678288.osr")
    source_14 = OsrReplay.read_path(ARTIFACT_1643386 / "rank14_5054510460.osr")

    meta_12 = score_replay(source_12, beatmap)
    meta_14 = score_replay(source_14, beatmap)
    synthesized = synthesize_replays(source_12, source_14, beatmap=beatmap).replay
    meta_synth = score_replay(synthesized, beatmap)

    assert meta_12.count_miss == 0
    assert meta_14.count_miss == 0
    assert meta_12.statistics["large_bonus"] == 5
    assert meta_14.statistics["large_bonus"] == 5
    assert meta_synth.count_miss == 0
    assert meta_synth.statistics["large_bonus"] == 5
