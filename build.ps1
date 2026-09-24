# Build script for terminal-injector
$ErrorActionPreference = 'Stop'
$projectRoot = $PSScriptRoot

# Locate Visual Studio installation via vswhere
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vsInstallDir = & $vswhere -latest -products * -requires Microsoft.Component.MSBuild -property installationPath
if (-not $vsInstallDir) {
    Write-Error 'Visual Studio not found.'
    exit 1
}
$vsDevCmd = Join-Path $vsInstallDir 'Common7\Tools\VsDevCmd.bat'
$msbuild = Join-Path $vsInstallDir 'MSBuild\Current\Bin\MSBuild.exe'

# Initialize VS environment
& cmd /c "`"$vsDevCmd`" -arch=x64 -host_arch=x64 & set" | ForEach-Object {
    if ($_ -match '^([^=]+)=(.*)') {
        [System.Environment]::SetEnvironmentVariable($matches[1], $matches[2])
    }
}

# Build
& $msbuild (Join-Path $projectRoot 'build\ALL_BUILD.vcxproj') /p:Configuration=Release /p:Platform=x64 /t:Rebuild /m
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# 32 位中继 DLL（relay32）：单独构建树，产物拷到 x64 输出目录
# 中继必须与 32 位目标进程同位数，主产物是 x64，无法在同一构建树产出
& (Join-Path $projectRoot 'build_relay.ps1')
exit $LASTEXITCODE
