from math import hypot


SPARSE_MOVEMENT_MIN_GAP_MS = 50.0
SPARSE_MOVEMENT_MAX_GAP_MS = 250.0
SPARSE_MOVEMENT_MIN_DISTANCE = 20.0
SPARSE_MOVEMENT_ERROR_COUNT = 1


def sparse_movement_gaps(
    frames,
    min_gap_ms=SPARSE_MOVEMENT_MIN_GAP_MS,
    max_gap_ms=SPARSE_MOVEMENT_MAX_GAP_MS,
    min_distance=SPARSE_MOVEMENT_MIN_DISTANCE,
):
    gaps = []
    for previous, current in zip(frames, frames[1:]):
        delta_ms = float(current.time_delta)
        distance = hypot(
            float(current.x) - float(previous.x),
            float(current.y) - float(previous.y),
        )
        if min_gap_ms <= delta_ms <= max_gap_ms and distance >= min_distance:
            gaps.append((delta_ms, distance))
    return gaps
