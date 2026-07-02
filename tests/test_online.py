import hashlib
import io
import zipfile
from pathlib import Path

import pytest

from synthesis_osu_play.online import (
    MAX_SCORE_REQUEST_LIMIT,
    BatchSynthesisItem,
    BatchSynthesisReport,
    LeaderboardScore,
    OnlineSynthesisError,
    batch_beatmap_archive_paths,
    batch_synthesize_dt,
    choose_batch_scores,
    default_lazer_game_ini,
    download_and_synthesize,
    extract_beatmap_file,
    ensure_beatmap_file,
    import_batch_beatmaps,
    leaderboard_score_at_index,
    parse_lazer_token,
    refresh_lazer_token,
    score_at_rank,
    update_ini_value,
)
from synthesis_osu_play.osr import OsrReplay, ReplayFrame
from synthesis_osu_play.synthesis import LEGACY_Z_KEY, SynthesisReport


def beatmap_bytes(beatmap_id: int = 123, set_id: int = 456) -> bytes:
    return f"""osu file format v14

[General]
AudioFilename: audio.mp3

[Metadata]
Title:test
Artist:test
Creator:test
Version:test
BeatmapID:{beatmap_id}
BeatmapSetID:{set_id}

[Difficulty]
HPDrainRate:5
CircleSize:5
OverallDifficulty:5
SliderMultiplier:1.4
SliderTickRate:1

[TimingPoints]
0,500,4,2,0,100,1,0

[HitObjects]
256,192,100,1,0,0:0:0:0:
""".encode("utf-8")


def replay_bytes(beatmap_md5: str) -> bytes:
    replay = OsrReplay(
        mode=0,
        game_version=20240101,
        beatmap_md5=beatmap_md5,
        player_name="source",
        replay_md5="hash",
        count_300=1,
        count_100=0,
        count_50=0,
        count_geki=0,
        count_katu=0,
        count_miss=0,
        score=12345,
        max_combo=1,
        perfect=True,
        mods=0,
        life_bar_graph="",
        timestamp=1,
        frames=(
            ReplayFrame(0, 256.0, 192.0, 0),
            ReplayFrame(100, 256.0, 192.0, LEGACY_Z_KEY),
            ReplayFrame(50, 256.0, 192.0, 0),
        ),
        online_score_id=0,
    )
    return replay.to_bytes()


def osz_bytes(name: str, data: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, mode="w") as archive:
        archive.writestr(name, data)
    return buffer.getvalue()


class FakeClient:
    def __init__(self) -> None:
        self.map = beatmap_bytes()
        self.map_md5 = hashlib.md5(self.map).hexdigest()
        self.replay = replay_bytes(self.map_md5)

    def get_beatmap(self, beatmap_id: int) -> dict[str, object]:
        return {
            "id": beatmap_id,
            "beatmapset_id": 456,
            "checksum": self.map_md5,
        }

    def get_scores(self, beatmap_id: int, **kwargs: object) -> dict[str, object]:
        return {
            "score_count": 2,
            "scores": [
                {
                    "id": 1001,
                    "user_id": 11,
                    "has_replay": True,
                    "mods": [{"acronym": "HD"}],
                    "user": {"username": "first"},
                },
                {
                    "id": 1002,
                    "user_id": 22,
                    "has_replay": True,
                    "mods": [{"acronym": "HD"}],
                    "user": {"username": "second"},
                },
            ],
        }

    def download_replay(self, score_id: int) -> bytes:
        return self.replay

    def download_beatmapset(self, beatmapset_id: int, *, no_video: bool = True) -> bytes:
        return osz_bytes("test.osu", self.map)


class FakeBatchClient(FakeClient):
    def __init__(self) -> None:
        super().__init__()
        self.map = beatmap_bytes(beatmap_id=10001, set_id=20001)
        self.map_md5 = hashlib.md5(self.map).hexdigest()
        self.replay = replay_bytes(self.map_md5)

    def search_beatmapsets(self, **kwargs: object) -> dict[str, object]:
        return {
            "beatmapsets": [
                {
                    "id": 20001,
                    "title": "batch map",
                    "ranked_date": "2026-06-20T00:00:00Z",
                    "beatmaps": [
                        {
                            "id": 10001,
                            "mode": "osu",
                            "difficulty_rating": 5.0,
                            "version": "Insane",
                            "checksum": self.map_md5,
                        }
                    ],
                }
            ]
        }

    def get_scores(self, beatmap_id: int, **kwargs: object) -> dict[str, object]:
        return {
            "scores": [
                {
                    "id": 30001,
                    "user_id": 11,
                    "has_replay": True,
                    "mods": [{"acronym": "DT"}],
                    "user": {"username": "dt1"},
                },
                {
                    "id": 30002,
                    "user_id": 22,
                    "has_replay": True,
                    "mods": [{"acronym": "NC"}],
                    "user": {"username": "dt2"},
                },
                {
                    "id": 30003,
                    "user_id": 33,
                    "has_replay": True,
                    "mods": [],
                    "user": {"username": "nm"},
                },
            ]
        }


class FakeResponse:
    def __init__(self, data: bytes) -> None:
        self.data = data

    def read(self) -> bytes:
        return self.data

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> object:
        return False


def test_parse_lazer_token_reads_lazer_pipe_format() -> None:
    token = parse_lazer_token("access-token|2000000000|refresh-token")

    assert token.access_token == "access-token"
    assert token.expiry == 2_000_000_000
    assert token.refresh_token == "refresh-token"


def test_refresh_lazer_token_uses_refresh_token() -> None:
    seen = {}

    def fake_urlopen(request):
        seen["url"] = request.full_url
        seen["data"] = request.data.decode("utf-8")
        return FakeResponse(b'{"access_token":"new-access","expires_in":3600,"refresh_token":"new-refresh"}')

    token = refresh_lazer_token(parse_lazer_token("old-access|1|old-refresh"), urlopen=fake_urlopen)

    assert token.access_token == "new-access"
    assert token.refresh_token == "new-refresh"
    assert "grant_type=refresh_token" in seen["data"]
    assert "refresh_token=old-refresh" in seen["data"]


def test_update_ini_value_replaces_existing_value(tmp_path: Path) -> None:
    config = tmp_path / "game.ini"
    config.write_text("Username = player\nToken = old\n", encoding="utf-8")

    update_ini_value(config, "Token", "new")

    assert config.read_text(encoding="utf-8") == "Username = player\nToken = new\n"


def test_default_lazer_game_ini_follows_storage_redirect(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    appdata = tmp_path / "appdata"
    osu_home = appdata / "osu"
    lazer_storage = tmp_path / "lazer"
    osu_home.mkdir(parents=True)
    lazer_storage.mkdir()
    (osu_home / "storage.ini").write_text(f"FullPath = {lazer_storage}\n", encoding="utf-8")
    (lazer_storage / "game.ini").write_text("Token = access|2000000000|refresh\n", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(appdata))

    assert default_lazer_game_ini() == lazer_storage / "game.ini"


def test_score_at_rank_reads_api_v2_score_shape() -> None:
    selected = score_at_rank(
        [
            {
                "id": "1001",
                "user_id": 11,
                "has_replay": True,
                "mods": [{"acronym": "DT"}, {"acronym": "HD"}],
                "user": {"username": "player"},
            }
        ],
        1,
    )

    assert selected.score_id == 1001
    assert selected.username == "player"
    assert selected.mods == ("DT", "HD")


def test_extract_beatmap_file_from_osz(tmp_path: Path) -> None:
    data = beatmap_bytes(beatmap_id=789)
    archive_path = tmp_path / "map.osz"
    output_path = tmp_path / "789.osu"
    archive_path.write_bytes(osz_bytes("nested/map.osu", data))

    extract_beatmap_file(archive_path, 789, output_path)

    assert output_path.read_bytes() == data


def test_ensure_beatmap_file_keeps_osz_available_when_osu_is_cached(tmp_path: Path) -> None:
    client = FakeClient()
    beatmap_path = tmp_path / "123.osu"
    beatmap_path.write_bytes(client.map)

    output = ensure_beatmap_file(
        client,
        123,
        456,
        tmp_path,
        checksum=client.map_md5,
        no_video=True,
    )

    assert output == beatmap_path
    assert (tmp_path / "beatmapset_456.osz").exists()


def test_download_and_synthesize_uses_downloaded_replays_and_beatmap(tmp_path: Path) -> None:
    output_path = tmp_path / "output.osr"
    json_path = tmp_path / "output.json"

    report = download_and_synthesize(
        123,
        1,
        2,
        output_path,
        work_dir=tmp_path / "work",
        player_name="synthetic",
        lazer_json_path=json_path,
        client=FakeClient(),
    )

    output = OsrReplay.read_path(output_path)
    assert output.player_name == "synthetic"
    assert report.first_score.score_id == 1001
    assert report.second_score.score_id == 1002
    assert report.beatmap_path.exists()
    assert report.first_replay_path.exists()
    assert report.second_replay_path.exists()
    assert report.leaderboard_path.exists()
    assert json_path.exists()


def test_download_and_synthesize_rejects_ranks_beyond_endpoint_limit(tmp_path: Path) -> None:
    with pytest.raises(OnlineSynthesisError, match=str(MAX_SCORE_REQUEST_LIMIT)):
        download_and_synthesize(123, 1, MAX_SCORE_REQUEST_LIMIT + 1, tmp_path / "output.osr", client=FakeClient())


def test_choose_batch_scores_prefers_dt_without_hr() -> None:
    scores = [
        leaderboard_score_at_index(
            1,
            {
                "id": 1,
                "has_replay": True,
                "mods": [{"acronym": "HR"}, {"acronym": "DT"}],
            },
        ),
        leaderboard_score_at_index(
            2,
            {
                "id": 2,
                "has_replay": True,
                "mods": [{"acronym": "DT"}],
            },
        ),
        leaderboard_score_at_index(
            3,
            {
                "id": 3,
                "has_replay": True,
                "mods": [{"acronym": "NC"}],
            },
        ),
        leaderboard_score_at_index(
            4,
            {
                "id": 4,
                "has_replay": True,
                "mods": [],
            },
        ),
    ]

    selected = choose_batch_scores(scores, __import__("random").Random(1))

    assert selected is not None
    first, second, pool = selected
    assert pool == "dt_no_hr"
    assert {first.rank, second.rank} == {2, 3}


def test_batch_synthesize_dt_writes_beatmap_id_outputs_and_manifest(tmp_path: Path) -> None:
    report = batch_synthesize_dt(
        1,
        output_dir=tmp_path / "exports",
        work_dir=tmp_path / "work",
        min_age_days=3,
        min_star=4.5,
        max_star=5.5,
        player_name="synthetic",
        request_delay_s=0,
        random_seed=1,
        client=FakeBatchClient(),
    )

    output = OsrReplay.read_path(tmp_path / "exports" / "10001.osr")

    assert output.player_name == "synthetic"
    assert output.mods == 64
    assert report.items[0].beatmap_id == 10001
    assert report.items[0].selection_pool == "dt_no_hr"
    assert report.manifest_path.exists()
    assert (tmp_path / "work" / "10001" / "beatmapset_20001.osz").exists()


def test_import_batch_beatmaps_launches_lazer_with_unique_archives(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    work_dir = tmp_path / "work"
    first_archive = work_dir / "123" / "beatmapset_456.osz"
    second_archive = work_dir / "456" / "beatmapset_456.osz"
    first_archive.parent.mkdir(parents=True)
    second_archive.parent.mkdir(parents=True)
    first_archive.write_bytes(b"first")
    second_archive.write_bytes(b"second")
    lazer_dir = tmp_path / "lazer"
    lazer_dir.mkdir()
    (lazer_dir / "osu!.exe").write_bytes(b"")

    report = BatchSynthesisReport(
        output_dir=tmp_path / "exports",
        work_dir=work_dir,
        manifest_path=tmp_path / "exports" / "batch_manifest.json",
        items=(
            BatchSynthesisItem(
                index=1,
                output_path=tmp_path / "exports" / "123.osr",
                beatmap_id=123,
                beatmapset_id=456,
                difficulty_rating=5.0,
                title="title",
                version="diff",
                ranked_date=__import__("datetime").datetime(2026, 6, 20),
                first_score=LeaderboardScore(1, 1001, 11, "first", ("DT",), True, {}),
                second_score=LeaderboardScore(2, 1002, 22, "second", ("DT",), True, {}),
                selection_pool="dt_no_hr",
                synthesis_report=SynthesisReport(3, 100, {LEGACY_Z_KEY: 1}),
            ),
            BatchSynthesisItem(
                index=2,
                output_path=tmp_path / "exports" / "456.osr",
                beatmap_id=456,
                beatmapset_id=456,
                difficulty_rating=5.0,
                title="title",
                version="diff",
                ranked_date=__import__("datetime").datetime(2026, 6, 20),
                first_score=LeaderboardScore(1, 1001, 11, "first", ("DT",), True, {}),
                second_score=LeaderboardScore(2, 1002, 22, "second", ("DT",), True, {}),
                selection_pool="dt_no_hr",
                synthesis_report=SynthesisReport(3, 100, {LEGACY_Z_KEY: 1}),
            ),
        ),
    )

    seen = {}

    class FakeProcess:
        returncode = 0

        def wait(self, timeout: float | None = None) -> int:
            seen["timeout"] = timeout
            return 0

    def fake_popen(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr("synthesis_osu_play.online.subprocess.Popen", fake_popen)

    assert batch_beatmap_archive_paths(report) == (first_archive,)

    imported = import_batch_beatmaps(report, lazer_path=lazer_dir)

    assert imported == (first_archive,)
    assert seen["command"][0] == str(lazer_dir / "osu!.exe")
    assert seen["command"][1:] == [str(first_archive)]
    assert seen["kwargs"]["cwd"] == str(lazer_dir)
