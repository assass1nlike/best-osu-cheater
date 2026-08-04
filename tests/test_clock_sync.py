from types import SimpleNamespace

from lazer_clock_sync import LazerClockReader, ReplayTimeline, initial_key_mask


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
