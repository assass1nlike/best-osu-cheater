import random

from auto_batch_play import (
    ABSOLUTE_COORDINATE_MAX,
    SAFE_CURSOR_MAX_RATIO,
    SAFE_CURSOR_MIN_RATIO,
    random_safe_cursor_position,
)


def test_random_safe_cursor_position_stays_in_central_screen_region() -> None:
    rng = random.Random(123)
    minimum = round(SAFE_CURSOR_MIN_RATIO * ABSOLUTE_COORDINATE_MAX)
    maximum = round(SAFE_CURSOR_MAX_RATIO * ABSOLUTE_COORDINATE_MAX)

    positions = [random_safe_cursor_position(rng) for _ in range(100)]

    assert all(
        minimum <= x <= maximum and minimum <= y <= maximum
        for x, y in positions
    )
    assert len(set(positions)) > 1
