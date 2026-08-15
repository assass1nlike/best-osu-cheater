import importlib.util
from pathlib import Path
import sys


module_path = Path(__file__).parents[1] / "tools" / "wsl" / "lazer_clock_reader.py"
module_spec = importlib.util.spec_from_file_location("wsl_clock_reader", module_path)
assert module_spec is not None and module_spec.loader is not None
wsl_clock_reader = importlib.util.module_from_spec(module_spec)
sys.modules[module_spec.name] = wsl_clock_reader
module_spec.loader.exec_module(wsl_clock_reader)


def test_parse_readable_regions_filters_unreadable_mappings() -> None:
    regions = wsl_clock_reader.parse_readable_regions(
        """
40000000-40600000 rw-p 00000000 00:00 0
80400000-a0000000 ---p 00000000 00:00 0
7f000000-7f001000 r-xp 00000000 00:00 0
        """
    )

    assert [(region.start, region.end) for region in regions] == [
        (0x40000000, 0x40600000),
        (0x7F000000, 0x7F001000),
    ]


def test_offset_data_loads_existing_reader_offsets() -> None:
    offsets = wsl_clock_reader.OffsetData.load(
        Path(__file__).parents[1] / "tools" / "LazerClockReader" / "offsets.json"
    )

    assert offsets.scan_pattern == bytes.fromhex("01 01 00 00 00 00 80 44 00 00 40 44")
    assert offsets.beatmap_clock > 0
