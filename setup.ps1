param([string]$GameSource)

$ErrorActionPreference = 'Stop'
$root = Join-Path $env:LOCALAPPDATA 'PolyBridgeBenchmark'
if (-not $GameSource) {
    $steam = (Get-ItemProperty 'HKCU:\Software\Valve\Steam' -ErrorAction SilentlyContinue).SteamPath
    if (-not $steam) { throw 'Could not find Steam. Supply -GameSource with the installed game folder.' }
    $roots = @($steam)
    $libraries = Get-Content -Raw -LiteralPath (Join-Path $steam 'steamapps\libraryfolders.vdf')
    foreach ($match in [regex]::Matches($libraries, '"path"\s+"([^"]+)"')) {
        $roots += $match.Groups[1].Value.Replace('\\', '\')
    }
    $GameSource = $roots | ForEach-Object { Join-Path $_ 'steamapps\common\Poly Bridge 2' } |
        Where-Object { Test-Path -LiteralPath (Join-Path $_ 'Poly Bridge 2.exe') } | Select-Object -First 1
}
if (-not $GameSource) { throw 'Could not find Poly Bridge 2. Supply -GameSource with the installed game folder.' }
$relativeAssembly = 'Poly Bridge 2_Data\Managed\Assembly-CSharp.dll'
$sourceAssembly = Join-Path $GameSource $relativeAssembly
$hash = (Get-FileHash -LiteralPath $sourceAssembly).Hash.ToLowerInvariant()
$game = Join-Path $root "game-$($hash.Substring(0, 12))"
$copied = Join-Path $game '.copy-complete'
if (-not (Test-Path -LiteralPath $copied)) {
    New-Item -ItemType Directory -Path $game -Force | Out-Null
    & robocopy $GameSource $game /E /NFL /NDL /NJH /NJS /NP /XD BepInEx /XF winhttp.dll doorstop_config.ini .doorstop_version steam_appid.txt
    if ($LASTEXITCODE -ge 8) { throw "Copying the Steam installation failed ($LASTEXITCODE)." }
    New-Item -ItemType File -Path $copied | Out-Null
}
$version = '5.4.23.5'
$digest = '82f9878551030f54657792c0740d9d51a09500eeae1fba21106b0c441e6732c4'
$tools = Join-Path $root 'tools'
New-Item -ItemType Directory -Path $tools -Force | Out-Null
$archive = Join-Path $tools "BepInEx_win_x64_$version.zip"
if (-not (Test-Path -LiteralPath $archive)) {
    $url = "https://github.com/BepInEx/BepInEx/releases/download/v$version/BepInEx_win_x64_$version.zip"
    Invoke-WebRequest $url -OutFile "$archive.download" -TimeoutSec 60 -UseBasicParsing
    if ((Get-FileHash -LiteralPath "$archive.download").Hash -ne $digest) { throw 'BepInEx SHA-256 mismatch.' }
    Move-Item -LiteralPath "$archive.download" -Destination $archive
}
if ((Get-FileHash -LiteralPath $archive).Hash -ne $digest) { throw 'BepInEx SHA-256 mismatch.' }
if (-not (Test-Path -LiteralPath (Join-Path $game 'BepInEx\core\BepInEx.dll'))) {
    Expand-Archive -LiteralPath $archive -DestinationPath $game -Force
}
$compiler = Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
$managed = Split-Path (Join-Path $game $relativeAssembly)
$core = Join-Path $game 'BepInEx\core'
$plugins = Join-Path $game 'BepInEx\plugins'
New-Item -ItemType Directory -Path $plugins -Force | Out-Null
$references = @('mscorlib.dll', 'System.dll', 'System.Core.dll', 'netstandard.dll', 'Assembly-CSharp.dll',
    'Assembly-CSharp-firstpass.dll', 'Newtonsoft.Json.dll', 'UnityEngine.dll', 'UnityEngine.CoreModule.dll',
    'UnityEngine.AudioModule.dll', 'UnityEngine.PhysicsModule.dll', 'Unity.TextMeshPro.dll',
    'UnityEngine.UI.dll') | ForEach-Object { "/reference:$(Join-Path $managed $_)" }
$references += @('BepInEx.dll', '0Harmony.dll') | ForEach-Object { "/reference:$(Join-Path $core $_)" }
$plugin = Join-Path $plugins 'PolyBridgeBenchmark.dll'
$sources = @('Benchmark.cs', 'SimulationFeedback.cs') | ForEach-Object { Join-Path $PSScriptRoot "game-host\$_" }
$assembly = Join-Path $tools 'PolyBridgeBenchmark.dll'
& $compiler /nologo /target:library /noconfig /nostdlib+ "/out:$assembly" @references @sources
if ($LASTEXITCODE -ne 0) { throw 'The benchmark game host did not compile.' }
Copy-Item -LiteralPath $assembly -Destination $plugin -Force
[ordered]@{
    game = $game
    game_assembly_sha256 = $hash
    host_sha256 = (Get-FileHash -LiteralPath $plugin).Hash.ToLowerInvariant()
    loader = "BepInEx $version"
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $root 'installation.json') -Encoding UTF8
Write-Output "Benchmark host built at $game"
