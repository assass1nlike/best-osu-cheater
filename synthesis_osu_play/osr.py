from __future__ import annotations

import hashlib
import json
import lzma
import struct
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

from .mods import legacy_mods_to_lazer_metadata


DOTNET_TICKS_AT_UNIX_EPOCH = 621_355_968_000_000_000
TICKS_PER_SECOND = 10_000_000
LAZER_REPLAY_METADATA_MIN_VERSION = 30_000_001
MISSING_ONLINE_ID = -1
UNKNOWN_USER_ID = 1


class OsrFormatError(ValueError):
    """Raised when a replay file cannot be parsed as osu!stable .osr."""


@dataclass(frozen=True)
class ReplayFrame:
    delta_ms: int
    x: float
    y: float
    keys: int


@dataclass(frozen=True)
class OsrReplay:
    mode: int
    game_version: int
    beatmap_md5: str
    player_name: str
    replay_md5: str
    count_300: int
    count_100: int
    count_50: int
    count_geki: int
    count_katu: int
    count_miss: int
    score: int
    max_combo: int
    perfect: bool
    mods: int
    life_bar_graph: str
    timestamp: int
    frames: tuple[ReplayFrame, ...]
    online_score_id: int
    trailing_bytes: bytes = b""

    @classmethod
    def read_path(cls, path: str | Path) -> "OsrReplay":
        return cls.from_bytes(Path(path).read_bytes())

    @classmethod
    def from_bytes(cls, data: bytes) -> "OsrReplay":
        reader = _BinaryReader(data)
        mode = reader.u8()
        game_version = reader.i32()
        beatmap_md5 = reader.osu_string()
        player_name = reader.osu_string()
        replay_md5 = reader.osu_string()
        count_300 = reader.u16()
        count_100 = reader.u16()
        count_50 = reader.u16()
        count_geki = reader.u16()
        count_katu = reader.u16()
        count_miss = reader.u16()
        score = reader.i32()
        max_combo = reader.u16()
        perfect = bool(reader.u8())
        mods = reader.i32()
        life_bar_graph = reader.osu_string()
        timestamp = reader.i64()
        compressed_length = reader.i32()
        if compressed_length < 0:
            raise OsrFormatError("negative replay data length")
        compressed = reader.bytes(compressed_length)
        frames = tuple(parse_replay_data(decompress_replay_data(compressed)))
        online_score_id = reader.i64() if reader.remaining >= 8 else 0
        trailing_bytes = reader.bytes(reader.remaining)
        return cls(
            mode=mode,
            game_version=game_version,
            beatmap_md5=beatmap_md5,
            player_name=player_name,
            replay_md5=replay_md5,
            count_300=count_300,
            count_100=count_100,
            count_50=count_50,
            count_geki=count_geki,
            count_katu=count_katu,
            count_miss=count_miss,
            score=score,
            max_combo=max_combo,
            perfect=perfect,
            mods=mods,
            life_bar_graph=life_bar_graph,
            timestamp=timestamp,
            frames=frames,
            online_score_id=online_score_id,
            trailing_bytes=trailing_bytes,
        )

    def with_frames(
        self,
        frames: tuple[ReplayFrame, ...],
        *,
        player_name: str | None = None,
        mods: int | None = None,
        timestamp: int | None = None,
        online_score_id: int | None = None,
    ) -> "OsrReplay":
        replay_text = format_replay_data(frames)
        digest = hashlib.md5(replay_text.encode("utf-8")).hexdigest()
        trailing_bytes = scrub_lazer_replay_metadata(self.trailing_bytes)
        output_mods = self.mods if mods is None else mods
        if output_mods != self.mods:
            metadata = decode_lazer_replay_metadata(trailing_bytes)
            if metadata is not None:
                metadata["mods"] = legacy_mods_to_lazer_metadata(output_mods)
                trailing_bytes = encode_lazer_replay_metadata(metadata)
        if online_score_id is None:
            online_score_id = MISSING_ONLINE_ID if trailing_bytes else 0
        return replace(
            self,
            player_name=player_name if player_name is not None else self.player_name,
            replay_md5=digest,
            mods=output_mods,
            timestamp=timestamp if timestamp is not None else current_dotnet_ticks(),
            frames=frames,
            online_score_id=online_score_id,
            trailing_bytes=trailing_bytes,
        )

    def with_score_metadata(
        self,
        *,
        count_300: int,
        count_100: int,
        count_50: int,
        count_geki: int,
        count_katu: int,
        count_miss: int,
        score: int,
        max_combo: int,
        perfect: bool,
        rank: str | None = None,
        statistics: dict[str, int] | None = None,
        maximum_statistics: dict[str, int] | None = None,
    ) -> "OsrReplay":
        trailing_bytes = self.trailing_bytes
        metadata = decode_lazer_replay_metadata(trailing_bytes)
        if metadata is not None:
            if rank is not None:
                metadata["rank"] = rank
            if statistics is not None:
                metadata["statistics"] = statistics
            if maximum_statistics is not None:
                metadata["maximum_statistics"] = maximum_statistics
            metadata["online_id"] = MISSING_ONLINE_ID
            metadata["user_id"] = UNKNOWN_USER_ID
            metadata["client_version"] = ""
            trailing_bytes = encode_lazer_replay_metadata(metadata)
        return replace(
            self,
            count_300=count_300,
            count_100=count_100,
            count_50=count_50,
            count_geki=count_geki,
            count_katu=count_katu,
            count_miss=count_miss,
            score=score,
            max_combo=max_combo,
            perfect=perfect,
            trailing_bytes=trailing_bytes,
        )

    def with_score_metadata_from(self, scored_replay: "OsrReplay") -> "OsrReplay":
        """Copy lazer-judged score metadata while keeping this replay's input stream."""

        trailing_bytes = metadata_bytes_with_scrubbed_identity(scored_replay.trailing_bytes)
        return replace(
            self,
            game_version=scored_replay.game_version,
            count_300=scored_replay.count_300,
            count_100=scored_replay.count_100,
            count_50=scored_replay.count_50,
            count_geki=scored_replay.count_geki,
            count_katu=scored_replay.count_katu,
            count_miss=scored_replay.count_miss,
            score=scored_replay.score,
            max_combo=scored_replay.max_combo,
            perfect=scored_replay.perfect,
            life_bar_graph=scored_replay.life_bar_graph,
            timestamp=scored_replay.timestamp,
            online_score_id=MISSING_ONLINE_ID if trailing_bytes else self.online_score_id,
            trailing_bytes=trailing_bytes,
        )

    def write_path(self, path: str | Path) -> None:
        Path(path).write_bytes(self.to_bytes())

    def to_bytes(self) -> bytes:
        replay_text = format_replay_data(self.frames)
        compressed = compress_replay_data(replay_text)
        writer = _BinaryWriter()
        writer.u8(self.mode)
        writer.i32(self.game_version)
        writer.osu_string(self.beatmap_md5)
        writer.osu_string(self.player_name)
        writer.osu_string(self.replay_md5)
        writer.u16(self.count_300)
        writer.u16(self.count_100)
        writer.u16(self.count_50)
        writer.u16(self.count_geki)
        writer.u16(self.count_katu)
        writer.u16(self.count_miss)
        writer.i32(self.score)
        writer.u16(self.max_combo)
        writer.u8(1 if self.perfect else 0)
        writer.i32(self.mods)
        writer.osu_string(self.life_bar_graph)
        writer.i64(self.timestamp)
        writer.i32(len(compressed))
        writer.bytes(compressed)
        writer.i64(self.online_score_id)
        writer.bytes(self.trailing_bytes)
        return writer.data


def current_dotnet_ticks() -> int:
    unix_seconds = datetime.now(timezone.utc).timestamp()
    return DOTNET_TICKS_AT_UNIX_EPOCH + int(unix_seconds * TICKS_PER_SECOND)


def decompress_replay_data(compressed: bytes) -> str:
    try:
        return lzma.decompress(compressed).decode("utf-8")
    except lzma.LZMAError:
        return lzma.decompress(compressed, format=lzma.FORMAT_ALONE).decode("utf-8")


def compress_replay_data(replay_text: str) -> bytes:
    return lzma.compress(replay_text.encode("utf-8"), format=lzma.FORMAT_ALONE)


def scrub_lazer_replay_metadata(trailing_bytes: bytes) -> bytes:
    """Keep lazer replay score metadata importable without inheriting an online score identity."""
    return metadata_bytes_with_scrubbed_identity(trailing_bytes)


def metadata_bytes_with_scrubbed_identity(trailing_bytes: bytes) -> bytes:
    metadata = decode_lazer_replay_metadata(trailing_bytes)
    if metadata is None:
        return trailing_bytes
    metadata["online_id"] = MISSING_ONLINE_ID
    metadata["user_id"] = UNKNOWN_USER_ID
    metadata["client_version"] = ""
    return encode_lazer_replay_metadata(metadata)


def decode_lazer_replay_metadata(trailing_bytes: bytes) -> dict[str, object] | None:
    if len(trailing_bytes) < 4:
        return None
    length = struct.unpack("<i", trailing_bytes[:4])[0]
    if length < 0 or 4 + length > len(trailing_bytes):
        return None
    payload = trailing_bytes[4 : 4 + length]
    try:
        decoded = json.loads(decompress_replay_data(payload))
    except (OsrFormatError, UnicodeDecodeError, json.JSONDecodeError, lzma.LZMAError):
        return None
    if not isinstance(decoded, dict):
        return None
    return decoded


def encode_lazer_replay_metadata(metadata: dict[str, object]) -> bytes:
    payload = json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8")
    compressed = lzma.compress(payload, format=lzma.FORMAT_ALONE)
    return struct.pack("<i", len(compressed)) + compressed


def parse_replay_data(replay_text: str) -> list[ReplayFrame]:
    frames: list[ReplayFrame] = []
    for raw in replay_text.split(","):
        raw = raw.strip()
        if not raw:
            continue
        parts = raw.split("|")
        if len(parts) != 4:
            raise OsrFormatError(f"invalid replay frame: {raw!r}")
        try:
            frames.append(
                ReplayFrame(
                    delta_ms=int(parts[0]),
                    x=float(parts[1]),
                    y=float(parts[2]),
                    keys=int(parts[3]),
                )
            )
        except ValueError as exc:
            raise OsrFormatError(f"invalid replay frame values: {raw!r}") from exc
    return frames


def format_replay_data(frames: tuple[ReplayFrame, ...] | list[ReplayFrame]) -> str:
    return ",".join(
        f"{frame.delta_ms}|{_format_float(frame.x)}|{_format_float(frame.y)}|{frame.keys}"
        for frame in frames
    )


def _format_float(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text if text else "0"


class _BinaryReader:
    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    @property
    def remaining(self) -> int:
        return len(self._data) - self._offset

    def bytes(self, length: int) -> bytes:
        if length < 0 or self._offset + length > len(self._data):
            raise OsrFormatError("unexpected end of replay file")
        chunk = self._data[self._offset : self._offset + length]
        self._offset += length
        return chunk

    def u8(self) -> int:
        return self._unpack("<B", 1)

    def u16(self) -> int:
        return self._unpack("<H", 2)

    def i32(self) -> int:
        return self._unpack("<i", 4)

    def i64(self) -> int:
        return self._unpack("<q", 8)

    def osu_string(self) -> str:
        marker = self.u8()
        if marker == 0x00:
            return ""
        if marker != 0x0B:
            raise OsrFormatError(f"invalid osu! string marker: 0x{marker:02x}")
        length = self.uleb128()
        return self.bytes(length).decode("utf-8")

    def uleb128(self) -> int:
        result = 0
        shift = 0
        while True:
            byte = self.u8()
            result |= (byte & 0x7F) << shift
            if not byte & 0x80:
                return result
            shift += 7
            if shift > 63:
                raise OsrFormatError("ULEB128 integer is too large")

    def _unpack(self, fmt: str, length: int) -> int:
        return struct.unpack(fmt, self.bytes(length))[0]


class _BinaryWriter:
    def __init__(self) -> None:
        self._data = bytearray()

    @property
    def data(self) -> bytes:
        return bytes(self._data)

    def bytes(self, value: bytes) -> None:
        self._data.extend(value)

    def u8(self, value: int) -> None:
        self._pack("<B", value)

    def u16(self, value: int) -> None:
        self._pack("<H", value)

    def i32(self, value: int) -> None:
        self._pack("<i", value)

    def i64(self, value: int) -> None:
        self._pack("<q", value)

    def osu_string(self, value: str) -> None:
        if value == "":
            self.u8(0x00)
            return
        encoded = value.encode("utf-8")
        self.u8(0x0B)
        self.uleb128(len(encoded))
        self.bytes(encoded)

    def uleb128(self, value: int) -> None:
        if value < 0:
            raise ValueError("ULEB128 cannot encode negative values")
        while True:
            byte = value & 0x7F
            value >>= 7
            if value:
                self.u8(byte | 0x80)
            else:
                self.u8(byte)
                return

    def _pack(self, fmt: str, value: int) -> None:
        self._data.extend(struct.pack(fmt, value))
