# synthesis-osu-play

Offline tools for synthesizing osu! replay input streams for osu!lazer playback.

The current implementation can either take two local replays for the same
beatmap or download two osu!lazer leaderboard replays, then create a new replay
whose cursor path and key press intervals are averaged. It does not inject input
into the official client, submit scores, or attempt to bypass any anti-cheat
system.

Stock osu!lazer imports replay scores through legacy `.osr` files and converts
them internally into `OsuReplayFrame(time, position, actions)`. This project
writes a lazer-importable `.osr` as the primary artifact. It can also write a
JSON view of the equivalent lazer actions for local tooling or a custom lazer
test scene.

## Usage

```powershell
python -m synthesis_osu_play synthesize first.osr second.osr output.osr --player-name synthesis
```

You can bias the synthesis toward one source replay with weights:

```powershell
python -m synthesis_osu_play synthesize first.osr second.osr output.osr `
  --first-weight 3 `
  --second-weight 1
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

To render a debugging video showing both source cursor paths and the synthesized
path:

```powershell
python -m synthesis_osu_play synthesize first.osr second.osr output.osr `
  --beatmap map.osu `
  --first-weight 0.3 `
  --second-weight 0.7 `
  --debug-video debug.mp4
```

The video overlays source 1, source 2, the synthesized cursor, the instantaneous
weighted point, key states, nearby hit objects, source cursor distance, and a
three-row key-hold timeline above the playfield for source 1, source 2, and the
synthesized replay. Use `--debug-video-start-ms` and `--debug-video-end-ms` to
render only a suspicious section:

```powershell
python -m synthesis_osu_play synthesize first.osr second.osr output.osr `
  --beatmap map.osu `
  --debug-video debug.mp4 `
  --debug-video-start-ms 60000 `
  --debug-video-end-ms 75000
```

If osu!lazer is signed in on this machine, the tool can download the beatmap
and two leaderboard replays before synthesizing:

```powershell
python -m synthesis_osu_play download-synthesize 4460247 1 25 output.osr `
  --player-name assassinlike `
  --first-weight 0.3 `
  --second-weight 0.7
```

`download-synthesize` accepts the same `--debug-video` options, so the one-shot
path can download the sources, synthesize, and render the diagnostic overlay in
one run.

To search recent ranked osu!standard beatmapsets and synthesize a batch of DT
replays:

```powershell
python -m synthesis_osu_play batch-dt 10 --player-name assassinlike
```

By default this scans ranked osu! beatmapsets that have been ranked for at least
3 days, newest first, chooses one osu! difficulty in the 4.5-5.5 star range, and
writes outputs named by beatmap id to `D:\osu-lazer\exports`:

- `4460247.osr`
- `4821683.osr`
- ...
- `batch_manifest.json`

Replay sources are selected per beatmap in this priority order:

- downloadable DT/NC plays without HR, requiring standard 1.5x speed when the
  API exposes a custom speed setting
- otherwise downloadable non-HR plays
- otherwise any downloadable play that can be normalized by the supported
  HD/HR/DT/NC/HT rules

Adjust count, age, star range, and output location like this:

```powershell
python -m synthesis_osu_play batch-dt 5 `
  --output-dir D:\osu-lazer\exports `
  --min-age-days 7 `
  --min-star 4.8 `
  --max-star 5.3 `
  --random-seed 123
```

This reads osu!lazer's saved API token from `game.ini`, following
`%APPDATA%\osu\storage.ini` when lazer uses a custom storage path. Use
`--lazer-storage D:\osu-lazer` or `--token-config D:\osu-lazer\game.ini` if
auto-discovery is not correct. Expired access tokens are refreshed through the
same OAuth refresh-token flow that osu!lazer uses, then written back to
`game.ini`.

After synthesis finishes, the command launches osu!lazer with the cached
`.osz` files so it can import the beatmaps in one pass. Set `OSU_LAZER_PATH` or
pass `--lazer-path` if your install is not under `%LOCALAPPDATA%\osulazer\current`.

Downloaded files are cached in `artifacts/<beatmap_id>` by default:

- `<beatmap_id>.osu`
- `leaderboard_global.json`
- `rank<rank>_<score_id>.osr`

The one-shot command currently supports ranks returned by osu!lazer's beatmap
score endpoint, up to rank 100. Use `--mods HD DT` to request an exact-mod
leaderboard filter when needed.

## Synthesis Model

Cursor positions are sampled at the union of both replay frame timestamps. Each source cursor position is linearly interpolated at that timestamp, then the two positions are averaged.

All shared cursor, key interval, seed, and skip-boundary averages use the same two-replay weights. The skip intro anchor still keeps the fixed post-skip/intro-end point as the third term.

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

## Automated Replay Playback

Two standalone scripts inject synthesized (or any) `.osr` replay frames as live
Windows input into osu!lazer, triggering genuine score submission.

### `replay_bot.py` — SPACE-triggered (skip mode)

For beatmaps with a long intro that can be skipped with spacebar:

```powershell
python replay_bot.py synth_replay.osr --speed 1.5 --advance 1050 --auto-tune
```

The user presses SPACE when gameplay appears; the bot skips intro pauses and
replays the input stream.  `--auto-tune` connects to tosu (bundled in
`tools/tosu/`) to measure hit errors and auto-adjust the advance timing.

### `replay_bot_enter.py` — ENTER + fixed-delay mode

For beatmaps where a deterministic delay from ENTER to SPACE is needed:

```powershell
python replay_bot_enter.py synth_replay.osr --leadin-time 3000 --advance 100 --speed 1.5
```

After ENTER is pressed, the bot waits `--leadin-time` ms, then presses SPACE and
starts the replay simultaneously — eliminating human reaction-time jitter.

### Config files

Both scripts accept `--config <file.json>` to load parameters from a JSON file
instead of long command lines:

```json
{
  "replay": "synth_replay.osr",
  "speed": 1.5,
  "advance": 1050,
  "auto_tune": true,
  "tune_n": 10,
  "restart_delay": 3
}
```

### Common parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--r` | 0.85 | Playfield height ratio (higher = cursor moves down) |
| `--advance` | 175 / 0 | Timing advance in ms (skip mode) |
| `--speed` | 1.0 | Playback speed (1.5 for DT, 0.75 for HT) |
| `--k1` / `--k2` | Z / X | Key bindings |
| `--auto-tune` | off | Enable tosu hit-error monitoring & auto-restart |
| `--tune-n` | 100 | Hit count before checking mean error |
| `--tune-threshold` | 10 | Mean error threshold in ms |
| `--restart-delay` | 3 | Seconds to wait after auto-abort for fail screen |
| `--config` | - | JSON config file path |

### tosu

The `--auto-tune` flag requires tosu (memory reader for osu!lazer). The bundled
copy lives at `tools/tosu/tosu.exe`. If it is missing, download the latest
Windows release from <https://github.com/tosuapp/tosu/releases> and extract
`tosu.exe` into that directory.
