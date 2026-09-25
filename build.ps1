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
# 注意：Windows 环境块里 PATH/Path 大小写变体可能并存，而 SetEnvironmentVariable
# 的键在首字符之后大小写不敏感 → 直接回灌会抛 "已添加项。字典中的关键字:'PATH'
# 所添加的关键字:'Path'"（MSB6001）。故先删掉除规范名 PATH 外的所有大小写变体，
# 再回灌 vcvars 输出；PATH 本身必须保留，否则后续找不到 cmd.exe。
$vsEnv = & cmd /c "`"$vsDevCmd`" -arch=x64 -host_arch=x64 & set"
[System.Environment]::GetEnvironmentVariables().Keys | ForEach-Object {
    if ($_ -match '^(?i)path$' -and $_ -cne 'PATH') {
        [System.Environment]::SetEnvironmentVariable($_, $null)
    }
}
$vsEnv | ForEach-Object {
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
