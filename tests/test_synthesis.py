from pathlib import Path

import pytest

from synthesis_osu_play.beatmap import Beatmap, HitObject
from synthesis_osu_play.finalize import finalize_replay_metadata
from synthesis_osu_play.osr import OsrReplay, ReplayFrame, decode_lazer_replay_metadata, encode_lazer_replay_metadata
from synthesis_osu_play.synthesis import (
    KeyInterval,
    LEGACY_X_KEY,
    LEGACY_Z_KEY,
    SynthesisError,
    ensure_no_triple_overlap,
    extract_key_intervals,
    synthesize_replays,
    to_absolute_frames,
)
from synthesis_osu_play.scoring import score_replay


ARTIFACT_1643386 = Path(__file__).resolve().parents[1] / "artifacts" / "1643386"


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

    result = synthesize_replays(first, second, player_name="synthetic")

    assert result.replay.player_name == "synthetic"
    assert result.replay.frames[-1] == ReplayFrame(-12345, 0.0, 0.0, 15)
    assert [(frame.delta_ms, frame.keys) for frame in result.replay.frames[:-1]] == [
        (0, 0),
        (10, 0),
        (5, 1),
        (5, 1),
        (10, 0),
        (10, 0),
    ]
    assert result.replay.frames[2].x == 15.0
    assert result.replay.frames[2].y == 5.0


def test_synthesize_rejects_different_mods() -> None:
    first = make_replay((ReplayFrame(0, 0.0, 0.0, 0),), mods=0)
    second = make_replay((ReplayFrame(0, 0.0, 0.0, 0),), mods=64)

    with pytest.raises(SynthesisError):
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


def test_object_aware_synthesis_fills_miss_chain_from_nearby_extra_clicks() -> None:
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

    result = synthesize_replays(first, second, beatmap=beatmap)

    assert result.report.matched_object_count == 3
    assert result.report.dropped_object_count == 0
    absolute = to_absolute_frames(result.replay.frames)
    assert extract_key_intervals(absolute, LEGACY_Z_KEY) == [
        KeyInterval(1010, 1060),
        KeyInterval(2975, 3025),
    ]
    assert extract_key_intervals(absolute, LEGACY_X_KEY) == [KeyInterval(1985, 2035)]


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

    result = synthesize_replays(first, second, first_skip_ms=1000, second_skip_ms=1500, intro_end_ms=2000)

    assert result.report.skip_press_ms == 1000
    assert result.replay.frames[1].x == pytest.approx(66.666666, abs=0.000001)
    assert result.replay.frames[1].y == pytest.approx(33.333333, abs=0.000001)


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

    result = synthesize_replays(first, second, beatmap=beatmap, recompute_score_metadata=True)

    assert result.replay.count_300 == 0
    assert result.replay.count_100 == 1
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
    synthesized = OsrReplay.read_path(ARTIFACT_1643386 / "synth_rank12_rank14.osr")

    meta_12 = score_replay(source_12, beatmap)
    meta_14 = score_replay(source_14, beatmap)
    meta_synth = score_replay(synthesized, beatmap)

    assert meta_12.count_miss == 0
    assert meta_14.count_miss == 0
    assert meta_12.statistics["large_bonus"] == 5
    assert meta_14.statistics["large_bonus"] == 5
    assert meta_synth.count_miss == 1
    assert meta_synth.statistics.get("large_bonus", 0) == 0
