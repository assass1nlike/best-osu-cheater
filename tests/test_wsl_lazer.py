from pathlib import Path

import pytest

from wsl_lazer import (
    WslgInputRateLimiter,
    distro_from_window_title,
    parse_xwininfo,
    to_wsl_path,
)


def test_distro_from_wslg_window_title() -> None:
    assert distro_from_window_title("osu! (Ubuntu-24.04)") == "Ubuntu-24.04"
    assert distro_from_window_title("osu!") is None
    assert distro_from_window_title("osu!lazer") is None


def test_windows_path_converts_to_wsl_mount() -> None:
    converted = to_wsl_path(Path("D:/lxxzyh/synthesis-osu-play/README.md"))

    assert converted == "/mnt/d/lxxzyh/synthesis-osu-play/README.md"


def test_xwininfo_viewport_parser() -> None:
    viewport = parse_xwininfo(
        """
          Absolute upper-left X:  1243
          Absolute upper-left Y:  723
          Width: 1366
          Height: 768
        """
    )

    assert (viewport.left, viewport.top, viewport.width, viewport.height) == (
        1243,
        723,
        1366,
        768,
    )


def test_xwininfo_viewport_parser_requires_all_dimensions() -> None:
    with pytest.raises(RuntimeError, match="complete"):
        parse_xwininfo("Width: 1366\nHeight: 768\n")


def test_wslg_input_limiter_caps_pure_cursor_updates() -> None:
    limiter = WslgInputRateLimiter(cursor_hz=100.0)

    assert limiter.should_send(10.000, 10.000)
    assert not limiter.should_send(10.005, 10.005)
    assert limiter.should_send(10.010, 10.010)


def test_wslg_input_limiter_preserves_key_edges() -> None:
    limiter = WslgInputRateLimiter(cursor_hz=100.0)

    assert limiter.should_send(10.000, 10.000)
    assert limiter.should_send(10.001, 10.100, has_key_events=True)


def test_wslg_input_limiter_drops_stale_movement_but_not_final_position() -> None:
    limiter = WslgInputRateLimiter(cursor_hz=100.0)

    assert not limiter.should_send(10.000, 10.010)
    assert limiter.should_send(10.001, 10.100, force=True)


def test_wslg_input_limiter_requires_positive_rate() -> None:
    with pytest.raises(ValueError, match="positive"):
        WslgInputRateLimiter(cursor_hz=0)
