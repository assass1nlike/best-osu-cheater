from __future__ import annotations

from dataclasses import dataclass


OSU_PLAYFIELD_WIDTH = 512.0
OSU_PLAYFIELD_HEIGHT = 384.0
OSU_PLAYFIELD_SIZE_ADJUST = 0.8
OSU_GAMEPLAY_Y_SHIFT = 8.0
ABSOLUTE_COORDINATE_MAX = 65535


@dataclass(frozen=True)
class FullscreenPlayfield:
    """The osu!lazer gameplay playfield fitted to a fullscreen window.

    These values mirror OsuPlayfieldAdjustmentContainer in osu!lazer:
    the playfield container consumes 80% of the window and gameplay is shifted
    down by 8 base playfield units for storyboard alignment.
    """

    screen_width: int
    screen_height: int
    scale: float
    left: float
    top: float

    @classmethod
    def from_screen(cls, screen_width: int, screen_height: int) -> FullscreenPlayfield:
        if screen_width <= 0 or screen_height <= 0:
            raise ValueError("screen dimensions must be positive")
        adjusted_width = screen_width * OSU_PLAYFIELD_SIZE_ADJUST
        adjusted_height = screen_height * OSU_PLAYFIELD_SIZE_ADJUST
        scale = min(
            adjusted_width / OSU_PLAYFIELD_WIDTH,
            adjusted_height / OSU_PLAYFIELD_HEIGHT,
        )
        width = OSU_PLAYFIELD_WIDTH * scale
        height = OSU_PLAYFIELD_HEIGHT * scale
        return cls(
            screen_width=screen_width,
            screen_height=screen_height,
            scale=scale,
            left=(screen_width - width) / 2.0,
            top=(screen_height - height) / 2.0 + OSU_GAMEPLAY_Y_SHIFT * scale,
        )

    @property
    def width(self) -> float:
        return OSU_PLAYFIELD_WIDTH * self.scale

    @property
    def height(self) -> float:
        return OSU_PLAYFIELD_HEIGHT * self.scale

    def to_absolute(
        self,
        osu_x: float,
        osu_y: float,
        *,
        hard_rock: bool = False,
    ) -> tuple[int, int]:
        if hard_rock:
            osu_y = OSU_PLAYFIELD_HEIGHT - osu_y
        screen_x = self.left + osu_x * self.scale
        screen_y = self.top + osu_y * self.scale
        absolute_x = round(screen_x * ABSOLUTE_COORDINATE_MAX / max(self.screen_width - 1, 1))
        absolute_y = round(screen_y * ABSOLUTE_COORDINATE_MAX / max(self.screen_height - 1, 1))
        return (
            max(0, min(ABSOLUTE_COORDINATE_MAX, absolute_x)),
            max(0, min(ABSOLUTE_COORDINATE_MAX, absolute_y)),
        )
