from __future__ import annotations


MOD_NO_FAIL = 1 << 0
MOD_EASY = 1 << 1
MOD_TOUCH_DEVICE = 1 << 2
MOD_HIDDEN = 1 << 3
MOD_HARD_ROCK = 1 << 4
MOD_SUDDEN_DEATH = 1 << 5
MOD_DOUBLE_TIME = 1 << 6
MOD_RELAX = 1 << 7
MOD_HALF_TIME = 1 << 8
MOD_NIGHTCORE = 1 << 9
MOD_FLASHLIGHT = 1 << 10
MOD_AUTOPLAY = 1 << 11
MOD_SPUN_OUT = 1 << 12
MOD_AUTOPILOT = 1 << 13
MOD_PERFECT = 1 << 14

FAST_SPEED_MOD_MASK = MOD_DOUBLE_TIME | MOD_NIGHTCORE
SPEED_MOD_MASK = FAST_SPEED_MOD_MASK | MOD_HALF_TIME
SYNTHESIS_VARIABLE_MOD_MASK = MOD_HIDDEN | MOD_HARD_ROCK | SPEED_MOD_MASK

SPEED_NORMAL = "normal"
SPEED_FAST = "fast"
SPEED_SLOW = "slow"


def speed_mod_category(mods: int) -> str:
    has_fast = bool(mods & FAST_SPEED_MOD_MASK)
    has_slow = bool(mods & MOD_HALF_TIME)
    if has_fast and has_slow:
        raise ValueError("DT/NC and HT cannot be active at the same time")
    if has_fast:
        return SPEED_FAST
    if has_slow:
        return SPEED_SLOW
    return SPEED_NORMAL


def canonical_speed_mod_bits(category: str, first_mods: int, second_mods: int) -> int:
    if category == SPEED_NORMAL:
        return 0
    if category == SPEED_SLOW:
        return MOD_HALF_TIME
    if category != SPEED_FAST:
        raise ValueError(f"unknown speed mod category: {category}")
    if first_mods & MOD_NIGHTCORE and second_mods & MOD_NIGHTCORE:
        return MOD_NIGHTCORE | MOD_DOUBLE_TIME
    return MOD_DOUBLE_TIME


def legacy_mods_to_lazer_metadata(mods: int) -> list[dict[str, str]]:
    return [{"acronym": acronym} for acronym in legacy_mod_acronyms(mods)]


def legacy_mod_acronyms(mods: int) -> tuple[str, ...]:
    acronyms: list[str] = []
    ordered_mods = (
        (MOD_NO_FAIL, "NF"),
        (MOD_EASY, "EZ"),
        (MOD_TOUCH_DEVICE, "TD"),
        (MOD_HIDDEN, "HD"),
        (MOD_HARD_ROCK, "HR"),
        (MOD_SUDDEN_DEATH, "SD"),
        (MOD_RELAX, "RX"),
        (MOD_HALF_TIME, "HT"),
        (MOD_FLASHLIGHT, "FL"),
        (MOD_AUTOPLAY, "AT"),
        (MOD_SPUN_OUT, "SO"),
        (MOD_AUTOPILOT, "AP"),
        (MOD_PERFECT, "PF"),
    )
    for bit, acronym in ordered_mods:
        if mods & bit:
            acronyms.append(acronym)
    if mods & MOD_NIGHTCORE:
        acronyms.append("NC")
    elif mods & MOD_DOUBLE_TIME:
        acronyms.append("DT")
    return tuple(acronyms)
