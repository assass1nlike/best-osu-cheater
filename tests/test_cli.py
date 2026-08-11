import json
from pathlib import Path

import pytest

import synthesis_osu_play.cli as cli_module
from synthesis_osu_play.cli import main
from synthesis_osu_play.lazer_scoring import JudgementIssue, LazerScoreReport
from synthesis_osu_play.online import BatchSynthesisItem, BatchSynthesisReport, LeaderboardScore, OnlineSynthesisReport
from synthesis_osu_play.osr import OsrReplay, ReplayFrame, encode_lazer_replay_metadata
from synthesis_osu_play.synthesis import LEGACY_Z_KEY, SynthesisReport, effective_weights


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


def test_parser_exposes_playback_controls(tmp_path: Path) -> None:
    parser = cli_module.build_parser()

    synthesize = parser.parse_args([
        "synthesize",
        "first.osr",
        "second.osr",
        "output.osr",
        "--no-dt",
        "--synthesis-seed",
        "7",
    ])
    assert synthesize.dt is False
    assert synthesize.synthesis_seed == 7

    batch = parser.parse_args([
        "batch-dt",
        "1",
        "--no-skip-filter",
        "--random-search-pages",
        "7",
    ])
    assert batch.no_skip_filter is True
    assert batch.min_skip_time == 0
    assert batch.spinner_mode == "all"
    assert batch.dt is True
    assert batch.beatmap_selection == "random-all"
    assert batch.random_search_pages == 7

    download = parser.parse_args([
        "download-synthesize",
        "123",
        "1",
        "2",
        "output.osr",
        "--lazer-path",
        str(tmp_path / "lazer"),
        "--synthesis-seed",
        "8",
    ])
    assert download.lazer_path == tmp_path / "lazer"
    assert download.synthesis_seed == 8

    fc_check = parser.parse_args([
        "fc-check",
        "replay.osr",
        "--beatmap",
        "map.osu",
        "--lazer-path",
        str(tmp_path / "lazer"),
    ])
    assert fc_check.replay == Path("replay.osr")
    assert fc_check.beatmap == Path("map.osu")
    assert fc_check.lazer_path == tmp_path / "lazer"


def test_synthesize_command_writes_readable_osr(tmp_path: Path) -> None:
    first_path = tmp_path / "first.osr"
    second_path = tmp_path / "second.osr"
    output_path = tmp_path / "output.osr"
    make_replay(
        first_path,
        (
            ReplayFrame(0, 0.0, 0.0, 0),
            ReplayFrame(10, 0.0, 0.0, 1),
            ReplayFrame(10, 0.0, 0.0, 0),
        ),
    )
    make_replay(
        second_path,
        (
            ReplayFrame(0, 20.0, 10.0, 0),
            ReplayFrame(20, 20.0, 10.0, 1),
            ReplayFrame(20, 20.0, 10.0, 0),
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


def test_synthesize_command_accepts_weights(tmp_path: Path) -> None:
    first_path = tmp_path / "first.osr"
    second_path = tmp_path / "second.osr"
    output_path = tmp_path / "output.osr"
    make_replay(
        first_path,
        (
            ReplayFrame(0, 0.0, 0.0, 0),
            ReplayFrame(10, 0.0, 0.0, 1),
            ReplayFrame(10, 0.0, 0.0, 0),
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
            "--first-weight",
            "3",
            "--second-weight",
            "1",
            "--synthesis-seed",
            "0",
        ]
    )

    assert exit_code == 0
    output = OsrReplay.read_path(output_path)
    from synthesis_osu_play.synthesis import DYNAMIC_PHASE_NOISE_STD_RAD
    import random

    phase_offset = random.Random(0).gauss(0.0, DYNAMIC_PHASE_NOISE_STD_RAD)
    first_weight, second_weight = effective_weights(10, 3.0, 1.0, phase_offset_rad=phase_offset)
    assert output.frames[1].x == pytest.approx(10.0 * second_weight)
    assert output.frames[1].y == pytest.approx(10.0 * second_weight)


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


def test_download_synthesize_command_invokes_online_workflow(tmp_path: Path, monkeypatch) -> None:
    output_path = tmp_path / "output.osr"
    work_dir = tmp_path / "work"
    captured = {}

    def fake_download_and_synthesize(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        output_path.write_bytes(b"not a real replay")
        return OnlineSynthesisReport(
            output_path=output_path,
            beatmap_path=work_dir / "123.osu",
            first_replay_path=work_dir / "rank1_1001.osr",
            second_replay_path=work_dir / "rank2_1002.osr",
            leaderboard_path=work_dir / "leaderboard_global.json",
            first_score=LeaderboardScore(1, 1001, 11, "first", ("HD",), True, {}),
            second_score=LeaderboardScore(2, 1002, 22, "second", ("HD",), True, {}),
            synthesis_report=SynthesisReport(
                frame_count=3,
                duration_ms=100,
                key_interval_counts={LEGACY_Z_KEY: 1},
                matched_object_count=1,
                dropped_object_count=0,
            ),
            debug_video_path=kwargs.get("debug_video_path"),
        )

    monkeypatch.setattr(cli_module, "download_and_synthesize", fake_download_and_synthesize)

    exit_code = main(
        [
            "download-synthesize",
            "123",
            "1",
            "2",
            str(output_path),
            "--work-dir",
            str(work_dir),
            "--player-name",
            "synthetic",
            "--first-weight",
            "0.3",
            "--second-weight",
            "0.7",
            "--debug-video",
            str(tmp_path / "debug.mp4"),
        ]
    )

    assert exit_code == 0
    assert captured["args"][:4] == (123, 1, 2, output_path)
    assert captured["kwargs"]["work_dir"] == work_dir
    assert captured["kwargs"]["player_name"] == "synthetic"
    assert captured["kwargs"]["first_weight"] == 0.3
    assert captured["kwargs"]["second_weight"] == 0.7
    assert captured["kwargs"]["debug_video_path"] == tmp_path / "debug.mp4"


def test_batch_dt_command_invokes_batch_workflow(tmp_path: Path, monkeypatch) -> None:
    captured = {}

    def fake_batch_synthesize_dt(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        score = LeaderboardScore(1, 1001, 11, "player", ("DT",), True, {})
        item = BatchSynthesisItem(
            index=1,
            output_path=tmp_path / "exports" / "10001.osr",
            beatmap_id=123,
            beatmapset_id=456,
            difficulty_rating=5.0,
            title="title",
            version="diff",
            ranked_date=__import__("datetime").datetime(2026, 6, 20),
            first_score=score,
            second_score=score,
            selection_pool="dt_no_hr",
            synthesis_report=SynthesisReport(3, 100, {LEGACY_Z_KEY: 1}),
        )
        return BatchSynthesisReport(
            output_dir=kwargs["output_dir"],
            work_dir=kwargs["work_dir"],
            manifest_path=kwargs["output_dir"] / "batch_manifest.json",
            items=(item,),
        )

    def fake_import_batch_beatmaps(report, *, lazer_path=None):
        captured["import_report"] = report
        captured["lazer_path"] = lazer_path
        return (tmp_path / "work" / "123" / "beatmapset_456.osz",)

    monkeypatch.setattr(cli_module, "batch_synthesize_dt", fake_batch_synthesize_dt)
    monkeypatch.setattr(cli_module, "import_batch_beatmaps", fake_import_batch_beatmaps)

    exit_code = main(
        [
            "batch-dt",
            "1",
            "--output-dir",
            str(tmp_path / "exports"),
            "--work-dir",
            str(tmp_path / "work"),
            "--min-age-days",
            "5",
            "--min-star",
            "4.8",
            "--max-star",
            "5.2",
            "--random-seed",
            "7",
            "--random-search-pages",
            "4",
            "--lazer-path",
            str(tmp_path / "lazer"),
        ]
    )

    assert exit_code == 0
    assert captured["args"] == (1,)
    assert captured["kwargs"]["output_dir"] == tmp_path / "exports"
    assert captured["kwargs"]["work_dir"] == tmp_path / "work"
    assert captured["kwargs"]["min_age_days"] == 5
    assert captured["kwargs"]["min_star"] == 4.8
    assert captured["kwargs"]["max_star"] == 5.2
    assert captured["kwargs"]["random_seed"] == 7
    assert captured["kwargs"]["random_search_pages"] == 4
    assert captured["import_report"].output_dir == tmp_path / "exports"
    assert captured["lazer_path"] == tmp_path / "lazer"


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


def test_fc_check_command_reports_fc(tmp_path: Path, monkeypatch, capsys) -> None:
    report = LazerScoreReport(
        is_fc=True,
        score=1_000_000,
        max_combo=100,
        maximum_combo=100,
        rank="X",
        accuracy=1.0,
        passed=True,
        count_300=80,
        count_100=0,
        count_50=0,
        count_miss=0,
        statistics={"great": 80},
        slider_breaks=(),
        other_combo_breaks=(),
    )
    monkeypatch.setattr(cli_module, "score_replay_with_lazer", lambda *args, **kwargs: report)

    exit_code = main(["fc-check", "replay.osr", "--beatmap", "map.osu"])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "FC: YES" in output
    assert "300/100/50/miss: 80/0/0/0" in output
    assert "combo: 100/100" in output


def test_fc_check_command_reports_slider_break_and_writes_json(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    issue = JudgementIssue(
        slider_part="tick",
        hit_object_type="SliderTick",
        result="LargeTickMiss",
        object_time_ms=1234.5,
        judgement_time_ms=1234.5,
        time_offset_ms=0.0,
        combo_before=12,
        combo_after=0,
    )
    report = LazerScoreReport(
        is_fc=False,
        score=900_000,
        max_combo=50,
        maximum_combo=100,
        rank="A",
        accuracy=0.97,
        passed=True,
        count_300=79,
        count_100=1,
        count_50=0,
        count_miss=0,
        statistics={"great": 79, "ok": 1, "large_tick_miss": 1},
        slider_breaks=(issue,),
        other_combo_breaks=(),
    )
    monkeypatch.setattr(cli_module, "score_replay_with_lazer", lambda *args, **kwargs: report)
    json_path = tmp_path / "reports" / "fc.json"

    exit_code = main(
        ["fc-check", "replay.osr", "--beatmap", "map.osu", "--json", str(json_path)]
    )

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "FC: NO" in output
    assert "1234.5ms tick: LargeTickMiss" in output
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["slider_breaks"][0]["slider_part"] == "tick"
