from synthesis_osu_play.osr import (
    MISSING_ONLINE_ID,
    UNKNOWN_USER_ID,
    OsrReplay,
    ReplayFrame,
    decode_lazer_replay_metadata,
    encode_lazer_replay_metadata,
)


def test_osr_round_trip() -> None:
    replay = OsrReplay(
        mode=0,
        game_version=20240101,
        beatmap_md5="beatmap",
        player_name="player",
        replay_md5="hash",
        count_300=1,
        count_100=2,
        count_50=3,
        count_geki=4,
        count_katu=5,
        count_miss=6,
        score=12345,
        max_combo=99,
        perfect=False,
        mods=0,
        life_bar_graph="0|1,",
        timestamp=1,
        frames=(
            ReplayFrame(0, 256.0, 192.0, 0),
            ReplayFrame(10, 260.5, 193.25, 1),
            ReplayFrame(-12345, 0.0, 0.0, 42),
        ),
        online_score_id=0,
    )

    parsed = OsrReplay.from_bytes(replay.to_bytes())

    assert parsed.mode == replay.mode
    assert parsed.game_version == replay.game_version
    assert parsed.beatmap_md5 == replay.beatmap_md5
    assert parsed.player_name == replay.player_name
    assert parsed.frames == replay.frames


def test_with_frames_keeps_lazer_metadata_without_online_identity() -> None:
    replay = OsrReplay(
        mode=0,
        game_version=30000016,
        beatmap_md5="beatmap",
        player_name="player",
        replay_md5="hash",
        count_300=1,
        count_100=0,
        count_50=0,
        count_geki=0,
        count_katu=0,
        count_miss=0,
        score=12345,
        max_combo=99,
        perfect=True,
        mods=584,
        life_bar_graph="",
        timestamp=1,
        frames=(ReplayFrame(0, 256.0, 192.0, 0),),
        online_score_id=-1,
        trailing_bytes=encode_lazer_replay_metadata(
            {
                "client_version": "",
                "rank": "XH",
                "user_id": 123,
                "online_id": 456,
                "mods": [{"acronym": "NC"}, {"acronym": "HD"}],
                "statistics": {"great": 1},
                "maximum_statistics": {"great": 1},
            }
        ),
    )

    output = replay.with_frames((ReplayFrame(0, 256.0, 192.0, 0),), player_name="synthetic")
    metadata = decode_lazer_replay_metadata(output.trailing_bytes)

    assert output.online_score_id == MISSING_ONLINE_ID
    assert metadata is not None
    assert metadata["online_id"] == MISSING_ONLINE_ID
    assert metadata["user_id"] == UNKNOWN_USER_ID
    assert metadata["rank"] == "XH"
    assert metadata["mods"] == [{"acronym": "NC"}, {"acronym": "HD"}]
