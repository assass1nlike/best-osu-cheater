"""
Replay Bot (ENTER variant) - ENTER triggers a fixed-delay SPACE + replay start.
Designed for skip-mode replays where the lead-in pause must align with SPACE.

Usage:
  python replay_bot_enter.py <replay.osr> [--leadin-time 6000] [--advance 0] ...
"""
import ctypes, time, sys, argparse, json, threading, os, subprocess, urllib.request
from ctypes import wintypes, byref, sizeof, Structure, Union
from osrparse import Replay
from lazer_clock_sync import (
    ClockSyncError,
    ReplayTimeline,
    initial_key_mask,
    start_clock_reader,
    window_process_id,
)
from synthesis_osu_play.playfield import FullscreenPlayfield

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except:
    ctypes.windll.user32.SetProcessDPIAware()

IM, IK = 0, 1
MF_M, MF_A = 0x0001, 0x8000
MF_LD, MF_LU = 0x0002, 0x0004
MF_RD, MF_RU = 0x0008, 0x0010
KF_UP = 0x0002
KF_SCANCODE = 0x0008
VK_Z, VK_X, VK_SPACE, VK_F7, VK_ESC, VK_RETURN = 0x5A, 0x58, 0x20, 0x76, 0x1B, 0x0D
KM1, KM2, KK1, KK2 = 1, 2, 4, 8

class MI(Structure):
    _fields_=[("dx",wintypes.LONG),("dy",wintypes.LONG),("md",wintypes.DWORD),
              ("fl",wintypes.DWORD),("tm",wintypes.DWORD),("ex",ctypes.POINTER(ctypes.c_ulong))]
class KI(Structure):
    _fields_=[("vk",wintypes.WORD),("sc",wintypes.WORD),("fl",wintypes.DWORD),
              ("tm",wintypes.DWORD),("ex",ctypes.POINTER(ctypes.c_ulong))]
class IU(Union):
    _fields_=[("mi",MI),("ki",KI)]
class IN(Structure):
    _fields_=[("tp",wintypes.DWORD),("u",IU)]

def mabs(x,y):
    i=IN(tp=IM); i.u.mi.dx=int(x); i.u.mi.dy=int(y)
    i.u.mi.fl=MF_M|MF_A; ctypes.windll.user32.SendInput(1,byref(i),sizeof(i))

def mbtn(down,left=True):
    i=IN(tp=IM)
    f=(MF_LD if down else MF_LU) if left else (MF_RD if down else MF_RU)
    i.u.mi.fl=f; ctypes.windll.user32.SendInput(1,byref(i),sizeof(i))

WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101

def kkey(vk,down):
    """Send keyboard via SendInput with scan code."""
    i=IN(tp=IK); i.u.ki.vk=vk
    i.u.ki.sc = ctypes.windll.user32.MapVirtualKeyW(vk, 0)
    i.u.ki.fl = KF_SCANCODE if down else (KF_SCANCODE | KF_UP)
    ctypes.windll.user32.SendInput(1,byref(i),sizeof(i))

def kdown(vk):
    return ctypes.windll.user32.GetAsyncKeyState(vk)&0x8000

def scr():
    return (ctypes.windll.user32.GetSystemMetrics(0),ctypes.windll.user32.GetSystemMetrics(1))

def find_osu():
    h=ctypes.windll.user32.FindWindowW(None,None)
    while h:
        n=ctypes.windll.user32.GetWindowTextLengthW(h)
        if n>0:
            b=ctypes.create_unicode_buffer(n+1)
            ctypes.windll.user32.GetWindowTextW(h,b,n+1)
            t = b.value.lower()
            # Match osu! but NOT "tosu" dashboards
            if (t.startswith('osu!') or 'osu!lazer' in t) and 'tosu' not in t:
                r=wintypes.RECT(); ctypes.windll.user32.GetWindowRect(h,byref(r))
                return h,r,b.value
        h=ctypes.windll.user32.GetWindow(h,2)
    return None,None,None

# ============================================================
# tosu launcher
# ============================================================

TOSU_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "tools", "tosu")
TOSU_EXE = os.path.join(TOSU_DIR, "tosu.exe")

def ensure_tosu():
    """Launch tosu if not already running."""
    if not os.path.exists(TOSU_EXE):
        print(f"  ERROR: {TOSU_EXE} not found!")
        return None

    # Check if already running
    try:
        req = urllib.request.Request("http://localhost:24050/json")
        with urllib.request.urlopen(req, timeout=0.5) as resp:
            pass
        print("  tosu: already running")
        return None
    except:
        pass

    # Kill any existing tosu processes
    try:
        subprocess.run(['taskkill', '/f', '/im', 'tosu.exe'],
                       capture_output=True, timeout=5)
        time.sleep(0.5)
    except:
        pass

    print("  Launching tosu...")
    proc = subprocess.Popen(
        [TOSU_EXE], cwd=TOSU_DIR,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for i in range(40):
        try:
            ws = websocket.create_connection("ws://localhost:24050/ws", timeout=1)
            ws.close()
            print("  tosu: connected")
            return proc
        except:
            time.sleep(0.5)
    print("  WARNING: tosu did not start in 20s")
    return proc


def main():
    parser = argparse.ArgumentParser(description='osu!lazer Replay Bot')
    parser.add_argument('replay', nargs='?',
                        default=None,
                        help='Path to .osr replay file')
    parser.add_argument('--advance', type=int, default=0,
                        help='Additional input timing advance in ms (default: 0)')
    parser.add_argument('--k1', default='Z',
                        help='K1 key (default: Z)')
    parser.add_argument('--k2', default='X',
                        help='K2 key (default: X)')
    parser.add_argument('--leadin-time', type=int, default=6000,
                        help='ms after ENTER to press SPACE; used as fixed-delay fallback with --no-space --no-clock-sync')
    parser.add_argument('--space', dest='send_space', action='store_true', default=True,
                        help='press SPACE after lead-in and sync to the accepted skip jump (default)')
    parser.add_argument('--no-space', dest='send_space', action='store_false',
                        help='do not press SPACE; sync when the gameplay clock starts moving')
    parser.add_argument('--skip-enter-wait', action='store_true',
                        help='skip waiting for ENTER (caller already pressed it)')
    parser.add_argument('--speed', type=float, default=1.0,
                        help='Playback speed multiplier (e.g. 1.5 for DT, 0.75 for HT)')
    parser.add_argument('--clock-sync', dest='clock_sync', action='store_true', default=True,
                        help='Align replay to the accepted SPACE clock jump (default)')
    parser.add_argument('--no-clock-sync', dest='clock_sync', action='store_false',
                        help='Use the legacy local timer after SPACE')
    parser.add_argument('--clock-reader', default=None,
                        help='Path to LazerClockReader.exe or its .dll')
    parser.add_argument('--clock-sync-timeout-ms', type=int, default=5000,
                        help='Timeout for reader startup and SPACE acceptance (default: 5000)')
    parser.add_argument('--clock-jump-threshold-ms', type=float, default=100.0,
                        help='Minimum positive clock jump treated as accepted SPACE (default: 100)')
    parser.add_argument('--no-space-sync-timeout-ms', type=int, default=15000,
                        help='Timeout waiting for gameplay clock movement with --no-space (default: 15000)')
    parser.add_argument('--no-space-min-lead-ms', type=int, default=1000,
                        help='Required time before the first key press when syncing with --no-space (default: 1000)')
    parser.add_argument('--auto-tune', action='store_true',
                        help='Auto-tune timing via tosu hit error monitoring')
    parser.add_argument('--tune-n', type=int, default=100,
                        help='Number of hits to check for auto-tune (default: 100)')
    parser.add_argument('--tune-threshold', type=float, default=10.0,
                        help='Mean error threshold in ms for auto-tune stage 1 (default: 10)')
    parser.add_argument('--tune-n2', type=int, default=150,
                        help='Number of hits for stage 2 check (default: 150)')
    parser.add_argument('--tune-threshold2', type=float, default=5.0,
                        help='Mean error threshold in ms for stage 2 (default: 5)')
    parser.add_argument('--config', default=None,
                        help='JSON config file (overrides other args)')
    parser.add_argument('--restart-delay', type=float, default=3.0,
                        help='Seconds to wait after auto-abort for fail screen (default: 3)')
    parser.add_argument('--restart-space-delay', type=float, default=2.0,
                        help='Seconds to wait before SPACE in skip-mode restart (default: 2)')
    args = parser.parse_args()

    # Load config file if specified
    if args.config:
        with open(args.config, 'r') as f:
            cfg = json.load(f)
        # Override argparse defaults with config values
        for k, v in cfg.items():
            if hasattr(args, k):
                setattr(args, k, v)
        # If config sets 'replay', override positional
        if 'replay' in cfg:
            args.replay = cfg['replay']

    # Map key names to VK codes
    keymap = {chr(c): c for c in range(0x41, 0x5B)}  # A-Z
    k1_vk = keymap.get(args.k1.upper(), 0x5A)
    k2_vk = keymap.get(args.k2.upper(), 0x58)

    print("="*55)
    print("  osu!lazer Replay Bot")
    print(f"  Replay: {args.replay}")
    print(f"  Keys: {args.k1}/{args.k2} | Advance={args.advance}ms")
    print("="*55)

    hwnd,rect,title = find_osu()
    global osu_hwnd; osu_hwnd = hwnd
    if not hwnd: print("ERROR: osu!lazer not running!"); return
    osu_pid = window_process_id(hwnd)
    print(f"\n  osu!lazer: \"{title}\"")
    s=ctypes.windll.user32.GetWindowLongW(hwnd,-16)
    if s&0x20000000:
        ctypes.windll.user32.ShowWindow(hwnd,9); ctypes.windll.user32.ShowWindow(hwnd,5)
    ctypes.windll.user32.SetForegroundWindow(hwnd)
    time.sleep(0.1)
    rect=wintypes.RECT(); ctypes.windll.user32.GetWindowRect(hwnd,byref(rect))

    ADVANCE_MS = args.advance
    advance_s = ADVANCE_MS / 1000.0
    leadin_s = args.leadin_time / 1000.0
    speed = args.speed

    r=Replay.from_path(args.replay)
    frames=r.replay_data
    timeline = ReplayTimeline.from_frames(frames)
    first_key_time_ms = timeline.first_key_time_ms(frames)
    tms=sum(f.time_delta for f in frames) / speed
    skipped=sum(f.time_delta for f in frames if f.time_delta>1000) / speed

    print(f"  leadin: {args.leadin_time}ms | advance: {ADVANCE_MS}ms | speed: {speed}x")
    print(f"  SPACE mode: {'on' if args.send_space else 'off'}")
    if args.clock_sync:
        print("  Clock sync: enabled (osu!lazer CurrentTime)")
    th=r.count_300+r.count_100+r.count_50+r.count_miss
    acc=100.0*(r.count_300*300+r.count_100*100+r.count_50*50)/(300*th) if th else 0
    print(f"  {r.username} | {r.score}pts | {r.max_combo}x | {acc:.2f}%")
    print(f"  {tms/1000:.1f}s replay ({skipped/1000:.1f}s pause skipped) | {len(frames)} frames")

    sw,sh=scr()
    playfield = FullscreenPlayfield.from_screen(sw, sh)
    window_width = rect.right - rect.left
    window_height = rect.bottom - rect.top
    if (rect.left, rect.top, window_width, window_height) != (0, 0, sw, sh):
        print("  WARNING: osu!lazer is not covering the primary screen; fullscreen is required")
    print(f"  Screen: {sw}x{sh} | Playfield: {playfield.width:.1f}x{playfield.height:.1f} "
          f"at ({playfield.left:.1f}, {playfield.top:.1f}) | Scale: {playfield.scale:.4f}")

    def map_osu(osu_x, osu_y):
        return playfield.to_absolute(osu_x, osu_y)

    center_ax, center_ay = map_osu(256, 192)

    # --- tosu auto-tune setup ---
    tosu_errors = []
    tosu_hp = [1.0]  # shared HP for fail detection
    tosu_thread = None
    tosu_running = [False]

    tosu_proc = None  # tosu subprocess handle

    if args.auto_tune:
        try:
            import websocket
        except ImportError:
            print("  ERROR: websocket-client required. pip install websocket-client")
            return

        # Auto-download and launch tosu
        tosu_proc = ensure_tosu()

        debug_done = False
        def tosu_poll():
            nonlocal debug_done
            try:
                ws = websocket.create_connection("ws://localhost:24050/ws", timeout=10)
                ws.settimeout(1.0)
                while tosu_running[0]:
                    try:
                        msg = ws.recv()
                        data = json.loads(msg)
                        if not debug_done:
                            hits = data.get('gameplay', {}).get('hits', {})
                            errs = hits.get('hitErrorArray', [])
                            print(f"  tosu: hitErrorArray present ({len(errs)} errors so far)")
                            debug_done = True
                        gp = data.get('gameplay', {})
                        hits = gp.get('hits', {})
                        errs = hits.get('hitErrorArray') or []
                        if errs:
                            tosu_errors[:] = errs
                        hp_data = gp.get('hp', {})
                        if hp_data:
                            tosu_hp[0] = hp_data.get('normal', 1.0)
                    except Exception:
                        continue
                ws.close()
            except Exception as e:
                print(f"  tosu WS error: {e}")

        tosu_running[0] = True
        tosu_thread = threading.Thread(target=tosu_poll, daemon=True)
        tosu_thread.start()
        print(f"  tosu: monitoring hit errors")

    # --- main loop (with auto-tune restart) ---
    run = 1
    _fail_direction = [1]  # +1=earlier (advance+), -1=later (advance-)
    _fail_advance_base = [args.advance]
    while True:
        clock_reader = None
        sync_jump = None
        if run == 1:
            print(f"\n{'='*55}")
            print("  1. Start the beatmap in osu!lazer")
            print("  2. Press ENTER to begin playing")
            if args.send_space:
                print("     (bot will auto-SPACE + replay after lead-in)")
            else:
                print("     (bot will sync to the running gameplay clock; no SPACE)")
            print("  F7 = ABORT")
            print(f"{'='*55}")

        if args.clock_sync:
            try:
                clock_reader = start_clock_reader(
                    osu_pid,
                    reader_path=args.clock_reader,
                    ready_timeout_ms=args.clock_sync_timeout_ms,
                    jump_threshold_ms=args.clock_jump_threshold_ms,
                    wait_for_jump=args.send_space,
                    stream_samples=not args.send_space,
                )
                print("  Clock reader: ready")
            except ClockSyncError as exc:
                print(f"  WARNING: clock sync unavailable ({exc})")
                if not args.send_space:
                    print("  ERROR: --no-space needs clock sync; use --no-clock-sync to force fixed lead-in fallback")
                    tosu_running[0] = False
                    sys.exit(1)

        if run == 1 and not args.skip_enter_wait:
            print(f"\n  [Run {run}] Waiting for ENTER...")
            while not kdown(VK_RETURN):
                if kdown(VK_F7):
                    print("  Aborted (exit=1).")
                    tosu_running[0] = False
                    if clock_reader is not None:
                        clock_reader.stop()
                    sys.exit(1)
                time.sleep(0.005)
        # On auto-restart or skip-enter-wait, ENTER already sent

        if args.send_space:
            # Wait fixed lead-in time, then press SPACE + start replay
            print(f"  [Run {run}] ENTER detected. Waiting {args.leadin_time}ms...")
            time.sleep(leadin_s)

            # Press SPACE (skip lead-in) + instant feedback
            kkey(VK_SPACE, True); time.sleep(0.05); kkey(VK_SPACE, False)
            if clock_reader is not None:
                try:
                    sync_jump = clock_reader.wait_for_jump(args.clock_sync_timeout_ms / 1000.0)
                except ClockSyncError as exc:
                    print(f"  WARNING: SPACE clock jump not observed ({exc}); using legacy timer")
            mabs(center_ax, center_ay)
            print(f"  [Run {run}] SPACE sent + GO!")
        else:
            if clock_reader is not None:
                clock_reader.discard_pending_events()
                max_anchor_time = None
                if first_key_time_ms is not None:
                    max_anchor_time = first_key_time_ms - args.no_space_min_lead_ms
                print(f"  [Run {run}] ENTER detected. Waiting for gameplay clock...")
                try:
                    sync_jump = clock_reader.wait_for_running_clock(
                        args.no_space_sync_timeout_ms / 1000.0,
                        max_time_ms=max_anchor_time,
                    )
                except ClockSyncError as exc:
                    print(f"  ERROR: no-space clock sync failed ({exc})")
                    tosu_running[0] = False
                    sys.exit(1)
            else:
                print(f"  [Run {run}] WARNING: fixed lead-in fallback. Waiting {args.leadin_time}ms...")
                time.sleep(leadin_s)
            mabs(center_ax, center_ay)
            print(f"  [Run {run}] GO! (no SPACE)")

        ks={k:False for k in ['k1','k2']}
        sync_start_index = 0
        if sync_jump is not None:
            sync_start_index = timeline.first_frame_at_or_after(sync_jump.time_ms)
            sync_start_index = min(sync_start_index, len(frames))
            previous_mask = initial_key_mask(frames, sync_start_index)
            if sync_start_index > 0:
                previous_frame = frames[sync_start_index - 1]
                mabs(*map_osu(previous_frame.x, previous_frame.y))
            for bit,name,vk in [(KK1,'k1',k1_vk),(KK2,'k2',k2_vk)]:
                if previous_mask & bit:
                    kkey(vk, True)
                    ks[name] = True
            label = "SPACE accepted" if args.send_space else "gameplay clock synced"
            print(f"  [Run {run}] {label} at lazer {sync_jump.time_ms:.1f}ms; "
                  f"starting frame {sync_start_index}/{len(frames)}")
            cnt = sync_start_index
        else:
            cnt = 0
        tot=len(frames); t0=time.perf_counter(); et=0; lr=t0
        skipped_ms=0
        auto_abort = False
        auto_abort_mean = 0.0
        first_key_pressed = any(ks.values())
        stage1_passed = False
        stage2_checked = False

        try:
            for frame_index, frame in enumerate(frames):
                if kdown(VK_F7): print("\n  F7 ABORT (exit=1)"); sys.exit(1)

                if sync_jump is not None:
                    if frame_index < sync_start_index:
                        continue
                    et = timeline.frame_times_ms[frame_index]
                    target = (sync_jump.qpc_seconds +
                              (et - sync_jump.time_ms) / 1000.0 / speed - advance_s)
                    w = target - time.perf_counter()
                    if w > 0.001:
                        if w > 0.003:
                            time.sleep(w - 0.0015)
                        while time.perf_counter() < target:
                            pass

                    ax,ay = map_osu(frame.x, frame.y)
                    mabs(ax, ay)
                    dt = 0
                else:
                    dt = frame.time_delta / speed

                if args.send_space and sync_jump is None and dt > 1000 and not first_key_pressed:
                    # SKIP long pauses entirely - don't wait
                    skipped_ms += dt
                    et += dt
                    ax,ay = map_osu(frame.x, frame.y)
                    mabs(ax, ay)
                else:
                    # Normal frame timing
                    if dt > 0:
                        et += dt
                        target = t0 + (et - skipped_ms)/1000.0 - advance_s
                        w = target - time.perf_counter()
                        if w > 0.001:
                            if w > 0.003:
                                time.sleep(w - 0.0015)
                            while time.perf_counter() < target:
                                pass

                    if sync_jump is None:
                        ax,ay = map_osu(frame.x, frame.y)
                        mabs(ax, ay)

                # Keyboard only via PostMessage (bypasses raw input)
                k = frame.keys
                for bit,name,vk in [(KK1,'k1',k1_vk),(KK2,'k2',k2_vk)]:
                    d = bool(k&bit)
                    if d != ks[name]:
                        kkey(vk, d)
                        if d and not first_key_pressed:
                            first_key_pressed = True
                        ks[name] = d
                cnt += 1

                # Fail detection via HP drop
                if args.auto_tune and tosu_hp[0] <= 0.01 and et > 5000:
                    auto_abort = True
                    auto_abort_mean = 0
                    print(f"\n  [Auto-tune] FAIL detected (HP=0). Restarting...")
                    break

                # Auto-tune stage 1: coarse check
                if args.auto_tune and not stage1_passed and len(tosu_errors) >= args.tune_n:
                    first_n = tosu_errors[:args.tune_n]
                    mean_err = sum(first_n) / len(first_n)
                    if abs(mean_err) > args.tune_threshold:
                        auto_abort = True
                        auto_abort_mean = mean_err
                        print(f"\n  [Auto-tune S1] {len(first_n)} hits, mean={mean_err:+.1f}ms > {args.tune_threshold}ms")
                        break
                    else:
                        stage1_passed = True
                        print(f"\n  [Auto-tune S1] {len(first_n)} hits, mean={mean_err:+.1f}ms OK")

                # Auto-tune stage 2: fine check (once)
                if args.auto_tune and stage1_passed and not stage2_checked and len(tosu_errors) >= args.tune_n2:
                    stage2_checked = True
                    all_n = tosu_errors[:args.tune_n2]
                    mean_err = sum(all_n) / len(all_n)
                    if abs(mean_err) > args.tune_threshold2:
                        auto_abort = True
                        auto_abort_mean = mean_err
                        print(f"\n  [Auto-tune S2] {len(all_n)} hits, mean={mean_err:+.1f}ms > {args.tune_threshold2}ms")
                        break
                    else:
                        print(f"\n  [Auto-tune S2] {len(all_n)} hits, mean={mean_err:+.1f}ms OK. Continuing.")

                n = time.perf_counter()
                if n - lr > 10:
                    pct = et/tms*100 if tms else 0
                    rem = (tms-et)/1000
                    print(f"  [{pct:.0f}%] {et/1000:.0f}s/{tms/1000:.0f}s ({rem:.0f}s left)")
                    lr = n

        except Exception as e:
            print(f"\n  ERROR: {e}")
            import traceback; traceback.print_exc()
        finally:
            for k,d in ks.items():
                if d:
                    if k=='k1': kkey(k1_vk,False)
                    elif k=='k2': kkey(k2_vk,False)
            if clock_reader is not None:
                clock_reader.stop()

        wt = time.perf_counter()-t0

        # --- auto-tune restart logic ---
        if auto_abort:
            if auto_abort_mean == 0 and len(tosu_errors) == 0:
                # Fail with 0 hits: systematic timing search
                direction = _fail_direction[0]
                if direction > 0:
                    args.advance += 200  # positive = earlier
                    if args.advance >= _fail_advance_base[0] + 2000:
                        _fail_direction[0] = -1
                        args.advance = _fail_advance_base[0] - 200
                        print(f"  [Fail] Trying later offsets...")
                elif direction == 0:
                    _fail_direction[0] = 1
                    args.advance += 200
                else:
                    args.advance -= 200  # negative = later
                    if args.advance <= _fail_advance_base[0] - 2000:
                        print(f"  [Fail] All offsets exhausted. Stopping.")
                        tosu_running[0] = False
                        sys.exit(1)
                adj = args.advance - _fail_advance_base[0]
                adj_label = "earlier" if adj > 0 else "later"
                print(f"  [Fail] advance={args.advance}ms ({adj:+d}ms {adj_label})")
            elif auto_abort_mean == 0:
                if len(tosu_errors) > 0:
                    mean_err = sum(tosu_errors) / len(tosu_errors)
                    adj = int(mean_err)
                    args.advance += adj
                    print(f"  [Fail] {len(tosu_errors)} hits, mean={mean_err:+.1f}ms -> advance={args.advance}ms")
                    _fail_direction[0] = 0
                    _fail_advance_base[0] = args.advance
                else:
                    print(f"  [Fail] no hit data, retrying same params")
            else:
                adj = int(auto_abort_mean)
                print(f"  [Auto-tune] Adjusting: mean_error = {auto_abort_mean:+.1f}ms")
                args.advance += adj
                _fail_direction[0] = 0
                _fail_advance_base[0] = args.advance
            advance_s = args.advance / 1000.0
            print(f"  -> advance = {args.advance}ms")

            # Fully automated restart: wait -> ESC -> ENTER
            restart_secs = args.restart_delay
            print(f"  Waiting {restart_secs}s for fail screen...")
            time.sleep(restart_secs)

            # ESC: exit fail/results screen
            print(f"  Pressing ESC...")
            kkey(VK_ESC, True); time.sleep(0.05); kkey(VK_ESC, False)
            time.sleep(0.5)

            # ENTER: start next play
            print(f"  Pressing ENTER to start play...")
            kkey(VK_RETURN, True); time.sleep(0.05); kkey(VK_RETURN, False)

            # After ENTER, the main loop handles leadin wait + SPACE + replay
            run += 1
            tosu_errors.clear()
            print(f"  >>> Auto-restarted run {run} <<<\n")
            continue  # back to start of main loop

        # --- finished or manual abort ---
        print(f"\n{'='*55}")
        if cnt >= tot:
            print(f"  DONE! {cnt}/{tot} frames in {wt:.1f}s")
            print(f"  Score submitted if online!")
        else:
            print(f"  ABORTED. {cnt}/{tot} frames in {wt:.1f}s")
        print(f"{'='*55}")
        tosu_running[0] = False
        break

if __name__=='__main__':
    try:
        main()
    finally:
        # Kill tosu subprocess if we launched it
        try:
            subprocess.run(['taskkill', '/f', '/im', 'tosu.exe'],
                          capture_output=True, timeout=5)
        except:
            pass
