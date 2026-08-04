using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text.Json.Serialization;

var options = ReaderOptions.Parse(args);
var offsets = OffsetData.Load(options.OffsetsPath);
using var memory = ProcessMemory.Open(options.ProcessId);
var startedAt = Stopwatch.GetTimestamp();
var clock = new LazerClock(memory, offsets);
var ready = false;
var attached = false;
var previousTime = double.NaN;
var lastSampleAt = 0L;

while (true)
{
    if (options.TimeoutMs is int timeoutMs && ElapsedMilliseconds(startedAt) >= timeoutMs)
    {
        WriteEvent(new
        {
            @event = "error",
            message = "timed out while resolving osu!lazer game clock",
            pid = options.ProcessId,
        });
        return 2;
    }

    try
    {
        if (!clock.TryReadCurrentTime(out var currentTime, out var addressInfo))
        {
            if (!attached && clock.TryResolveGameBase(out var gameBase))
            {
                attached = true;
                WriteEvent(new
                {
                    @event = "attached",
                    pid = options.ProcessId,
                    qpc_frequency = Stopwatch.Frequency,
                    qpc_seconds = QpcSeconds(),
                    game_base = $"0x{gameBase:X}",
                    osu_version = offsets.OsuVersion,
                });
            }
            previousTime = double.NaN;
            Thread.Sleep(Math.Max(options.IntervalMs, 25));
            continue;
        }

        var now = QpcSeconds();
        if (!ready)
        {
            ready = true;
            previousTime = currentTime;
            lastSampleAt = Stopwatch.GetTimestamp();
            WriteEvent(new
            {
                @event = "ready",
                pid = options.ProcessId,
                qpc_frequency = Stopwatch.Frequency,
                qpc_seconds = now,
                time_ms = currentTime,
                game_base = $"0x{addressInfo.GameBase:X}",
                current_time_address = $"0x{addressInfo.CurrentTimeAddress:X}",
                osu_version = offsets.OsuVersion,
            });
        }
        else
        {
            var delta = currentTime - previousTime;
            if (delta >= options.JumpThresholdMs)
            {
                WriteEvent(new
                {
                    @event = "jump",
                    time_ms = currentTime,
                    delta_ms = delta,
                    qpc_seconds = now,
                    game_base = $"0x{addressInfo.GameBase:X}",
                    current_time_address = $"0x{addressInfo.CurrentTimeAddress:X}",
                });
                if (options.WaitForJump)
                    return 0;
            }

            if (options.EmitSamples && ElapsedMilliseconds(lastSampleAt) >= options.SampleIntervalMs)
            {
                lastSampleAt = Stopwatch.GetTimestamp();
                WriteEvent(new
                {
                    @event = "sample",
                    time_ms = currentTime,
                    qpc_seconds = now,
                    game_base = $"0x{addressInfo.GameBase:X}",
                    current_time_address = $"0x{addressInfo.CurrentTimeAddress:X}",
                });
            }

            previousTime = currentTime;
        }

        Thread.Sleep(options.IntervalMs);
    }
    catch (Win32Exception ex)
    {
        WriteEvent(new { @event = "error", message = ex.Message, win32_error = ex.NativeErrorCode });
        return 1;
    }
    catch (InvalidOperationException ex)
    {
        if (options.WaitForJump)
        {
            WriteEvent(new { @event = "error", message = ex.Message });
            return 1;
        }

        Thread.Sleep(Math.Max(options.IntervalMs, 25));
    }
}

static double QpcSeconds() => Stopwatch.GetTimestamp() / (double)Stopwatch.Frequency;

static double ElapsedMilliseconds(long startedAt)
    => (Stopwatch.GetTimestamp() - startedAt) * 1000.0 / Stopwatch.Frequency;

static void WriteEvent(object value)
{
    Console.WriteLine(JsonSerializer.Serialize(value));
    Console.Out.Flush();
}

sealed class LazerClock
{
    private readonly ProcessMemory memory;
    private readonly OffsetData offsets;
    private ulong gameBase;

    public LazerClock(ProcessMemory memory, OffsetData offsets)
    {
        this.memory = memory;
        this.offsets = offsets;
    }

    public bool TryReadCurrentTime(out double currentTime, out ClockAddressInfo addressInfo)
    {
        currentTime = 0;
        addressInfo = default;

        if (!TryResolveGameBase(out _))
            return false;

        if (!memory.TryReadPointer(gameBase + (ulong)offsets.BeatmapClock, out var beatmapClock) || beatmapClock == 0)
            return false;
        if (!memory.TryReadPointer(beatmapClock + (ulong)offsets.FramedBeatmapClockFinalSource, out var finalClockSource) || finalClockSource == 0)
            return false;

        var currentTimeAddress = finalClockSource + (ulong)offsets.FramedClockCurrentTime;
        if (!memory.TryReadDouble(currentTimeAddress, out currentTime) ||
            !double.IsFinite(currentTime) ||
            currentTime < -120_000 ||
            currentTime > 24 * 60 * 60 * 1000)
        {
            return false;
        }

        addressInfo = new ClockAddressInfo(gameBase, currentTimeAddress);
        return true;
    }

    public bool TryResolveGameBase(out ulong resolvedGameBase)
    {
        if (!IsGameBaseValid(gameBase))
            gameBase = ResolveGameBase();
        resolvedGameBase = gameBase;
        return gameBase != 0;
    }

    private ulong ResolveGameBase()
    {
        foreach (var anchor in memory.ScanPattern(offsets.ScanPattern))
        {
            foreach (var delta in offsets.GameBaseFromAnchorDeltas)
            {
                if (anchor < (ulong)delta)
                    continue;

                if (!memory.TryReadPointer(anchor - (ulong)delta, out var externalLinkOpener) || externalLinkOpener == 0)
                    continue;
                if (!memory.TryReadPointer(externalLinkOpener + (ulong)offsets.ExternalLinkOpenerApi, out var api) || api == 0)
                    continue;
                if (!memory.TryReadPointer(api + (ulong)offsets.ApiGame, out var candidate) || candidate == 0)
                    continue;
                if (IsGameBaseValid(candidate))
                    return candidate;
            }
        }

        return 0;
    }

    private bool IsGameBaseValid(ulong candidate)
    {
        if (candidate == 0 ||
            !memory.TryReadPointer(candidate, out var vtable) ||
            vtable == 0 ||
            !memory.TryReadUInt64(vtable, out var firstVtableEntry))
        {
            return false;
        }

        return firstVtableEntry == offsets.GameBaseVtable;
    }
}

readonly record struct ClockAddressInfo(ulong GameBase, ulong CurrentTimeAddress);

sealed class ProcessMemory : IDisposable
{
    private const uint ProcessVmRead = 0x0010;
    private const uint ProcessQueryInformation = 0x0400;
    private const uint MemCommit = 0x1000;
    private const uint PageNoAccess = 0x01;
    private const uint PageGuard = 0x100;
    private const int ChunkSize = 16 * 1024 * 1024;
    private readonly IntPtr handle;
    private bool disposed;

    private ProcessMemory(IntPtr handle)
    {
        this.handle = handle;
    }

    public static ProcessMemory Open(int processId)
    {
        var handle = OpenProcess(ProcessVmRead | ProcessQueryInformation, false, processId);
        if (handle == IntPtr.Zero)
        {
            var error = Marshal.GetLastWin32Error();
            throw new Win32Exception(error, $"could not open osu!lazer process {processId}");
        }

        return new ProcessMemory(handle);
    }

    public bool TryReadPointer(ulong address, out ulong value) => TryReadUInt64(address, out value);

    public bool TryReadUInt64(ulong address, out ulong value)
    {
        value = 0;
        if (!TryRead(address, 8, out var bytes))
            return false;
        value = BitConverter.ToUInt64(bytes, 0);
        return true;
    }

    public bool TryReadDouble(ulong address, out double value)
    {
        value = 0;
        if (!TryRead(address, 8, out var bytes))
            return false;
        value = BitConverter.ToDouble(bytes, 0);
        return true;
    }

    public IEnumerable<ulong> ScanPattern(string patternText)
    {
        var pattern = ParsePattern(patternText);
        foreach (var region in ReadableRegions())
        {
            foreach (var match in ScanRegion(region, pattern))
                yield return match;
        }
    }

    private IEnumerable<ulong> ScanRegion(MemoryRegion region, byte[] pattern)
    {
        var overlap = Math.Max(pattern.Length - 1, 0);
        var buffer = new byte[ChunkSize + overlap];
        ulong offset = 0;
        var previousTail = 0;

        while (offset < region.Size)
        {
            var bytesToRead = (int)Math.Min((ulong)ChunkSize, region.Size - offset);
            var readAddress = region.BaseAddress + offset - (ulong)previousTail;
            var requested = bytesToRead + previousTail;
            if (!TryRead(readAddress, requested, out var data))
            {
                offset += (ulong)bytesToRead;
                previousTail = 0;
                continue;
            }

            data.CopyTo(buffer, 0);
            var available = data.Length;
            for (var index = 0; index <= available - pattern.Length; index++)
            {
                if (Matches(buffer, index, pattern))
                    yield return readAddress + (ulong)index;
            }

            previousTail = overlap;
            if (previousTail > available)
                previousTail = available;
            if (previousTail > 0)
                Buffer.BlockCopy(buffer, available - previousTail, buffer, 0, previousTail);
            offset += (ulong)bytesToRead;
        }
    }

    private IEnumerable<MemoryRegion> ReadableRegions()
    {
        ulong address = 0;
        while (address < long.MaxValue)
        {
            var result = VirtualQueryEx(handle, (IntPtr)(long)address, out var info, (UIntPtr)Marshal.SizeOf<MemoryBasicInformation>());
            if (result == UIntPtr.Zero)
                yield break;

            var baseAddress = (ulong)info.BaseAddress.ToInt64();
            var size = info.RegionSize.ToUInt64();
            if (size == 0)
                yield break;

            var protection = info.Protect & 0xff;
            var readable = info.State == MemCommit &&
                protection != PageNoAccess &&
                (info.Protect & PageGuard) == 0;
            if (readable)
                yield return new MemoryRegion(baseAddress, size);

            var next = baseAddress + size;
            if (next <= address)
                yield break;
            address = next;
        }
    }

    private bool TryRead(ulong address, int count, out byte[] data)
    {
        data = new byte[count];
        if (count <= 0)
            return true;

        if (!ReadProcessMemory(handle, (IntPtr)(long)address, data, (UIntPtr)count, out var bytesRead) ||
            bytesRead.ToUInt64() != (ulong)count)
        {
            data = Array.Empty<byte>();
            return false;
        }

        return true;
    }

    private static byte[] ParsePattern(string patternText)
    {
        var tokens = patternText.Split(' ', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
        return tokens.Select(token => byte.Parse(token, NumberStyles.HexNumber, CultureInfo.InvariantCulture)).ToArray();
    }

    private static bool Matches(byte[] data, int offset, byte[] pattern)
    {
        for (var index = 0; index < pattern.Length; index++)
        {
            if (data[offset + index] != pattern[index])
                return false;
        }

        return true;
    }

    public void Dispose()
    {
        if (!disposed)
        {
            CloseHandle(handle);
            disposed = true;
        }
    }

    private readonly record struct MemoryRegion(ulong BaseAddress, ulong Size);

    [StructLayout(LayoutKind.Sequential)]
    private struct MemoryBasicInformation
    {
        public IntPtr BaseAddress;
        public IntPtr AllocationBase;
        public uint AllocationProtect;
        public UIntPtr RegionSize;
        public uint State;
        public uint Protect;
        public uint Type;
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern IntPtr OpenProcess(uint access, bool inheritHandle, int processId);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr handle);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern UIntPtr VirtualQueryEx(IntPtr process, IntPtr address, out MemoryBasicInformation buffer, UIntPtr length);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool ReadProcessMemory(IntPtr process, IntPtr baseAddress, [Out] byte[] buffer, UIntPtr size, out UIntPtr numberOfBytesRead);
}

sealed class OffsetData
{
    [JsonPropertyName("osu_version")]
    public string OsuVersion { get; init; } = "unknown";
    [JsonPropertyName("scan_pattern")]
    public string ScanPattern { get; init; } = "";
    [JsonPropertyName("game_base_vtable")]
    public ulong GameBaseVtable { get; init; }
    [JsonPropertyName("game_base_from_anchor_deltas")]
    public int[] GameBaseFromAnchorDeltas { get; init; } = Array.Empty<int>();
    [JsonPropertyName("external_link_opener_api")]
    public int ExternalLinkOpenerApi { get; init; }
    [JsonPropertyName("api_game")]
    public int ApiGame { get; init; }
    [JsonPropertyName("beatmap_clock")]
    public int BeatmapClock { get; init; }
    [JsonPropertyName("framed_beatmap_clock_final_source")]
    public int FramedBeatmapClockFinalSource { get; init; }
    [JsonPropertyName("framed_clock_current_time")]
    public int FramedClockCurrentTime { get; init; }

    public static OffsetData Load(string path)
    {
        if (!File.Exists(path))
            throw new FileNotFoundException("osu!lazer memory offsets file was not found", path);

        var value = JsonSerializer.Deserialize<OffsetData>(File.ReadAllText(path));
        if (value is null || string.IsNullOrWhiteSpace(value.ScanPattern) || value.GameBaseFromAnchorDeltas.Length == 0)
            throw new InvalidDataException("invalid osu!lazer memory offsets file");
        return value;
    }
}

sealed class ReaderOptions
{
    public required int ProcessId { get; init; }
    public required string OffsetsPath { get; init; }
    public int IntervalMs { get; init; } = 5;
    public double JumpThresholdMs { get; init; } = 100;
    public bool WaitForJump { get; init; }
    public bool EmitSamples { get; init; }
    public int SampleIntervalMs { get; init; } = 20;
    public int? TimeoutMs { get; init; }

    public static ReaderOptions Parse(string[] args)
    {
        int? processId = null;
        var offsetsPath = Path.Combine(AppContext.BaseDirectory, "offsets.json");
        var intervalMs = 5;
        var jumpThresholdMs = 100.0;
        var waitForJump = false;
        var emitSamples = false;
        var sampleIntervalMs = 20;
        int? timeoutMs = null;

        for (var index = 0; index < args.Length; index++)
        {
            switch (args[index])
            {
                case "--pid":
                    processId = int.Parse(Next(args, ref index), CultureInfo.InvariantCulture);
                    break;
                case "--offsets-path":
                    offsetsPath = Path.GetFullPath(Next(args, ref index));
                    break;
                case "--interval-ms":
                    intervalMs = int.Parse(Next(args, ref index), CultureInfo.InvariantCulture);
                    break;
                case "--jump-threshold-ms":
                    jumpThresholdMs = double.Parse(Next(args, ref index), CultureInfo.InvariantCulture);
                    break;
                case "--wait-for-jump":
                    waitForJump = true;
                    break;
                case "--emit-samples":
                    emitSamples = true;
                    break;
                case "--sample-interval-ms":
                    sampleIntervalMs = int.Parse(Next(args, ref index), CultureInfo.InvariantCulture);
                    break;
                case "--timeout-ms":
                    timeoutMs = int.Parse(Next(args, ref index), CultureInfo.InvariantCulture);
                    break;
                case "--help":
                    Console.WriteLine("LazerClockReader --pid PID [--wait-for-jump] [--emit-samples] [--timeout-ms 10000]");
                    Environment.Exit(0);
                    break;
                default:
                    throw new ArgumentException($"unknown argument: {args[index]}");
            }
        }

        if (processId is not int pid || pid <= 0)
            throw new ArgumentException("--pid must be a positive process id");
        if (intervalMs <= 0)
            throw new ArgumentException("--interval-ms must be positive");
        if (jumpThresholdMs <= 0)
            throw new ArgumentException("--jump-threshold-ms must be positive");
        if (sampleIntervalMs <= 0)
            throw new ArgumentException("--sample-interval-ms must be positive");
        if (timeoutMs is <= 0)
            throw new ArgumentException("--timeout-ms must be positive");

        return new ReaderOptions
        {
            ProcessId = pid,
            OffsetsPath = offsetsPath,
            IntervalMs = intervalMs,
            JumpThresholdMs = jumpThresholdMs,
            WaitForJump = waitForJump,
            EmitSamples = emitSamples,
            SampleIntervalMs = sampleIntervalMs,
            TimeoutMs = timeoutMs,
        };
    }

    private static string Next(string[] args, ref int index)
    {
        if (++index >= args.Length)
            throw new ArgumentException($"missing value for {args[index - 1]}");
        return args[index];
    }
}
