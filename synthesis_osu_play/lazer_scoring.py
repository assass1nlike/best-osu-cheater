from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class LazerScoringError(RuntimeError):
    pass


@dataclass(frozen=True)
class JudgementIssue:
    slider_part: str | None
    hit_object_type: str
    result: str
    object_time_ms: float
    judgement_time_ms: float
    time_offset_ms: float
    combo_before: int
    combo_after: int

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> JudgementIssue:
        return cls(
            slider_part=value.get("slider_part"),
            hit_object_type=str(value["hit_object_type"]),
            result=str(value["result"]),
            object_time_ms=float(value["object_time_ms"]),
            judgement_time_ms=float(value["judgement_time_ms"]),
            time_offset_ms=float(value["time_offset_ms"]),
            combo_before=int(value["combo_before"]),
            combo_after=int(value["combo_after"]),
        )


@dataclass(frozen=True)
class LazerScoreReport:
    is_fc: bool
    score: int
    max_combo: int
    maximum_combo: int
    rank: str
    accuracy: float
    passed: bool
    count_300: int
    count_100: int
    count_50: int
    count_miss: int
    statistics: dict[str, int]
    slider_breaks: tuple[JudgementIssue, ...]
    other_combo_breaks: tuple[JudgementIssue, ...]

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> LazerScoreReport:
        return cls(
            is_fc=bool(value["is_fc"]),
            score=int(value["score"]),
            max_combo=int(value["max_combo"]),
            maximum_combo=int(value["maximum_combo"]),
            rank=str(value["rank"]),
            accuracy=float(value["accuracy"]),
            passed=bool(value["passed"]),
            count_300=int(value["count_300"]),
            count_100=int(value["count_100"]),
            count_50=int(value["count_50"]),
            count_miss=int(value["count_miss"]),
            statistics={str(key): int(count) for key, count in value["statistics"].items()},
            slider_breaks=tuple(JudgementIssue.from_json(item) for item in value["slider_breaks"]),
            other_combo_breaks=tuple(
                JudgementIssue.from_json(item) for item in value["other_combo_breaks"]
            ),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "is_fc": self.is_fc,
            "score": self.score,
            "max_combo": self.max_combo,
            "maximum_combo": self.maximum_combo,
            "rank": self.rank,
            "accuracy": self.accuracy,
            "passed": self.passed,
            "count_300": self.count_300,
            "count_100": self.count_100,
            "count_50": self.count_50,
            "count_miss": self.count_miss,
            "statistics": self.statistics,
            "slider_breaks": [issue.__dict__ for issue in self.slider_breaks],
            "other_combo_breaks": [issue.__dict__ for issue in self.other_combo_breaks],
        }


def score_replay_with_lazer(
    replay_path: str | Path,
    beatmap_path: str | Path,
    *,
    lazer_path: str | Path | None = None,
) -> LazerScoreReport:
    replay = Path(replay_path).resolve()
    beatmap = Path(beatmap_path).resolve()
    if not replay.is_file():
        raise LazerScoringError(f"replay not found: {replay}")
    if not beatmap.is_file():
        raise LazerScoringError(f"beatmap not found: {beatmap}")

    resolved_lazer_path = resolve_lazer_path(lazer_path)
    scorer = ensure_lazer_scorer(resolved_lazer_path)
    with tempfile.TemporaryDirectory(prefix="synthesis-osu-fc-") as temp_dir:
        temp = Path(temp_dir)
        scored_replay = temp / "scored.osr"
        json_report = temp / "report.json"
        completed = subprocess.run(
            [
                str(scorer),
                str(beatmap),
                str(replay),
                str(scored_replay),
                str(resolved_lazer_path),
                "--json",
                str(json_report),
            ],
            cwd=temp,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=_no_window_creation_flag(),
            check=False,
        )
        if completed.returncode != 0:
            details = completed.stderr.strip() or completed.stdout.strip()
            raise LazerScoringError(
                f"osu!lazer scorer exited with code {completed.returncode}"
                + (f": {details}" if details else "")
            )
        try:
            payload = json.loads(json_report.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise LazerScoringError(f"could not read scorer report: {exc}") from exc
    try:
        return LazerScoreReport.from_json(payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise LazerScoringError(f"invalid scorer report: {exc}") from exc


def resolve_lazer_path(explicit_path: str | Path | None = None) -> Path:
    if explicit_path is not None:
        candidate = Path(explicit_path).expanduser()
    elif os.environ.get("OSU_LAZER_PATH"):
        candidate = Path(os.environ["OSU_LAZER_PATH"])
    elif os.environ.get("LOCALAPPDATA"):
        candidate = Path(os.environ["LOCALAPPDATA"]) / "osulazer" / "current"
    else:
        raise LazerScoringError("pass --lazer-path or set OSU_LAZER_PATH")
    if candidate.is_file():
        candidate = candidate.parent
    candidate = candidate.resolve()
    if not (candidate / "osu.Game.dll").is_file():
        raise LazerScoringError(f"osu!lazer installation not found: {candidate}")
    return candidate


def ensure_lazer_scorer(lazer_path: Path) -> Path:
    root = Path(__file__).resolve().parents[1]
    project = root / "tools" / "LazerReplayScorer" / "LazerReplayScorer.csproj"
    executable = project.parent / "bin" / "Release" / "net8.0" / "LazerReplayScorer.exe"
    runtime_config = executable.with_suffix(".runtimeconfig.json")
    sources = (
        project,
        project.parent / "Program.cs",
        lazer_path / "osu.Game.dll",
        lazer_path / "osu.Game.Rulesets.Osu.dll",
        lazer_path / "osu.Framework.dll",
    )
    if (
        executable.is_file()
        and runtime_config.is_file()
        and executable.stat().st_mtime >= max(path.stat().st_mtime for path in sources)
    ):
        return executable

    completed = subprocess.run(
        [
            "dotnet",
            "build",
            str(project),
            "--configuration",
            "Release",
            "--nologo",
            "--no-incremental",
            f"-p:OsuLazerPath={lazer_path}",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=_no_window_creation_flag(),
        check=False,
    )
    if completed.returncode != 0 or not executable.is_file() or not runtime_config.is_file():
        details = completed.stderr.strip() or completed.stdout.strip()
        raise LazerScoringError("could not build LazerReplayScorer" + (f": {details}" if details else ""))
    return executable


def _no_window_creation_flag() -> int:
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)
