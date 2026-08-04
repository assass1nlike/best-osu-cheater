from __future__ import annotations

import json
import logging
import os
import random
import ssl
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from .beatmap import Beatmap
from .lazer import replay_to_lazer_export, write_lazer_json
from .mods import MOD_DOUBLE_TIME
from .osr import OsrReplay
from .synthesis import SynthesisReport, synthesize_replays
from .visualize import write_replay_debug_video


PRODUCTION_API_URL = "https://osu.ppy.sh"
PRODUCTION_CLIENT_ID = "5"
PRODUCTION_CLIENT_SECRET = "FGc9GAtyHzeQDshWP5Ah7dega8hJACAJpQtw6OXk"
DEVELOPMENT_API_URL = "https://dev.ppy.sh"
DEVELOPMENT_CLIENT_SECRET = "3LP2mhUrV89xxzD1YKNndXHEhWWCRLPNKioZ9ymT"
MAX_SCORE_REQUEST_LIMIT = 100
DEFAULT_SCORE_REQUEST_LIMIT = 50
DEFAULT_BATCH_OUTPUT_DIR = Path(r"D:\osu-lazer\exports")
DEFAULT_BATCH_WORK_DIR = Path("artifacts") / "batch-dt"
API_REQUEST_TIMEOUT_S = 30
BATCH_SUPPORTED_SOURCE_MODS = frozenset(
    {
        "EZ",
        "NF",
        "HT",
        "DC",
        "HR",
        "SD",
        "PF",
        "DT",
        "NC",
        "HD",
        "TC",
        "FL",
        "BL",
        "ST",
        "AC",
        "CL",
    }
)

LOGGER = logging.getLogger(__name__)


def api_ssl_context() -> ssl.SSLContext:
    """Create the TLS context used for osu! API connections.

    Some osu!/Cloudflare connections close without sending TLS close-notify.
    OpenSSL 3 treats that as a protocol error unless this compatibility option
    is enabled. Certificate and hostname verification remain enabled.
    """
    context = ssl.create_default_context()
    ignore_unexpected_eof = getattr(ssl, "OP_IGNORE_UNEXPECTED_EOF", 0)
    if ignore_unexpected_eof:
        context.options |= ignore_unexpected_eof
    return context


_API_OPENER = urllib.request.build_opener(
    urllib.request.HTTPSHandler(context=api_ssl_context()),
)


def api_urlopen(request: urllib.request.Request) -> HttpResponse:
    return _API_OPENER.open(request, timeout=API_REQUEST_TIMEOUT_S)


class OnlineSynthesisError(ValueError):
    """Raised when online replay download or one-shot synthesis cannot continue."""


class HttpResponse(Protocol):
    def read(self) -> bytes:
        ...

    def __enter__(self) -> "HttpResponse":
        ...

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> object:
        ...


UrlOpen = Callable[[urllib.request.Request], HttpResponse]


@dataclass(frozen=True)
class LazerOAuthToken:
    access_token: str
    expiry: int | None = None
    refresh_token: str | None = None

    @property
    def is_expired(self) -> bool:
        return self.expiry is not None and self.expiry <= int(time.time()) + 30

    def to_config_string(self) -> str:
        parts = [self.access_token, "" if self.expiry is None else str(self.expiry)]
        if self.refresh_token is not None:
            parts.append(self.refresh_token)
        return "|".join(parts)


@dataclass(frozen=True)
class LeaderboardScore:
    rank: int
    score_id: int
    user_id: int | None
    username: str | None
    mods: tuple[str, ...]
    has_replay: bool
    raw: dict[str, Any]


@dataclass(frozen=True)
class OnlineSynthesisReport:
    output_path: Path
    beatmap_path: Path
    first_replay_path: Path
    second_replay_path: Path
    leaderboard_path: Path
    first_score: LeaderboardScore
    second_score: LeaderboardScore
    synthesis_report: SynthesisReport
    lazer_json_path: Path | None = None
    debug_video_path: Path | None = None
    work_dir: Path | None = None


@dataclass(frozen=True)
class BeatmapSearchCandidate:
    beatmapset_id: int
    beatmap_id: int
    ranked_date: datetime
    difficulty_rating: float
    title: str
    version: str
    checksum: str | None
    raw_beatmapset: dict[str, Any]
    raw_beatmap: dict[str, Any]


@dataclass(frozen=True)
class BatchSynthesisItem:
    index: int
    output_path: Path
    beatmap_id: int
    beatmapset_id: int
    difficulty_rating: float
    title: str
    version: str
    ranked_date: datetime
    first_score: LeaderboardScore
    second_score: LeaderboardScore
    selection_pool: str
    synthesis_report: SynthesisReport


@dataclass(frozen=True)
class BatchSynthesisReport:
    output_dir: Path
    work_dir: Path
    manifest_path: Path
    items: tuple[BatchSynthesisItem, ...]


class OsuApiClient:
    def __init__(
        self,
        access_token: str,
        *,
        api_url: str = PRODUCTION_API_URL,
        api_version: int | None = None,
        request_delay_s: float = 0.0,
        urlopen: UrlOpen | None = None,
    ) -> None:
        self.access_token = access_token
        self.api_url = api_url.rstrip("/")
        self.api_version = api_version if api_version is not None else int(datetime.now().strftime("%Y%m%d"))
        self.request_delay_s = request_delay_s
        self.urlopen = urlopen or api_urlopen
        self._last_request_at = 0.0

    def get_beatmap(self, beatmap_id: int) -> dict[str, Any]:
        payload = self.get_json("beatmaps/lookup", {"id": beatmap_id})
        if not isinstance(payload, dict):
            raise OnlineSynthesisError("beatmap lookup returned a non-object response")
        return payload

    def get_scores(
        self,
        beatmap_id: int,
        *,
        scope: str = "global",
        ruleset: str = "osu",
        limit: int = DEFAULT_SCORE_REQUEST_LIMIT,
        mods: Iterable[str] = (),
    ) -> dict[str, Any]:
        limit = max(1, min(MAX_SCORE_REQUEST_LIMIT, limit))
        query: dict[str, object] = {
            "type": scope,
            "mode": ruleset,
            "limit": limit,
        }
        mod_list = tuple(mods)
        if mod_list:
            query["mods[]"] = mod_list
        payload = self.get_json(f"beatmaps/{beatmap_id}/scores", query)
        if not isinstance(payload, dict):
            raise OnlineSynthesisError("leaderboard request returned a non-object response")
        return payload

    def search_beatmapsets(
        self,
        *,
        mode: str = "osu",
        status: str = "ranked",
        sort: str = "ranked_desc",
        cursor_string: str | None = None,
    ) -> dict[str, Any]:
        query: dict[str, object] = {
            "m": mode_id(mode),
            "s": status,
            "sort": sort,
        }
        if cursor_string:
            query["cursor_string"] = cursor_string
        payload = self.get_json("beatmapsets/search", query)
        if not isinstance(payload, dict):
            raise OnlineSynthesisError("beatmapset search returned a non-object response")
        return payload

    def download_replay(self, score_id: int) -> bytes:
        return self.get_bytes(f"scores/{score_id}/download")

    def download_beatmapset(self, beatmapset_id: int, *, no_video: bool = True) -> bytes:
        query = {"noVideo": "1"} if no_video else None
        return self.get_bytes(f"beatmapsets/{beatmapset_id}/download", query)

    def get_json(self, target: str, query: dict[str, object] | None = None) -> object:
        data = self.get_bytes(target, query)
        try:
            return json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise OnlineSynthesisError(f"invalid JSON response for {target}") from exc

    def get_bytes(self, target: str, query: dict[str, object] | None = None, *, _retries: int = 3) -> bytes:
        request = urllib.request.Request(self.url(target, query), headers=self.headers)
        self.wait_for_rate_limit()
        try:
            with self.urlopen(request) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and _retries > 0:
                wait = 5.0 * (4 - _retries)
                LOGGER.warning("rate limited, retrying in %.0fs...", wait)
                time.sleep(wait)
                self._last_request_at = time.monotonic()
                return self.get_bytes(target, query, _retries=_retries - 1)
            message = exc.reason
            try:
                body = exc.read().decode("utf-8", errors="replace")
            except OSError:
                body = ""
            if body:
                message = f"{message}: {body}"
            raise OnlineSynthesisError(f"osu! API request failed ({exc.code}) for {target}: {message}") from exc
        except urllib.error.URLError as exc:
            if _retries > 0 and is_transient_network_error(exc.reason):
                wait = float(4 - _retries)
                LOGGER.warning(
                    "transient osu! API connection error for %s; retrying in %.0fs...",
                    target,
                    wait,
                )
                time.sleep(wait)
                self._last_request_at = time.monotonic()
                return self.get_bytes(target, query, _retries=_retries - 1)
            raise OnlineSynthesisError(f"osu! API request failed for {target}: {exc.reason}") from exc
        except (ssl.SSLError, ConnectionError, TimeoutError) as exc:
            if _retries > 0:
                wait = float(4 - _retries)
                LOGGER.warning(
                    "transient osu! API connection error for %s; retrying in %.0fs...",
                    target,
                    wait,
                )
                time.sleep(wait)
                self._last_request_at = time.monotonic()
                return self.get_bytes(target, query, _retries=_retries - 1)
            raise OnlineSynthesisError(f"osu! API request failed for {target}: {exc}") from exc

    def wait_for_rate_limit(self) -> None:
        if self.request_delay_s <= 0:
            return
        now = time.monotonic()
        elapsed = now - self._last_request_at
        if elapsed < self.request_delay_s:
            time.sleep(self.request_delay_s - elapsed)
        self._last_request_at = time.monotonic()

    def url(self, target: str, query: dict[str, object] | None = None) -> str:
        url = f"{self.api_url}/api/v2/{target.lstrip('/')}"
        if not query:
            return url
        items: list[tuple[str, str]] = []
        for key, value in query.items():
            if isinstance(value, (tuple, list)):
                items.extend((key, str(item)) for item in value)
            else:
                items.append((key, str(value)))
        return f"{url}?{urllib.parse.urlencode(items)}"

    @property
    def headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Accept-Language": "en",
            "User-Agent": "synthesis-osu-play/0.1",
            "x-api-version": str(self.api_version),
        }


def download_and_synthesize(
    beatmap_id: int,
    first_rank: int,
    second_rank: int,
    output_path: str | Path,
    *,
    work_dir: str | Path | None = None,
    player_name: str | None = None,
    first_weight: float = 1.0,
    second_weight: float = 1.0,
    scope: str = "global",
    ruleset: str = "osu",
    mods: Iterable[str] = (),
    no_video: bool = True,
    lazer_storage: str | Path | None = None,
    token_config: str | Path | None = None,
    api_url: str = PRODUCTION_API_URL,
    api_version: int | None = None,
    leaderboard_limit: int | None = None,
    lazer_json_path: str | Path | None = None,
    recompute_score_metadata: bool = False,
    output_mods: int | None = None,
    debug_video_path: str | Path | None = None,
    debug_video_start_ms: int | None = None,
    debug_video_end_ms: int | None = None,
    debug_video_fps: float = 60.0,
    debug_video_speed: float = 1.0,
    debug_video_size: tuple[int, int] = (1280, 720),
    client: OsuApiClient | None = None,
    spinner_library_path: str | None = None,
    sequential_match: bool = True,
    dt_mode: bool = False,
    spinner_mode: str = "all",
) -> OnlineSynthesisReport:
    validate_rank(first_rank)
    validate_rank(second_rank)
    if max(first_rank, second_rank) > MAX_SCORE_REQUEST_LIMIT:
        raise OnlineSynthesisError(
            f"rank {max(first_rank, second_rank)} is not supported by the osu!lazer beatmap score endpoint "
            f"(maximum {MAX_SCORE_REQUEST_LIMIT})"
        )

    output_path = Path(output_path)
    work_dir = Path(work_dir) if work_dir is not None else Path("artifacts") / str(beatmap_id)
    work_dir.mkdir(parents=True, exist_ok=True)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if client is None:
        config_path = Path(token_config) if token_config is not None else default_lazer_game_ini(lazer_storage)
        token = read_lazer_token_path(config_path)
        if token.is_expired:
            token = refresh_lazer_token(token, api_url=api_url)
            update_ini_value(config_path, "Token", token.to_config_string())
        client = OsuApiClient(token.access_token, api_url=api_url, api_version=api_version)

    beatmap_info = client.get_beatmap(beatmap_id)
    beatmapset_id = int_field(beatmap_info, "beatmapset_id")
    checksum = string_field(beatmap_info, "checksum", required=False)

    limit = leaderboard_limit if leaderboard_limit is not None else max(DEFAULT_SCORE_REQUEST_LIMIT, first_rank, second_rank)
    leaderboard = client.get_scores(beatmap_id, scope=scope, ruleset=ruleset, limit=limit, mods=mods)
    leaderboard_path = work_dir / f"leaderboard_{scope}.json"
    leaderboard_path.write_text(json.dumps(leaderboard, ensure_ascii=False, indent=2), encoding="utf-8")

    scores = leaderboard_scores(leaderboard)
    first_score = score_at_rank(scores, first_rank)
    second_score = score_at_rank(scores, second_rank)

    first_replay_path = download_replay_if_needed(client, first_score, work_dir)
    second_replay_path = download_replay_if_needed(client, second_score, work_dir)
    beatmap_path = ensure_beatmap_file(
        client,
        beatmap_id,
        beatmapset_id,
        work_dir,
        checksum=checksum,
        no_video=no_video,
    )

    first = OsrReplay.read_path(first_replay_path)
    second = OsrReplay.read_path(second_replay_path)
    beatmap = Beatmap.read_path(beatmap_path)
    result = synthesize_replays(
        first,
        second,
        beatmap=beatmap,
        player_name=player_name,
        first_weight=first_weight,
        second_weight=second_weight,
        recompute_score_metadata=recompute_score_metadata,
        output_mods=output_mods,
        spinner_library_path=spinner_library_path,
        sequential_match=sequential_match,
        dt_mode=dt_mode,
        spinner_mode=spinner_mode,
    )
    result.replay.write_path(output_path)

    lazer_json_output = Path(lazer_json_path) if lazer_json_path is not None else None
    if lazer_json_output is not None:
        lazer_json_output.parent.mkdir(parents=True, exist_ok=True)
        write_lazer_json(
            replay_to_lazer_export(
                result.replay,
                skip_press_ms=result.report.skip_press_ms,
                skip_target_ms=result.report.intro_end_ms,
            ),
            lazer_json_output,
        )

    debug_video_output = Path(debug_video_path) if debug_video_path is not None else None
    if debug_video_output is not None:
        width, height = debug_video_size
        write_replay_debug_video(
            first,
            second,
            debug_video_output,
            beatmap=beatmap,
            synthesized=result.replay,
            player_name=player_name,
            first_weight=first_weight,
            second_weight=second_weight,
            start_ms=debug_video_start_ms,
            end_ms=debug_video_end_ms,
            fps=debug_video_fps,
            speed=debug_video_speed,
            width=width,
            height=height,
        )

    return OnlineSynthesisReport(
        output_path=output_path,
        beatmap_path=beatmap_path,
        first_replay_path=first_replay_path,
        second_replay_path=second_replay_path,
        leaderboard_path=leaderboard_path,
        first_score=first_score,
        second_score=second_score,
        synthesis_report=result.report,
        lazer_json_path=lazer_json_output,
        debug_video_path=debug_video_output,
        work_dir=work_dir,
    )


def batch_synthesize_dt(
    count: int,
    *,
    output_dir: str | Path = DEFAULT_BATCH_OUTPUT_DIR,
    work_dir: str | Path = DEFAULT_BATCH_WORK_DIR,
    min_age_days: int = 3,
    min_star: float = 4.5,
    max_star: float = 5.0,
    player_name: str | None = None,
    scope: str = "global",
    ruleset: str = "osu",
    no_video: bool = True,
    lazer_storage: str | Path | None = None,
    token_config: str | Path | None = None,
    api_url: str = PRODUCTION_API_URL,
    api_version: int | None = None,
    leaderboard_limit: int = MAX_SCORE_REQUEST_LIMIT,
    search_pages_limit: int = 25,
    request_delay_s: float = 2.0,
    random_seed: int | None = None,
    min_skip_time_ms: int = 4000,
    spinner_library_path: str | Path | None = None,
    spinner_mode: str = "all",
    dt_mode: bool = True,
    client: OsuApiClient | None = None,
) -> BatchSynthesisReport:
    if count < 1:
        raise OnlineSynthesisError("batch count must be positive")
    if min_age_days < 0:
        raise OnlineSynthesisError("minimum age must be non-negative")
    if min_star > max_star:
        raise OnlineSynthesisError("minimum star rating cannot exceed maximum star rating")
    leaderboard_limit = max(2, min(MAX_SCORE_REQUEST_LIMIT, leaderboard_limit))

    output_dir = Path(output_dir)
    work_dir = Path(work_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    if client is None:
        config_path = Path(token_config) if token_config is not None else default_lazer_game_ini(lazer_storage)
        token = read_lazer_token_path(config_path)
        if token.is_expired:
            token = refresh_lazer_token(token, api_url=api_url)
            update_ini_value(config_path, "Token", token.to_config_string())
        client = OsuApiClient(
            token.access_token,
            api_url=api_url,
            api_version=api_version,
            request_delay_s=request_delay_s,
        )

    rng = random.Random(random_seed)
    cutoff = datetime.now(timezone.utc).timestamp() - min_age_days * 24 * 60 * 60
    items: list[BatchSynthesisItem] = []
    seen_beatmap_ids: set[int] = set()
    cursor_string: str | None = None
    pages = 0

    if min_skip_time_ms < 0:
        raise OnlineSynthesisError("minimum skip time must be non-negative")

    LOGGER.info(
        "Searching ranked beatmaps (>= %sd old, %s-%s*), skip>= %sms...",
        min_age_days,
        min_star,
        max_star,
        min_skip_time_ms,
    )
    while len(items) < count and pages < search_pages_limit:
        pages += 1
        payload = client.search_beatmapsets(
            mode=ruleset,
            status="ranked",
            sort="ranked_desc",
            cursor_string=cursor_string,
        )
        (work_dir / f"search_page_{pages}.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        candidates = search_candidates_from_payload(payload, min_star=min_star, max_star=max_star)
        LOGGER.info("page %s: %s candidates in star range, %s/%s collected", pages, len(candidates), len(items), count)
        for candidate in candidates:
            if len(items) >= count:
                break
            if candidate.beatmap_id in seen_beatmap_ids:
                continue
            seen_beatmap_ids.add(candidate.beatmap_id)
            if candidate.ranked_date.timestamp() > cutoff:
                continue
            candidate_label = (
                f"[{len(items)+1}/{count}] beatmap {candidate.beatmap_id}: "
                f"{candidate.title[:40]} [{candidate.version[:20]}] ({candidate.difficulty_rating:.2f}*)"
            )
            try:
                item = try_synthesize_batch_candidate(
                    client,
                    candidate,
                    len(items) + 1,
                    output_dir=output_dir,
                    work_dir=work_dir,
                    player_name=player_name,
                    scope=scope,
                    ruleset=ruleset,
                    no_video=no_video,
                    leaderboard_limit=leaderboard_limit,
                    rng=rng,
                    min_skip_time_ms=min_skip_time_ms,
                    spinner_library_path=spinner_library_path,
                    spinner_mode=spinner_mode,
                    dt_mode=dt_mode,
                )
                if item is not None:
                    LOGGER.info("%s ... OK (%s/%s)", candidate_label, item.first_score.username, item.second_score.username)
                else:
                    LOGGER.info("%s ... SKIP (intro too short)", candidate_label)
            except (OnlineSynthesisError, OSError, ValueError) as exc:
                LOGGER.info("%s ... FAIL (%s)", candidate_label, exc)
                write_batch_skip_reason(work_dir, candidate, str(exc))
                continue
            if item is not None:
                items.append(item)

        cursor_string = cursor_string_from_payload(payload)
        if not cursor_string:
            break

    if len(items) < count:
        raise OnlineSynthesisError(
            f"only synthesized {len(items)} replay(s) after scanning {pages} search page(s); requested {count}"
        )

    manifest_path = output_dir / "batch_manifest.json"
    manifest_path.write_text(json.dumps(batch_manifest(items), ensure_ascii=False, indent=2), encoding="utf-8")
    return BatchSynthesisReport(
        output_dir=output_dir,
        work_dir=work_dir,
        manifest_path=manifest_path,
        items=tuple(items),
    )


def try_synthesize_batch_candidate(
    client: OsuApiClient,
    candidate: BeatmapSearchCandidate,
    index: int,
    *,
    output_dir: Path,
    work_dir: Path,
    player_name: str | None,
    scope: str,
    ruleset: str,
    no_video: bool,
    leaderboard_limit: int,
    rng: random.Random,
    min_skip_time_ms: int = 4000,
    spinner_library_path: str | Path | None = None,
    spinner_mode: str = "all",
    dt_mode: bool = True,
) -> BatchSynthesisItem | None:
    beatmap_dir = work_dir / str(candidate.beatmap_id)
    beatmap_dir.mkdir(parents=True, exist_ok=True)
    leaderboard = client.get_scores(
        candidate.beatmap_id,
        scope=scope,
        ruleset=ruleset,
        limit=leaderboard_limit,
    )
    leaderboard_path = beatmap_dir / f"leaderboard_{scope}.json"
    leaderboard_path.write_text(json.dumps(leaderboard, ensure_ascii=False, indent=2), encoding="utf-8")

    scores = [leaderboard_score_at_index(index + 1, score) for index, score in enumerate(leaderboard_scores(leaderboard))]
    selected = choose_batch_scores(scores, rng)
    if selected is None:
        return None
    first_score, second_score, pool_name = selected

    beatmap_path = ensure_beatmap_file(
        client,
        candidate.beatmap_id,
        candidate.beatmapset_id,
        beatmap_dir,
        checksum=candidate.checksum,
        no_video=no_video,
    )
    first_replay_path = download_replay_if_needed(client, first_score, beatmap_dir)
    second_replay_path = download_replay_if_needed(client, second_score, beatmap_dir)

    first = OsrReplay.read_path(first_replay_path)
    second = OsrReplay.read_path(second_replay_path)
    beatmap = Beatmap.read_path(beatmap_path)
    if beatmap.first_hit_object_time_ms < min_skip_time_ms:
        return None  # skip: intro too short for skip
    result = synthesize_replays(
        first,
        second,
        beatmap=beatmap,
        player_name=player_name,
        output_mods=MOD_DOUBLE_TIME,
        allow_source_mod_mismatch=True,
        spinner_library_path=str(spinner_library_path) if spinner_library_path is not None else None,
        spinner_mode=spinner_mode,
        dt_mode=dt_mode,
    )
    output_path = output_dir / f"{candidate.beatmap_id}.osr"
    result.replay.write_path(output_path)
    return BatchSynthesisItem(
        index=index,
        output_path=output_path,
        beatmap_id=candidate.beatmap_id,
        beatmapset_id=candidate.beatmapset_id,
        difficulty_rating=candidate.difficulty_rating,
        title=candidate.title,
        version=candidate.version,
        ranked_date=candidate.ranked_date,
        first_score=first_score,
        second_score=second_score,
        selection_pool=pool_name,
        synthesis_report=result.report,
    )


def write_batch_skip_reason(work_dir: Path, candidate: BeatmapSearchCandidate, reason: str) -> None:
    skipped_path = work_dir / "skipped.jsonl"
    skipped_path.parent.mkdir(parents=True, exist_ok=True)
    with skipped_path.open("a", encoding="utf-8") as file:
        file.write(
            json.dumps(
                {
                    "beatmap_id": candidate.beatmap_id,
                    "beatmapset_id": candidate.beatmapset_id,
                    "title": candidate.title,
                    "version": candidate.version,
                    "ranked_date": candidate.ranked_date.isoformat(),
                    "reason": reason,
                },
                ensure_ascii=False,
            )
            + "\n"
        )


def search_candidates_from_payload(
    payload: dict[str, Any],
    *,
    min_star: float,
    max_star: float,
) -> list[BeatmapSearchCandidate]:
    beatmapsets = payload.get("beatmapsets")
    if not isinstance(beatmapsets, list):
        raise OnlineSynthesisError("beatmapset search response does not contain a beatmapsets list")

    candidates: list[BeatmapSearchCandidate] = []
    for beatmapset in beatmapsets:
        if not isinstance(beatmapset, dict):
            continue
        ranked_raw = beatmapset.get("ranked_date") or beatmapset.get("submitted_date")
        if not isinstance(ranked_raw, str):
            continue
        ranked_date = parse_api_datetime(ranked_raw)
        beatmaps = beatmapset.get("beatmaps")
        if not isinstance(beatmaps, list):
            continue
        title = stringish(beatmapset.get("title"))
        set_id = int_field(beatmapset, "id")
        eligible: list[dict[str, Any]] = []
        for beatmap in beatmaps:
            if not isinstance(beatmap, dict):
                continue
            if stringish(beatmap.get("mode")) not in {"", "osu"}:
                continue
            try:
                stars = float(beatmap.get("difficulty_rating"))
            except (TypeError, ValueError):
                continue
            if min_star <= stars <= max_star:
                eligible.append(beatmap)
        if not eligible:
            continue
        beatmap = max(eligible, key=lambda item: float(item.get("difficulty_rating", 0.0)))
        candidates.append(
            BeatmapSearchCandidate(
                beatmapset_id=set_id,
                beatmap_id=int_field(beatmap, "id"),
                ranked_date=ranked_date,
                difficulty_rating=float(beatmap.get("difficulty_rating")),
                title=title,
                version=stringish(beatmap.get("version")),
                checksum=string_field(beatmap, "checksum", required=False),
                raw_beatmapset=beatmapset,
                raw_beatmap=beatmap,
            )
        )
    return sorted(candidates, key=lambda item: item.ranked_date, reverse=True)


def choose_batch_scores(
    scores: list[LeaderboardScore],
    rng: random.Random,
) -> tuple[LeaderboardScore, LeaderboardScore, str] | None:
    eligible = [score for score in scores if score.has_replay and score_mods_supported_for_batch(score)]
    if len(eligible) < 2:
        return None
    first, second = rng.sample(eligible, 2)
    return first, second, "supported_random"


def leaderboard_score_at_index(rank: int, score: dict[str, Any]) -> LeaderboardScore:
    score_id = int_field(score, "id")
    user = score.get("user")
    username = user.get("username") if isinstance(user, dict) and isinstance(user.get("username"), str) else None
    return LeaderboardScore(
        rank=rank,
        score_id=score_id,
        user_id=int_field(score, "user_id", required=False),
        username=username,
        mods=mod_acronyms(score.get("mods")),
        has_replay=bool(score.get("has_replay", score.get("replay", False))),
        raw=score,
    )


def score_mods_supported_for_batch(score: LeaderboardScore) -> bool:
    """Return whether a leaderboard score uses only approved source mods."""
    mods = {mod.strip().upper() for mod in score.mods if mod.strip()}
    if not mods or mods == {"NM"}:
        return True
    return mods <= BATCH_SUPPORTED_SOURCE_MODS


def cursor_string_from_payload(payload: dict[str, Any]) -> str | None:
    value = payload.get("cursor_string")
    if isinstance(value, str) and value:
        return value
    cursor = payload.get("cursor")
    if isinstance(cursor, dict):
        cursor_value = cursor.get("cursor_string")
        if isinstance(cursor_value, str) and cursor_value:
            return cursor_value
    return None


def parse_api_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise OnlineSynthesisError(f"invalid osu! API datetime: {value!r}") from exc
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def stringish(value: object) -> str:
    return value if isinstance(value, str) else ""


def mode_id(mode: str) -> int:
    modes = {
        "osu": 0,
        "taiko": 1,
        "fruits": 2,
        "catch": 2,
        "mania": 3,
    }
    try:
        return modes[mode.lower()]
    except KeyError as exc:
        raise OnlineSynthesisError(f"unsupported ruleset for beatmap search: {mode}") from exc


def batch_manifest(items: list[BatchSynthesisItem]) -> list[dict[str, object]]:
    return [
        {
            "index": item.index,
            "output_path": str(item.output_path),
            "beatmap_id": item.beatmap_id,
            "beatmapset_id": item.beatmapset_id,
            "difficulty_rating": item.difficulty_rating,
            "title": item.title,
            "version": item.version,
            "ranked_date": item.ranked_date.isoformat(),
            "first_score": {
                "rank": item.first_score.rank,
                "score_id": item.first_score.score_id,
                "username": item.first_score.username,
                "mods": item.first_score.mods,
            },
            "second_score": {
                "rank": item.second_score.rank,
                "score_id": item.second_score.score_id,
                "username": item.second_score.username,
                "mods": item.second_score.mods,
            },
            "selection_pool": item.selection_pool,
            "matched_objects": item.synthesis_report.matched_object_count,
            "dropped_objects": item.synthesis_report.dropped_object_count,
        }
        for item in items
    ]


def validate_rank(rank: int) -> None:
    if rank < 1:
        raise OnlineSynthesisError(f"rank must be positive: {rank}")


def is_transient_network_error(reason: object) -> bool:
    return isinstance(reason, (ssl.SSLError, ConnectionError, TimeoutError))


def read_lazer_token(
    *,
    lazer_storage: str | Path | None = None,
    token_config: str | Path | None = None,
) -> LazerOAuthToken:
    config_path = Path(token_config) if token_config is not None else default_lazer_game_ini(lazer_storage)
    return read_lazer_token_path(config_path)


def read_lazer_token_path(config_path: Path) -> LazerOAuthToken:
    token_string = read_ini_value(config_path, "Token")
    if not token_string:
        raise OnlineSynthesisError(f"no saved osu!lazer token found in {config_path}")
    return parse_lazer_token(token_string)


def parse_lazer_token(token_string: str) -> LazerOAuthToken:
    parts = token_string.split("|", 2)
    access_token = parts[0].strip()
    if not access_token:
        raise OnlineSynthesisError("saved osu!lazer token is empty")
    expiry = None
    if len(parts) >= 2 and parts[1].strip():
        try:
            expiry = int(parts[1])
        except ValueError as exc:
            raise OnlineSynthesisError("saved osu!lazer token has an invalid expiry") from exc
    refresh_token = parts[2].strip() if len(parts) >= 3 and parts[2].strip() else None
    return LazerOAuthToken(access_token=access_token, expiry=expiry, refresh_token=refresh_token)


def refresh_lazer_token(
    token: LazerOAuthToken,
    *,
    api_url: str = PRODUCTION_API_URL,
    urlopen: UrlOpen | None = None,
) -> LazerOAuthToken:
    if not token.refresh_token:
        raise OnlineSynthesisError("osu!lazer API token is expired and no refresh token is available")

    urlopen = urlopen or api_urlopen
    payload = urllib.parse.urlencode(
        {
            "grant_type": "refresh_token",
            "client_id": PRODUCTION_CLIENT_ID,
            "client_secret": client_secret_for_api(api_url),
            "scope": "*",
            "refresh_token": token.refresh_token,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"{api_url.rstrip('/')}/oauth/token",
        data=payload,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "synthesis-osu-play/0.1",
        },
        method="POST",
    )
    try:
        with urlopen(request) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except OSError:
            body = ""
        detail = f": {body}" if body else ""
        raise OnlineSynthesisError(f"failed to refresh osu!lazer API token ({exc.code}){detail}") from exc
    except urllib.error.URLError as exc:
        raise OnlineSynthesisError(f"failed to refresh osu!lazer API token: {exc.reason}") from exc

    try:
        response = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OnlineSynthesisError("token refresh returned invalid JSON") from exc
    if not isinstance(response, dict):
        raise OnlineSynthesisError("token refresh returned a non-object response")

    access_token = response.get("access_token")
    expires_in = response.get("expires_in")
    refresh_token = response.get("refresh_token", token.refresh_token)
    if not isinstance(access_token, str) or not access_token:
        raise OnlineSynthesisError("token refresh did not return an access token")
    try:
        expiry = int(time.time() + int(expires_in))
    except (TypeError, ValueError) as exc:
        raise OnlineSynthesisError("token refresh did not return a valid expiry") from exc
    if not isinstance(refresh_token, str) or not refresh_token:
        raise OnlineSynthesisError("token refresh did not return a refresh token")
    return LazerOAuthToken(access_token=access_token, expiry=expiry, refresh_token=refresh_token)


def client_secret_for_api(api_url: str) -> str:
    if api_url.rstrip("/").lower() == DEVELOPMENT_API_URL:
        return DEVELOPMENT_CLIENT_SECRET
    return PRODUCTION_CLIENT_SECRET


def update_ini_value(path: Path, key: str, value: str) -> None:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise OnlineSynthesisError(f"could not read {path}") from exc
    output: list[str] = []
    replaced = False
    for raw_line in text.splitlines():
        if "=" not in raw_line:
            output.append(raw_line)
            continue
        raw_key, _ = raw_line.split("=", 1)
        if raw_key.strip() == key:
            output.append(f"{raw_key.strip()} = {value}")
            replaced = True
        else:
            output.append(raw_line)
    if not replaced:
        output.append(f"{key} = {value}")
    path.write_text("\n".join(output) + "\n", encoding="utf-8")


def default_lazer_game_ini(lazer_storage: str | Path | None = None) -> Path:
    if lazer_storage is not None:
        return Path(lazer_storage) / "game.ini"

    appdata = os.environ.get("APPDATA")
    candidates: list[Path] = []
    if appdata:
        candidates.append(Path(appdata) / "osu")
    candidates.append(Path.home() / "AppData" / "Roaming" / "osu")

    for base in candidates:
        redirected = read_optional_storage_redirect(base)
        if redirected is not None and (redirected / "game.ini").exists():
            return redirected / "game.ini"
        if (base / "game.ini").exists():
            return base / "game.ini"

    raise OnlineSynthesisError("could not find osu!lazer game.ini; pass --lazer-storage or --token-config")


def read_optional_storage_redirect(base: Path) -> Path | None:
    storage_ini = base / "storage.ini"
    if not storage_ini.exists():
        return None
    full_path = read_ini_value(storage_ini, "FullPath", required=False)
    return Path(full_path) if full_path else None


def read_ini_value(path: Path, key: str, *, required: bool = True) -> str:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        if required:
            raise OnlineSynthesisError(f"could not read {path}") from exc
        return ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line.startswith(";") or "=" not in line:
            continue
        raw_key, raw_value = line.split("=", 1)
        if raw_key.strip() == key:
            return raw_value.strip()
    if required:
        raise OnlineSynthesisError(f"{key} was not found in {path}")
    return ""


def leaderboard_scores(payload: dict[str, Any]) -> list[dict[str, Any]]:
    scores = payload.get("scores")
    if not isinstance(scores, list):
        raise OnlineSynthesisError("leaderboard response does not contain a scores list")
    return [score for score in scores if isinstance(score, dict)]


def score_at_rank(scores: list[dict[str, Any]], rank: int) -> LeaderboardScore:
    if rank > len(scores):
        raise OnlineSynthesisError(f"leaderboard returned {len(scores)} scores; rank {rank} is unavailable")
    score = scores[rank - 1]
    score_id = int_field(score, "id")
    user = score.get("user")
    username = user.get("username") if isinstance(user, dict) and isinstance(user.get("username"), str) else None
    selected = LeaderboardScore(
        rank=rank,
        score_id=score_id,
        user_id=int_field(score, "user_id", required=False),
        username=username,
        mods=mod_acronyms(score.get("mods")),
        has_replay=bool(score.get("has_replay", score.get("replay", False))),
        raw=score,
    )
    if not selected.has_replay:
        raise OnlineSynthesisError(f"rank {rank} score {score_id} has no downloadable replay")
    return selected


def mod_acronyms(raw_mods: object) -> tuple[str, ...]:
    if not isinstance(raw_mods, list):
        return ()
    output: list[str] = []
    for item in raw_mods:
        if isinstance(item, str):
            output.append(item)
        elif isinstance(item, dict) and isinstance(item.get("acronym"), str):
            output.append(item["acronym"])
    return tuple(output)


def download_replay_if_needed(client: OsuApiClient, score: LeaderboardScore, work_dir: Path) -> Path:
    path = work_dir / f"rank{score.rank}_{score.score_id}.osr"
    if not path.exists():
        path.write_bytes(client.download_replay(score.score_id))
    return path


def ensure_beatmap_file(
    client: OsuApiClient,
    beatmap_id: int,
    beatmapset_id: int,
    work_dir: Path,
    *,
    checksum: str | None,
    no_video: bool,
) -> Path:
    beatmap_path = work_dir / f"{beatmap_id}.osu"
    osz_path = work_dir / f"beatmapset_{beatmapset_id}.osz"
    if not osz_path.exists():
        osz_path.write_bytes(client.download_beatmapset(beatmapset_id, no_video=no_video))
    if not beatmap_path.exists():
        extract_beatmap_file(osz_path, beatmap_id, beatmap_path, checksum=checksum)
    return beatmap_path


def batch_beatmap_archive_paths(report: BatchSynthesisReport) -> tuple[Path, ...]:
    archives: list[Path] = []
    seen_beatmapset_ids: set[int] = set()
    for item in report.items:
        if item.beatmapset_id in seen_beatmapset_ids:
            continue
        seen_beatmapset_ids.add(item.beatmapset_id)
        archives.append(report.work_dir / str(item.beatmap_id) / f"beatmapset_{item.beatmapset_id}.osz")
    return tuple(archives)


def resolve_lazer_executable(explicit_path: str | Path | None = None) -> Path:
    candidates: list[Path] = []
    if explicit_path is not None:
        candidates.append(Path(explicit_path))
    else:
        configured = os.environ.get("OSU_LAZER_PATH")
        if configured:
            candidates.append(Path(configured))

        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            candidates.append(Path(local_app_data) / "osulazer" / "current")

    for candidate in candidates:
        exe = candidate if candidate.suffix.lower() == ".exe" else candidate / "osu!.exe"
        if exe.exists():
            return exe

    if explicit_path is not None:
        raise OnlineSynthesisError(f"could not find osu!lazer executable at {Path(explicit_path)}")
    raise OnlineSynthesisError("could not find osu!lazer executable; set OSU_LAZER_PATH or pass --lazer-path")


def import_batch_beatmaps(report: BatchSynthesisReport, *, lazer_path: str | Path | None = None) -> tuple[Path, ...]:
    archive_paths = batch_beatmap_archive_paths(report)
    if not archive_paths:
        return ()

    missing = [path for path in archive_paths if not path.exists()]
    if missing:
        raise OnlineSynthesisError(
            "missing cached beatmap archive(s): " + ", ".join(str(path) for path in missing)
        )

    lazer_executable = resolve_lazer_executable(lazer_path)
    imported: list[Path] = []
    for i, path in enumerate(archive_paths):
        try:
            proc = subprocess.Popen(
                [str(lazer_executable), str(path)],
                cwd=str(lazer_executable.parent),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            proc.wait(timeout=30)
            imported.append(path)
            if i % 3 == 2:
                time.sleep(1.0)
        except OSError as exc:
            LOGGER.warning("import failed for %s: %s", path, exc)
        except subprocess.TimeoutExpired:
            proc.terminate()
            proc.wait()
    return tuple(imported)


def extract_beatmap_file(
    osz_path: str | Path,
    beatmap_id: int,
    output_path: str | Path,
    *,
    checksum: str | None = None,
) -> None:
    osz_path = Path(osz_path)
    output_path = Path(output_path)
    try:
        with zipfile.ZipFile(osz_path) as archive:
            for member in archive.infolist():
                if not member.filename.lower().endswith(".osu"):
                    continue
                data = archive.read(member)
                if beatmap_data_matches(data, beatmap_id, checksum=checksum):
                    output_path.write_bytes(data)
                    return
    except zipfile.BadZipFile as exc:
        raise OnlineSynthesisError(f"downloaded beatmapset is not a valid .osz archive: {osz_path}") from exc
    raise OnlineSynthesisError(f"beatmap {beatmap_id} was not found in {osz_path}")


def beatmap_data_matches(data: bytes, beatmap_id: int, *, checksum: str | None = None) -> bool:
    if checksum is not None:
        import hashlib

        if hashlib.md5(data).hexdigest() == checksum:
            return True
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("BeatmapID:"):
            continue
        try:
            return int(line.split(":", 1)[1].strip()) == beatmap_id
        except ValueError:
            return False
    return False


def int_field(payload: dict[str, Any], key: str, *, required: bool = True) -> int | None:
    value = payload.get(key)
    if value is None:
        if required:
            raise OnlineSynthesisError(f"missing integer field {key}")
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise OnlineSynthesisError(f"invalid integer field {key}: {value!r}") from exc


def string_field(payload: dict[str, Any], key: str, *, required: bool = True) -> str | None:
    value = payload.get(key)
    if value is None:
        if required:
            raise OnlineSynthesisError(f"missing string field {key}")
        return None
    if not isinstance(value, str):
        raise OnlineSynthesisError(f"invalid string field {key}: {value!r}")
    return value
