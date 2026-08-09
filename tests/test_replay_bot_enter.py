from replay_bot_enter import should_try_auto_space


def test_auto_space_does_not_trigger_for_short_zero_leadin() -> None:
    assert not should_try_auto_space(-11.7, 1000.0)


def test_auto_space_triggers_for_clear_opening_intro() -> None:
    assert should_try_auto_space(0.0, 3000.0)
