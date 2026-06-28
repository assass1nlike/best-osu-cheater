import json
from pathlib import Path

from synthesis_osu_play.cli import main
from synthesis_osu_play.osr import OsrReplay, ReplayFrame, encode_lazer_replay_metadata
from synthesis_osu_play.synthesis import LEGACY_Z_KEY


def make_replay(path: Path, frames: tuple[ReplayFrame, ...]) -> None:
    replay = OsrReplay(
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
        frames=frames,
        online_score_id=0,
    )
    replay.write_path(path)


def test_synthesize_command_writes_readable_osr(tmp_path: Path) -> None:
    first_path = tmp_path / "first.osr"
    second_path = tmp_path / "second.osr"
    output_path = tmp_path / "output.osr"
    make_replay(
        first_path,
        (
            ReplayFrame(0, 0.0, 0.0, 0),
            ReplayFrame(10, 10.0, 0.0, 1),
            ReplayFrame(10, 20.0, 0.0, 0),
        ),
    )
    make_replay(
        second_path,
        (
            ReplayFrame(0, 0.0, 10.0, 0),
            ReplayFrame(20, 20.0, 10.0, 1),
            ReplayFrame(20, 40.0, 10.0, 0),
        ),
    )

    exit_code = main(
        [
            "synthesize",
            str(first_path),
            str(second_path),
            str(output_path),
            "--player-name",
            "synthetic",
        ]
    )

    assert exit_code == 0
    output = OsrReplay.read_path(output_path)
    assert output.player_name == "synthetic"
    assert len(output.frames) == 6


def test_synthesize_command_writes_lazer_json(tmp_path: Path) -> None:
    first_path = tmp_path / "first.osr"
    second_path = tmp_path / "second.osr"
    output_path = tmp_path / "output.osr"
    json_path = tmp_path / "output.json"
    make_replay(
        first_path,
        (
            ReplayFrame(0, 256.0, 192.0, 0),
            ReplayFrame(100, 256.0, 192.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 192.0, 0),
        ),
    )
    make_replay(
        second_path,
        (
            ReplayFrame(0, 256.0, 192.0, 0),
            ReplayFrame(100, 256.0, 192.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 192.0, 0),
        ),
    )

    exit_code = main(
        [
            "synthesize",
            str(first_path),
            str(second_path),
            str(output_path),
            "--lazer-json",
            str(json_path),
        ]
    )

    assert exit_code == 0
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["frames"][1]["actions"] == ["LeftButton"]


def test_finalize_command_copies_scored_metadata(tmp_path: Path) -> None:
    provisional_path = tmp_path / "provisional.osr"
    scored_path = tmp_path / "scored.osr"
    output_path = tmp_path / "final.osr"
    frames = (
        ReplayFrame(0, 256.0, 192.0, 0),
        ReplayFrame(100, 256.0, 192.0, LEGACY_Z_KEY),
    )
    make_replay(provisional_path, frames)
    provisional = OsrReplay.read_path(provisional_path)
    scored = OsrReplay(
        mode=provisional.mode,
        game_version=provisional.game_version,
        beatmap_md5=provisional.beatmap_md5,
        player_name=provisional.player_name,
        replay_md5=provisional.replay_md5,
        count_300=1,
        count_100=0,
        count_50=0,
        count_geki=0,
        count_katu=0,
        count_miss=1,
        score=1234,
        max_combo=1,
        perfect=False,
        mods=provisional.mods,
        life_bar_graph=provisional.life_bar_graph,
        timestamp=provisional.timestamp,
        frames=provisional.frames,
        online_score_id=999,
        trailing_bytes=encode_lazer_replay_metadata(
            {
                "client_version": "2026.624.0-lazer",
                "rank": "D",
                "user_id": 36703588,
                "online_id": 999,
                "mods": [],
                "statistics": {"great": 1, "miss": 1},
                "maximum_statistics": {"great": 2},
            }
        ),
    )
    scored.write_path(scored_path)

    exit_code = main(["finalize", str(provisional_path), str(scored_path), str(output_path)])

    assert exit_code == 0
    output = OsrReplay.read_path(output_path)
    assert output.count_300 == 1
    assert output.count_miss == 1
    assert output.score == 1234
