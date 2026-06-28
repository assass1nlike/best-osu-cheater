using System.Reflection;
using System.Runtime.Loader;

var lazer = args.Length > 0 ? args[0] : ResolveLazerPath();
var keywords = args.Skip(1).ToArray();
if (keywords.Length == 0)
{
    keywords =
    [
        "LegacyScoreDecoder",
        "LegacyScoreEncoder",
        "ScoreInfo",
        "Replay",
        "FlatWorkingBeatmap",
        "LegacyBeatmapDecoder",
        "OsuRuleset",
        "TestReplayPlayer",
    ];
}

AssemblyLoadContext.Default.Resolving += (_, name) =>
{
    var candidate = Path.Combine(lazer, $"{name.Name}.dll");
    return File.Exists(candidate) ? AssemblyLoadContext.Default.LoadFromAssemblyPath(candidate) : null;
};

foreach (var dll in Directory.EnumerateFiles(lazer, "*.dll"))
{
    try
    {
        AssemblyLoadContext.Default.LoadFromAssemblyPath(dll);
    }
    catch
    {
        // Some native-ish assemblies are not loadable this way; skip them.
    }
}

foreach (var assembly in AssemblyLoadContext.Default.Assemblies.OrderBy(a => a.GetName().Name))
{
    Type[] types;
    try
    {
        types = assembly.GetTypes();
    }
    catch (ReflectionTypeLoadException ex)
    {
        types = ex.Types.Where(t => t != null).Cast<Type>().ToArray();
    }

    foreach (var type in types.Where(t => keywords.Any(k => t.FullName?.Contains(k, StringComparison.OrdinalIgnoreCase) == true)))
    {
        Console.WriteLine($"TYPE {type.FullName} asm={assembly.GetName().Name} public={type.IsPublic} abstract={type.IsAbstract}");
        foreach (var ctor in type.GetConstructors(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance))
            Console.WriteLine($"  CTOR {Visibility(ctor)} {ctor}");
        foreach (var member in type.GetMembers(BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.Instance | BindingFlags.Static | BindingFlags.DeclaredOnly)
                     .Where(m => m.MemberType is MemberTypes.Method or MemberTypes.Property or MemberTypes.Field)
                     .OrderBy(m => m.Name))
            Console.WriteLine($"  {member.MemberType} {Visibility(member)} {member}");
    }
}

static string Visibility(MemberInfo member)
{
    return member switch
    {
        MethodBase m when m.IsPublic => "public",
        MethodBase m when m.IsFamily => "protected",
        MethodBase m when m.IsAssembly => "internal",
        MethodBase m when m.IsPrivate => "private",
        FieldInfo f when f.IsPublic => "public",
        FieldInfo f when f.IsFamily => "protected",
        FieldInfo f when f.IsAssembly => "internal",
        FieldInfo f when f.IsPrivate => "private",
        PropertyInfo p => Visibility(p.GetMethod ?? p.SetMethod!),
        _ => "unknown",
    };
}

static string ResolveLazerPath()
{
    var configured = Environment.GetEnvironmentVariable("OSU_LAZER_PATH");
    if (!string.IsNullOrWhiteSpace(configured))
        return configured;

    var localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
    if (!string.IsNullOrWhiteSpace(localAppData))
        return Path.Combine(localAppData, "osulazer", "current");

    throw new InvalidOperationException("Pass an osu!lazer path or set OSU_LAZER_PATH.");
}
