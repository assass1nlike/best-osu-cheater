import json

from synthesis_osu_play.lazer import (
    default_lazer_skip_target_ms,
    replay_to_lazer_export,
    write_lazer_json,
)
from synthesis_osu_play.osr import OsrReplay, ReplayFrame
from synthesis_osu_play.synthesis import LEGACY_X_KEY, LEGACY_Z_KEY


def make_replay() -> OsrReplay:
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
        mods=0,
        life_bar_graph="",
        timestamp=1,
        frames=(
            ReplayFrame(0, 256.0, 192.0, 0),
            ReplayFrame(100, 256.0, 192.0, LEGACY_Z_KEY),
            ReplayFrame(100, 256.0, 192.0, LEGACY_X_KEY),
        ),
        online_score_id=0,
    )


def test_lazer_skip_target_matches_stock_minimum_skip_time() -> None:
    assert default_lazer_skip_target_ms(2500) == 1500
    assert default_lazer_skip_target_ms(500) == 0


def test_lazer_export_maps_legacy_buttons_to_actions(tmp_path) -> None:
    output = tmp_path / "replay.json"

    write_lazer_json(
        replay_to_lazer_export(make_replay(), skip_press_ms=300, skip_target_ms=1000),
        output,
    )

    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["skip_press_ms"] == 300
    assert payload["skip_target_ms"] == 1000
    assert payload["frames"][1]["actions"] == ["LeftButton"]
    assert payload["frames"][2]["actions"] == ["RightButton"]
