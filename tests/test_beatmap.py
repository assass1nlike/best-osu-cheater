from synthesis_osu_play.beatmap import Beatmap


def test_parse_beatmap_hit_objects_and_difficulty() -> None:
    beatmap = Beatmap.from_bytes(
        b"""osu file format v14

[Difficulty]
HPDrainRate:6
CircleSize:4
OverallDifficulty:7
SliderMultiplier:1.4
SliderTickRate:1

[General]
AudioLeadIn:500

[Events]
0,0,"background.jpg"
2,147054,161374

[TimingPoints]
0,500,4,2,1,60,1,0

[HitObjects]
256,192,1000,1,0,0:0:0:0:
128,96,2000,2,0,B|256:96,1,140
256,192,3000,8,0,4000
"""
    )

    assert beatmap.audio_lead_in_ms == 500
    assert beatmap.hp_drain_rate == 6
    assert beatmap.circle_size == 4
    assert beatmap.overall_difficulty == 7
    assert beatmap.slider_multiplier == 1.4
    assert beatmap.slider_tick_rate == 1
    assert beatmap.hit_window_50_ms == 130
    assert beatmap.first_hit_object_time_ms == 1000
    assert [(period.start_ms, period.end_ms) for period in beatmap.break_periods] == [
        (147054, 161374)
    ]
    assert [obj.time_ms for obj in beatmap.clickable_hit_objects] == [1000, 2000]
    assert beatmap.hit_objects[1].end_time_ms == 2500
    assert beatmap.hit_objects[1].slider_nested_hit_count == 1
    assert beatmap.hit_objects[1].slider_curve_type == "B"
    assert beatmap.hit_objects[1].slider_control_points == ((128.0, 96.0), (256.0, 96.0))
    assert beatmap.hit_objects[2].end_time_ms == 4000
    assert beatmap.max_combo == 4
