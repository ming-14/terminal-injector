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
exit $LASTEXITCODE
