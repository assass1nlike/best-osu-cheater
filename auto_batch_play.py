"""End-to-end batch play: type beatmap IDs into osu!lazer and play each replay.

Usage:
  python auto_batch_play.py [--manifest batch_manifest.json]
                            [--output-dir outputs]
                            [--leadin-time 6000] [--advance 0]
                            [--tune-n 30] [--tune-threshold 10]
                            [--tune-n2 150] [--tune-threshold2 5]
                            [--speed 1.5] [--k1 Z] [--k2 X]
                            [--skip 1 5]
                            [--post-enter-wait-s 0]
                            [--startup-wait-s 3]
                            [--bot-ready-timeout-s 10]
                            [--start-bot-after-enter]
"""

import ctypes, time, sys, argparse, subprocess, json, random, urllib.request
from ctypes import wintypes, byref, sizeof, Structure, Union
from pathlib import Path

from synthesis_osu_play.online import DEFAULT_BATCH_OUTPUT_DIR

# ---------------------------------------------------------------------------
# Windows input (same as replay_bot)
# ---------------------------------------------------------------------------
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except:
    ctypes.windll.user32.SetProcessDPIAware()

IM, IK = 0, 1
MF_M, MF_A = 0x0001, 0x8000
KF_UP = 0x0002
KF_SCANCODE = 0x0008
ABSOLUTE_COORDINATE_MAX = 65535
SAFE_CURSOR_MIN_RATIO = 0.35
SAFE_CURSOR_MAX_RATIO = 0.65

class MI(Structure):
    _fields_=[("dx",wintypes.LONG),("dy",wintypes.LONG),("md",wintypes.DWORD),
              ("fl",wintypes.DWORD),("tm",wintypes.DWORD),("ex",ctypes.POINTER(ctypes.c_ulong))]
class KI(Structure):
    _fields_=[("vk",wintypes.WORD),("sc",wintypes.WORD),("fl",wintypes.DWORD),
              ("tm",wintypes.DWORD),("ex",ctypes.POINTER(ctypes.c_ulong))]
class IU(Union):
    _fields_=[("mi",MI),("ki",KI)]
class INP(Structure):
    _fields_=[("tp",wintypes.DWORD),("u",IU)]

def send_key(vk, down):
    i = INP(tp=IK)
    i.u.ki.vk = vk
    i.u.ki.sc = ctypes.windll.user32.MapVirtualKeyW(vk, 0)
    i.u.ki.fl = KF_SCANCODE if down else (KF_SCANCODE | KF_UP)
    ctypes.windll.user32.SendInput(1, byref(i), sizeof(i))

def tap(vk, dur=0.06):
    send_key(vk, True); time.sleep(dur); send_key(vk, False); time.sleep(0.03)

def random_safe_cursor_position(rng):
    return (
        round(rng.uniform(SAFE_CURSOR_MIN_RATIO, SAFE_CURSOR_MAX_RATIO) * ABSOLUTE_COORDINATE_MAX),
        round(rng.uniform(SAFE_CURSOR_MIN_RATIO, SAFE_CURSOR_MAX_RATIO) * ABSOLUTE_COORDINATE_MAX),
    )

def move_mouse_absolute(x, y):
    i = INP(tp=IM)
    i.u.mi.dx = x
    i.u.mi.dy = y
    i.u.mi.fl = MF_M | MF_A
    ctypes.windll.user32.SendInput(1, byref(i), sizeof(i))

WM_CHAR = 0x0102

def find_osu_hwnd():
    h = ctypes.windll.user32.FindWindowW(None, None)
    while h:
        n = ctypes.windll.user32.GetWindowTextLengthW(h)
        if n > 0:
            buf = ctypes.create_unicode_buffer(n + 1)
            ctypes.windll.user32.GetWindowTextW(h, buf, n + 1)
            t = buf.value.lower()
            if t.startswith('osu!') and 'tosu' not in t:
                return h
        h = ctypes.windll.user32.GetWindow(h, 2)
    return None

def type_into_osu(text):
    """Type text into osu!lazer using WM_CHAR messages."""
    hwnd = find_osu_hwnd()
    if not hwnd:
        print("    WARNING: osu!lazer window not found, using SendInput fallback")
        for ch in str(text):
            vk = ord(ch)
            if 0x30 <= vk <= 0x39:
                tap(vk, 0.08)
                time.sleep(0.05)
        return

    for i, ch in enumerate(str(text)):
        ctypes.windll.user32.PostMessageW(hwnd, WM_CHAR, ord(ch), 0)
        time.sleep(0.06)

def press_backspace(count=7):
    hwnd = find_osu_hwnd()
    for _ in range(count):
        if hwnd:
            ctypes.windll.user32.PostMessageW(hwnd, 0x0100, 0x08, 0)  # WM_KEYDOWN VK_BACK
            time.sleep(0.03)
            ctypes.windll.user32.PostMessageW(hwnd, 0x0101, 0x08, 0)  # WM_KEYUP VK_BACK
        else:
            tap(0x08, 0.04)
        time.sleep(0.03)

def press_enter():
    tap(0x0D, 0.08)

def press_esc():
    hwnd = find_osu_hwnd()
    if hwnd:
        ctypes.windll.user32.PostMessageW(hwnd, 0x0100, 0x1B, 0)
        time.sleep(0.05)
        ctypes.windll.user32.PostMessageW(hwnd, 0x0101, 0x1B, 0)
    else:
        tap(0x1B, 0.08)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Batch auto-play synthesized replays")
    parser.add_argument("--manifest", type=Path,
                        help="batch manifest JSON; defaults to <output-dir>/batch_manifest.json")
    parser.add_argument("--output-dir", type=Path,
                        default=DEFAULT_BATCH_OUTPUT_DIR,
                        help="directory with [beatmap_id].osr files")
    # Replay bot params
    parser.add_argument("--leadin-time", type=int, default=6000)
    parser.add_argument("--advance", type=int, default=0)
    parser.add_argument("--tune-n", type=int, default=30)
    parser.add_argument("--tune-threshold", type=float, default=10.0)
    parser.add_argument("--tune-n2", type=int, default=150)
    parser.add_argument("--tune-threshold2", type=float, default=5.0)
    parser.add_argument("--speed", type=float, default=1.5)
    parser.add_argument("--auto-tune", action="store_true",
                        help="Enable tosu hit-error monitoring and restart tuning")
    parser.add_argument("--k1", default="Z")
    parser.add_argument("--k2", default="X")
    parser.add_argument("--clock-reader", default=None,
                        help="Path to LazerClockReader.exe or its .dll")
    parser.add_argument("--clock-sync-timeout-ms", type=int, default=5000)
    parser.add_argument("--clock-jump-threshold-ms", type=float, default=100.0)
    parser.add_argument("--no-clock-sync", action="store_true",
                        help="Use the replay bot's legacy local timer")
    parser.add_argument("--no-space", action="store_true",
                        help="Do not press SPACE; sync to the running gameplay clock")
    parser.add_argument("--post-enter-wait-s", type=float, default=0.0,
                        help="optional extra seconds after ENTER; clock sync handles loading by default")
    parser.add_argument("--start-from", type=int, default=0,
                        help="start from this index (0-based) in the manifest")
    parser.add_argument("--count", type=int, default=0,
                        help="max beatmaps to play (0 = all)")
    parser.add_argument(
        "--skip",
        "--skip-indices",
        dest="skip_indices",
        nargs="+",
        metavar="INDEX",
        help="skip manifest entries by 1-based position, e.g. --skip 1 5 or --skip 1,5",
    )
    parser.add_argument("--startup-wait-s", type=float, default=3.0,
                        help="seconds to wait before processing the first beatmap")
    parser.add_argument("--bot-ready-timeout-s", type=float, default=10.0,
                        help="seconds to wait for replay_bot_enter to arm before ENTER")
    parser.add_argument("--start-bot-after-enter", action="store_true",
                        help="legacy mode: press ENTER before launching the replay bot")
    args = parser.parse_args()
    if args.manifest is None:
        args.manifest = args.output_dir / "batch_manifest.json"

    if args.startup_wait_s < 0:
        parser.error("--startup-wait-s must be non-negative")
    if args.post_enter_wait_s < 0:
        parser.error("--post-enter-wait-s must be non-negative")
    if args.bot_ready_timeout_s < 0:
        parser.error("--bot-ready-timeout-s must be non-negative")

    skip_indices: set[int] = set()
    for raw_value in args.skip_indices or []:
        for value in raw_value.split(","):
            try:
                index = int(value)
            except ValueError:
                parser.error(f"--skip expects positive manifest positions, got {value!r}")
            if index < 1:
                parser.error(f"--skip expects positive manifest positions, got {index}")
            skip_indices.add(index)

    # Load manifest
    with open(args.manifest, encoding='utf-8') as f:
        manifest = json.load(f)

    items = manifest if isinstance(manifest, list) else manifest.get("items", [])
    if not items:
        print("No items in manifest"); return

    print(f"Batch play: {len(items)} beatmaps")
    print(f"  Leadin: {args.leadin_time}ms | Advance: {args.advance}ms")
    print(f"  Tune: {args.tune_n} hits @{args.tune_threshold}ms → {args.tune_n2} hits @{args.tune_threshold2}ms")
    print(f"  Speed: {args.speed}x | Keys: {args.k1}/{args.k2}")
    if skip_indices:
        print(f"  Skip manifest positions: {', '.join(str(index) for index in sorted(skip_indices))}")
    print(f"{'='*60}")

    # --- Start tosu only when auto-tune is explicitly requested ---
    TOSU_EXE = Path(__file__).resolve().parent / "tools" / "tosu" / "tosu.exe"
    tosu_proc = None
    if args.auto_tune and TOSU_EXE.exists():
        try:
            subprocess.run(['taskkill', '/f', '/im', 'tosu.exe'], capture_output=True, timeout=5)
            time.sleep(0.5)
        except: pass

        print("Starting tosu...")
        # tosu needs a visible console to start — just minimize it
        tosu_proc = subprocess.Popen(
            [str(TOSU_EXE)], cwd=str(TOSU_EXE.parent),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        print("Waiting for tosu...")
        for i in range(120):
            if tosu_proc.poll() is not None:
                print(f"tosu crashed! Exit code: {tosu_proc.returncode}")
                break
            try:
                req = urllib.request.Request("http://localhost:24050/json")
                with urllib.request.urlopen(req, timeout=0.5) as resp:
                    if resp.status == 200:
                        print("tosu ready!")
                        time.sleep(1.0)
                        osu_hwnd = find_osu_hwnd()
                        if osu_hwnd:
                            ctypes.windll.user32.SetForegroundWindow(osu_hwnd)
                            ctypes.windll.user32.ShowWindow(osu_hwnd, 9)
                        break
            except:
                if i % 10 == 9:
                    print(f"  still waiting ({(i+1)//2}s)...")
                time.sleep(0.3)
        else:
            print("WARNING: tosu did not start in 60s")
    elif args.auto_tune:
        print(f"tosu not found at {TOSU_EXE}")

    print(f"\n{'='*60}")
    if args.auto_tune:
        print("tosu is running. Switch to osu!lazer now!")
    else:
        print("Clock-synchronized playback is ready. Switch to osu!lazer now!")
    print("F7 = STOP EVERYTHING")
    print(f"Starting first beatmap in {args.startup_wait_s:.1f}s...")
    print(f"{'='*60}\n")

    time.sleep(args.startup_wait_s)

    # F7 global stop flag
    import threading
    f7_stop = [False]
    def f7_watcher():
        while not f7_stop[0]:
            if ctypes.windll.user32.GetAsyncKeyState(0x76) & 0x8000:
                f7_stop[0] = True
                print("\n  F7 pressed! Stopping...")
                break
            time.sleep(0.05)
    f7_thread = threading.Thread(target=f7_watcher, daemon=True)
    f7_thread.start()

    bot_script = Path(__file__).resolve().parent / "replay_bot_enter.py"
    cursor_rng = random.Random()

    max_count = args.count if args.count > 0 else len(items)
    played = 0
    for idx in range(args.start_from, len(items)):
        if played >= max_count or f7_stop[0]:
            break
        item = items[idx]
        if idx + 1 in skip_indices:
            print(f"\n[{idx+1}/{len(items)}] beatmap {item.get('beatmap_id', '?')}: skipped by request")
            continue
        if f7_stop[0]:
            break
        bid = item["beatmap_id"]
        output_path = item.get("output_path")
        osr_path = Path(output_path) if output_path else args.output_dir / f"{bid}.osr"

        if not osr_path.exists():
            print(f"\n[{idx+1}/{len(items)}] beatmap {bid}: replay not found ({osr_path}), skip")
            continue

        print(f"\n{'='*60}")
        print(f"[{idx+1}/{len(items)}] beatmap {bid}: {item.get('title','?')[:40]} [{item.get('version','?')[:15]}]")
        print(f"  Replay: {osr_path}")
        print(f"{'='*60}")

        # Clear search and type beatmap ID
        print("  Typing beatmap ID...")
        if played > 0:
            cursor_x, cursor_y = random_safe_cursor_position(cursor_rng)
            move_mouse_absolute(cursor_x, cursor_y)
        press_backspace(10)
        time.sleep(0.2)
        type_into_osu(str(bid))
        time.sleep(0.6)

        cmd = [
            sys.executable, "-u", str(bot_script), str(osr_path),
            "--leadin-time", str(args.leadin_time),
            "--advance", str(args.advance),
            "--tune-n", str(args.tune_n),
            "--tune-threshold", str(args.tune_threshold),
            "--tune-n2", str(args.tune_n2),
            "--tune-threshold2", str(args.tune_threshold2),
            "--speed", str(args.speed),
            "--k1", args.k1, "--k2", args.k2,
        ]
        if args.auto_tune:
            cmd.append("--auto-tune")
        if args.no_space:
            cmd.append("--no-space")
        if not args.no_clock_sync:
            cmd.append("--clock-sync")
            cmd.extend(["--clock-sync-timeout-ms", str(args.clock_sync_timeout_ms)])
            cmd.extend(["--clock-jump-threshold-ms", str(args.clock_jump_threshold_ms)])
            if args.clock_reader:
                cmd.extend(["--clock-reader", args.clock_reader])
        else:
            cmd.append("--no-clock-sync")

        proc = None
        output_thread = None
        bot_ready = threading.Event()

        def forward_bot_output():
            stream = proc.stdout if proc is not None else None
            if stream is None:
                return
            for line in stream:
                print(line, end="")
                if "Waiting for ENTER" in line:
                    bot_ready.set()

        if args.start_bot_after_enter:
            # Legacy mode: ENTER first, then bot startup. This is useful as a
            # fallback, but can miss very early gameplay clocks on fast maps.
            cmd.append("--skip-enter-wait")
        else:
            print(f"  Starting bot before ENTER: {' '.join(cmd)}")
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
            )
            output_thread = threading.Thread(target=forward_bot_output, daemon=True)
            output_thread.start()

            deadline = time.perf_counter() + args.bot_ready_timeout_s
            while not bot_ready.is_set():
                if f7_stop[0]:
                    break
                if proc.poll() is not None:
                    print(f"  Bot exited before ENTER with code {proc.returncode}, stopping batch.")
                    break
                if time.perf_counter() >= deadline:
                    print(f"  Bot was not ready for ENTER within {args.bot_ready_timeout_s:.1f}s, stopping batch.")
                    proc.terminate()
                    try:
                        proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait(timeout=2.0)
                    break
                time.sleep(0.02)
            if f7_stop[0] or not bot_ready.is_set() or proc.poll() is not None:
                break

        # Press ENTER to start play
        print(f"  Pressing ENTER (beatmap {bid})...")
        hwnd = find_osu_hwnd()
        if hwnd:
            ctypes.windll.user32.SetForegroundWindow(hwnd)
            time.sleep(0.05)
        press_enter()
        if args.post_enter_wait_s > 0:
            print(f"  Extra post-ENTER wait: {args.post_enter_wait_s:.1f}s")
            deadline = time.perf_counter() + args.post_enter_wait_s
            while time.perf_counter() < deadline:
                if f7_stop[0]:
                    break
                time.sleep(min(0.1, deadline - time.perf_counter()))
            if f7_stop[0]:
                break
        if proc is None:
            print(f"  Starting bot after ENTER: {' '.join(cmd)}")
            proc = subprocess.Popen(cmd)

        while proc.poll() is None:
            if f7_stop[0]:
                print("  F7 pressed, killing bot...")
                proc.terminate()
                proc.wait()
                break
            time.sleep(0.1)
        ret = proc.returncode

        if f7_stop[0]:
            break
        if ret != 0:
            print(f"  Bot exited with code {ret}, stopping batch.")
            break

        # After completion: wait, ESC, prepare for next
        if f7_stop[0]: break
        print("  Waiting 3s then ESC...")
        for _ in range(30):
            if f7_stop[0]: break
            time.sleep(0.1)
        if f7_stop[0]: break
        press_esc()
        time.sleep(0.5)
        played += 1

    print(f"\n{'='*60}")
    print("All beatmaps played!")
    print(f"{'='*60}")


if __name__ == "__main__":
    main()
