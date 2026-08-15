from types import SimpleNamespace

import pytest

from lazer_clock_sync import (
    ClockSample,
    ClockSyncError,
    LazerClockReader,
    LiveClockCorrector,
    ReplayTimeline,
    WslLazerClockReader,
    initial_key_mask,
)


def frame(delta: int, keys: int = 0) -> SimpleNamespace:
    return SimpleNamespace(time_delta=delta, keys=keys)


def test_replay_timeline_uses_absolute_frame_times() -> None:
    timeline = ReplayTimeline.from_frames([frame(0), frame(120), frame(80)])

    assert timeline.frame_times_ms == (0.0, 120.0, 200.0)
    assert timeline.first_frame_at_or_after(120) == 1
    assert timeline.first_frame_at_or_after(121) == 2


def test_replay_timeline_finds_first_key_time() -> None:
    frames = [frame(0, 0), frame(250, 0), frame(500, 4)]
    timeline = ReplayTimeline.from_frames(frames)

    assert timeline.first_key_time_ms(frames) == 750.0


def test_initial_key_mask_preserves_key_held_at_clock_cut() -> None:
    frames = [frame(0, 0), frame(100, 1), frame(100, 1), frame(100, 0)]

    assert initial_key_mask(frames, 0) == 0
    assert initial_key_mask(frames, 2) == 1
    assert initial_key_mask(frames, 3) == 1
    assert initial_key_mask(frames, 4) == 0


def test_live_clock_corrector_advances_input_when_game_clock_runs_fast() -> None:
    anchor = ClockSample(time_ms=0.0, qpc_seconds=100.0, source_address="gameplay")
    corrector = LiveClockCorrector(anchor, 1.5, window_size=3, minimum_samples=3)

    corrector.observe(
        ClockSample(time_ms=map_ms, qpc_seconds=100.0 + real_s, source_address="gameplay")
        for real_s, map_ms in [(1.0, 1500.0), (2.0, 3001.5), (3.0, 4503.0)]
    )
    initial_target = corrector.target_qpc_seconds(6000.0)

    corrector.observe(
        ClockSample(time_ms=map_ms, qpc_seconds=100.0 + real_s, source_address="gameplay")
        for real_s, map_ms in [(4.0, 6006.0), (5.0, 7507.5), (6.0, 9009.0)]
    )

    assert corrector.correction_ms < 0
    assert corrector.target_qpc_seconds(6000.0) < initial_target


def test_live_clock_corrector_rejects_samples_from_another_clock_source() -> None:
    anchor = ClockSample(time_ms=0.0, qpc_seconds=100.0, source_address="gameplay")
    corrector = LiveClockCorrector(anchor, 1.0, window_size=1, minimum_samples=1)

    corrector.observe(
        [ClockSample(time_ms=5000.0, qpc_seconds=101.0, source_address="preview")]
    )

    assert corrector.correction_ms == 0.0


def test_clock_reader_drains_available_samples_without_blocking() -> None:
    reader = LazerClockReader(1, reader_path="missing.exe")
    reader._events.put({"event": "sample", "time_ms": 100.0, "qpc_seconds": 1.0})
    reader._events.put({"event": "attached"})
    reader._events.put({"event": "sample", "time_ms": 120.0, "qpc_seconds": 1.02})

    samples = reader.drain_samples()

    assert [(sample.time_ms, sample.qpc_seconds) for sample in samples] == [
        (100.0, 1.0),
        (120.0, 1.02),
    ]
    assert reader.drain_samples() == ()


def test_wait_for_running_clock_uses_forward_moving_samples() -> None:
    reader = LazerClockReader(1, reader_path="missing.exe", wait_for_jump=False, stream_samples=True)
    for time_ms, qpc_seconds in [(100.0, 1.0), (125.0, 1.02), (150.0, 1.04)]:
        reader._events.put({"event": "sample", "time_ms": time_ms, "qpc_seconds": qpc_seconds})

    sample = reader.wait_for_running_clock(0.5, required_samples=3)

    assert sample.time_ms == 150.0
    assert sample.qpc_seconds == 1.04


def test_wait_for_running_clock_ignores_samples_after_max_time() -> None:
    reader = LazerClockReader(1, reader_path="missing.exe", wait_for_jump=False, stream_samples=True)
    for time_ms, qpc_seconds in [(5000.0, 1.0), (5010.0, 1.01), (200.0, 2.0), (230.0, 2.03), (260.0, 2.06)]:
        reader._events.put({"event": "sample", "time_ms": time_ms, "qpc_seconds": qpc_seconds})

    sample = reader.wait_for_running_clock(0.5, max_time_ms=1000.0, required_samples=3)

    assert sample.time_ms == 260.0


def test_wait_for_gameplay_start_requires_preview_clock_boundary() -> None:
    reader = LazerClockReader(1, reader_path="missing.exe", wait_for_jump=False, stream_samples=True)
    for time_ms, qpc_seconds in [
        (5000.0, 1.00),
        (5025.0, 1.02),
        (5050.0, 1.04),
        (5050.0, 1.06),
        (-120.0, 2.00),
        (-95.0, 2.02),
        (-70.0, 2.04),
    ]:
        reader._events.put({"event": "sample", "time_ms": time_ms, "qpc_seconds": qpc_seconds})

    sample = reader.wait_for_gameplay_start(0.5, boundary_samples=2)

    assert sample.time_ms == -95.0


def test_wait_for_gameplay_start_anchors_same_value_restart_without_extra_samples() -> None:
    reader = LazerClockReader(1, reader_path="missing.exe", wait_for_jump=False, stream_samples=True)
    for time_ms, qpc_seconds in [
        (0.0, 1.00),
        (0.0, 1.06),
        (0.0, 1.12),
        (5.0, 1.14),
    ]:
        reader._events.put({"event": "sample", "time_ms": time_ms, "qpc_seconds": qpc_seconds})

    sample = reader.wait_for_gameplay_start(0.5, boundary_samples=2)

    assert sample.time_ms == 5.0
    assert sample.qpc_seconds == 1.14


def test_wait_for_gameplay_start_does_not_return_stopped_reset_clock() -> None:
    reader = LazerClockReader(1, reader_path="missing.exe", wait_for_jump=False, stream_samples=True)
    for time_ms, qpc_seconds in [
        (5000.0, 1.00),
        (5050.0, 1.05),
        (0.0, 2.00),
        (0.0, 2.02),
        (0.0, 2.04),
    ]:
        reader._events.put({"event": "sample", "time_ms": time_ms, "qpc_seconds": qpc_seconds})

    with pytest.raises(ClockSyncError):
        reader.wait_for_gameplay_start(0.01, boundary_samples=2)


def test_wait_for_gameplay_start_accepts_running_clock_when_boundary_was_missed() -> None:
    reader = LazerClockReader(1, reader_path="missing.exe", wait_for_jump=False, stream_samples=True)
    for time_ms, qpc_seconds in [
        (15.0, 1.00),
        (20.0, 1.005),
        (25.0, 1.010),
        (30.0, 1.015),
    ]:
        reader._events.put({"event": "sample", "time_ms": time_ms, "qpc_seconds": qpc_seconds})

    sample = reader.wait_for_gameplay_start(0.5, max_time_ms=1000.0, running_samples=3)

    assert sample.time_ms == 25.0


def test_wait_for_gameplay_start_rejects_preview_clock_without_boundary() -> None:
    reader = LazerClockReader(1, reader_path="missing.exe", wait_for_jump=False, stream_samples=True)
    for time_ms, qpc_seconds in [
        (15.0, 1.000),
        (15.0, 1.005),
        (15.0, 1.010),
        (40.0, 1.025),
        (40.0, 1.030),
        (40.0, 1.035),
        (65.0, 1.050),
    ]:
        reader._events.put({
            "event": "sample",
            "time_ms": time_ms,
            "qpc_seconds": qpc_seconds,
            "current_time_address": "preview",
        })

    with pytest.raises(ClockSyncError):
        reader.wait_for_gameplay_start(
            0.01,
            max_time_ms=1000.0,
            running_samples=3,
            allow_running_without_boundary=False,
        )


def test_wait_for_gameplay_start_accepts_clock_source_transition() -> None:
    reader = LazerClockReader(1, reader_path="missing.exe", wait_for_jump=False, stream_samples=True)
    for time_ms, qpc_seconds, source_address in [
        (15.0, 1.000, "preview"),
        (20.0, 1.005, "preview"),
        (25.0, 1.010, "preview"),
        (-100.0, 2.000, "gameplay"),
        (-95.0, 2.005, "gameplay"),
    ]:
        reader._events.put({
            "event": "sample",
            "time_ms": time_ms,
            "qpc_seconds": qpc_seconds,
            "current_time_address": source_address,
        })

    sample = reader.wait_for_gameplay_start(
        0.5,
        max_time_ms=1000.0,
        allow_running_without_boundary=False,
    )

    assert sample.time_ms == -95.0
    assert sample.source_address == "gameplay"


def test_wait_for_gameplay_start_accepts_high_time_after_transition_boundary() -> None:
    reader = LazerClockReader(1, reader_path="missing.exe", wait_for_jump=False, stream_samples=True)
    for time_ms, qpc_seconds in [
        (9000.0, 1.00),
        (9000.0, 1.06),
        (9000.0, 1.12),
        (9020.0, 1.14),
    ]:
        reader._events.put({"event": "sample", "time_ms": time_ms, "qpc_seconds": qpc_seconds})

    sample = reader.wait_for_gameplay_start(0.5, max_time_ms=1000.0, boundary_samples=2)

    assert sample.time_ms == 9020.0


def test_wsl_clock_calibration_uses_lowest_round_trip(monkeypatch) -> None:
    reader = WslLazerClockReader("Ubuntu-24.04", reader_path="missing.py")
    windows_times = iter([100.000, 100.020, 200.000, 200.004])
    events = iter(
        [
            {"event": "sync", "id": 0, "qpc_seconds": 10.005},
            {"event": "sync", "id": 1, "qpc_seconds": 20.001},
        ]
    )
    monkeypatch.setattr("lazer_clock_sync.time.perf_counter", lambda: next(windows_times))
    monkeypatch.setattr(reader, "_send_control", lambda _value: None)
    monkeypatch.setattr(reader, "_next_event", lambda _timeout: next(events))

    reader._calibrate_clock(sample_count=2)

    assert reader._qpc_offset_seconds == pytest.approx(180.001)
