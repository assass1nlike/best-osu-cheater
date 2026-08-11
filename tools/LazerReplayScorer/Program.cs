using System.Reflection;
using System.Runtime.ExceptionServices;
using System.Runtime.InteropServices;
using System.Runtime.Loader;
using System.Text.Json;
using System.Text.Json.Serialization;
using osu.Framework;
using NUnit.Framework;
using osu.Framework.Platform;
using osu.Framework.Screens;
using osu.Framework.Testing;
using osu.Framework.Testing.Drawables.Steps;
using osu.Game.Beatmaps;
using osu.Game.Beatmaps.Formats;
using osu.Game.IO;
using osu.Game.Replays;
using osu.Game.Rulesets;
using osu.Game.Rulesets.Judgements;
using osu.Game.Rulesets.Mods;
using osu.Game.Rulesets.Osu;
using osu.Game.Rulesets.Scoring;
using osu.Game.Scoring;
using osu.Game.Scoring.Legacy;
using osu.Game.Screens.Play;

if (args.Length < 3)
{
    Console.Error.WriteLine(
        "usage: LazerReplayScorer <beatmap.osu> <input.osr> <output.osr> [lazer-path] [--json <report.json>]");
    return 2;
}

string? explicitLazerPath = null;
string? jsonPath = null;

for (var i = 3; i < args.Length; i++)
{
    if (args[i] == "--json")
    {
        if (++i >= args.Length)
        {
            Console.Error.WriteLine("--json requires an output path");
            return 2;
        }

        jsonPath = Path.GetFullPath(args[i]);
        continue;
    }

    if (explicitLazerPath != null)
    {
        Console.Error.WriteLine($"unexpected argument: {args[i]}");
        return 2;
    }

    explicitLazerPath = args[i];
}

var lazerPath = ResolveLazerPath(explicitLazerPath);

if (OperatingSystem.IsWindows())
    SetDllDirectory(lazerPath);

Environment.SetEnvironmentVariable(
    "PATH",
    $"{lazerPath};{Environment.GetEnvironmentVariable("PATH")}");

AssemblyLoadContext.Default.Resolving += (_, name) =>
{
    var candidate = Path.Combine(lazerPath, $"{name.Name}.dll");
    return File.Exists(candidate) ? AssemblyLoadContext.Default.LoadFromAssemblyPath(candidate) : null;
};

var beatmapPath = Path.GetFullPath(args[0]);
var replayPath = Path.GetFullPath(args[1]);
var outputPath = Path.GetFullPath(args[2]);

var scene = new ScoreReplayTestScene(beatmapPath, replayPath, outputPath);

using var host = new TestRunHeadlessGameHost(
    "synthesis-osu-play-scorer",
    new HostOptions
    {
        PortableInstallation = true,
    },
    bypassCleanup: false,
    realtime: false);

using var runner = new osu.Game.Tests.Visual.OsuTestScene.OsuTestSceneTestRunner();
Exception? hostException = null;
var hostThread = new Thread(() =>
{
    try
    {
        host.Run(runner);
    }
    catch (Exception ex)
    {
        hostException = ex;
    }
})
{
    IsBackground = true,
    Name = "lazer scorer host",
};
hostThread.Start();

var loadStartedAt = DateTime.UtcNow;
while (!runner.IsLoaded && hostThread.IsAlive && hostException == null)
{
    if (DateTime.UtcNow - loadStartedAt > TimeSpan.FromMinutes(2))
        throw new TimeoutException("headless runner did not load within 2 minutes");

    Thread.Sleep(10);
}

if (hostException != null)
    ExceptionDispatchInfo.Capture(hostException).Throw();

if (!runner.IsLoaded)
    throw new InvalidOperationException("headless host exited before test runner loaded");

runner.RunTestBlocking(scene);
host.Exit();
hostThread.Join(TimeSpan.FromSeconds(10));

if (hostException != null)
    ExceptionDispatchInfo.Capture(hostException).Throw();

Console.WriteLine($"wrote {outputPath}");
Console.WriteLine(scene.ResultSummary);
if (jsonPath != null)
{
    Directory.CreateDirectory(Path.GetDirectoryName(jsonPath)!);
    File.WriteAllText(
        jsonPath,
        JsonSerializer.Serialize(
            scene.Report,
            new JsonSerializerOptions
            {
                PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
                WriteIndented = true,
            }));
    Console.WriteLine($"wrote {jsonPath}");
}
return 0;

sealed class ScoreReplayTestScene : osu.Game.Tests.Visual.RateAdjustedBeatmapTestScene
{
    private readonly string beatmapPath;
    private readonly string replayPath;
    private readonly string outputPath;
    private readonly List<JudgementResult> results = new();
    private ReplayPlayer? currentPlayer;
    private Score? decodedScore;
    private IBeatmap? playableBeatmap;
    private WorkingBeatmap? workingBeatmap;

    public string ResultSummary { get; private set; } = "";
    public ReplayScoreReport? Report { get; private set; }

    protected override Ruleset CreateRuleset() => new OsuRuleset();

    public ScoreReplayTestScene(string beatmapPath, string replayPath, string outputPath)
    {
        this.beatmapPath = beatmapPath;
        this.replayPath = replayPath;
        this.outputPath = outputPath;

        AddStep("mute audio", muteAudio);
        AddStep("load beatmap", loadBeatmap);
        AddStep("decode replay", decodeReplay);
        AddStep("push replay player", pushPlayer);
        AddLongUntilStep("wait until player is loaded", () => currentPlayer?.IsCurrentScreen() == true, TimeSpan.FromMinutes(2));
        AddStep("skip intro if present", skipIntroIfPresent);
        AddLongUntilStep("wait for completion", () => currentPlayer?.GameplayState.HasCompleted == true, TimeSpan.FromMinutes(15));
        AddStep("copy score metadata", copyScoreMetadata);
        AddStep("encode final replay", encodeFinalReplay);
    }

    private void muteAudio()
    {
        Audio.Volume.Value = 0;
        Audio.VolumeTrack.Value = 0;
        Audio.VolumeSample.Value = 0;
    }

    private void AddLongUntilStep(string name, Func<bool> assertion, TimeSpan timeout)
    {
        AddStep(new LongUntilStepButton(name, assertion, timeout) { IsSetupStep = false });
    }

    private void loadBeatmap()
    {
        using var stream = File.OpenRead(beatmapPath);
        using var reader = new LineBufferedReader(stream);
        var decoder = Decoder.GetDecoder<Beatmap>(reader);
        var beatmap = decoder.Decode(reader);
        workingBeatmap = CreateWorkingBeatmap(beatmap);
        Beatmap.Value = workingBeatmap;
        Ruleset.Value = CreateRuleset().RulesetInfo;
        SelectedMods.Value = Array.Empty<Mod>();
    }

    private sealed class LongUntilStepButton : StepButton
    {
        private readonly string name;
        private readonly Func<bool> assertion;
        private readonly TimeSpan timeout;
        private readonly DateTime startedAt = DateTime.UtcNow;
        private bool completed;

        public LongUntilStepButton(string name, Func<bool> assertion, TimeSpan timeout)
        {
            this.name = name;
            this.assertion = assertion;
            this.timeout = timeout;
            Text = name;
        }

        public override int RequiredRepetitions => completed ? 1 : int.MaxValue;

        public override void PerformStep(bool userTriggered)
        {
            if (completed)
                return;

            if (assertion())
            {
                completed = true;
                Success();
                return;
            }

            if (DateTime.UtcNow - startedAt > timeout)
                throw new TimeoutException($"{name} timed out after {timeout}");
        }

        public override string ToString() => name;
    }

    private void decodeReplay()
    {
        if (workingBeatmap == null)
            throw new InvalidOperationException("beatmap not loaded");

        using var stream = File.OpenRead(replayPath);
        decodedScore = new FlatScoreDecoder(workingBeatmap, Ruleset.Value).Parse(stream);
        playableBeatmap = workingBeatmap.GetPlayableBeatmap(Ruleset.Value, decodedScore.ScoreInfo.Mods);
        Beatmap.Value = workingBeatMapWithPlayableInfo(playableBeatmap, workingBeatmap);
        Ruleset.Value = playableBeatmap.BeatmapInfo.Ruleset;
        SelectedMods.Value = decodedScore.ScoreInfo.Mods;
    }

    private WorkingBeatmap workingBeatMapWithPlayableInfo(IBeatmap playable, WorkingBeatmap fallback)
    {
        // The replay player obtains the playable beatmap from the working beatmap, but score
        // metadata should use the converted BeatmapInfo created during decode.
        fallback.BeatmapInfo.Ruleset = playable.BeatmapInfo.Ruleset;
        fallback.BeatmapInfo.MD5Hash = playable.BeatmapInfo.MD5Hash;
        fallback.BeatmapInfo.Hash = playable.BeatmapInfo.Hash;
        return fallback;
    }

    private void pushPlayer()
    {
        if (decodedScore == null)
            throw new InvalidOperationException("replay not decoded");

        var player = new ReplayPlayer(decodedScore);
        player.OnLoadComplete += delegate
        {
            player.GameplayState.ScoreProcessor.NewJudgement += result =>
            {
                if (currentPlayer == player)
                    results.Add(result);
            };
        };
        LoadScreen(currentPlayer = player);
        results.Clear();
    }

    private void skipIntroIfPresent()
    {
        if (currentPlayer == null)
            throw new InvalidOperationException("player not loaded");

        var clock = currentPlayer.ChildrenOfType<GameplayClockContainer>().Single();
        if (clock.CurrentTime < 0)
            currentPlayer.Seek(0);
    }

    private void copyScoreMetadata()
    {
        if (decodedScore == null || currentPlayer == null)
            throw new InvalidOperationException("player not completed");

        var scoreInfo = decodedScore.ScoreInfo;
        var processor = currentPlayer.GameplayState.ScoreProcessor;
        processor.PopulateScore(scoreInfo);
        scoreInfo.Passed = !currentPlayer.GameplayState.HasFailed;
        scoreInfo.OnlineID = -1;
        scoreInfo.LegacyOnlineID = -1;
        scoreInfo.HasOnlineReplay = false;
        scoreInfo.ClientVersion = "";
        decodedScore.Replay.HasReceivedAllFrames = true;

        var counts = scoreInfo.Statistics;
        counts.TryGetValue(HitResult.Great, out var great);
        counts.TryGetValue(HitResult.Ok, out var ok);
        counts.TryGetValue(HitResult.Meh, out var meh);
        counts.TryGetValue(HitResult.Miss, out var miss);
        var sliderBreaks = results
                           .Where(result => result.Type.BreaksCombo() && sliderPart(result) != null)
                           .Select(toJudgementIssue)
                           .ToArray();
        var otherComboBreaks = results
                               .Where(result => result.Type.BreaksCombo()
                                                && sliderPart(result) == null
                                                && result.Type != HitResult.Miss)
                               .Select(toJudgementIssue)
                               .ToArray();
        var maximumCombo = processor.MaximumCombo;
        var isFc = miss == 0
                   && sliderBreaks.Length == 0
                   && otherComboBreaks.Length == 0
                   && scoreInfo.MaxCombo == maximumCombo;

        Report = new ReplayScoreReport(
            isFc,
            scoreInfo.TotalScore,
            scoreInfo.MaxCombo,
            maximumCombo,
            scoreInfo.Rank.ToString(),
            scoreInfo.Accuracy,
            scoreInfo.Passed,
            Convert.ToInt64(great),
            Convert.ToInt64(ok),
            Convert.ToInt64(meh),
            Convert.ToInt64(miss),
            counts.ToDictionary(
                pair => JsonNamingPolicy.SnakeCaseLower.ConvertName(pair.Key.ToString()),
                pair => Convert.ToInt64(pair.Value)),
            sliderBreaks,
            otherComboBreaks);
        ResultSummary =
            $"score={scoreInfo.TotalScore} combo={scoreInfo.MaxCombo} rank={scoreInfo.Rank} " +
            $"accuracy={scoreInfo.Accuracy:P4} great/ok/meh/miss={great}/{ok}/{meh}/{miss} " +
            $"slider_breaks={sliderBreaks.Length} fc={isFc} passed={scoreInfo.Passed}";
    }

    private static string? sliderPart(JudgementResult result) => result.HitObject.GetType().Name switch
    {
        "SliderHeadCircle" => "head",
        "SliderTick" => "tick",
        "SliderRepeat" => "repeat",
        "SliderTailCircle" => "tail",
        _ => null,
    };

    private static JudgementIssue toJudgementIssue(JudgementResult result) => new(
        sliderPart(result),
        result.HitObject.GetType().Name,
        result.Type.ToString(),
        result.HitObject.StartTime,
        result.TimeAbsolute,
        result.TimeOffset,
        result.ComboAtJudgement,
        result.ComboAfterJudgement);

    private void encodeFinalReplay()
    {
        if (decodedScore == null || playableBeatmap == null)
            throw new InvalidOperationException("score metadata not ready");

        Directory.CreateDirectory(Path.GetDirectoryName(outputPath)!);
        using var stream = File.Create(outputPath);
        new LegacyScoreEncoder(decodedScore, playableBeatmap).Encode(stream, leaveOpen: false);
    }

    private sealed class FlatScoreDecoder : LegacyScoreDecoder
    {
        private readonly WorkingBeatmap beatmap;
        private readonly RulesetInfo ruleset;

        public FlatScoreDecoder(WorkingBeatmap beatmap, RulesetInfo ruleset)
        {
            this.beatmap = beatmap;
            this.ruleset = ruleset;
        }

        protected override Ruleset GetRuleset(int rulesetId) => ruleset.CreateInstance();

        protected override WorkingBeatmap GetBeatmap(string md5Hash) => beatmap;
    }
}

sealed record ReplayScoreReport(
    bool IsFc,
    long Score,
    int MaxCombo,
    int MaximumCombo,
    string Rank,
    double Accuracy,
    bool Passed,
    [property: JsonPropertyName("count_300")]
    long Count300,
    [property: JsonPropertyName("count_100")]
    long Count100,
    [property: JsonPropertyName("count_50")]
    long Count50,
    [property: JsonPropertyName("count_miss")]
    long CountMiss,
    IReadOnlyDictionary<string, long> Statistics,
    IReadOnlyList<JudgementIssue> SliderBreaks,
    IReadOnlyList<JudgementIssue> OtherComboBreaks);

sealed record JudgementIssue(
    string? SliderPart,
    string HitObjectType,
    string Result,
    double ObjectTimeMs,
    double JudgementTimeMs,
    double TimeOffsetMs,
    int ComboBefore,
    int ComboAfter);

partial class Program
{
    [DllImport("kernel32", SetLastError = true, CharSet = CharSet.Unicode)]
    private static extern bool SetDllDirectory(string lpPathName);

    private static string ResolveLazerPath(string? explicitPath)
    {
        if (!string.IsNullOrWhiteSpace(explicitPath))
            return explicitPath;

        var configured = Environment.GetEnvironmentVariable("OSU_LAZER_PATH");
        if (!string.IsNullOrWhiteSpace(configured))
            return configured;

        var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        if (!string.IsNullOrWhiteSpace(localAppData))
            return Path.Combine(localAppData, "osulazer", "current");

        throw new InvalidOperationException("Pass an osu!lazer path or set OSU_LAZER_PATH.");
    }
}
