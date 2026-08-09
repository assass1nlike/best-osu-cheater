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
    source_address: str | None = None


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
        self._last_sample: ClockSample | None = None

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

    def wait_for_gameplay_start(
        self,
        timeout_s: float,
        *,
        max_time_ms: float | None = None,
        boundary_samples: int = 2,
        boundary_epsilon_ms: float = 1.0,
        stable_boundary_duration_ms: float = 100.0,
        running_samples: int = 3,
        min_delta_ms: float = 5.0,
        stable_boundary_max_ms: float | None = 1000.0,
        allow_running_without_boundary: bool = True,
    ) -> ClockSample:
        """Return the earliest reliable moving gameplay clock sample.

        The global lazer beatmap clock is also used for song-select previews.
        A moving clock alone therefore cannot identify the gameplay screen.
        Gameplay resets or pauses that clock while transitioning from the
        preview to the newly loaded player. Treat the reset/pause as a boundary,
        but do not anchor on it until the clock is observed moving afterwards;
        the reset can happen while the player is still loading.

        If the reader was launched after ENTER, it may be too late to see the
        transition boundary. ``allow_running_without_boundary`` retains a
        short early-time fallback for that compatibility mode. A reader armed
        before ENTER should disable the fallback so song-select preview samples
        cannot be mistaken for gameplay.
        """

        if boundary_samples < 1:
            raise ValueError("boundary_samples must be positive")
        if boundary_epsilon_ms < 0:
            raise ValueError("boundary_epsilon_ms must be non-negative")
        if stable_boundary_duration_ms < 0:
            raise ValueError("stable boundary duration must be non-negative")
        if running_samples < 2:
            raise ValueError("running_samples must be at least 2")
        if min_delta_ms < 0:
            raise ValueError("min_delta_ms must be non-negative")
        if stable_boundary_max_ms is not None and stable_boundary_max_ms < 0:
            raise ValueError("stable_boundary_max_ms must be non-negative")

        deadline = time.perf_counter() + timeout_s
        previous = self._last_sample
        stable_count = 1 if previous is not None else 0
        stable_since_qpc = previous.qpc_seconds if previous is not None else None
        saw_gameplay_boundary = False
        recent_running: list[ClockSample] = []

        while True:
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                raise ClockSyncError(
                    f"osu!lazer gameplay clock did not restart within {timeout_s:.1f}s"
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

            in_range = max_time_ms is None or sample.time_ms <= max_time_ms
            if previous is not None:
                source_changed = (
                    previous.source_address is not None
                    and sample.source_address is not None
                    and sample.source_address != previous.source_address
                )
                delta = sample.time_ms - previous.time_ms
                if source_changed:
                    saw_gameplay_boundary = True
                    stable_count = 1
                    stable_since_qpc = sample.qpc_seconds
                    recent_running = [sample]
                elif delta < -boundary_epsilon_ms:
                    saw_gameplay_boundary = True
                    stable_count = 1
                    stable_since_qpc = sample.qpc_seconds
                    recent_running = [sample]
                elif abs(delta) <= boundary_epsilon_ms:
                    if stable_count == 0 or stable_since_qpc is None:
                        stable_since_qpc = previous.qpc_seconds
                    stable_count += 1
                    stable_duration_ms = (
                        sample.qpc_seconds - stable_since_qpc
                    ) * 1000.0
                    if (
                        stable_count >= boundary_samples
                        and stable_duration_ms >= stable_boundary_duration_ms
                    ):
                        saw_gameplay_boundary = True
                    recent_running = [sample]
                elif delta > boundary_epsilon_ms:
                    stable_count = 0
                    stable_since_qpc = None
                    recent_running.append(sample)
                    if len(recent_running) > running_samples:
                        recent_running = recent_running[-running_samples:]
                    if saw_gameplay_boundary:
                        return sample
                    early_running_range = (
                        in_range
                        and (
                            stable_boundary_max_ms is None
                            or sample.time_ms <= stable_boundary_max_ms
                        )
                    )
                    if (
                        allow_running_without_boundary
                        and len(recent_running) >= running_samples
                        and early_running_range
                    ):
                        elapsed_s = recent_running[-1].qpc_seconds - recent_running[0].qpc_seconds
                        delta_ms = recent_running[-1].time_ms - recent_running[0].time_ms
                        if elapsed_s > 0 and delta_ms >= min_delta_ms:
                            rate_ms_per_s = delta_ms / elapsed_s
                            if 100.0 <= rate_ms_per_s <= 3000.0:
                                return recent_running[-1]
                else:
                    stable_count = 0
                    stable_since_qpc = None
                    recent_running.clear()
            elif in_range:
                stable_count = 1
                stable_since_qpc = sample.qpc_seconds
                recent_running = [sample]
            previous = sample

    def discard_pending_events(self) -> None:
        while True:
            try:
                self._remember_event(self._events.get_nowait())
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
            event = self._events.get(timeout=max(timeout_s, 0.001))
            self._remember_event(event)
            return event
        except queue.Empty as exc:
            raise ClockSyncError("timed out waiting for osu!lazer clock reader") from exc

    def _remember_event(self, event: dict[str, object]) -> None:
        sample = _event_to_sample(event)
        if sample is not None:
            self._last_sample = sample


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
        source_address = event.get("current_time_address")
        return ClockSample(
            time_ms=float(event["time_ms"]),
            qpc_seconds=float(event["qpc_seconds"]),
            source_address=str(source_address) if source_address is not None else None,
        )
    except (KeyError, TypeError, ValueError):
        return None
