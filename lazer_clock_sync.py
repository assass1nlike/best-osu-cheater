"""Read the osu!lazer gameplay clock and align replay playback."""

from __future__ import annotations

import bisect
import json
import os
import queue
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence


class ClockSyncError(RuntimeError):
    """The live osu!lazer clock could not be read or synchronized."""


@dataclass(frozen=True)
class ClockJump:
    time_ms: float
    delta_ms: float
    qpc_seconds: float


@dataclass(frozen=True)
class ClockSample:
    time_ms: float
    qpc_seconds: float


@dataclass(frozen=True)
class ReplayTimeline:
    """Absolute replay frame times in the same millisecond domain as lazer."""

    frame_times_ms: tuple[float, ...]

    @classmethod
    def from_frames(cls, frames: Iterable[object]) -> "ReplayTimeline":
        current = 0.0
        times: list[float] = []
        for frame in frames:
            current += float(frame.time_delta)
            times.append(current)
        return cls(tuple(times))

    def first_frame_at_or_after(self, time_ms: float) -> int:
        return bisect.bisect_left(self.frame_times_ms, time_ms)

    def first_key_time_ms(self, frames: Sequence[object]) -> float | None:
        for index, frame in enumerate(frames):
            if int(frame.keys):
                return self.frame_times_ms[index]
        return None

    def __len__(self) -> int:
        return len(self.frame_times_ms)


def default_reader_path() -> Path:
    return (
        Path(__file__).resolve().parent
        / "tools"
        / "LazerClockReader"
        / "bin"
        / "Release"
        / "net8.0"
        / "LazerClockReader.exe"
    )


def window_process_id(hwnd: int) -> int:
    """Return the owning process id for a Win32 window handle."""

    import ctypes
    from ctypes import byref, wintypes

    process_id = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, byref(process_id))
    return int(process_id.value)


class LazerClockReader:
    """Own one reader process and expose its clock JSONL events."""

    def __init__(
        self,
        process_id: int,
        *,
        reader_path: str | os.PathLike[str] | None = None,
        ready_timeout_s: float = 5.0,
        jump_threshold_ms: float = 100.0,
        interval_ms: int = 5,
        wait_for_jump: bool = True,
        stream_samples: bool = False,
        sample_interval_ms: int = 20,
    ) -> None:
        self.process_id = process_id
        self.reader_path = Path(reader_path) if reader_path else default_reader_path()
        self.ready_timeout_s = ready_timeout_s
        self.jump_threshold_ms = jump_threshold_ms
        self.interval_ms = interval_ms
        self.wait_for_jump_mode = wait_for_jump
        self.stream_samples = stream_samples
        self.sample_interval_ms = sample_interval_ms
        self._process: subprocess.Popen[str] | None = None
        self._events: queue.Queue[dict[str, object]] = queue.Queue()
        self._reader_thread: threading.Thread | None = None

    @property
    def started(self) -> bool:
        return self._process is not None

    def start(self) -> bool:
        if self.started:
            return True
        if not self.reader_path.exists():
            raise ClockSyncError(
                f"clock reader not found: {self.reader_path}; "
                "build tools/LazerClockReader first"
            )

        command = self._command()
        try:
            self._process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except OSError as exc:
            raise ClockSyncError(f"could not start clock reader: {exc}") from exc

        self._reader_thread = threading.Thread(
            target=self._collect_events,
            name="osu-lazer-clock-reader",
            daemon=True,
        )
        self._reader_thread.start()
        try:
            event = self._next_event(self.ready_timeout_s)
        except ClockSyncError:
            self.stop()
            raise
        if event.get("event") not in {"attached", "ready"}:
            self.stop()
            raise ClockSyncError(f"unexpected clock reader event: {event}")
        return True

    def wait_for_jump(self, timeout_s: float) -> ClockJump:
        deadline = time.perf_counter() + timeout_s
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise ClockSyncError(
                    f"osu!lazer did not accept SPACE within {timeout_s:.1f}s"
                )
            event = self._next_event(remaining)
            event_type = event.get("event")
            if event_type == "jump":
                return ClockJump(
                    time_ms=float(event["time_ms"]),
                    delta_ms=float(event.get("delta_ms", 0.0)),
                    qpc_seconds=float(event["qpc_seconds"]),
                )
            if event_type == "error":
                raise ClockSyncError(str(event.get("message", "clock reader error")))

    def wait_for_running_clock(
        self,
        timeout_s: float,
        *,
        min_time_ms: float = -120_000.0,
        max_time_ms: float | None = None,
        required_samples: int = 3,
        min_delta_ms: float = 5.0,
    ) -> ClockSample:
        """Wait until CurrentTime is valid and moving forward in the target range."""

        if required_samples < 2:
            raise ValueError("required_samples must be at least 2")

        deadline = time.perf_counter() + timeout_s
        recent: list[ClockSample] = []
        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise ClockSyncError(
                    f"osu!lazer gameplay clock did not start within {timeout_s:.1f}s"
                )

            event = self._next_event(remaining)
            event_type = event.get("event")
            if event_type == "error":
                raise ClockSyncError(str(event.get("message", "clock reader error")))
            if event_type not in {"ready", "sample", "jump"}:
                continue

            sample = _event_to_sample(event)
            if sample is None:
                continue
            if sample.time_ms < min_time_ms:
                recent.clear()
                continue
            if max_time_ms is not None and sample.time_ms > max_time_ms:
                recent.clear()
                continue

            if recent and sample.time_ms <= recent[-1].time_ms:
                recent = [sample]
                continue

            recent.append(sample)
            if len(recent) > required_samples:
                recent = recent[-required_samples:]
            if len(recent) < required_samples:
                continue

            elapsed_s = recent[-1].qpc_seconds - recent[0].qpc_seconds
            delta_ms = recent[-1].time_ms - recent[0].time_ms
            if elapsed_s <= 0 or delta_ms < min_delta_ms:
                continue

            rate_ms_per_s = delta_ms / elapsed_s
            if 100.0 <= rate_ms_per_s <= 3000.0:
                return recent[-1]

    def discard_pending_events(self) -> None:
        while True:
            try:
                self._events.get_nowait()
            except queue.Empty:
                return

    def stop(self) -> None:
        process = self._process
        self._process = None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=1.0)

    def _command(self) -> list[str]:
        if self.reader_path.suffix.lower() == ".dll":
            command = ["dotnet", str(self.reader_path)]
        else:
            command = [str(self.reader_path)]
        args = command + [
            "--pid",
            str(self.process_id),
            "--interval-ms",
            str(self.interval_ms),
            "--jump-threshold-ms",
            str(self.jump_threshold_ms),
        ]
        if self.wait_for_jump_mode:
            args.append("--wait-for-jump")
        if self.stream_samples:
            args.extend(["--emit-samples", "--sample-interval-ms", str(self.sample_interval_ms)])
        return args

    def _collect_events(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            return
        for line in process.stdout:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                self._events.put(event)
        if process.poll() not in (None, 0):
            self._events.put(
                {
                    "event": "error",
                    "message": f"clock reader exited with code {process.returncode}",
                }
            )

    def _next_event(self, timeout_s: float) -> dict[str, object]:
        try:
            return self._events.get(timeout=max(timeout_s, 0.001))
        except queue.Empty as exc:
            raise ClockSyncError("timed out waiting for osu!lazer clock reader") from exc


def start_clock_reader(
    process_id: int,
    *,
    reader_path: str | os.PathLike[str] | None = None,
    ready_timeout_ms: int = 5000,
    jump_threshold_ms: float = 100.0,
    interval_ms: int = 5,
    wait_for_jump: bool = True,
    stream_samples: bool = False,
    sample_interval_ms: int = 20,
) -> LazerClockReader:
    reader = LazerClockReader(
        process_id,
        reader_path=reader_path,
        ready_timeout_s=ready_timeout_ms / 1000.0,
        jump_threshold_ms=jump_threshold_ms,
        interval_ms=interval_ms,
        wait_for_jump=wait_for_jump,
        stream_samples=stream_samples,
        sample_interval_ms=sample_interval_ms,
    )
    reader.start()
    return reader


def initial_key_mask(frames: Sequence[object], frame_index: int) -> int:
    """Return the key state at the instant just before ``frame_index``."""

    if frame_index <= 0:
        return 0
    return int(frames[frame_index - 1].keys)


def _event_to_sample(event: dict[str, object]) -> ClockSample | None:
    try:
        return ClockSample(
            time_ms=float(event["time_ms"]),
            qpc_seconds=float(event["qpc_seconds"]),
        )
    except (KeyError, TypeError, ValueError):
        return None
