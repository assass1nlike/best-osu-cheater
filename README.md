# synthesis-osu-play

Tools for synthesizing osu! replay input streams for osu!lazer playback.

The Python package handles replay/beatmap processing offline. The standalone
Windows playback scripts in this repository are the intended end-to-end path
for feeding a synthesized replay into osu!lazer.

The current implementation can either take two local replays for the same
beatmap or download two osu!lazer leaderboard replays, then create a new replay
whose cursor path and key press intervals are dynamically blended. The package
itself does not inject input into the official client; the separate playback
scripts below do live Windows input playback and may trigger normal osu!lazer
score submission.

Stock osu!lazer imports replay scores through legacy `.osr` files and converts
them internally into `OsuReplayFrame(time, position, actions)`. This project
writes a lazer-importable `.osr` as the primary artifact. It can also write a
JSON view of the equivalent lazer actions for local tooling or a custom lazer
test scene.

## Usage

```powershell
python -m synthesis_osu_play synthesize first.osr second.osr output.osr --player-name synthesis
```

The synthesis uses dynamic weights by default. `--first-weight` and
`--second-weight` are multipliers on the dynamic blend, not fixed proportions:

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
one run. The bundled spinner trajectory library is used automatically; pass
`--spinner-mode never` to disable spinner trajectory replacement.

To search recent ranked osu!standard beatmapsets and synthesize a batch of DT
replays:

```powershell
python -m synthesis_osu_play batch-dt 10 --player-name assassinlike
```

By default this scans ranked osu! beatmapsets that have been ranked for at least
3 days, newest first, chooses one osu! difficulty in the 4.5-5.0 star range, and
writes outputs named by beatmap id to `D:\osu-lazer\exports`. Candidates whose
first hit object is earlier than 4000 ms are skipped by default because they are
less suitable for stable automated intro skipping. Use `--no-skip-filter` or
`--min-skip-time 0` to include them:

The bundled spinner trajectory library is also used automatically for every
generated DT replay. Pass `--spinner-mode never` to disable spinner trajectory
replacement.

- `4460247.osr`
- `4821683.osr`
- ...
- `batch_manifest.json`

Replay sources are selected randomly from downloadable plays whose mods are
within the supported source set: `EZ`, `NF`, `HT`, `DC`, `HR`, `SD`, `PF`, `DT`,
`NC`, `HD`, `TC`, `FL`, `BL`, `ST`, `AC`, and `CL`, including combinations and
NM. There is no longer a preference for native DT/NC or non-HR scores. The
generated batch replay is still written with DT as its output mod; source-only
mods are normalized away after their cursor and key streams are used.

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

Cursor positions are sampled at the union of both replay frame timestamps. Each
source cursor position is linearly interpolated at that timestamp, then blended
with dynamic weights. The first replay's base weight follows a 30-second sine
cycle:

dynamic_first = 0.5 + 0.5 * sin(pi * (time_ms % 30000) / 15000)

The user-provided weights multiply the two dynamic weights before normalization.

All shared cursor, key interval, seed, and skip-boundary averages use the same
dynamic two-replay weights. The skip intro anchor still keeps the fixed
post-skip/intro-end point as the third term.

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
- averaged intervals use legacy `K1` as the primary key. Each synthesis run
  samples one repeat threshold uniformly from 400-600 ms: after a primary-key
  note, the next note uses `K1` when its start is more than that threshold later
  and `K2` otherwise; every `K2` note is followed by `K1`
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

The local judge is retained only as a legacy debug fallback:

```powershell
python -m synthesis_osu_play synthesize first.osr second.osr output.osr --beatmap map.osu --local-score
```

This local path is not used for the automated-playback goal and may disagree
with osu!lazer on edge cases.

Without `--beatmap`, the command cannot judge objects and keeps the first
replay's score metadata.

## Automated Replay Playback

Two standalone scripts inject synthesized (or any) `.osr` replay frames as live
Windows input into osu!lazer, triggering genuine score submission.

### `replay_bot.py` — SPACE-triggered (skip mode)

For beatmaps with a long intro that can be skipped with spacebar:

```powershell
python replay_bot.py synth_replay.osr --speed 1.5 --auto-tune
```

The user presses SPACE when gameplay appears; the bot skips intro pauses and
replays the input stream. By default it starts the bundled `LazerClockReader`,
reads osu!lazer's live `CurrentTime` directly from the game process, waits for
the clock jump caused by the accepted SPACE, and starts the replay at the
matching absolute frame time. This removes the need to search for a large
manual `--advance` value. `--auto-tune` remains available for residual input
latency and connects to tosu (bundled in `tools/tosu/`) to measure hit errors.

### `replay_bot_enter.py` — ENTER-triggered mode

For beatmaps where a deterministic delay from ENTER to SPACE is needed:

```powershell
python replay_bot_enter.py synth_replay.osr --leadin-time 3000 --speed 1.5
```

After ENTER is pressed, the bot waits `--leadin-time` ms, then presses SPACE.
The replay starts from the `CurrentTime` observed when lazer accepts that SPACE,
eliminating both human reaction-time jitter and the old fixed offset search.

For a beatmap without a skip-able intro, use `--no-space`. The bot starts the
clock reader before waiting for ENTER, discards samples from before ENTER, then
waits for the gameplay clock to begin moving. It does not send SPACE:

```powershell
python replay_bot_enter.py synth_replay.osr --speed 1.0 --no-space
```

The no-space path requires clock sync. It refuses to guess a fixed loading delay
unless explicitly forced with `--no-space --no-clock-sync --leadin-time 6000`.

The reader is built from the source tree with .NET 8:

```powershell
dotnet build tools/LazerClockReader/LazerClockReader.csproj -c Release
```

The scripts use `tools/LazerClockReader/bin/Release/net8.0/LazerClockReader.exe`
by default. If the reader cannot attach, they print a warning and fall back to
the legacy local timer. Use `--no-clock-sync` to select that fallback explicitly.

### Config files

Both scripts accept `--config <file.json>` to load parameters from a JSON file
instead of long command lines:

```json
{
  "replay": "synth_replay.osr",
  "speed": 1.5,
  "advance": 0,
  "auto_tune": true,
  "tune_n": 10,
  "restart_delay": 3
}
```

### Common parameters

The playback scripts require osu!lazer to be fullscreen on the primary screen.
They reproduce osu!lazer's own `OsuPlayfieldAdjustmentContainer`: the gameplay
playfield uses the source-defined 80% window adjustment, fits `512x384` at 4:3,
and applies the source-defined 8-unit gameplay vertical shift. There is no
manual playfield ratio parameter.

| Parameter | Default | Description |
|-----------|---------|-------------|
| `--advance` | 0 | Optional residual input timing advance in ms |
| `--speed` | 1.0 | Playback speed (1.5 for DT, 0.75 for HT) |
| `--clock-sync` | on | Synchronize to the accepted osu!lazer SPACE clock jump |
| `--no-clock-sync` | off | Disable live clock reading and use the legacy timer |
| `--clock-reader` | bundled | Override the reader executable or DLL path |
| `--clock-sync-timeout-ms` | 5000 | Reader startup and SPACE acceptance timeout |
| `--clock-jump-threshold-ms` | 100 | Minimum positive `CurrentTime` jump to accept |
| `--no-space` | off | Start from the gameplay clock without sending SPACE |
| `--no-space-sync-timeout-ms` | 15000 | Timeout waiting for the non-skip gameplay clock |
| `--no-space-min-lead-ms` | 1000 | Required lead time before the first key in no-space mode |
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
