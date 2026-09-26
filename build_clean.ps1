# 在干净环境中构建（绕开宿主进程注入的 PATH/Path/path 三重变体）。
#
# 背景：宿主 IDE 进程的环境块里同时存在 PATH / Path / path 三个键，
# 而 System.Environment 的键在首字符之后大小写不敏感 →
# MSBuild 构造 cl.exe 子进程的 ProcessStartInfo 时抛
#   "已添加项。字典中的关键字:'PATH' 所添加的关键字:'Path'"（MSB6001）
# 该重复无法从当前进程内删除（三个变体都删不掉）。
#
# 解法：用 Start-Process 传一份精简的 -Environment（只含干净 PATH 等必需项）
# 启动一个新 PowerShell，在其中回灌 vcvars 环境并调 msbuild。
param(
    [string]$Project = 'injected',
    [switch]$Rebuild
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot

# 定位 Visual Studio（走 vswhere，不写死版本号）
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio\Installer\vswhere.exe'
$vsDir = & $vswhere -latest -products * -requires Microsoft.Component.MSBuild -property installationPath
if (-not $vsDir) { Write-Error 'Visual Studio not found.'; exit 1 }
$msbuild = Join-Path $vsDir 'MSBuild\Current\Bin\MSBuild.exe'
$vsDevCmd = Join-Path $vsDir 'Common7\Tools\VsDevCmd.bat'

$projs = @{
    injected = 'build\src\dll\injected_dll.vcxproj'
    all      = 'build\ALL_BUILD.vcxproj'
}
$proj = Join-Path $root $projs[$Project]
if (-not (Test-Path $proj)) { Write-Error "工程不存在: $proj"; exit 1 }

# 干净 PATH：只要系统目录，保证能找到 cmd.exe / MSBuild 依赖
$cleanPath = 'C:\Windows\system32;C:\Windows;C:\Windows\System32\Wbem;C:\Windows\System32\WindowsPowerShell\v1.0'
$cleanEnv = @{
    'PATH'           = $cleanPath
    'SystemRoot'     = $env:SystemRoot
    'SystemDrive'    = $env:SystemDrive
    'COMSPEC'        = $env:COMSPEC
    'TEMP'           = $env:TEMP
    'TMP'            = $env:TMP
    'USERPROFILE'    = $env:USERPROFILE
    'APPDATA'        = $env:APPDATA
    'LOCALAPPDATA'   = $env:LOCALAPPDATA
    'ProgramData'    = $env:ProgramData
    'ProgramFiles'   = $env:ProgramFiles
    'NUMBER_OF_PROCESSORS' = '8'
    'PROCESSOR_ARCHITECTURE' = $env:PROCESSOR_ARCHITECTURE
}

$targets = if ($Rebuild) { 'Clean;Build' } else { 'Build' }
$inner = @"
`$ErrorActionPreference = 'Stop'
& "$vsDevCmd" -arch=x64 -host_arch=x64 -no_logo | Out-Null
& "$msbuild" "$proj" /p:Configuration=Release /p:Platform=x64 /t:$targets /nologo /v:m
exit `$LASTEXITCODE
"@

$tmp = Join-Path $env:TEMP ('ti_build_{0}.ps1' -f (Get-Random))
Set-Content -Path $tmp -Value $inner -Encoding UTF8

$p = Start-Process -FilePath 'powershell.exe' `
    -ArgumentList '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $tmp `
    -Environment $cleanEnv -NoNewWindow -PassThru -Wait
Remove-Item $tmp -Force -ErrorAction SilentlyContinue

Write-Host "[clean-build] exit=$($p.ExitCode)"

# 成功时不调用 exit：脚本内的 exit 会拆断上游管道（如 `.\build_clean.ps1 ... 2>&1 |
# Select-Object -Last N`），调用方会把"管道被中断"误判为非零退出 —— 实测同一份构建
# 加管道调用报 exit=1、不加管道报 exit=0，且输出量越大（真实编译）越容易触发。
# 脚本正常结束即为 0，故成功路径直接返回；失败路径仍 exit 以保证错误码传递。
if ($p.ExitCode -ne 0) { exit $p.ExitCode }
