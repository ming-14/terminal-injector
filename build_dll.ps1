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
cmd.exe /c "`"$vsDevCmd`" -arch=x64 -host_arch=x64 > nul 2>&1 && set" | ForEach-Object {
    if ($_ -match '^([^=]+)=(.*)$') {
        [System.Environment]::SetEnvironmentVariable($matches[1], $matches[2])
    }
}

# Run MSBuild
& $msbuild (Join-Path $projectRoot 'build\src\dll\injected_dll.vcxproj') /p:Configuration=Release /p:Platform=x64 "/t:Clean;Build" /nologo /v:m
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

# 32 位中继 DLL（relay32）：改 injected.dll 的 ProcessHooks 时通常要一起改中继，
# 故此快速迭代脚本一并构建，产物拷到 x64 输出目录供 ProcessHooks 定位
& (Join-Path $projectRoot 'build_relay.ps1')
exit $LASTEXITCODE
