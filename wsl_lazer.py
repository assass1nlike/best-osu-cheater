"""Windows-side helpers for an osu!lazer window hosted by WSLg."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
import re
import subprocess


WSLG_TITLE_PATTERN = re.compile(r"^osu! \(([^()]+)\)$", re.IGNORECASE)
DEFAULT_WSLG_CURSOR_HZ = 60.0
_TIMING_EPSILON_S = 1e-9


@dataclass(frozen=True)
class WslgViewport:
    left: int
    top: int
    width: int
    height: int


@dataclass
class WslgInputRateLimiter:
    """Limit WSLg cursor traffic while preserving every keyboard edge."""

    cursor_hz: float = DEFAULT_WSLG_CURSOR_HZ
    _last_cursor_target_s: float | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        if self.cursor_hz <= 0:
            raise ValueError("cursor_hz must be positive")

    @property
    def interval_s(self) -> float:
        return 1.0 / self.cursor_hz

    def should_send(
        self,
        target_s: float,
        now_s: float,
        *,
        has_key_events: bool = False,
        force: bool = False,
    ) -> bool:
        if has_key_events or force:
            self._last_cursor_target_s = target_s
            return True

        # Catch up by discarding old movement instead of flooding WSLg with it.
        if now_s - target_s + _TIMING_EPSILON_S >= self.interval_s:
            return False

        if (
            self._last_cursor_target_s is not None
            and target_s - self._last_cursor_target_s + _TIMING_EPSILON_S < self.interval_s
        ):
            return False

        self._last_cursor_target_s = target_s
        return True


def distro_from_window_title(title: str) -> str | None:
    match = WSLG_TITLE_PATTERN.fullmatch(title.strip())
    return match.group(1) if match is not None else None


def to_wsl_path(path: str | Path) -> str:
    windows_path = PureWindowsPath(Path(path).resolve())
    if not windows_path.drive or len(windows_path.drive) != 2:
        raise ValueError(f"cannot convert path to WSL: {path}")
    drive = windows_path.drive[0].lower()
    relative = "/".join(windows_path.parts[1:])
    return f"/mnt/{drive}/{relative}"


def query_viewport(distro: str, *, timeout_s: float = 5.0) -> WslgViewport:
    try:
        result = subprocess.run(
            ["wsl.exe", "-d", distro, "--", "xwininfo", "-name", "osu!"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout_s,
            check=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"could not query the WSLg osu! window: {exc}") from exc

    return parse_xwininfo(result.stdout)


def parse_xwininfo(contents: str) -> WslgViewport:
    values: dict[str, int] = {}
    labels = {
        "Absolute upper-left X": "left",
        "Absolute upper-left Y": "top",
        "Width": "width",
        "Height": "height",
    }
    for line in contents.splitlines():
        label, separator, value = line.strip().partition(":")
        if separator and label in labels:
            try:
                values[labels[label]] = int(value.strip())
            except ValueError:
                pass
    if set(values) != {"left", "top", "width", "height"}:
        raise RuntimeError("xwininfo did not return a complete osu! viewport")
    if values["width"] <= 0 or values["height"] <= 0:
        raise RuntimeError("WSLg reported an invalid osu! viewport size")
    return WslgViewport(**values)
