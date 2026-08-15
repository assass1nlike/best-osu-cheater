import importlib.util
import json
from pathlib import Path
import sys

import pytest


module_path = Path(__file__).parents[1] / "tools" / "wsl" / "prepare_lazer_input.py"
performance_module_path = (
    Path(__file__).parents[1] / "tools" / "wsl" / "prepare_lazer_performance.py"
)
launcher_path = Path(__file__).parents[1] / "launch_wsl_lazer.ps1"
module_spec = importlib.util.spec_from_file_location("prepare_lazer_input", module_path)
assert module_spec is not None and module_spec.loader is not None
prepare_lazer_input = importlib.util.module_from_spec(module_spec)
sys.modules[module_spec.name] = prepare_lazer_input
module_spec.loader.exec_module(prepare_lazer_input)

performance_module_spec = importlib.util.spec_from_file_location(
    "prepare_lazer_performance", performance_module_path
)
assert performance_module_spec is not None and performance_module_spec.loader is not None
prepare_lazer_performance = importlib.util.module_from_spec(performance_module_spec)
sys.modules[performance_module_spec.name] = prepare_lazer_performance
performance_module_spec.loader.exec_module(prepare_lazer_performance)

disable_relative_mouse_mode = prepare_lazer_input.disable_relative_mouse_mode
prepare_input_config = prepare_lazer_input.prepare_input_config
prepare_framework_config = prepare_lazer_performance.prepare_framework_config
update_settings = prepare_lazer_performance.update_settings


def make_config(relative_mode: bool = True) -> dict:
    return {
        "InputHandlers": [
            {
                "$type": "osu.Framework.Input.Handlers.Keyboard.KeyboardHandler, osu.Framework",
                "Enabled": True,
            },
            {
                "$type": "osu.Framework.Input.Handlers.Mouse.MouseHandler, osu.Framework",
                "UseRelativeMode": relative_mode,
                "Sensitivity": 1.0,
                "Enabled": True,
            },
        ]
    }


def test_disable_relative_mouse_mode_preserves_other_settings() -> None:
    payload = make_config()

    assert disable_relative_mouse_mode(payload) is True
    mouse = payload["InputHandlers"][1]
    assert mouse["UseRelativeMode"] is False
    assert mouse["Sensitivity"] == 1.0
    assert payload["InputHandlers"][0]["Enabled"] is True


def test_prepare_input_config_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "input.json"
    path.write_text(json.dumps(make_config()), encoding="utf-8")

    assert prepare_input_config(path) is True
    first_result = path.read_text(encoding="utf-8")
    assert prepare_input_config(path) is False
    assert path.read_text(encoding="utf-8") == first_result


def test_disable_relative_mouse_mode_requires_one_mouse_handler() -> None:
    with pytest.raises(ValueError, match="exactly one mouse input handler"):
        disable_relative_mouse_mode({"InputHandlers": []})


def test_launcher_disables_wslg_mouse_capture_paths() -> None:
    launcher = launcher_path.read_text(encoding="utf-8")

    assert '"SDL_VIDEODRIVER=x11"' in launcher
    assert '"SDL_VIDEO_X11_XINPUT2=0"' in launcher
    assert '"SDL_MOUSE_AUTO_CAPTURE=0"' in launcher


def test_performance_profile_updates_only_renderer_settings() -> None:
    contents = (
        "WindowedSize = 2560x1440\n"
        "ExecutionMode = SingleThread\n"
        "FrameSync = Limit4x\n"
        "WindowMode = Borderless\n"
        "VolumeUniversal = 0.57\n"
    )

    updated = update_settings(
        contents, prepare_lazer_performance.PROFILE_SETTINGS["balanced"]
    )

    assert "WindowedSize = 1366x768\n" in updated
    assert "ExecutionMode = MultiThreaded\n" in updated
    assert "FrameSync = Limit4x\n" in updated
    assert "WindowMode = Windowed\n" in updated
    assert "VolumeUniversal = 0.57\n" in updated


def test_native_profile_preserves_restored_window_size(tmp_path: Path) -> None:
    path = tmp_path / "framework.ini"
    path.write_text(
        "WindowedSize = 1600x900\nWindowMode = Borderless\nFrameSync = Limit4x\n",
        encoding="utf-8",
    )

    assert prepare_framework_config(path, "native") is True
    updated = path.read_text(encoding="utf-8")
    assert "WindowedSize = 1600x900\n" in updated
    assert "WindowMode = Windowed\n" in updated
    assert "FrameSync = Limit4x\n" in updated
    assert "ExecutionMode = MultiThreaded\n" in updated


def test_launcher_restores_non_native_window_from_wslg_maximisation() -> None:
    launcher = launcher_path.read_text(encoding="utf-8")

    assert '[ValidateSet("Balanced", "Quality", "Native")]' in launcher
    assert '[string] $PerformanceProfile = "Balanced"' in launcher
    assert "[WslgWindow]::ShowWindow" in launcher
