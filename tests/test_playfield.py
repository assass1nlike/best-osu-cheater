import pytest

from synthesis_osu_play.playfield import FullscreenPlayfield


def test_fullscreen_playfield_fits_4_3_screen() -> None:
    playfield = FullscreenPlayfield.from_screen(1920, 1080)

    assert playfield.scale == pytest.approx(2.25)
    assert playfield.width == pytest.approx(1152.0)
    assert playfield.height == pytest.approx(864.0)
    assert playfield.left == pytest.approx(384.0)
    assert playfield.top == pytest.approx(126.0)


def test_fullscreen_playfield_maps_center_and_clamps_edges() -> None:
    playfield = FullscreenPlayfield.from_screen(1920, 1080)

    assert playfield.to_absolute(256, 192) == (32785, round(558 * 65535 / 1079))
    assert playfield.to_absolute(0, 0)[0] == round(384 * 65535 / 1919)
    assert playfield.to_absolute(512, 384) == (round(1536 * 65535 / 1919), round(990 * 65535 / 1079))
    assert playfield.to_absolute(1000, 384) == (65535, round(990 * 65535 / 1079))
    assert playfield.to_absolute(1000, 1000) == (65535, 65535)


def test_fullscreen_playfield_rejects_invalid_screen() -> None:
    with pytest.raises(ValueError):
        FullscreenPlayfield.from_screen(0, 1080)
