#requires -Version 7
# Offline build: calls the Roslyn compiler bundled with the .NET SDK and references the game's
# Managed/ and Libs/ assemblies. No NuGet restore.
param(
    [string]$GameDir = 'C:\Program Files (x86)\Steam\steamapps\common\Beat Saber',
    [string]$OutDir = (Join-Path $PSScriptRoot 'bin'),
    [switch]$Install
)
$ErrorActionPreference = 'Stop'

$csc = Get-ChildItem 'C:\Program Files\dotnet\sdk\*\Roslyn\bincore\csc.dll' | Sort-Object FullName -Descending | Select-Object -First 1
if (-not $csc) { throw 'No .NET SDK found (need csc.dll under C:\Program Files\dotnet\sdk)' }
$managed = Join-Path $GameDir 'Beat Saber_Data\Managed'
$refs = @(
    "$managed\mscorlib.dll", "$managed\netstandard.dll", "$managed\System.dll", "$managed\System.Core.dll",
    "$managed\UnityEngine.CoreModule.dll", "$managed\Main.dll", "$managed\IPA.Loader.dll",
    (Join-Path $GameDir 'Libs\0Harmony.dll')
)
New-Item -ItemType Directory -Force $OutDir | Out-Null
$out = Join-Path $OutDir 'PresenceFix.dll'

$cscArgs = @('-noconfig', '-nostdlib+', '-target:library', '-optimize+', '-langversion:9', '-nologo',
    "-out:$out", "-resource:$(Join-Path $PSScriptRoot 'manifest.json'),PresenceFix.manifest.json") +
    ($refs | ForEach-Object { "-r:$_" }) + (Join-Path $PSScriptRoot 'Plugin.cs')
& dotnet $csc.FullName @cscArgs
if ($LASTEXITCODE) { throw "csc exited $LASTEXITCODE" }
Get-FileHash $out | Select-Object Hash, Path

if ($Install) {
    $plugins = Join-Path $GameDir 'Plugins'
    New-Item -ItemType Directory -Force $plugins | Out-Null
    Copy-Item $out $plugins -Force
    "Installed to $plugins"
}
