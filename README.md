# synthesis-osu-play

Offline tools for synthesizing osu! replay input streams for osu!lazer playback.

The current implementation takes two local replays for the same beatmap and
creates a new replay whose cursor path and key press intervals are averaged.
It does not download leaderboard data, inject input into the official client,
submit scores, or attempt to bypass any anti-cheat system.

Stock osu!lazer imports replay scores through legacy `.osr` files and converts
them internally into `OsuReplayFrame(time, position, actions)`. This project
writes a lazer-importable `.osr` as the primary artifact. It can also write a
JSON view of the equivalent lazer actions for local tooling or a custom lazer
test scene.

## Usage

```powershell
python -m synthesis_osu_play synthesize first.osr second.osr output.osr --player-name synthesis
```

By default the command is strict:

- both replays must be osu!standard replays for the same beatmap MD5
- mods must match
- each key bit must have the same number of press intervals in both replays

For object-aware synthesis, pass the `.osu` beatmap:

```powershell
python -m synthesis_osu_play synthesize first.osr second.osr output.osr --beatmap map.osu
```

If an intro skip happened in either source replay, provide the measured skip
times. The earlier one is used as the synthesized skip time. Legacy `.osr`
cannot store the spacebar `SkipCutscene` action itself. In stock lazer, skip is
gameplay clock behavior rather than an `OsuAction` replay frame. The command
reports the synthesized skip press time and uses lazer's normal skip target by
default: first hit object time minus 1000 ms, clamped at 0.

```powershell
python -m synthesis_osu_play synthesize first.osr second.osr output.osr `
  --beatmap map.osu `
  --first-skip-ms 1260 `
  --second-skip-ms 1440
```

To also emit a local JSON representation of lazer replay actions:

```powershell
python -m synthesis_osu_play synthesize first.osr second.osr output.osr `
  --beatmap map.osu `
  --lazer-json output.lazer-replay.json
```

## Synthesis Model

Cursor positions are sampled at the union of both replay frame timestamps. Each source cursor position is linearly interpolated at that timestamp, then the two positions are averaged.

When a skip time and intro-end time are known, positions before the synthesized skip press use a three-point average:

- replay 1 cursor at that time
- replay 2 cursor at that time
- the averaged cursor position at the fixed post-skip/intro-end time

This keeps the cursor path continuous through the skip boundary. When `--beatmap` is present and a skip time is provided, the intro end defaults to lazer's skip target; use `--intro-end-ms` to override it.

## Click Model

With `--beatmap`, key intervals are matched by hit object:

- first extract all press intervals from both hit keys in each replay
- mark intervals that hit a circle or slider head within the OD 50 window and CS radius
- for miss chains, greedily match nearby unused press intervals to the missed object times
- if either replay has no interval for an object after this process, the synthesized replay does not press that object
- averaged intervals are assigned alternately to legacy `K1`/`K2` states, starting with `K1`
- if three synthesized intervals overlap at any time, synthesis fails

The generated legacy states are `5` (`Left1 | K1`) and `10` (`Right1 | K2`).
In lazer these convert to `OsuAction.LeftButton` and
`OsuAction.RightButton`; with default key bindings these correspond to `z` and
`x`.

When `--beatmap` is provided, the generated replay keeps provisional score
metadata by default. The intended flow is:

```powershell
python -m synthesis_osu_play synthesize first.osr second.osr provisional.osr --beatmap map.osu
```

Then import/watch the provisional replay in osu!lazer and use a lazer-scored
metadata source to finalize the file:

```powershell
python -m synthesis_osu_play finalize provisional.osr scored.osr final.osr --delete-provisional
```

The finalize step copies hit counts, score, max combo, perfect flag, rank, and
statistics from the scored replay while clearing online score identity fields.

Note that stock osu!lazer's normal export action for an imported replay copies
the original stored replay file. It does not, by itself, write the replay
player's freshly recomputed `ScoreInfo` back to a new `.osr`. The `finalize`
command is the safe metadata-rewrite boundary; a dedicated lazer/headless
metadata extractor still needs to provide the scored source for full automation.

The local judge is still available as a debug fallback:

```powershell
python -m synthesis_osu_play synthesize first.osr second.osr output.osr --beatmap map.osu --local-score
```

This local path is not the default source of truth. It may disagree with
osu!lazer on edge cases.

Without `--beatmap`, the command cannot judge objects and keeps the first
replay's score metadata.
