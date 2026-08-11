from types import SimpleNamespace

import replay_bot_enter as replay_bot
from replay_bot_enter import (
    KK1,
    KK2,
    SPARSE_MOVEMENT_ERROR_COUNT,
    preposition_cursor_positions,
    should_try_auto_space,
    sparse_movement_gaps,
)


def test_auto_space_does_not_trigger_for_short_zero_leadin() -> None:
    assert not should_try_auto_space(-11.7, 1000.0)


def test_auto_space_triggers_for_clear_opening_intro() -> None:
    assert should_try_auto_space(0.0, 3000.0)


def replay_frames_with_sparse_movements(count: int) -> list[SimpleNamespace]:
    frames = [SimpleNamespace(time_delta=0, x=0.0, y=0.0)]
    for index in range(count):
        frames.append(
            SimpleNamespace(
                time_delta=166,
                x=100.0 if index % 2 == 0 else 0.0,
                y=0.0,
            )
        )
    return frames


def test_sparse_movement_detection_flags_a_visible_gap() -> None:
    gaps = sparse_movement_gaps(replay_frames_with_sparse_movements(1))

    assert len(gaps) == 1
    assert len(gaps) >= SPARSE_MOVEMENT_ERROR_COUNT


def test_sparse_movement_detection_rejects_isolated_tail_gap() -> None:
    gaps = sparse_movement_gaps(replay_frames_with_sparse_movements(3))

    assert len(gaps) >= SPARSE_MOVEMENT_ERROR_COUNT


def test_sparse_movement_detection_ignores_stationary_and_long_idle_frames() -> None:
    frames = [
        SimpleNamespace(time_delta=0, x=0.0, y=0.0),
        SimpleNamespace(time_delta=166, x=0.0, y=0.0),
        SimpleNamespace(time_delta=1000, x=200.0, y=200.0),
    ]

    assert sparse_movement_gaps(frames) == []


def test_frame_input_batches_mouse_before_key_changes(monkeypatch) -> None:
    batches = []
    monkeypatch.setattr(replay_bot, "_send_inputs", lambda inputs: batches.append(inputs))

    replay_bot.send_frame_input(
        12345,
        23456,
        [(replay_bot.VK_Z, True), (replay_bot.VK_X, False)],
    )

    assert len(batches) == 1
    mouse, key_down, key_up = batches[0]
    assert mouse.tp == replay_bot.IM
    assert (mouse.u.mi.dx, mouse.u.mi.dy) == (12345, 23456)
    assert mouse.u.mi.fl == replay_bot.MF_M | replay_bot.MF_NC | replay_bot.MF_A
    assert key_down.tp == replay_bot.IK
    assert key_down.u.ki.vk == replay_bot.VK_Z
    assert key_down.u.ki.fl == replay_bot.KF_SCANCODE
    assert key_up.tp == replay_bot.IK
    assert key_up.u.ki.vk == replay_bot.VK_X
    assert key_up.u.ki.fl == replay_bot.KF_SCANCODE | replay_bot.KF_UP


def test_cursor_preposition_holds_upcoming_press_position() -> None:
    frames = [
        SimpleNamespace(time_delta=0, x=0.0, y=0.0, keys=0),
        SimpleNamespace(time_delta=75, x=75.0, y=0.0, keys=0),
        SimpleNamespace(time_delta=15, x=90.0, y=0.0, keys=0),
        SimpleNamespace(time_delta=10, x=100.0, y=10.0, keys=KK1),
        SimpleNamespace(time_delta=10, x=110.0, y=20.0, keys=KK1),
        SimpleNamespace(time_delta=10, x=120.0, y=30.0, keys=0),
    ]

    positions = preposition_cursor_positions(frames, (0, 75, 90, 100, 110, 120), 25, 10)

    assert positions == (
        (0.0, 0.0),
        (75.0, 0.0),
        (100.0, 10.0),
        (100.0, 10.0),
        (110.0, 20.0),
        (120.0, 30.0),
    )


def test_cursor_preposition_prioritizes_each_dense_press_in_order() -> None:
    frames = [
        SimpleNamespace(time_delta=0, x=0.0, y=0.0, keys=0),
        SimpleNamespace(time_delta=100, x=100.0, y=10.0, keys=KK1),
        SimpleNamespace(time_delta=5, x=105.0, y=15.0, keys=0),
        SimpleNamespace(time_delta=5, x=110.0, y=20.0, keys=0),
        SimpleNamespace(time_delta=10, x=120.0, y=30.0, keys=KK2),
    ]

    positions = preposition_cursor_positions(frames, (0, 100, 105, 110, 120), 25, 10)

    assert positions[1] == (100.0, 10.0)
    assert positions[2] != (105.0, 15.0)
    assert positions[2] != (120.0, 30.0)
    assert positions[3:] == ((120.0, 30.0),) * 2
