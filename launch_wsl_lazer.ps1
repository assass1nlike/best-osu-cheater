[CmdletBinding()]
param(
    [string] $Distro = "Ubuntu-24.04",
    [string] $AppImagePath,
    [string] $LazerDataPath,
    [string] $GpuAdapter = "Intel",
    [ValidateSet("Balanced", "Quality", "Native")]
    [string] $PerformanceProfile = "Balanced"
)

$ErrorActionPreference = "Stop"

function Invoke-WslText {
    param([Parameter(Mandatory)][string[]] $Arguments)

    $output = & wsl.exe -d $Distro -- @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "WSL command failed with exit code ${LASTEXITCODE}: $($Arguments -join ' ')"
    }
    return ($output -join "`n").Trim()
}

function ConvertTo-WslPath {
    param([Parameter(Mandatory)][string] $WindowsPath)

    $resolvedPath = (Resolve-Path -LiteralPath $WindowsPath).Path
    if ($resolvedPath -notmatch '^([A-Za-z]):\\(.*)$') {
        throw "Only local Windows drive paths can be converted to WSL paths: $resolvedPath"
    }

    $drive = $Matches[1].ToLowerInvariant()
    $relativePath = $Matches[2].Replace('\', '/')
    return "/mnt/$drive/$relativePath"
}

$linuxHome = Invoke-WslText -Arguments @("sh", "-lc", 'printf %s "$HOME"')
if (-not $AppImagePath) {
    $AppImagePath = "$linuxHome/Applications/osu.AppImage"
}
if (-not $LazerDataPath) {
    $LazerDataPath = "$linuxHome/.local/share/osu"
}

& wsl.exe -d $Distro -- pgrep -x "osu!" *> $null
if ($LASTEXITCODE -eq 0) {
    throw "osu!lazer is already running in $Distro. Close it before using this launcher."
}

$linuxScriptPath = ConvertTo-WslPath (Join-Path $PSScriptRoot "tools\wsl\prepare_lazer_input.py")
$linuxPerformanceScriptPath = ConvertTo-WslPath (
    Join-Path $PSScriptRoot "tools\wsl\prepare_lazer_performance.py"
)

Write-Host "Preparing osu!lazer mouse input for WSLg..."
& wsl.exe -d $Distro -- python3 $linuxScriptPath --config "$LazerDataPath/input.json"
if ($LASTEXITCODE -ne 0) {
    throw "Could not prepare osu!lazer input configuration."
}

Write-Host "Applying the $PerformanceProfile WSLg performance profile..."
& wsl.exe -d $Distro -- python3 $linuxPerformanceScriptPath `
    --config "$LazerDataPath/framework.ini" `
    --profile $PerformanceProfile.ToLowerInvariant()
if ($LASTEXITCODE -ne 0) {
    throw "Could not prepare osu!lazer performance configuration."
}

$launchArguments = @(
    "-d", $Distro, "--",
    "env",
    "SDL_VIDEODRIVER=x11",
    "SDL_VIDEO_X11_XINPUT2=0",
    "SDL_MOUSE_AUTO_CAPTURE=0",
    "GALLIUM_DRIVER=d3d12",
    "MESA_D3D12_DEFAULT_ADAPTER_NAME=$GpuAdapter",
    "PULSE_SERVER=unix:/mnt/wslg/PulseServer",
    $AppImagePath
)

Write-Host "Starting osu!lazer in WSLg (profile: $PerformanceProfile, D3D12 adapter: $GpuAdapter)..."
Start-Process -FilePath "wsl.exe" -ArgumentList $launchArguments -WindowStyle Hidden

if (-not ("WslgWindow" -as [type])) {
    Add-Type @"
using System;
using System.Runtime.InteropServices;

public static class WslgWindow {
    [DllImport("user32.dll")]
    public static extern bool ShowWindow(IntPtr window, int command);
}
"@
}

$windowTitle = "osu! ($Distro)"
$deadline = [DateTime]::UtcNow.AddSeconds(20)
$lazerWindow = $null
do {
    Start-Sleep -Milliseconds 200
    $lazerWindow = Get-Process -Name "msrdc" -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle -ne 0 -and $_.MainWindowTitle -eq $windowTitle } |
        Select-Object -First 1
} while (-not $lazerWindow -and [DateTime]::UtcNow -lt $deadline)

if (-not $lazerWindow) {
    Write-Warning "osu!lazer started, but its WSLg window was not found for profile adjustment."
    return
}

# WSLg remembers Windows-side maximisation independently of lazer's WindowedSize.
# Restoring the RAIL window is required to avoid rendering a 4K client every run.
$showCommand = if ($PerformanceProfile -eq "Native") { 3 } else { 9 }
[void][WslgWindow]::ShowWindow($lazerWindow.MainWindowHandle, $showCommand)
Write-Host "osu!lazer window profile applied."
