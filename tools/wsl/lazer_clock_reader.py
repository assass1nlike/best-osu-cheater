#!/usr/bin/env python3
"""Read osu!lazer's live clock from a Linux process under WSL."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
import os
from pathlib import Path
import struct
import sys
import time
from typing import Iterator


CHUNK_SIZE = 16 * 1024 * 1024


def write_event(**values: object) -> None:
    print(json.dumps(values, separators=(",", ":")), flush=True)


@dataclass(frozen=True)
class OffsetData:
    osu_version: str
    scan_pattern: bytes
    game_base_vtable: int
    game_base_from_anchor_deltas: tuple[int, ...]
    external_link_opener_api: int
    api_game: int
    beatmap_clock: int
    framed_beatmap_clock_final_source: int
    framed_clock_current_time: int

    @classmethod
    def load(cls, path: Path) -> "OffsetData":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            return cls(
                osu_version=str(payload.get("osu_version", "unknown")),
                scan_pattern=bytes.fromhex(payload["scan_pattern"]),
                game_base_vtable=int(payload["game_base_vtable"]),
                game_base_from_anchor_deltas=tuple(
                    int(value) for value in payload["game_base_from_anchor_deltas"]
                ),
                external_link_opener_api=int(payload["external_link_opener_api"]),
                api_game=int(payload["api_game"]),
                beatmap_clock=int(payload["beatmap_clock"]),
                framed_beatmap_clock_final_source=int(
                    payload["framed_beatmap_clock_final_source"]
                ),
                framed_clock_current_time=int(payload["framed_clock_current_time"]),
            )
        except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"invalid osu!lazer offsets file {path}: {exc}") from exc


@dataclass(frozen=True)
class MemoryRegion:
    start: int
    end: int


def parse_readable_regions(contents: str) -> tuple[MemoryRegion, ...]:
    regions: list[MemoryRegion] = []
    for line in contents.splitlines():
        fields = line.split(maxsplit=5)
        if len(fields) < 2 or not fields[1].startswith("r"):
            continue
        start_text, end_text = fields[0].split("-", 1)
        regions.append(MemoryRegion(int(start_text, 16), int(end_text, 16)))
    return tuple(regions)


def find_lazer_pid() -> int:
    candidates: list[int] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if (entry / "comm").read_text(encoding="utf-8").strip() == "osu!":
                candidates.append(int(entry.name))
        except (FileNotFoundError, PermissionError, ProcessLookupError):
            continue
    if not candidates:
        raise ValueError("no Linux osu!lazer process is running")
    if len(candidates) > 1:
        raise ValueError(f"multiple Linux osu!lazer processes found: {candidates}")
    return candidates[0]


class ProcessMemory:
    def __init__(self, process_id: int) -> None:
        self.process_id = process_id
        self._memory_fd = os.open(f"/proc/{process_id}/mem", os.O_RDONLY)

    def close(self) -> None:
        if self._memory_fd >= 0:
            os.close(self._memory_fd)
            self._memory_fd = -1

    def __enter__(self) -> "ProcessMemory":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()

    def read(self, address: int, count: int) -> bytes | None:
        try:
            data = os.pread(self._memory_fd, count, address)
        except (OSError, OverflowError):
            return None
        return data if len(data) == count else None

    def read_uint64(self, address: int) -> int | None:
        data = self.read(address, 8)
        return struct.unpack("<Q", data)[0] if data is not None else None

    def read_double(self, address: int) -> float | None:
        data = self.read(address, 8)
        return struct.unpack("<d", data)[0] if data is not None else None

    def readable_regions(self) -> tuple[MemoryRegion, ...]:
        contents = Path(f"/proc/{self.process_id}/maps").read_text(encoding="utf-8")
        return parse_readable_regions(contents)

    def scan_pattern(self, pattern: bytes) -> Iterator[int]:
        overlap = max(len(pattern) - 1, 0)
        for region in self.readable_regions():
            previous = b""
            address = region.start
            while address < region.end:
                count = min(CHUNK_SIZE, region.end - address)
                data = self.read(address, count)
                if data is None:
                    address += count
                    previous = b""
                    continue
                combined = previous + data
                combined_start = address - len(previous)
                search_from = 0
                while True:
                    index = combined.find(pattern, search_from)
                    if index < 0:
                        break
                    yield combined_start + index
                    search_from = index + 1
                previous = combined[-overlap:] if overlap else b""
                address += count


@dataclass(frozen=True)
class ClockAddress:
    game_base: int
    current_time: int


class LazerClock:
    def __init__(
        self,
        memory: ProcessMemory,
        offsets: OffsetData,
        *,
        cached_game_base: int = 0,
    ) -> None:
        self.memory = memory
        self.offsets = offsets
        self.game_base = cached_game_base

    def read_current_time(self) -> tuple[float, ClockAddress] | None:
        if not self.resolve_game_base():
            return None
        beatmap_clock = self.memory.read_uint64(
            self.game_base + self.offsets.beatmap_clock
        )
        if not beatmap_clock:
            return None
        final_source = self.memory.read_uint64(
            beatmap_clock + self.offsets.framed_beatmap_clock_final_source
        )
        if not final_source:
            return None
        current_time_address = final_source + self.offsets.framed_clock_current_time
        current_time = self.memory.read_double(current_time_address)
        if (
            current_time is None
            or not math.isfinite(current_time)
            or current_time < -120_000
            or current_time > 24 * 60 * 60 * 1000
        ):
            return None
        return current_time, ClockAddress(self.game_base, current_time_address)

    def resolve_game_base(self) -> bool:
        if not self._is_game_base_valid(self.game_base):
            self.game_base = self._scan_game_base()
        return self.game_base != 0

    def _scan_game_base(self) -> int:
        for anchor in self.memory.scan_pattern(self.offsets.scan_pattern):
            for delta in self.offsets.game_base_from_anchor_deltas:
                if anchor < delta:
                    continue
                external_link_opener = self.memory.read_uint64(anchor - delta)
                if not external_link_opener:
                    continue
                api = self.memory.read_uint64(
                    external_link_opener + self.offsets.external_link_opener_api
                )
                if not api:
                    continue
                candidate = self.memory.read_uint64(api + self.offsets.api_game)
                if candidate and self._is_game_base_valid(candidate):
                    return candidate
        return 0

    def _is_game_base_valid(self, candidate: int) -> bool:
        if not candidate:
            return False
        vtable = self.memory.read_uint64(candidate)
        if not vtable:
            return False
        return self.memory.read_uint64(vtable) == self.offsets.game_base_vtable


def wait_for_start_command() -> None:
    write_event(event="service", qpc_seconds=time.perf_counter())
    for line in sys.stdin:
        try:
            command = json.loads(line)
        except json.JSONDecodeError:
            continue
        if command.get("command") == "sync":
            write_event(
                event="sync",
                id=command.get("id"),
                qpc_seconds=time.perf_counter(),
            )
        elif command.get("command") == "start":
            return
    raise EOFError("clock reader control stream closed before start")


def game_base_cache_path(process_id: int) -> Path:
    return Path(f"/tmp/synthesis-osu-play-lazer-clock-{process_id}.json")


def load_cached_game_base(process_id: int) -> int:
    try:
        payload = json.loads(game_base_cache_path(process_id).read_text(encoding="utf-8"))
        return int(str(payload["game_base"]), 16)
    except (FileNotFoundError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return 0


def save_cached_game_base(process_id: int, game_base: int) -> None:
    path = game_base_cache_path(process_id)
    temporary = path.with_suffix(f".{os.getpid()}.tmp")
    temporary.write_text(
        json.dumps({"pid": process_id, "game_base": f"0x{game_base:X}"}),
        encoding="utf-8",
    )
    os.replace(temporary, path)


def monitor(args: argparse.Namespace, offsets: OffsetData) -> int:
    process_id = args.pid or find_lazer_pid()
    started_at = time.perf_counter()
    with ProcessMemory(process_id) as memory:
        clock = LazerClock(
            memory,
            offsets,
            cached_game_base=load_cached_game_base(process_id),
        )
        ready = False
        attached = False
        previous_time = math.nan
        last_sample_at = 0.0

        while True:
            if args.timeout_ms and (time.perf_counter() - started_at) * 1000 >= args.timeout_ms:
                write_event(
                    event="error",
                    message="timed out while resolving osu!lazer game clock",
                    pid=process_id,
                )
                return 2

            result = clock.read_current_time()
            if result is None:
                if not attached and clock.resolve_game_base():
                    attached = True
                    write_event(
                        event="attached",
                        pid=process_id,
                        qpc_seconds=time.perf_counter(),
                        game_base=f"0x{clock.game_base:X}",
                        osu_version=offsets.osu_version,
                    )
                previous_time = math.nan
                time.sleep(max(args.interval_ms, 25) / 1000.0)
                continue

            current_time, address = result
            if not attached:
                save_cached_game_base(process_id, address.game_base)
                attached = True
            now = time.perf_counter()
            if not ready:
                ready = True
                previous_time = current_time
                last_sample_at = now
                write_event(
                    event="ready",
                    pid=process_id,
                    qpc_seconds=now,
                    time_ms=current_time,
                    game_base=f"0x{address.game_base:X}",
                    current_time_address=f"0x{address.current_time:X}",
                    osu_version=offsets.osu_version,
                )
            else:
                delta = current_time - previous_time
                if delta >= args.jump_threshold_ms:
                    write_event(
                        event="jump",
                        time_ms=current_time,
                        delta_ms=delta,
                        qpc_seconds=now,
                        game_base=f"0x{address.game_base:X}",
                        current_time_address=f"0x{address.current_time:X}",
                    )
                    if args.wait_for_jump:
                        return 0
                if args.emit_samples and (now - last_sample_at) * 1000 >= args.sample_interval_ms:
                    last_sample_at = now
                    write_event(
                        event="sample",
                        time_ms=current_time,
                        qpc_seconds=now,
                        game_base=f"0x{address.game_base:X}",
                        current_time_address=f"0x{address.current_time:X}",
                    )
                previous_time = current_time
            time.sleep(args.interval_ms / 1000.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int)
    parser.add_argument("--offsets-path", type=Path, required=True)
    parser.add_argument("--interval-ms", type=int, default=5)
    parser.add_argument("--jump-threshold-ms", type=float, default=100.0)
    parser.add_argument("--wait-for-jump", action="store_true")
    parser.add_argument("--emit-samples", action="store_true")
    parser.add_argument("--sample-interval-ms", type=int, default=20)
    parser.add_argument("--timeout-ms", type=int)
    parser.add_argument("--serve", action="store_true")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.pid is not None and args.pid <= 0:
        raise SystemExit("error: --pid must be positive")
    if args.interval_ms <= 0 or args.sample_interval_ms <= 0:
        raise SystemExit("error: sample intervals must be positive")
    if args.jump_threshold_ms <= 0:
        raise SystemExit("error: --jump-threshold-ms must be positive")
    try:
        offsets = OffsetData.load(args.offsets_path)
        if args.serve:
            wait_for_start_command()
        return monitor(args, offsets)
    except (EOFError, OSError, ValueError) as exc:
        write_event(event="error", message=str(exc))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
